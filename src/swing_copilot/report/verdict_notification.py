"""Build the daily post-verdict Discord notification body (Issue #383, FR-09).

Sent once per day, after `copilot-ingest-analysis` has (or has not) landed a
verdict -- never from inside the pipeline itself. `pipeline/daily.py` step 7
used to fire right after `6_analysis_export`, before any qualitative verdict
existed, so it could only ever report the deterministic candidate count. This
module is read by `scripts/notify_daily.py`, which runs as the workflow's last
step regardless of how the day ended (`always()`), so it is this module's job
to turn *any* terminal state into exactly one Discord send.

Everything here reads only JSON already sitting on disk -- `copilot-daily`'s
outcome file (`COPILOT_DAILY_OUTCOME_FILE`) and, for a `success`/`degraded`
day, the run's own `analysis_input.json` / `analysis_result.json` /
`report_context.json` under `reports/<run_date>/<run_id>/`. It never opens
DuckDB: the notification step runs after the day's R2 push, and DuckDB's file
lock is exclusive between a read-write process and everything else, so
touching it here could break the sync a caller elsewhere depends on.

Verification is not reimplemented. `analysis/validate.py::validate_analysis`
(the same function `copilot-ingest-analysis` calls) is reused as-is to get
provenance-checked, CON-03-checked, fail-closed-per-symbol outcomes; this
module only decides how to *render* them. It still runs
`analysis/safety.py::check_display_texts` a second time over every fully
assembled block before it is queued to send (CON-03 is enforced here, not
assumed from ingest) -- a violation withholds that one block, exactly the way
ingest withholds one symbol, and the day still gets its one message.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.safety import ForbiddenLanguageError, check_display_texts
from swing_copilot.analysis.snapshot import REPORT_CONTEXT_FILENAME, read_report_context
from swing_copilot.analysis.validate import (
    AnalysisIngestError,
    ArtifactIdentity,
    load_analysis_input,
    load_analysis_result,
    validate_analysis,
    validate_artifact_identity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from swing_copilot.analysis.schemas import Verdict
    from swing_copilot.analysis.validate import SymbolOutcome, ValidatedAnalysis
    from swing_copilot.report.daily_brief import BriefCandidate, DailyBrief

logger = logging.getLogger(__name__)

#: Discord's hard cap on one message's `content` field.
DISCORD_MESSAGE_CHAR_LIMIT = 2000
#: Reserved headroom for a later message's continuation marker
#: (`"(続き 12/34)"` is well under this). Only message 1 uses the real header
#: length instead, since it is known upfront.
_CONTINUATION_MARKER_RESERVE = 40
#: A single symbol block must fit inside any message on its own -- Decision C
#: forbids splitting one across two messages. This leaves generous headroom
#: (500 chars) for the largest realistic header/continuation prefix, so a
#: block this size is guaranteed to fit in the first message and every later
#: one alike.
_MAX_BLOCK_BODY_CHARS = DISCORD_MESSAGE_CHAR_LIMIT - 500
_ELLIPSIS_MARKER = "…"

_PREFLIGHT_ABORT_OUTCOME = "preflight_abort"
#: Mirrors `scripts/check_daily_complete.py::_LEGITIMATE_STOP_REASONS`. Kept
#: as a second small literal set (not imported) because `scripts/` is
#: deliberately never imported by the installed package (see its own
#: `pyproject.toml` comment) -- update both together if a new reason is added.
_LEGITIMATE_PREFLIGHT_REASONS = frozenset({"same_day_rerun", "no_trading_day"})

_WITHHELD_CON03_NOTE = "CON-03 の安全確認に抵触したため、この銘柄の詳細は通知から除外しました。レポートを直接確認してください。"
_WITHHELD_GENERIC_NOTE = "検証不合格のため、この銘柄の詳細は通知から除外しました。レポートを直接確認してください。"


@dataclass(frozen=True, slots=True)
class DailyOutcome:
    """One `copilot-daily` invocation's terminal state (the outcome-file JSON)."""

    outcome: str
    reason: str | None
    run_id: str | None
    run_date: str | None
    candidates: int | None


def _read_outcome(path: Path | None) -> DailyOutcome | None:
    """Parse the outcome file, or return `None` if it cannot be trusted.

    A missing/unreadable/malformed file is genuinely ambiguous -- it can mean
    `copilot-daily` never started, crashed before writing it, or the CI job
    died before this step ran -- so the caller treats `None` as an abnormal
    day rather than guessing further.
    """
    if path is None:
        logger.error("no outcome file path was configured")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        logger.exception("outcome file %s could not be read", path)
        return None
    if not isinstance(payload, dict):
        logger.error("outcome file %s is not a JSON object", path)
        return None
    outcome = payload.get("outcome")
    if not isinstance(outcome, str):
        logger.error("outcome file %s has no string 'outcome' field", path)
        return None
    return DailyOutcome(
        outcome=outcome,
        reason=payload.get("reason")
        if isinstance(payload.get("reason"), str)
        else None,
        run_id=payload.get("run_id")
        if isinstance(payload.get("run_id"), str)
        else None,
        run_date=(
            payload.get("run_date")
            if isinstance(payload.get("run_date"), str)
            else None
        ),
        candidates=(
            payload.get("candidates")
            if isinstance(payload.get("candidates"), int)
            else None
        ),
    )


def build_daily_notification(
    *, outcome_file: Path | None, reports_dir: Path
) -> list[str]:
    """Build this day's ready-to-send Discord message(s).

    Exactly one logical notification is always produced -- possibly split
    into several Discord messages under `DISCORD_MESSAGE_CHAR_LIMIT`
    (Decision C) -- covering every terminal state `copilot-daily` can reach:
    a legitimate preflight stop, an abnormal abort/failure, a day with no
    `proceed` verdicts, and a day with one or more.

    Args:
        outcome_file: Path to `copilot-daily`'s terminal-outcome JSON
            (`COPILOT_DAILY_OUTCOME_FILE`), or `None` if never configured.
        reports_dir: The daily run archive root (normally `reports/`).

    Returns:
        One or more message bodies, each at most `DISCORD_MESSAGE_CHAR_LIMIT`
        characters, to send in order.
    """
    outcome = _read_outcome(outcome_file)
    if outcome is None:
        return [
            "[swing-copilot] 終了状態が確認できませんでした（outcome ファイル欠落/破損）。"
            "copilot-daily が起動していないか、実行が異常終了した可能性があります。"
        ]
    if outcome.outcome == _PREFLIGHT_ABORT_OUTCOME:
        return [_preflight_abort_message(outcome)]
    if outcome.outcome not in ("success", "degraded"):
        return [_abnormal_message(outcome)]
    if outcome.run_id is None or outcome.run_date is None:
        return [_abnormal_message(outcome)]
    return _result_messages(outcome, reports_dir)


def _preflight_abort_message(outcome: DailyOutcome) -> str:
    if outcome.reason in _LEGITIMATE_PREFLIGHT_REASONS:
        return (
            f"[swing-copilot] {outcome.run_date or '(run_date 未解決)'}: "
            f"正常停止（PREFLIGHT_ABORT[{outcome.reason}]）。分析対象日なし、"
            "または既に本日分の分析が完了しています。"
        )
    return _abnormal_message(outcome)


def _abnormal_message(outcome: DailyOutcome) -> str:
    reason_note = f"（reason={outcome.reason}）" if outcome.reason else ""
    return (
        f"[swing-copilot] {outcome.run_date or '(run_date 未解決)'}: "
        f"異常終了（outcome={outcome.outcome}{reason_note}）。"
        "定性分析が完了していない可能性があります。ジョブのログを確認してください。"
    )


def _status_label(outcome: DailyOutcome) -> str:
    return "分析完了（一部縮退あり）" if outcome.outcome == "degraded" else "分析完了"


def _result_messages(outcome: DailyOutcome, reports_dir: Path) -> list[str]:
    run_dir = reports_dir / str(outcome.run_date) / str(outcome.run_id)
    run_dir_label = f"reports/{outcome.run_date}/{outcome.run_id}/"
    result_path = run_dir / ANALYSIS_RESULT_FILENAME
    if not result_path.exists():
        return [_no_analysis_message(outcome)]

    try:
        analysis_input = load_analysis_input(run_dir / ANALYSIS_INPUT_FILENAME)
        result = load_analysis_result(result_path)
        context = read_report_context(run_dir / REPORT_CONTEXT_FILENAME)
        validate_artifact_identity(
            analysis_input,
            result,
            ArtifactIdentity(
                run_id=context.brief.run_id,
                as_of=context.brief.run_date,
                strategy_key=context.strategy_key,
                input_digest=context.input_digest,
            ),
        )
        validated = validate_analysis(analysis_input, result)
    except AnalysisIngestError:
        logger.exception(
            "run %s (%s): could not verify analysis_result.json for notification",
            outcome.run_id,
            outcome.run_date,
        )
        return [
            f"[swing-copilot] {outcome.run_date}: {_status_label(outcome)}。\n"
            "分析結果 (analysis_result.json) を検証できませんでした。"
            "ジョブのログと reports/ を直接確認してください。"
        ]

    return _verdict_messages(outcome, context.brief, validated, run_dir_label)


def _no_analysis_message(outcome: DailyOutcome) -> str:
    header = f"[swing-copilot] {outcome.run_date}: {_status_label(outcome)}。"
    if (outcome.candidates or 0) == 0:
        return f"{header}\n本日はスクリーニング候補が無かったため、定性分析の対象はありません。"
    return (
        f"{header}\n候補 {outcome.candidates} 件に対する分析結果 "
        "(analysis_result.json) が見つかりません。定性分析が未完了の可能性があります。"
    )


def _verdict_messages(
    outcome: DailyOutcome,
    brief: DailyBrief,
    validated: ValidatedAnalysis,
    run_dir_label: str,
) -> list[str]:
    candidates_by_symbol = {
        candidate.symbol: candidate for candidate in brief.candidates
    }
    proceed_outcomes = [
        item
        for item in validated.outcomes
        if item.error is None
        and item.verdict is not None
        and item.verdict.recommendation == "proceed"
    ]
    withheld = [item for item in validated.outcomes if item.error is not None]

    header = "\n".join(
        _header_lines(outcome, brief, validated, proceed_outcomes, withheld)
    )
    blocks = [
        _proceed_block(item.symbol, item.verdict, candidates_by_symbol.get(item.symbol))
        for item in proceed_outcomes
        if item.verdict is not None  # always true here; narrows the type for mypy
    ]
    blocks.extend(_withheld_block(item) for item in withheld)
    if not proceed_outcomes:
        blocks.append(_no_proceed_block(validated))

    return _pack_messages(header, blocks, run_dir_label)


def _header_lines(
    outcome: DailyOutcome,
    brief: DailyBrief,
    validated: ValidatedAnalysis,
    proceed_outcomes: Sequence[SymbolOutcome],
    withheld: Sequence[SymbolOutcome],
) -> list[str]:
    skip_count = sum(
        1
        for item in validated.outcomes
        if item.error is None
        and item.verdict is not None
        and item.verdict.recommendation == "skip"
    )
    lines = [f"[swing-copilot] {outcome.run_date}: {_status_label(outcome)}"]
    if brief.exposure is not None:
        exposure = brief.exposure
        lines.append(
            f"Exposure Ceiling: {exposure.verdict} (Gate: {exposure.gate}, "
            f"DD: {exposure.dd_level}, Data quality: {exposure.data_quality})"
        )
    lines.append(
        f"verdict内訳: proceed {len(proceed_outcomes)} / skip {skip_count} / "
        f"withheld {len(withheld)}（対象 {len(validated.outcomes)} 件）"
    )
    return lines


def _money(value: float | None) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _one_r(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _number(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:{'.' + str(digits) + 'f'}}"


def _per_share_risk(
    limit_price: float | None, stop_price: float | None
) -> float | None:
    """1株あたりリスク: `limit_price - stop_price`, the only derived value here.

    Every other figure is `RiskAssessment`'s own value, shown unchanged
    (per the task's "決定論的出力を再計算しない" constraint). This one
    subtraction is the deliberate, explicitly-labeled exception: the account
    is unknown to this product (`risk/checks.py`), so a reader needs the
    per-share dollar risk, not a share count, to size a position themselves.
    """
    if limit_price is None or stop_price is None:
        return None
    return limit_price - stop_price


def _proceed_block(
    symbol: str, verdict: Verdict, candidate: BriefCandidate | None
) -> str:
    lines = [
        f"■ {symbol}"
        + (
            f"（{candidate.company_name}）"
            if candidate is not None and candidate.company_name
            else ""
        )
    ]
    if candidate is not None:
        risk = candidate.risk
        lines.append(
            f"順位: {candidate.rank} / 合計スコア: {_number(candidate.score, 3)}"
        )
        lines.append(f"参照終値(entry_price): {_money(risk.entry_price)}")
        lines.append(
            f"指値(limit_price): {_money(risk.limit_price)} / "
            f"逆指値(stop_price): {_money(risk.stop_price)}"
        )
        lines.append(f"1R(stop_distance_pct): {_one_r(risk.stop_distance_pct)}")
        per_share_risk = _per_share_risk(risk.limit_price, risk.stop_price)
        lines.append(
            f"1株あたりリスク: {_money(per_share_risk)}"
            "（指値-逆指値。口座規模に依存しない、この2値からの単純な減算）"
        )
        lines.append(f"ATR14: {_number(risk.atr14)}")
        lines.append(
            f"状態(status): {risk.status} / "
            f"制約(binding_constraint): {risk.binding_constraint or 'なし'}"
        )
        if risk.reasons:
            lines.append(f"blocking_reasons: {', '.join(risk.reasons)}")
        if risk.warnings:
            lines.append(f"warnings: {', '.join(risk.warnings)}")
    else:
        lines.append(
            "（この銘柄のスクリーニング詳細は report_context.json から見つかりませんでした）"
        )
    lines.append("verdict理由:")
    if verdict.reasons:
        lines.extend(
            f"- [{reason.basis}] {reason.text}" if reason.basis else f"- {reason.text}"
            for reason in verdict.reasons
        )
    else:
        lines.append("- (理由の記載なし)")
    return "\n".join(lines)


def _withheld_block(outcome: SymbolOutcome) -> str:
    reason = outcome.error or ""
    note = (
        _WITHHELD_CON03_NOTE
        if reason.startswith("CON-03 violation")
        else _WITHHELD_GENERIC_NOTE
    )
    return f"■ {outcome.symbol}\n{note}"


def _no_proceed_block(validated: ValidatedAnalysis) -> str:
    if validated.no_trade and validated.no_trade_reason:
        return f"本日 proceed 銘柄はありません。\nno_trade_reason: {validated.no_trade_reason}"
    return "本日 proceed 銘柄はありません。"


def _safe_block(block: str) -> str:
    """Return `block` unchanged, or a generic withheld note (CON-03 fail-closed).

    Re-runs the same CON-03 checker `analysis/validate.py` already applied at
    ingest, over the fully assembled block text (Decision E) -- never trusting
    that an earlier check makes this one redundant.
    """
    try:
        check_display_texts([block])
    except ForbiddenLanguageError:
        logger.exception(
            "CON-03 violation in assembled notification block; withholding it"
        )
        first_line = block.split("\n", 1)[0]
        return f"{first_line}\n{_WITHHELD_CON03_NOTE}"
    return block


def _shrink_block(block: str, run_dir_label: str) -> str:
    """Truncate an over-long block's tail, with an explicit elision marker.

    `verdict.reasons[].text` is the only unbounded field in a block (Decision
    C); every other line is a short, fixed-format, code-computed value. Cutting
    from the end therefore always removes reason text first.
    """
    if len(block) <= _MAX_BLOCK_BODY_CHARS:
        return block
    marker = f"\n{_ELLIPSIS_MARKER}（省略。全文は {run_dir_label} を参照）"
    keep = max(_MAX_BLOCK_BODY_CHARS - len(marker), 0)
    return block[:keep].rstrip() + marker


def _pack_messages(header: str, blocks: Sequence[str], run_dir_label: str) -> list[str]:
    """Greedily pack `header` + `blocks` into <=`DISCORD_MESSAGE_CHAR_LIMIT` messages.

    The first message opens with `header`; every later one opens with a short
    continuation marker instead. A block is never split across two messages
    (Decision C): each is shrunk to fit on its own before packing, and is
    CON-03-checked one more time (`_safe_block`) right before being placed.
    """
    safe_blocks = [_safe_block(_shrink_block(block, run_dir_label)) for block in blocks]

    groups: list[list[str]] = [[]]
    group_len = 0  # running content length (blocks + separators) of the open group
    for block in safe_blocks:
        is_first_group = len(groups) == 1
        budget = DISCORD_MESSAGE_CHAR_LIMIT - (
            len(header) if is_first_group else _CONTINUATION_MARKER_RESERVE
        )
        addition = len(block) + (2 if groups[-1] else 0)
        if groups[-1] and group_len + addition > budget:
            groups.append([])
            group_len = 0
            addition = len(block)
        groups[-1].append(block)
        group_len += addition

    total = len(groups)
    messages: list[str] = []
    for index, group in enumerate(groups, start=1):
        prefix = header if index == 1 else f"(続き {index}/{total})"
        messages.append("\n\n".join([prefix, *group]))
    return messages

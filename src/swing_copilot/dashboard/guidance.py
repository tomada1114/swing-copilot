"""How to read each section, written once and shared by every page.

A dashboard that only shows values leaves the reader to remember what "good"
looks like. These captions state it inline: what a HIT actually asserts, which
direction of a regime badge is the defensive one, and why the `skip` stratum
is a control group rather than a recommendation.

The wording is derived from the code that produces the values, not from
memory — `retro/evaluate.py` for the classifications, `regime/gate.py` and
`regime/distribution.py` for the regime badges. Thresholds are named by their
configuration key rather than quoted as numbers: the dashboard never reads
`settings.yaml`, and a caption that hardcoded a value would silently go stale
the day it changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Hint:
    """One "how to read this" caption.

    Attributes:
        summary: One or two lines, always visible.
        details: Longer material, folded behind a disclosure so a section
            that is already dense does not become unreadable.
        details_label: The disclosure's summary text.
    """

    summary: str
    details: tuple[str, ...] = field(default_factory=tuple)
    details_label: str = "詳しい定義を見る"


#: The most important caption on the whole dashboard: without it, a `skip`
#: whose symbol fell reads as a failure. `retro/evaluate.py` defines the
#: classification *relative to the verdict's own claim*, so both sides can be
#: read the same way — HIT means the call was right.
OUTCOME = Hint(
    summary=(
        "当否は verdict の向きを織り込んで定義されている。"
        "proceed でも skip でも、HIT = その判断が正しかった、"
        "MISS = 外れた、と同じ向きで読んでよい。"
    ),
    details=(
        "HIT — proceed: ノイズ帯を超える下落を免れた。"
        "skip: 見送った銘柄が実際にノイズ帯を超えて下落した（回避成功）。",
        "MISS_MILD / MISS_SEVERE — proceed: 下落した（重度境界を超えると SEVERE）。"
        "skip: 上昇した = 機会損失（重度境界を超えると SEVERE）。",
        "NEUTRAL — skip にしかない。値動きがノイズ帯に収まり、"
        "見送りの当否を判定できなかった場合である。"
        "proceed は「重い逆行がない」という片側の主張なので、"
        "小さな値動きでは否定されず NEUTRAL を持たない。",
        "閾値は postmortem.neutral_threshold_pct（ノイズ帯）と "
        "postmortem.severe_threshold_pct（重度境界）。境界ちょうどの扱いは "
        "verdict の向きで逆になる——proceed はノイズ帯ちょうどの下落が既に MISS、"
        "skip はノイズ帯ちょうどの下落が HIT である。",
        "判定は 5・20 営業日の満期に到達した verdict にしか付かない。"
        "満期前は「未成熟」であって中立でもゼロでもない。",
    ),
)

#: Appended to the classification hint on the history facets. Says the thing a
#: reader would otherwise have to derive: because the classification already
#: accounts for the verdict's direction, the colour reads the same way in
#: both facets.
CLASSIFICATION_FACETS = Hint(
    summary=(
        "proceed と skip は必ず分けて数える。#190 以降、skip も反実仮想として"
        "同じ手仕舞い規則で追跡されているので、混ぜた平均は判断とその対照群を"
        "足し合わせただけになる。分類が recommendation を織り込んで定義されている"
        "ため、proceed・skip どちらの facet も緑が正解、赤が不正解として読める。"
        "棒の高さはその run 日に満期を迎えた判定の件数である。"
    ),
    details=OUTCOME.details,
)

#: `regime/gate.py` and `regime/distribution.py`. The reader needs to know
#: which end of each scale is the defensive one, and that UNKNOWN is not the
#: mild case it looks like.
REGIME = Hint(
    summary=(
        "GATE は市場ゲートの判定で、BULL（SPY が EMA 超 かつ VIX が上限未満）が"
        "新規建てを許す側、NEUTRAL は縮小のみ、BEAR は現金優先。"
        "DRAWDOWN は分売日カウントの警戒度で、NORMAL → CAUTION → HIGH → SEVERE の"
        "順に強まり、右へ行くほど露出上限が絞られる。"
    ),
    details=(
        "露出は 2 つの厳しい方が決める——BEAR か SEVERE なら現金優先、"
        "NEUTRAL か HIGH なら縮小のみ、それ以外で新規建て可となる。",
        "どちらの UNKNOWN も「判定不能」であって穏当という意味ではない。"
        "パイプラインは UNKNOWN を SEVERE より厳しく扱い、決して緩める側に"
        "倒さない。",
    ),
)

#: The banner body for a run whose analysis phase never finished. The last
#: sentence exists because the two states look identical in the table: a
#: newest run that finished perfectly also shows "verdict未取込", and reading
#: that as a failure is the easiest mistake on the whole dashboard.
ANALYSIS_PENDING = (
    "決定論パイプラインは完走したが analysis_result.json が無い。"
    "スクリーニング・落選理由は確定値、verdict 列は未確定。"
    "/swing-daily で分析フェーズを再実行すると埋まる。"
    "なお判断列の「verdict未取込」自体は異常ではない——"
    "verdicts は次の run の retro collect で DB へ取り込まれるため、"
    "分析が正常に終わった最新 run でも当日はそう表示される。"
)

#: The same clarification for a run with no banner, shown next to the verdict
#: table. Only one of the two is ever rendered on a page.
VERDICT_INGESTION = Hint(
    summary=(
        "判断列の「verdict未取込」は、分析が正常に終わった run でも当日は出る。"
        "verdicts は次の run の retro collect で DB へ取り込まれるためで、"
        "分析が失敗したという意味ではない。"
    ),
)

#: `skip` positions are a research population (Issue #190). Presenting them
#: without this line would read as a list of symbols to buy.
LEDGER = Hint(
    summary=(
        "proceed 行は実際に推奨した銘柄の仮想成績。"
        "skip 行は #190 以降シャドウ追跡している反実仮想の対照群で、"
        "「見送らず全部買っていたら」を同じ手仕舞い規則で測ったものである。"
        "買い推奨の一覧ではないので、両者を混ぜた平均も意味を持たない。"
    ),
    details=(
        "手仕舞い規則はバックテストと同じ——backtest.exit_atr_multiple の "
        "ATR14 トレーリングストップと backtest.max_hold_days の最大保有営業日。"
        "両区分が同じ規則で回るからこそ、proceed と skip の差が"
        "「定性判断が上乗せした分」として読める。",
    ),
)

#: The VIX line and drawdown strip on the history page.
REGIME_TIMELINE = Hint(
    summary=(
        "折れ線は VIX 終値で、上へ行くほど警戒側。"
        "下段の帯は各 run のドローダウン圧力（ホバーで gate と level）。"
        "帯が濃く赤いほど露出が絞られていた日である。"
    ),
)

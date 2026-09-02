# RP-002 ペアード指標に平均した日次差の本数を併記し、sample_size との違いを読めるようにする

- 提案日: 2026-09-02
- level: L2
- status: proposed
- proposal_key: `retro:aggregate:paired_metric_day_count`
- 対象: `src/swing_copilot/retro/aggregate.py の _paired_separation_for、および src/swing_copilot/retro/schemas.py の MetricEntry`
- 証拠の種類: qualitative

## 主張

ペアード指標の sample_size は差の計算に寄与した outcome 行数であって、両側 95% 区間を作った 日次差の本数ではない。verdict_mix が示すとおりこの窓は run 17 日・verdict 148 件に対し proceed が 6 件しかなく、proceed と skip が同じ満期日に揃う日はごく限られる。dossier は 採用した日次差の本数を公表しておらず、excluded_day_count は捨てた日数しか示さないため、読み手が sample_size を区間の n と取り違えたまま L1 の床（n≥20 かつ区間が 0 を跨がない）を 満たしたと判断しうる状態にあると読める。実際に本振り返りでは、この本数を確かめるために dossier の外（読み取り専用の DuckDB 照会）に出る必要があった。

## 期待効果

ペアード指標の区間がどれだけの日次差から作られたかが dossier だけで読めるようになり、L1 の証拠ゲートを字面で満たすが実効日数が薄いケースを、提案を書く前に切り分けられるように なると考えられる。references/proposal-rules.md の「散らばりの読み方」に、この本数を見る旨を 追記することまでを一体の変更とする。

## 証拠

- `verdict_mix`
- `metric:separation:5d`
- `metric:separation:20d`

## 検証計画

1) MetricEntry に日次差の本数を表す任意フィールド（既定 None）を追加し、_paired_separation_for が平均した差の本数を設定する。既存フィールドの意味は変更しない。2) 日次差が 3 本しか作れない合成 outcome で集約を実行し、新フィールドが 3、sample_size が 寄与 outcome 行数、excluded_day_count が捨てた日数になることを表明する単体テストを追加する。3) 新フィールドを schemas.py の _drop_legacy_defaults に登録し、既存アーカイブ （reports/retro/2026-07-30 と reports/retro/2026-08-12 の retro_input.json）の input_digest が 変更後も検証を通ることを表明するテストを追加する。4) uv run pytest tests/retro -q と just verify が通ること。合否基準: 追加テストが変更前に失敗し変更後に成功すること、既存アーカイブの digest 検証が 1 件も壊れないこと、既存の retro テストが退行しないこと。

## リスク

- MetricEntry へのフィールド追加は retro-input-v1 のスキーマ変更であり、_drop_legacy_defaults への登録を誤ると過去アーカイブの input_digest 検証が一斉に失敗する
- 本数を公表しても、それを読む規律が references/proposal-rules.md に書かれなければ同じ取り違えは再発しうるため、ドキュメント追記を同じ変更に含める必要がある
- フィールド名や意味の選び方を誤ると、既存の excluded_day_count との関係がかえって読みにくくなる可能性がある

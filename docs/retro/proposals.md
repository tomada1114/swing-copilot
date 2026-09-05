# 改善提案台帳

`copilot-retro ingest` が検証を通った提案を status=proposed で追記する。
以降の遷移（applied / rejected / deferred / verification_failed、および
applied 後の merged / reverted）は適用段階のスキルと人間が記録する（D10）。

| RP-ID | 日付 | level | proposal_key | タイトル | status | PR/決裁メモ | リンク |
|---|---|---|---|---|---|---|---|
| RP-001 | 2026-09-02 | L2 | retro:evidence_id_space:issue190_metric_ids | ペアード separation と tracked_performance の metric_id を証拠 ID 空間に登録する | applied | [#416](https://github.com/tomada1114/swing-copilot/pull/416) | [全文](proposals/RP-001-retro-evidence-id-space-issue190-metric-ids.md) |
| RP-002 | 2026-09-02 | L2 | retro:aggregate:paired_metric_day_count | ペアード指標に平均した日次差の本数を併記し、sample_size との違いを読めるようにする | proposed |  | [全文](proposals/RP-002-retro-aggregate-paired-metric-day-count.md) |

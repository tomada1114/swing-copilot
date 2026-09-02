# RP-001 ペアード separation と tracked_performance の metric_id を証拠 ID 空間に登録する

- 提案日: 2026-09-02
- level: L2
- status: proposed
- proposal_key: `retro:evidence_id_space:issue190_metric_ids`
- 対象: `src/swing_copilot/retro/validate.py の evidence_id_space、および .claude/skills/swing-retro/references/result-schema.md の証拠 ID 表`
- 証拠の種類: qualitative

## 主張

SKILL.md は separation を 3 版とも読むこと、tracked_performance を proceed / skip の closed_count 付きで読むことを求めているが、evidence_id_space が登録しているのは metric:separation:5d / 20d / composed とその config 別分割までで、Issue #190 が追加した metric:separation_paired:*、metric:separation_paired_excess:*、metric:tracked_performance:* は 証拠空間に含まれていない。これらを evidence_refs に書いた提案・叙述は fail-closed で非表示に なるため、読むよう指示されている指標を根拠にした提案が原理的に書けない状態にあると読める。本提案が引用できる証拠が metric:separation:5d / 20d / composed と verdict_mix という プール版と混合比だけに限られること自体が、この欠落の現れであると考えられる。

## 期待効果

地合い交絡を除いたペアード版や、反実仮想の追跡台帳の成績を根拠にした提案・叙述が書けるように なり、SKILL.md が求める「3 版が一致するか食い違うか」の所見をそのまま提案の証拠に接続できる ようになると考えられる。逆に言えば、この変更がない限り、ペアード版だけが 0 を跨がない窓では 証拠ゲートを満たす提案が構造的に書けない状態が続く可能性がある。

## 証拠

- `metric:separation:5d`
- `metric:separation:20d`
- `metric:separation:composed`
- `verdict_mix`

## 検証計画

1) evidence_id_space に aggregates.separation_paired / separation_paired_excess / tracked_performance の ID を追加し、references/result-schema.md の証拠 ID 表にも対応する 3 行を追記する。2) 回帰テストとして、dossier が公表するすべての metric_id が evidence_id_space に含まれることを表明する単体テストを追加する。変更前に失敗し、変更後に 成功することを確認する。3) uv run pytest tests/retro -q が通ること。4) just verify （lint / docs-check / test-changed）が通ること。合否基準: 追加テストが変更前に失敗し変更後に 成功すること、既存の retro テストが 1 件も退行しないこと、および retro_input.json の スキーマ・input_digest が一切変わらないこと（本変更は result 側の検証範囲のみを広げる）。

## リスク

- 証拠空間を広げることは、実効日数の少ないペアード指標を字面のゲート通過だけで根拠にする余地も同時に広げる。RP-2（日次差の本数の公表）と併せて適用しない限り、L1 の床が見かけ上通りやすくなる副作用がありうる
- 過去の retro_result を再 ingest した場合、以前は非表示だった項目が表示されるようになるため、既存の retro_report.md との差分が生じうる
- 証拠空間の定義が広がることで、どの ID が引用可能かをスキル側が正しく把握できているかの検証負担が増える

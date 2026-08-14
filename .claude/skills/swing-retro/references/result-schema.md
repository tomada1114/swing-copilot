# `retro_result.json` の組み立て方

**最終正本は `src/swing_copilot/retro/schemas.py`（`RetroResult`）。** 本書は
書き出し手順の要約であり、フィールド名が食い違ったら schemas.py を正とする。
スキーマは全階層 `extra="forbid"` なので、発明したフィールド・綴り違いは
黙って捨てられずに hard fail する。

## 全体の形

```json
{
  "schema_version": "retro-result-v1",
  "as_of": "2027-03-11",
  "input_digest": "<retro_input.json から逐語コピーした 64 桁の hex>",
  "structural_review_note": "…（L2/L3 相当の構造的観察の有無。必須）",
  "narrations": [
    {
      "surprise_id": "surprise:<run_id>:<SYMBOL>",
      "failure_class": "information_absent",
      "narrative": "…",
      "evidence_refs": ["surprise:…", "news:…"]
    }
  ],
  "proposals": [
    {
      "proposal_key": "config:postmortem.severe_threshold_pct",
      "level": "L1",
      "target": "settings.postmortem.severe_threshold_pct",
      "title": "…",
      "claim": "…",
      "expected_effect": "…",
      "evidence_refs": ["metric:separation:5d", "metric:separation:20d"],
      "evidence_basis": "quantitative",
      "verification_plan": "…（L1/L2 は必須、L3 のみ null 可）",
      "risks": ["…"],
      "reopen_justification": null
    }
  ]
}
```

- `as_of` と `input_digest` は `retro_input.json` から**逐語コピー**する。
  不一致は run 全体の hard fail（何も書かれずに非 0 終了）
- `schema_version` は `retro-result-v1` 固定
- `narrations` / `proposals` は 0 件でもよい（空配列）。ただし
  `structural_review_note` は常に必須
- `surprise_id` は `narrations` 内で重複不可、`proposal_key` は `proposals` 内で重複不可

## `structural_review_note`（必須・省略不可）

Step 4 の自問「L2/L3 相当の構造的観察はないか」への答えをそのまま書く。
探した上で無ければ **「再点検の上でなし」** と、何を見て無いと判断したかを書く。
スキーマ上の必須フィールドなのは、スキル手順にしか書かれていない規律が
いちばん先に守られなくなるため。

CON-03 機械検査の対象なので、断定的売買指示・命令形はここにも書けない。
違反すると本文が「検証不合格のため非表示」に差し替えられる。

## 証拠 ID の空間（`evidence_refs`）

`evidence_refs` に書けるのは `retro_input.json` が供給した ID **だけ**。
部分集合でない参照を 1 つでも含む提案・叙述は、その項目だけが非表示になる
（fail-closed、リトライなし）。ID を推測・整形・生成しない。

| 由来 | 形 | 例 |
|---|---|---|
| 集約指標（ホライズン別） | `metric:<名前>:<N>d` | `metric:separation:5d`, `metric:skip_hit_rate:20d` |
| 集約指標（重み合成） | `metric:<名前>:composed` | `metric:proceed_severe_miss_rate:composed` |
| verdict_mix | `verdict_mix`（接頭辞なし） | `aggregates.verdict_mix.metric_id` をそのまま。`metric:` を補わない |
| 人間整合クロス集計 | `metric:human_alignment:<decision>:<recommendation>:<N>d` | `metric:human_alignment:followed:proceed:5d` |
| ソース貢献 | `metric:source_contribution:<source_type>:<provider>` | `metric:source_contribution:news:finnhub` |
| news_supply（全体 / セル） | `metric:news_supply` / `metric:news_supply:<level>:<recommendation>` | `metric:news_supply:sparse:proceed` |
| サプライズ銘柄 | `surprise:<run_id>:<SYMBOL>` | `surprises.items[].surprise_id` をそのまま |
| 引用ソース | `source_id` | `surprises.items[]` の `cited_source_ids` / `reasons[].source_ids` / `freshness.news[].source_id` / `freshness.filings[].source_id` |

`signal_performance` の行には ID がない（P2-11 の出力を逐語同梱しているだけで、
retro が採番していない）。シグナルについて提案するときは、シグナル名ではなく、
それを示している指標 ID かサプライズ ID を引く。

`narrations[].evidence_refs` は非空必須。叙述の `surprise_id` は
`retro_input.json` の `surprises.items[]` に実在するものに限る。

## 書き出し後の確認

```bash
uv run python -c "import json,sys;json.load(open(sys.argv[1]))" <RETRODIR>/retro_result.json
uv run copilot-retro ingest <RETRODIR>
```

`ingest` は成功時に、台帳へ記録した RP-ID・level・タイトル、非表示になった項目、
`retro_report.md` のパスを出力する。非表示が出たら文言を書き換えて再投入せず、
そのまま報告する（AC15）。

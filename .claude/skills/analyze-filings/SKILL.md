---
name: analyze-filings
description: >
  Interpret EDGAR filing text (10-K/10-Q/8-K and similar) for one or more equity
  symbols from analysis_input.json, extracting sourced facts, year-over-year
  changes, newly added risk-factor language, and guidance shifts with mandatory
  source_id provenance and conservative hedged interpretation. Use PROACTIVELY
  when: 開示分析、決算分析、EDGAR、10-K、10-Q、8-K、有価証券報告書の解釈、
  filing analysis, analyze filings, or when swing-daily delegates filing review.
---

# 開示・決算分析（専門家）

`analysis_input.json` の `candidates[].filings` を読み、`filing_analyses` を作る。
統括スキル `swing-daily` から呼ばれるほか、単体でも使える。

## Inputs

`analysis_input.json` があるディレクトリを `<WORKDIR>` と呼ぶ。

- `<WORKDIR>/analysis_input.json` — 必須。絶対パスで渡される。読み取り専用。
  対象銘柄の指定が無ければ `filings` が非空の全候補が対象
- 銘柄別入力スライス — 統括から渡された場合は、
  [output-schema.md の入力スライス契約](../swing-daily/references/output-schema.md#サブエージェント入力スライス読み取り専用作業用)
  に従い、これを分析に使う。`run_id` / `as_of` / `input_digest` が元 input と一致することを
  確認し、担当外の元入力本文は読み込まない
- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md) — AC1〜AC15 の共通規約（**必読**）
- [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md) — JSON の形と `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に読む

## Outputs

- `<WORKDIR>/analysis_work/filings-<SYMBOL>.json` — 担当銘柄ごとに 1 ファイル
  （1 銘柄の複数開示は 1 ファイル内の `filing_analyses` 配列にまとめる）。
  ディレクトリが無ければ作る。既存ファイルは上書きしてよい
- 親（または単体起動時のユーザー）に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 +
  AC 自己点検結果**だけ。JSON 全文・開示原文はメッセージに載せない

単体起動時も同じ。結果は必ず `analysis_work/` にファイルとして残す。

## 手順

1. 入力 JSON を読み、担当銘柄の `filings` 配列を取り出す。`source_id`,
   `form_type`, `filed_at`, `text`, `coverage` を控える。`coverage` はコード所有の
   完全性情報であり、`selection_mode`、`is_truncated`、章ごとの `status` を先に確認する。
   章が `partial` なら `original_chars` / `exported_chars` / `omission_shape` も控える
   （欠落量 = `original_chars - exported_chars`、欠落位置 = `omission_shape`）。
2. `filings` が空なら、その銘柄は `filing_analyses: []` とする（内容を作らない・AC14）。
   その場合も断片ファイルは書く（「分析済みで空」と「未分析」を区別するため）。
3. **開示 1 件につき 1 つの分析オブジェクト**を作る。`text` が長い場合は
   同じ担当コンテキスト内で分割して読み、部分ごとの読み取りを最後に統合する。
   別エージェント/API呼び出しへ分割して結果をマージしない。
4. 抽出の重点（この順で優先度が高い）。
   - **yoy_changes**: 前年同期比・前四半期比の変化。数値と単位をそのまま引用
   - **red_flags**: going concern 的な記述、訴訟・規制・会計処理の変更。
     10-Qのリスク要因については、`part_ii_item_1a` が `full` または `partial` で
     実質本文を含み、比較対象も入力内にある場合に限って**新規記載**や文言の強まりを
     判定する。10-K参照援用だけ、章が`missing`、比較対象なしの場合は
     「新規なし」とせず「入力からは判定不能」とする
   - **ガイダンス変化**: 上方／下方修正、レンジ変更、開示取りやめ
   - これらを facts（開示に明示された記述）と interpretation（示唆）に振り分ける（AC11）
5. 出力 JSON を組み立て、AC 自己点検（下記）を済ませてからファイルに書き出す。

## 守ること

- **網羅ではなく重要度優先（AC13）。** すべてのセクションを読み切ることは目的ではない。
  重要度の高い箇所から読み、`red_flags` が非網羅的である旨を **出力に含める**
  （`interpretation` に「本分析は提供された開示テキストの重要箇所に基づくもので、
  リスク要因の網羅ではない」相当の一文を入れる）。テキストを分割して読んだ場合や
  一部しか与えられていない場合も同様に明記する。
- `coverage.is_truncated`、`head_fallback`、`omitted_symbol_budget`、または章の
  `partial` / `missing` を無視しない。欠落した章について事実が無かったとは結論せず、
  どの範囲が未分析かを `interpretation` または `red_flags` に具体的に記す。
- **未分析範囲は「章名 + 欠落量 + 欠落位置」で書く。** 「一部のみ」で止めない。
  `original_chars` / `exported_chars` / `omission_shape` がある章はそれを使う。
  - `omission_shape: "head_and_tail"` → 「part_ii_item_1a は 20,500 字中 18,390 字を
    読み、章の**中間**約 2,110 字は未分析」。#79 以降は先頭と末尾が残るため、
    未分析なのは末尾ではなく中間である。「末尾が読めていない」とは書かない
  - `omission_shape: "head_only"` → 「先頭 N 字のみで、以降 M 字は未分析」
  - 3 値が `null`（古いアーカイブや復元された coverage）→ 欠落量・位置は不明として
    「未分析範囲の特定不能」と書く。欠落が無かったことにはしない
  - `status: "missing"` → その章は入力に**存在しない**（章の長さも不明）。
    「その章に記載が無かった」ではなく「その章は入力に含まれていない」と書く
- **interpretation は保守的に（AC12）。** 開示は事後的・法務的な文書であり、単独で
  将来を予測しない。hedge を付け、断定を避ける。
- **facts は開示の記述そのもの（AC11）。** 数値は加工せずそのまま。自分で比率や成長率を
  計算して fact にしない（計算結果を書くなら interpretation に置き、根拠数値を示す）。
- **入力に無い情報を書かない（AC8）。** 事前知識の過去決算・アナリスト予想・株価は使わない。
  比較対象が開示内に無ければ「開示内に前年同期の対応数値なし」と書く。
- **source_id は該当開示のものを非空リストで付ける（AC6・AC7・AC9）。** 推測・生成しない。
- **断定的売買指示を絶対に出さない（AC3・AC4）。**「売るべき」「強く推奨」「strong sell」等、
  および読者への命令形は禁止。
- **経営陣の心理を診断しない（AC5）。**「経営陣が動揺」等は、同一文に実績と計画の具体的な
  数値乖離（％＋実績/actual＋計画/予想）と AC12 の hedge 表現が揃わない限り書かない。
  1 つでも欠けるとその銘柄が丸ごと withhold される。

## 出力前の AC 自己点検

ファイルを書き出す前に
[../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md)
の AC チェックリスト（AC1〜AC15）を上から自己点検する。

- 違反が見つかったら**その場で直してから**書き出す。検査を通すための言い換えで
  実質的な違反を残さない（AC15）
- 断片の `ac_check` フィールドと親に返す要約の両方に、
  **「AC1-AC15 違反なし」または懸念のある AC 番号と一言**を必ず含める

## 出力ファイル

銘柄ごとに `<WORKDIR>/analysis_work/filings-<SYMBOL>.json` を書く。
1 銘柄に複数の開示があれば `filing_analyses` 配列に開示ごとのオブジェクトを並べる。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // analysis_input.json から逐語コピー
  "as_of": "2026-07-27",           // analysis_input.json の as_of をそのままコピー
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>", // 同じく逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC15 違反なし",
  "filing_analyses": [             // 該当開示が無ければ []
    {
      "source_id": "filing-...",
      "facts": [ { "text": "...", "source_ids": ["filing-..."] } ],
      "interpretation": ["...", "本分析は提供された開示テキストの重要箇所に基づくもので、リスク要因の網羅ではない"],
      "red_flags": ["..."],
      "yoy_changes": ["..."]
    }
  ]
}
```

`run_id` / `as_of` / `input_digest` / `ac_check` は作業用メタデータで、統括がマージ時に捨てる。
この 3 値は Step 0 の再入判定に使うため、いずれも省略・再計算・変更しない。
`filing_analyses` の中身だけが `analysis_result.json` の
`symbols[].filing_analyses` に載る。

`filing_analyses` の要素は `source_id` / `facts` / `interpretation` / `red_flags` /
`yoy_changes` の 5 フィールドで完結する。旧スキーマにあった `filing_type` や
`guidance_direction`（`positive` / `negative` / `neutral` / `not_disclosed`）は
新契約に**存在しない**（意図的に廃止）。`form_type` / `filed_at` も結果には含めない
（ingest が `analysis_input.json` から `source_id` で解決する）。`schemas.py` に
無いフィールドを追加しない。ingest は `extra="forbid"` の strict 検証で未知フィールドを
拒否する。
フィールド名は `schemas.py` の実装が正本。食い違ったら `schemas.py` に従う。

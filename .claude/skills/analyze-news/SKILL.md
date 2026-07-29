---
name: analyze-news
description: >
  Interpret collected news items for one or more equity symbols from
  analysis_input.json, extracting sourced facts, hedged interpretation, and risk
  flags with mandatory source_id provenance. Separates observed facts from
  inference and never emits imperative buy/sell language. Use PROACTIVELY when:
  ニュース分析、ニュース解釈、材料分析、ヘッドライン分析、news analysis,
  analyze news, or when swing-daily delegates news interpretation.
---

# ニュース分析（専門家）

`analysis_input.json` の `candidates[].news` を読み、`news_summary` を作る。
統括スキル `swing-daily` から呼ばれるほか、単体でも使える。

## Inputs

`analysis_input.json` があるディレクトリを `<WORKDIR>` と呼ぶ。

- `<WORKDIR>/analysis_input.json` — 必須。絶対パスで渡される。読み取り専用。
  対象銘柄の指定が無ければ `news` が非空の全候補が対象
- 銘柄別入力スライス — 統括から渡された場合は、
  [output-schema.md の入力スライス契約](../swing-daily/references/output-schema.md#サブエージェント入力スライス読み取り専用作業用)
  に従い、これを分析に使う。`run_id` / `as_of` / `input_digest` が元 input と一致することを
  確認し、担当外の元入力本文は読み込まない
- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md) — AC1〜AC15 の共通規約（**必読**）
- [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md) — JSON の形と `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に読む

## Outputs

- `<WORKDIR>/analysis_work/news-<SYMBOL>.json` — 担当銘柄ごとに 1 ファイル。
  ディレクトリが無ければ作る。既存ファイルは上書きしてよい
- 親（または単体起動時のユーザー）に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 +
  AC 自己点検結果**だけ。JSON 全文・生の入力テキストはメッセージに載せない

単体起動時も同じ。結果は必ず `analysis_work/` にファイルとして残す。

## 手順

1. 入力 JSON を読み、担当銘柄の `news` 配列を取り出す。各項目の `source_id`,
   `published_at`, `headline`, `summary`, `provider` を控える。
2. `news` が空なら、その銘柄は `news_summary: null` とする（内容を作らない・AC14）。
   その場合も断片ファイルは書く（「分析済みで空」と「未分析」を区別するため）。
3. 銘柄ごとに以下を抽出する。
   - **facts**: 入力テキストに**明示されている**事実だけ。数値・日付・当事者名・
     発表内容。各 fact に、その根拠となった項目の `source_id` を非空リストで付ける。
     複数項目に支えられるなら全部列挙する。
   - **interpretation**: その事実がスイングトレードの時間軸で何を示唆しうるか。
     hedge 付き（「〜の可能性がある」「〜と読める」）。1〜4 項目程度。
   - **risk_flags**: 短い懸念の列挙（例:「決算発表が直近に控える」「訴訟リスクの
     新規報道」）。非網羅である旨は interpretation 側に書く（AC13）。
4. 出力 JSON を組み立て、AC 自己点検（下記）を済ませてからファイルに書き出す。

## 守ること

- **憶測と事実を分離する（AC11）。** 評価語（「好調」「悪化」「サプライズ」）は
  facts に入れず interpretation に置く。
- **入力に無い情報を書かない（AC8）。** 事前知識の企業情報・株価・決算数値を補完しない。
  headline しか無く中身が分からない項目は、分かる範囲だけを fact にする。
- **source_id は入力の文字列をそのままコピーする（AC6・AC7・AC9）。** 推測・生成・整形しない。
- **断定的売買指示を絶対に出さない（AC3・AC4）。**「買うべき」「売るべき」「強く推奨」
  「you should buy」等、および読者への命令形（「〜してください」「〜せよ」）は禁止。
- **根拠なき心理診断を書かない（AC5）。**「投資家心理が悪化」「パニック」等は、同一文に
  実績と計画の具体的な数値乖離（％＋実績/actual＋計画/予想）と AC12 の hedge 表現が
  揃わない限り書かない。1 つでも欠けるとその銘柄が丸ごと withhold される。
  観測可能な事実で置き換えるのが安全。
- 出典が明らかに古い（`published_at` が as_of から大きく離れている）項目は、
  その旨を interpretation に添える。

## 出力前の AC 自己点検

ファイルを書き出す前に
[../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md)
の AC チェックリスト（AC1〜AC15）を上から自己点検する。

- 違反が見つかったら**その場で直してから**書き出す。検査を通すための言い換えで
  実質的な違反を残さない（AC15）
- 断片の `ac_check` フィールドと親に返す要約の両方に、
  **「AC1-AC15 違反なし」または懸念のある AC 番号と一言**を必ず含める

## 出力ファイル

銘柄ごとに `<WORKDIR>/analysis_work/news-<SYMBOL>.json` を書く。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // analysis_input.json から逐語コピー
  "as_of": "2026-07-27",           // analysis_input.json の as_of をそのままコピー
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>", // 同じく逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC15 違反なし",
  "news_summary": {                // 該当ニュースが無ければ null
    "facts": [ { "text": "...", "source_ids": ["news-..."] } ],
    "interpretation": ["..."],
    "risk_flags": ["..."]
  }
}
```

`run_id` / `as_of` / `input_digest` / `ac_check` は作業用メタデータで、統括がマージ時に捨てる。
この 3 値は Step 0 の再入判定に使うため、いずれも省略・再計算・変更しない。
`news_summary` の中身だけが `analysis_result.json` の
`symbols[].news_summary` に載る。

`news_summary` は `facts` / `interpretation` / `risk_flags` の 3 フィールドで完結する。
旧スキーマにあった `sentiment` / `catalyst_quality` / `sources` 等は新契約に**存在しない**
（意図的に廃止）。`schemas.py` に無いフィールドを追加しない。ingest は
`extra="forbid"` の strict 検証で未知フィールドを拒否する。
フィールド名は `schemas.py` の実装が正本。食い違ったら `schemas.py` に従う。

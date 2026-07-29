---
name: interpret-screening
description: >
  Add qualitative reading to the deterministic screening output in
  analysis_input.json — why a symbol survived, its strengths, and its concerns —
  by interpreting score_breakdown, risk_constraints, and market_regime without
  ever recomputing or overriding the numbers. Use PROACTIVELY when:
  スクリーニング評価、スコア解釈、候補の定性評価、なぜこの銘柄が残ったか、
  リスク制約の読み解き、screening interpretation, or when swing-daily delegates
  screening assessment.
---

# スクリーニング定性評価（専門家）

`analysis_input.json` の `score_breakdown` / `risk_constraints` /
`context.market_regime` を読み、`screening_assessment` を作る。
統括スキル `swing-daily` から呼ばれるほか、単体でも使える。

## Inputs

`analysis_input.json` があるディレクトリを `<WORKDIR>` と呼ぶ。

- `<WORKDIR>/analysis_input.json` — 必須。絶対パスで渡される。読み取り専用。
  対象銘柄の指定が無ければ **全候補**が対象（news/filings が空の銘柄も含む）
- 銘柄別入力スライス — 統括から渡された場合は、
  [output-schema.md の入力スライス契約](../swing-daily/references/output-schema.md#サブエージェント入力スライス読み取り専用作業用)
  に従い、これを分析に使う。`run_id` / `as_of` / `input_digest` が元 input と一致することを
  確認し、担当外の元入力本文は読み込まない
- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md) — AC1〜AC15 の共通規約（**必読**）
- [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md) — JSON の形と `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に読む

## Outputs

- `<WORKDIR>/analysis_work/screening-<SYMBOL>.json` — 担当銘柄ごとに 1 ファイル。
  ディレクトリが無ければ作る。既存ファイルは上書きしてよい
- 親（または単体起動時のユーザー）に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 +
  AC 自己点検結果**だけ。JSON 全文・入力テキストの丸写しはメッセージに載せない

単体起動時も同じ。結果は必ず `analysis_work/` にファイルとして残す。

## 手順

1. 入力 JSON を読み、`context.market_regime` / `context.performance_summary` /
   `context.calendar_events`（マクロ／経済カレンダーイベント）を把握する
   （当日の市場環境の前提になる）。
2. 銘柄ごとに `score_breakdown`, `risk_constraints`, あれば `decision_history`
   を読む。
3. 次の 3 点を書く。
   - **summary**: なぜこの銘柄がスクリーニングを通過したか。寄与の大きい要素を
     数値のまま引用しつつ、市場環境との関係を添える
   - **strengths**: 強みの短い列挙
   - **concerns**: 懸念の短い列挙（リスク制約の余裕が薄い、寄与が単一指標に偏る、
     市場環境と相性が悪い等）
4. 出力 JSON を組み立て、AC 自己点検（下記）を済ませてからファイルに書き出す。

## 守ること

- **決定論的スコアを再計算・上書きしない（AC1）。** スコア、ランキング、リスク制約の値は
  Python が確定させたもの。合計の検算や「本来はこの順位のはず」といった修正はしない。
- **数値はそのまま引用する（AC1）。** 丸め直し・単位変換・言い換えをしない。
- **加えるのは解釈だけ（AC2）。** 「この寄与は市場環境が trending のときに効きやすいと
  考えられる」のような読み方を足す。
- **入力に無い情報を書かない（AC8）。** 事前知識の株価・ファンダメンタル・セクター動向を
  持ち込まない。判断材料が足りなければ「入力の範囲では判断できない」と書く。
- **hedge を付ける（AC12）。** 断定（「必ず伸びる」）は避ける。
- **断定的売買指示・命令形を出さない（AC3・AC4）。** 懸念は懸念として書き、行動を指示しない。
- **根拠なき心理診断を書かない（AC5）。**
- `screening_assessment` は **全銘柄必須**。news/filings が空の銘柄でも必ず書く。
- `context.calendar_events`（マクロイベント）は懸念の根拠として利用可。`concerns`は
  `source_ids`フィールドを持たないため、引用する場合はイベントの`source_id`を本文中に
  明記する（例:「(source_id: fred:...) の指標発表を控える」）。
- ここで書く内容は決定論的入力に基づくため `source_ids` を持たない
  （`screening_assessment` に provenance フィールドは無い・AC10）。テキスト由来の主張を
  混ぜたくなったら、それはニュース／開示分析側の担当（AC11）。

## 出力前の AC 自己点検

ファイルを書き出す前に
[../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md)
の AC チェックリスト（AC1〜AC15）を上から自己点検する。

- 違反が見つかったら**その場で直してから**書き出す。検査を通すための言い換えで
  実質的な違反を残さない（AC15）
- 断片の `ac_check` フィールドと親に返す要約の両方に、
  **「AC1-AC15 違反なし」または懸念のある AC 番号と一言**を必ず含める

## 出力ファイル

銘柄ごとに `<WORKDIR>/analysis_work/screening-<SYMBOL>.json` を書く。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // analysis_input.json から逐語コピー
  "as_of": "2026-07-27",           // analysis_input.json の as_of をそのままコピー
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>", // 同じく逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC15 違反なし",
  "screening_assessment": {        // 全銘柄必須。null や省略は不可
    "summary": "...",
    "strengths": ["..."],
    "concerns": ["..."]
  }
}
```

`run_id` / `as_of` / `input_digest` / `ac_check` は作業用メタデータで、統括がマージ時に捨てる。
この 3 値は Step 0 の再入判定に使うため、いずれも省略・再計算・変更しない。
`screening_assessment` の中身だけが `analysis_result.json` の
`symbols[].screening_assessment` に載る。
フィールド名は `schemas.py` の実装が正本。食い違ったら `schemas.py` に従う。

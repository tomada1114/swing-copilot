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
- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md) — AC1〜AC16 の共通規約（**必読**）
- [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md) — JSON の形と `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に読む

## Outputs

- `<WORKDIR>/analysis_work/news-<SYMBOL>.json` — 担当銘柄ごとに 1 ファイル。
  ディレクトリが無ければ作る。既存ファイルは上書きしてよい
- 親（または単体起動時のユーザー）に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 +
  AC 自己点検結果**だけ。JSON 全文・生の入力テキストはメッセージに載せない

単体起動時も同じ。結果は必ず `analysis_work/` にファイルとして残す。

### 一時ファイル

作業用の一時ファイル（抽出テキスト等）は**セッションの scratchpad
ディレクトリ配下にだけ**作る。`<WORKDIR>` 配下やリポジトリ配下には作らない。
**契約検証のスクリプトは書かない**（下記「書き出し後の契約検証」の共有コマンドを使う）。

**作った一時ファイルを削除しない。`rm` を実行しない。** scratchpad はセッション終了時に
破棄されるため掃除は不要であり、このスキルは平日定時の無人実行から呼ばれるため、
`rm` を出すとワークフロー全体が承認待ちで停止する。

## 手順

1. 入力 JSON を読み、担当銘柄の `news` 配列を取り出す。各項目の `source_id`,
   `published_at`, `headline`, `summary`, `provider` を控える。あわせて同じ候補の
   `news_supply`（コードが数えた自社材料の供給量。下記「自社材料の供給量」）を控える。
2. `news` が空なら、その銘柄は `news_summary: null` とする（内容を作らない・AC14）。
   その場合も断片ファイルは書く（「分析済みで空」と「未分析」を区別するため）。
3. 銘柄ごとに以下を抽出する。
   - **facts**: 入力テキストに**明示されている**事実だけ。数値・日付・当事者名・
     発表内容。各 fact に、その根拠となった項目の `source_id` を非空リストで付ける。
     複数項目に支えられるなら全部列挙する。さらに、その `source_id` の
     `headline` ＋ `summary` から実際に読んだ箇所を `evidence_quote`
     （12〜300 字の逐語引用）として付ける。要約や言い換えではなく、本文に
     含まれる文字列そのものを抜き出す。
   - **interpretation**: その事実がスイングトレードの時間軸で何を示唆しうるか。
     hedge 付き（「〜の可能性がある」「〜と読める」）。1〜4 項目程度。
   - **risk_flags**: 短い懸念の列挙（例:「決算発表が直近に控える」「訴訟リスクの
     新規報道」）。非網羅である旨は interpretation 側に書く（AC13）。自社材料が
     薄いときは下記「自社材料の供給量」の申告をこの先頭に置く。
4. 出力 JSON を組み立て、AC 自己点検（下記）を済ませてからファイルに書き出す。
5. 書き出したら `copilot-verify-analysis` で契約検証する（下記「書き出し後の契約検証」）。

## 自社材料の供給量（Issue #130）

ニュース入力には、同業他社の決算記事・セクター横断記事・定型マーケットサマリが
混ざる。20 件供給されていても担当銘柄自身の材料が数件しか無いことがあり、その状態は
**「悪材料が見当たらない」ではなく「判断材料が供給されていない」**である。両者を
下流が取り違えないよう、コードは候補ごとに `news_supply` を数えて渡す。

```jsonc
"news_supply": {
  "collected_items": 24,        // 収集できたニュース件数（枠で切る前）
  "exported_items": 20,         // 実際に news[] へ載った件数
  "symbol_mention_items": 4,    // うち headline/summary にティッカーが現れる件数
  "level": "sparse"             // "sufficient" | "sparse" | "none"
}
```

`symbol_mention_items` はティッカー表記の有無だけで数えた**下限値**であり、社名しか
書かれていない自社記事は数え落とす。逆に `sufficient` でも中身が同業他社の話である
ことはある。よってこの数値は結論ではなく、**申告を義務づける引き金**として使う。

- **`level` が `sparse` / `none` のとき、または本文を読んだ結果として担当銘柄自身の
  決算・開示・事業行動を報じた項目が見当たらないとき**は、`risk_flags` の先頭に
  `材料供給不足:` で始まる項目を必ず 1 つ置き、供給量（`symbol_mention_items` /
  `exported_items`）と、読み取れた材料の性質（例: 記念行事・長期リターン記事・
  同業の決算）を書く。`news_supply` が入力に無い（旧アーカイブ）場合も、自分で
  読んだ結果に基づいて同じ判断をする
- **悪材料の不在を好材料として書かない。** `interpretation` にも `facts` にも、
  「重大な悪材料は無い」「懸念は見当たらない」を裏付けとして置かない。書けるのは
  「本入力の範囲では確認できない」までで、これは中立の情報欠落であって安心材料ではない
- **同業他社の記事は担当銘柄の実績として書かない。** 他社の決算・ガイダンスを fact に
  するときは主語を必ずその他社にし、担当銘柄への含意は hedge 付きで
  `interpretation` に置く。同じ事業環境が立場の違う 2 社に逆向きに効くことがある

## 守ること

- **憶測と事実を分離する（AC11）。** 評価語（「好調」「悪化」「サプライズ」）は
  facts に入れず interpretation に置く。
- **入力に無い情報を書かない（AC8）。** 事前知識の企業情報・株価・決算数値を補完しない。
  headline しか無く中身が分からない項目は、分かる範囲だけを fact にする。
- **source_id は入力の文字列をそのままコピーする（AC6・AC7・AC9）。** 推測・生成・整形しない。
- **evidence_quote は自分が引用する source_id の本文からの逐語引用にする（AC6）。**
  読んでいない項目・別銘柄の項目から書いた fact には引用できる文字列が無い。
  ingest 側が本文との一致を機械検証するため、要約や記憶での言い換えは通らない。
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
の AC チェックリスト（AC1〜AC16）を上から自己点検する。

- 違反が見つかったら**その場で直してから**書き出す。検査を通すための言い換えで
  実質的な違反を残さない（AC15）
- 断片の `ac_check` フィールドと親に返す要約の両方に、
  **「AC1-AC16 違反なし」または懸念のある AC 番号と一言**を必ず含める

## 書き出し後の契約検証（共有コマンド）

**検証スクリプトを自作しない。** 断片を書き出したら次を実行する。

```bash
uv run copilot-verify-analysis <WORKDIR>/analysis_work/news-<SYMBOL>.json
```

- 複数の断片を書いたなら、まとめて渡すか `<WORKDIR>/analysis_work` を渡す
- このコマンドは ingest（`copilot-ingest-analysis`）と**同一の関数**で
  strict schema・provenance・`evidence_quote` の逐語一致・CON-03 を検査する。
  逐語一致と CON-03 は Unicode NFKC 正規化を経て判定されるため、grep や
  自作スクリプトでは再現できず、**ingest では落ちるものを「合格」と誤報告する**
- 終了コード `0` なら合格。`1` なら FAIL 行に違反理由が出るので、**文言を検査に
  合わせて書き換えるのではなく**内容を直して書き出し直し、再実行する（AC15）
- 詳細は
  [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md)
  の「断片の契約検証」を参照

## 出力ファイル

銘柄ごとに `<WORKDIR>/analysis_work/news-<SYMBOL>.json` を書く。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // analysis_input.json から逐語コピー
  "as_of": "2026-07-27",           // analysis_input.json の as_of をそのままコピー
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>", // 同じく逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC16 違反なし",
  "news_summary": {                // 該当ニュースが無ければ null
    "facts": [ { "text": "...", "source_ids": ["news-..."],
                  "evidence_quote": "headline か summary からの12〜300字の逐語引用" } ],
    "interpretation": ["..."],
    "risk_flags": ["材料供給不足: ...", "..."]  // 該当時は供給量の申告を先頭に置く
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

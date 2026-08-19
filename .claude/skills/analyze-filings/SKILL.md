---
name: analyze-filings
description: >
  Interpret EDGAR filing text (10-K/10-Q/8-K and similar) for one or more equity
  symbols from analysis_input.json, extracting sourced facts, year-over-year
  changes, risk-factor language, and guidance shifts with mandatory
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
- [../swing-daily/references/analysis-conventions.md](../swing-daily/references/analysis-conventions.md) — AC1〜AC16 の共通規約（**必読**）
- [../swing-daily/references/output-schema.md](../swing-daily/references/output-schema.md) — JSON の形と `analysis_work/` 断片の形式
- `src/swing_copilot/analysis/schemas.py` — **スキーマの最終正本**。JSON を組み立てる前に読む

## Outputs

- `<WORKDIR>/analysis_work/filings-<SYMBOL>.json` — 担当銘柄ごとに 1 ファイル
  （1 銘柄の複数開示は 1 ファイル内の `filing_analyses` 配列にまとめる）。
  ディレクトリが無ければ作る。既存ファイルは上書きしてよい
- 親（または単体起動時のユーザー）に返すのは **銘柄ごと 1〜2 行の要約 + 特記事項 +
  AC 自己点検結果**だけ。JSON 全文・開示原文はメッセージに載せない

単体起動時も同じ。結果は必ず `analysis_work/` にファイルとして残す。

### 一時ファイル

作業用の一時ファイル（開示本文の抽出等）は**セッションの scratchpad
ディレクトリ配下にだけ**作る。`<WORKDIR>` 配下やリポジトリ配下には作らない。
**契約検証のスクリプトは書かない**（下記「書き出し後の契約検証」の共有コマンドを使う）。

**作った一時ファイルを削除しない。`rm` を実行しない。** scratchpad はセッション終了時に
破棄されるため掃除は不要であり、このスキルは平日定時の無人実行から呼ばれるため、
`rm` を出すとワークフロー全体が承認待ちで停止する。

## 実行上限（統括から呼ばれた場合）

統括（`swing-daily`）から渡される壁時計・ツール呼び出し回数・`copilot-verify-analysis`
再試行回数の上限に従う（正本は
[../swing-daily/SKILL.md](../swing-daily/SKILL.md) の「サブエージェントの実行上限と
打ち切り」。数値はここに写さない）。開示本文は長く、読み切ろうとすると上限に達しやすい。

- 渡された入力スライスは **通し 1 回で読む**。読み終えた範囲を読み直さない。長い `text`
  を先頭から順にチャンクへ分けて読むのは「1 回」に含まれる（下記「手順」3 の分割読みは
  この上限に抵触しない）。元の `analysis_input.json` は
  `run_id` / `as_of` / `input_digest` の照合に必要な範囲だけを開き、担当外銘柄の
  `text` に触れない
- 網羅ではなく重要度優先（下記「守ること」AC13）は、この上限の下で特に効く。
  全章を読み切ることを目標にしない
- 上限に達したら、**未完了の銘柄のファイルを書かずに**終了する。書きかけの断片や
  一部の開示しか見ていない断片を出さない（統括はそれを「分析済み」として扱わない）。
  完了した銘柄のファイルだけを残し、どの銘柄が未完了かを親への要約に明記する
- 上限に達しても `rm` は実行しない（上記「一時ファイル」）

## 手順

1. 入力 JSON を読み、担当銘柄の `filings` 配列を取り出す。`source_id`,
   `form_type`, `filed_at`, `text`, `coverage` を控える。`coverage` はコード所有の
   完全性情報であり、`selection_mode`、`is_truncated`、`exhibit_truncated`、
   章ごとの `status` を先に確認する。
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
     リスク要因（`part_ii_item_1a`）の扱いは下記「リスク要因の扱い（Item 1A）」に従う
   - **ガイダンス変化**: 上方／下方修正、レンジ変更、開示取りやめ
   - これらを facts（開示に明示された記述）と interpretation（示唆）に振り分ける（AC11）。
     各 fact には、その `source_id` の入力 `text` から実際に読んだ箇所を
     `evidence_quote`（12〜300 字の逐語引用）として付ける
5. 出力 JSON を組み立て、AC 自己点検（下記）を済ませてからファイルに書き出す。
6. 書き出したら `copilot-verify-analysis` で契約検証する（下記「書き出し後の契約検証」）。

## リスク要因の扱い（Item 1A）

10-Q の Item 1A は、前回提出（多くは直近 10-K）から重要な変更が無ければ**参照援用だけで
済ませてよい**。つまり参照援用は例外ではなく通常状態であり、比較対象となる 10-K の Item 1A
本文はこの入力に含まれない。**「新規のリスク記載があるか」「文言が強まったか」は、この入力
からは構造的に判定できない。判定しようとしない**（Issue #127）。

章の中身で扱いを分ける。

- **実質本文がある**（リスクの記述本文、あるいはリスク見出し／キャプションの列挙を含む）→
  従来どおり読む。**そこに書かれているリスクの内容そのもの**（訴訟・規制・顧客集中・
  資金調達・サプライチェーン等）を評価し、重要なものを `red_flags` に、含意を
  `interpretation` に置く。fact には他と同様 `evidence_quote` を付ける
- **参照援用のみ**（「前回 10-K から重要な変更なし」の申告と 10-K への参照だけで、リスクの
  記述本文を持たない）→ **リスク要因を論点として立てない**。「新規記載の有無は判定不能」
  「比較対象が無い」といった文言を `red_flags` / `interpretation` に**書かない**。毎回同じ
  判定不能を書いても情報価値がなく、他の論点を薄めるだけである。この場合 Item 1A について
  は何も出力しなくてよい

どちらかの判定は**字数ではなく内容**で行う。参照援用の前置きが長くても、リスクの記述や
リスク見出しの列挙が続いていれば「実質本文がある」側として扱う。

いずれの場合も、比較対象が入力に無いまま「新規リスクなし」「リスクは変化していない」と
**判定してはならない**（AC8）。判定不能を書かないことは「変化なし」を意味しない。

章の `status` が `absent_from_filing` / `not_parsed` / `partial` / `missing` のときは、
新規性ではなく**入力の欠落**の問題であり、下記「守ること」の欠落章ルールをそのまま適用する
（欠落の明示は続ける）。

## 守ること

- **網羅ではなく重要度優先（AC13）。** すべてのセクションを読み切ることは目的ではない。
  重要度の高い箇所から読み、`red_flags` が非網羅的である旨を **出力に含める**
  （`interpretation` に「本分析は提供された開示テキストの重要箇所に基づくもので、
  リスク要因の網羅ではない」相当の一文を入れる）。テキストを分割して読んだ場合や
  一部しか与えられていない場合も同様に明記する。
- `coverage.is_truncated`、`coverage.exhibit_truncated`、`head_fallback`、
  `omitted_symbol_budget`、または章の
  `partial` / `absent_from_filing` / `not_parsed` / `missing` を無視しない。
  欠落した章について事実が無かったとは結論せず、
  どの範囲が未分析かを `interpretation` または `red_flags` に具体的に記す。
- **`exhibit_truncated: true` は「取得段で Exhibit が欠けている」**。この欠落は
  `is_truncated` にも `original_chars` / `exported_chars` にも現れないので、
  `is_truncated: false` / `selection_mode: "full"` と同時に立ちうる。
  どちらの上限で欠けたかは **本文中のマーカーで区別する**（両方入ることもある）。
  - `[... exhibit truncated ...]` → 1 開示 500,000 字の安全弁（Issue #180）。
    「プレスリリース本文の末尾が入力に含まれていない。欠落の範囲・位置は入力からは
    特定できない」旨を `interpretation` に書き、末尾に置かれがちな非GAAP調整表・
    補足表・ガイダンス表について「記載が無かった」と結論しない
  - `[... exhibit omitted: per-filing exhibit count cap ...]` → 1 開示 3 件の
    件数上限。4 本目以降の `EX-99` 添付は**取得されておらず、本文が一切無い**。
    「補足資料・プレゼン等の添付が入力に含まれていない」旨を書き、
    その内容について何も推測しない
- **`exhibit_truncated: false` は「マーカーが無い」であって「欠落が無い」ではない。**
  Exhibit の取得自体に失敗した開示や、マーカー導入前のアーカイブも `false` になる。
  非網羅である旨（AC13）は `false` でも省略しない。
- **未分析範囲は「章名 + 欠落量 + 欠落位置」で書く。** 「一部のみ」で止めない。
  `original_chars` / `exported_chars` / `omission_shape` がある章はそれを使う。
  - `omission_shape: "head_and_tail"` → 「part_ii_item_1a は 20,500 字中 18,390 字を
    読み、章の**中間**約 2,110 字は未分析」。#79 以降は先頭と末尾が残るため、
    未分析なのは末尾ではなく中間である。「末尾が読めていない」とは書かない
  - `omission_shape: "head_only"` → 「先頭 N 字のみで、以降 M 字は未分析」
  - `omission_shape: "value_selected"`（8-K の Exhibit。Issue #181）→
    落ちたのは末尾でも中間でもなく、本文中の
    `[... omitted lower-value exhibit passage ...]` が入っている箇所である。
    「exhibit_ex_99_1 は 179,761 字中 96,400 字を読み、マーカーの位置に
    未分析の段落がある」と書く。定型文から先に落ちるため財務諸表・非 GAAP
    調整表は残りやすいが、**残っている表が全てだとは書かない**
  - 章名が `exhibit_primary` / `exhibit_ex_99_1` / `exhibit_ex_99_2` … の場合、
    それぞれ 8-K の主文書・Exhibit 99.1（プレスリリース）・後続 Exhibit
    （補足資料）を指す。どの Exhibit が `partial` なのかまで書く
  - 3 値が `null`（古いアーカイブや復元された coverage）→ 欠落量・位置は不明として
    「未分析範囲の特定不能」と書く。欠落が無かったことにはしない
  - `status: "absent_from_filing"` → 同じPartの他の章はパーサが取れているため、
    この章自体が提出書類に**無い可能性が高い**（10-Qでは前回提出から重要な変更が
    無ければItem 1A等を省略できる）。ただし断定はせず「入力からは記載の有無を
    判定できない」と書く
  - `status: "not_parsed"` → 同じPart自体の構造をパーサが取れておらず、この章が
    実際に存在するかどうか**入力からは判定できない**。「記載が無い」とは書かない
  - `status: "missing"`（過去アーカイブのみに残る値。新規生成物には出ない）→
    その章は入力に**存在しない**（章の長さも不明）。
    「その章に記載が無かった」ではなく「その章は入力に含まれていない」と書く
- **interpretation は保守的に（AC12）。** 開示は事後的・法務的な文書であり、単独で
  将来を予測しない。hedge を付け、断定を避ける。
- **facts は開示の記述そのもの（AC11）。** 数値は加工せずそのまま。自分で比率や成長率を
  計算して fact にしない（計算結果を書くなら interpretation に置き、根拠数値を示す）。
- **入力に無い情報を書かない（AC8）。** 事前知識の過去決算・アナリスト予想・株価は使わない。
  比較対象が開示内に無ければ「開示内に前年同期の対応数値なし」と書く。
- **source_id は該当開示のものを非空リストで付ける（AC6・AC7・AC9）。** 推測・生成しない。
- **evidence_quote は自分が引用する source_id の入力 `text` からの逐語引用にする（AC6）。**
  読んでいない開示・別銘柄の開示から書いた fact には引用できる文字列が無い。
  ingest 側が本文との一致を機械検証するため、要約や記憶での言い換えは通らない。
  長文を分割して読んだ場合も、実際にその範囲を読んだ箇所から引用する。
- **断定的売買指示を絶対に出さない（AC3・AC4）。**「売るべき」「強く推奨」「strong sell」等、
  および読者への命令形は禁止。
- **経営陣の心理を診断しない（AC5）。**「経営陣が動揺」等は、同一文に実績と計画の具体的な
  数値乖離（％＋実績/actual＋計画/予想）と AC12 の hedge 表現が揃わない限り書かない。
  1 つでも欠けるとその銘柄が丸ごと withhold される。

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
uv run copilot-verify-analysis <WORKDIR>/analysis_work/filings-<SYMBOL>.json
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

銘柄ごとに `<WORKDIR>/analysis_work/filings-<SYMBOL>.json` を書く。
1 銘柄に複数の開示があれば `filing_analyses` 配列に開示ごとのオブジェクトを並べる。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // analysis_input.json から逐語コピー
  "as_of": "2026-07-27",           // analysis_input.json の as_of をそのままコピー
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>", // 同じく逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC16 違反なし",
  "filing_analyses": [             // 該当開示が無ければ []
    {
      "source_id": "filing-...",
      "facts": [ { "text": "...", "source_ids": ["filing-..."],
                    "evidence_quote": "入力の text からの12〜300字の逐語引用" } ],
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

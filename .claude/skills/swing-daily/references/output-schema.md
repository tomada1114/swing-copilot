# analysis_input.json / analysis_result.json スキーマ

## 正本の所在

**最終的な正本は `src/swing_copilot/analysis/schemas.py`（pydantic）**。
本書は契約書レベルの要約であり、フィールド名の細部は Python 実装が決める。

**実行時に必ず `src/swing_copilot/analysis/schemas.py` を読んで最新のフィールド名・
必須／任意・Literal 値を確認してから JSON を組み立てること。** 本書と schemas.py が
食い違ったら schemas.py に従う。ingest 側は strict 検証（未知フィールド拒否）なので、
名前が 1 つ違うだけで hard fail する。

確定済みの事実（schemas.py 実装確認済み。以下は変更しない前提で読んでよい）:

- `SourcedFact` のフィールド名は `text`（旧 `llm/schemas.py` 時代の `statement` ではない）。
- `SourcedFact` は `evidence_quote`（正規化後 12〜300 字の逐語引用、`str | None`）を持つ。
  新規に書く fact は必ず値を入れる（省略・null は provenance 検査で fail-closed になる）。
  `VerdictReason` にはこのフィールドは無い。詳細は
  [analysis-conventions.md の AC6](analysis-conventions.md) を参照。
- 旧スキーマにあった `sentiment` / `sources` / `catalyst_quality` /
  `catalyst_quality_source_ids` / `period`（`NewsSummary`）、`filing_type` /
  `guidance_direction`（`FilingAnalysis`）は新契約に**存在しない**。意図的に廃止された
  フィールドであり、書いてはいけない。ingest は `extra="forbid"` の strict 検証で
  未知フィールドを拒否する。
- `FilingAnalysis` に `form_type` / `filed_at` は含めない。ingest が
  `analysis_input.json` から `source_id` で解決する（コード側が持つメタデータを
  スキルに正確な echo back させない設計）。

下の例は schemas.py と一致した完全形。ここに無いフィールドを追加しない。

## ファイル配置【固定】

- `analysis_input.json` / `analysis_result.json` / `report_context.json` は
  `reports/<run_date>/<run_id>/` に置く。この run 専用ディレクトリを以下
  `<WORKDIR>` と呼ぶ。Markdown は従来どおり`reports/<run_date>/<run_id>.md`に残る。
- `copilot-daily` は終了時に `analysis_input.json` の絶対パスをターミナルに出力する。
- 専門家サブエージェントの中間成果物は `<WORKDIR>/analysis_work/` に置く。
- `rejections.json` は落選銘柄の明細と candidate_limit 切り捨て銘柄の**診断記録**。
  読み取り専用で、`analysis_result.json` の入力にはしない（定性分析の対象は
  `analysis_input.json` の候補のみ）。

```text
<WORKDIR>/
├── analysis_input.json          ← copilot-daily が生成（読み取り専用）
├── analysis_work/               ← 専門家サブエージェントが生成（中間成果物）
│   ├── news-<SYMBOL>.json
│   ├── filings-<SYMBOL>.json
│   └── screening-<SYMBOL>.json
├── analysis_result.json         ← swing-daily が断片をマージして生成
├── rejections.json              ← copilot-daily が生成（読み取り専用・診断用）
└── report_context.json           ← copilot-daily が生成（読み取り専用）
```

## analysis_work 断片【命名・形式固定】

- ファイル名は `news-<SYMBOL>.json` / `filings-<SYMBOL>.json` /
  `screening-<SYMBOL>.json`。`<SYMBOL>` は入力の `symbol` をそのまま使う（大文字）。
- **1 ファイル = 1 銘柄 × 1 専門家。** 複数銘柄を 1 ファイルにまとめない。
- 断片は作業用アーティファクトであり、`analysis_result.json` に**そのまま入れない**。

```jsonc
{
  "run_id": "...",              // 入力の run_id を逐語コピー
  "as_of": "2026-07-27",        // 入力の as_of を逐語コピー
  "input_digest": "...",        // 入力の完全 SHA-256 を逐語コピー
  "symbol": "AAPL",
  "ac_check": "AC1-AC16 違反なし",   // または懸念のある AC 番号と一言
  "news_summary": { }           // 担当に応じて news_summary / filing_analyses /
                                //   screening_assessment のいずれか 1 キー
}
```

- `run_id` / `as_of` / `input_digest` / `ac_check` は**作業用メタデータ**。統括はマージ時にこれらを捨て、
  ペイロードキー（`news_summary` / `filing_analyses` / `screening_assessment`）
  だけを `analysis_result.json` に載せる。ingest は strict 検証（未知フィールド拒否）
  なので、混入すると hard fail する。
- 該当テキストが無い銘柄の断片は、ペイロードを `null`（news）/ `[]`（filings）
  にしたファイルを書く（＝「分析済みで空」と「未分析」を区別できるようにする）。
- 断片の契約の正本は `src/swing_copilot/analysis/fragment.py` の `AnalysisFragment`
  （strict schema）。`analysis_result.json` の `AnalysisResult` とは別物で、
  ペイロードキーは 3 つのうち**ちょうど 1 つ**でなければならない。

## 断片の契約検証【共有手段・自前実装しない】

断片を書き出したら、**自前の検証スクリプトを書かずに**次のコマンドを実行する。

```bash
uv run copilot-verify-analysis <WORKDIR>/analysis_work/news-AAPL.json
```

- **検査の実体は ingest と同一の関数**である。このコマンドは
  `analysis/validate.py` の `verify_symbol_analysis()` を呼ぶ。これは
  `copilot-ingest-analysis` が銘柄ごとに呼ぶのと同じ関数で、strict schema・
  provenance・`evidence_quote` の逐語一致・CON-03 を通す。したがって
  「ここで合格 ⇒ ingest でも合格」が成り立つ
- **grep や自作スクリプトで代用しない。** 逐語一致と CON-03 は Unicode NFKC 正規化・
  記号統一・空白畳み込み・大小無視を経て判定される。素の文字列検索ではこの正規化を
  再現できず、**ingest では落ちるものを「合格」と報告してしまう**
- 引数は複数指定でき、ディレクトリを渡すとその直下の `*.json` をすべて検査する。
  `<WORKDIR>/analysis_work` なら全断片、`<WORKDIR>` ならマージ後の
  `analysis_result.json`（`analysis_input.json` / `report_context.json` /
  `rejections.json` はコード所有なので自動的に対象外）
- `analysis_input.json` は対象ファイルの隣か 1 つ上の階層から自動解決する。
  別の場所にある場合だけ `--input <path>` を付ける
- 検査項目: 断片の strict schema（未知フィールド・ペイロードキーの数・
  `screening_assessment: null` を拒否）、`run_id` / `as_of` / `input_digest` が
  `analysis_input.json` と一致すること、ファイル名の `<kind>-<SYMBOL>` が
  ペイロードと一致すること、provenance、`evidence_quote`、CON-03
- 終了コード: `0` 全件合格 / `1` 契約違反あり / `2` パスや入力の解決に失敗
- このコマンドは読み取り専用である。ネットワークにも DB にも触れず、レポートも
  書かない。何度実行してもよい
- FAIL したら、**検査を通すために文言を書き換えるのではなく**内容を直す（AC15）

書き出し前の自己点検を省略してよいという意味ではない。ingest は fail-closed で
リトライされないため、違反を後から見つけてもその銘柄のその日の分析は消える。

## サブエージェント入力スライス【読み取り専用・作業用】

`analysis_input.json` は大きいので、専門家ごと・銘柄ごとに必要な範囲だけを
読み取り専用ファイルへ切り出して渡す。これはコンテキスト消費を抑えるための
**作業用の輸送形式**であり、`AnalysisInput` の JSON スキーマでも、成果物でもない。

**手で切り出さず、`uv run copilot-export-slices <analysis_input.json> --out-dir
<scratchpad>/slices` が生成する**（Issue #260）。ファイル名は
`slice-<kind>-<SYMBOL>.json`（`<kind>` は `news` / `filings` / `screening`）で、
`analysis_work/<kind>-<SYMBOL>.json` の断片と取り違えないよう `slice-` が付く。
置き場所は**セッションの scratchpad ディレクトリ配下**とし、`<WORKDIR>` 配下
（`analysis_work/` を含む）やリポジトリ配下には置かない。実行後に削除もしない
（SKILL.md「一時ファイルと後始末」を参照）。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // 元 input から逐語コピー
  "as_of": "2026-07-27",                            // 元 input から逐語コピー
  "input_digest": "<input の値を逐語コピー>",        // 元 input 全体の digest。slice の再計算値ではない
  "kind": "news",                                   // news | filings | screening
  "context": { "...": "担当分析に必要な run-wide context のみ" },
  "candidate": {
    "symbol": "AAPL",
    "...": "担当専門家に必要な元 candidates[] のフィールドだけ"
  }
}
```

- 元入力の `run_id` / `as_of` / `input_digest` は必ず含め、専門家は断片出力の同名 3 値へ
  逐語コピーする。統括は元の `analysis_input.json` と一致を確認する
- `source_id` と、その専門家が分析する `summary` / `text` は元入力から逐語コピーされる。
  担当対象の source object を要約・再採番・省略しない
- ニュース／開示スライスには担当銘柄の該当 source object だけが、スクリーニング
  スライスにはその銘柄の決定論的入力（`score_breakdown` / `risk_constraints` /
  `decision_history` / `prior_verdicts`）と run-wide context（`market_regime` /
  `performance_summary` / `calendar_events`）だけが入る。担当外の候補や長文テキストは
  入らない
- ニューススライスには `news_supply`（元入力にあれば）も逐語コピーされる。自社材料の
  供給量はニュース担当が申告する対象であり、落とすと申告経路が切れる
- スライスは `analysis_input.json` を置き換えない。digest は元入力全体に対する値なので、
  スライス単体で digest を再計算・検証しない
- スライスは strict スキーマ（`extra="forbid"`、`analysis/slices.py` の `InputSlice`）
  で検証されてから書かれ、同じ入力からは常にバイト同一になる（キー順・UTF-8・LF・
  末尾改行 1 個を固定し、時刻やパスなど実行環境依存の値を含めない）

## analysis_input.json（Python が生成、読み取り専用）

```jsonc
{
  "schema_version": "analysis-input-v3",
  "run_id": "11111111-2222-3333-4444-555555555555",
  "as_of": "2026-07-27",
  "strategy_key": "default",
  "input_digest": "<64 lowercase hexadecimal SHA-256 characters>",
  "generated_at": "...",
  "context": {
    "market_regime": "...",          // 整形済みテキストブロック or null
    "performance_summary": "...",    // 同上
    "calendar_events": [             // run単位のマクロ/経済カレンダーイベント。symbolを持たず、
                                      //   どの銘柄からも source_id 引用可（news/filings とは別集合）
      { "source_id": "fred:...", "published_at": "...", "title": "...",
        "summary": "...", "url": "...", "provider": "..." }
    ]
  },
  "candidates": [
    {
      "symbol": "AAPL",
      "score_breakdown": "...",      // 整形済みテキスト
      "risk_constraints": "...",
      "decision_history": "... or null",   // live 当日のみ
      "news": [
        { "source_id": "news-...", "published_at": "...", "headline": "...",
          "summary": "...", "url": "...", "provider": "..." }
      ],
      "news_supply": {                 // コードが数えた自社材料の供給量（Issue #130）
        "collected_items": 24,         // 収集件数（max_news_items で切る前）
        "exported_items": 20,          // news[] に載った件数
        "symbol_mention_items": 4,     // うち headline/summary にティッカーが現れる件数
        "level": "sparse"              // "sufficient" | "sparse" | "none"
      },
      "filings": [
        { "source_id": "filing-...", "form_type": "10-Q", "filed_at": "...",
          "text": "...", "url": "...",
          "coverage": {
            "original_chars": 180000,
            "exported_chars": 120000,
            "is_truncated": true,
            "selection_mode": "section_priority_partial",
            // 取得段（8-K Exhibit の文字数安全弁）で切られたか（Issue #157）。
            // is_truncated とは独立で、false は「マーカーが無い」であって
            // 「欠落が無い」ではない
            "exhibit_truncated": false,
            "sections": [
              { "name": "part_i_item_2", "status": "full",
                "original_chars": 38000, "exported_chars": 38000,
                "omission_shape": null },
              // partial は「章の中間 2,110 字が落ちている」と読む
              { "name": "part_ii_item_1a", "status": "partial",
                "original_chars": 20500, "exported_chars": 18390,
                "omission_shape": "head_and_tail" },
              // 同じPart(II)の他章(part_ii_item_1a)がparsedされているので
              // absent_from_filing（この章自体は提出書類に無い可能性が高い）
              { "name": "part_ii_item_1", "status": "absent_from_filing",
                "original_chars": null, "exported_chars": null,
                "omission_shape": null }
            ]
          } }
      ]
    }
  ]
}
```

- `source_id` というキー名は【固定】。
- 新規runは`analysis-input-v3`で、全filingにコード所有の`coverage`が必須。
  `analysis-input-v2`は過去のP8アーカイブ読み込みに限り互換対応し、新規生成しない。
- `sections[].status`は`"full"` / `"partial"` / `"absent_from_filing"` /
  `"not_parsed"` / `"missing"`の5値。新規runが出すのは前4つのみで、`"missing"`は
  過去アーカイブ読み込み専用（新規生成物には出ない）。`absent_from_filing`は
  同じPartの他章がparsedされているのにこの章だけ見つからない場合
  （章自体が提出書類に無い可能性が高い。10-Qは前回提出から重要な変更が無ければ
  Item 1A等を省略できる）、`not_parsed`は同じPart自体の構造をパーサが取れず
  この章の有無が判定できない場合。
- 8-Kの`sections[]`は章ではなくExhibit単位で、`exhibit_primary`（主文書）/
  `exhibit_ex_99_1`（プレスリリース）/ `exhibit_ex_99_2`…（補足資料）という名前を
  取る（Issue #181）。`selection_mode`は`section_priority_partial`のまま
  （値は増やしていない）。
- `sections[]`の`original_chars` / `exported_chars` / `omission_shape`は**任意**
  （スキーマは`analysis-input-v3`のまま）。`omission_shape`は残した形を表し、
  `head_and_tail`＝章の中間が欠落、`head_only`＝先頭スライスのみで以降が欠落、
  `value_selected`＝価値の低い段落から落としたので欠落位置は本文中の
  `[... omitted lower-value exhibit passage ...]`マーカーの位置。
  `status: "partial"`以外には付かない。3値が`null`のときは「未記録」であって
  「欠落なし」ではない（フィールド追加前のアーカイブと、P8がDB行から復元した
  coverageが該当する）。欠落量は`original_chars - exported_chars`で読む。
- `news_supply` は**任意**（スキーマは`analysis-input-v3`のまま）。新規runは常に出すが、
  フィールド追加前のアーカイブには無い。`symbol_mention_items`はティッカー表記だけで
  数えた**下限値**で、社名しか書かれていない自社記事は数え落とす。`level`が
  `sparse` / `none`のとき、およびフィールドが無い旧アーカイブを読むときは、
  **「悪材料が見当たらない」を根拠に使わない**（判断材料の不在であって好材料ではない）。
  ニュース担当は該当時に`risk_flags`の先頭へ`材料供給不足:`で始まる申告を置く
  （`.claude/skills/analyze-news/SKILL.md`）。
- news/filings が空の候補も `candidates` に含まれる（screening 評価は行うため）。
- `candidates[].symbol` は文書内で一意、各候補の news と filings を合わせた
  `source_id` も一意にする。重複は strict schema の parse failure になる。

## analysis_result.json（スキルが生成、ingest が検証）

```jsonc
{
  "schema_version": "analysis-result-v3",
  "run_id": "11111111-2222-3333-4444-555555555555", // input を逐語コピー
  "as_of": "2026-07-27",             // input と一致必須（不一致は hard fail）
  "strategy_key": "default",          // input を逐語コピー
  "input_digest": "<input の値を逐語コピー>",
  "generated_by": "swing-daily skill",
  "symbols": [
    {
      "symbol": "AAPL",
      "news_summary": {              // 該当ニュースが無ければ null
        "facts": [ { "text": "...", "source_ids": ["news-..."],
                      "evidence_quote": "headline か summary からの12〜300字の逐語引用" } ],
        "interpretation": ["..."],
        "risk_flags": ["..."]
      },
      "filing_analyses": [           // 該当開示が無ければ []
        {
          "source_id": "filing-...",
          "facts": [ { "text": "...", "source_ids": ["filing-..."],
                        "evidence_quote": "入力の text からの12〜300字の逐語引用" } ],
          "interpretation": ["..."],
          "red_flags": ["..."],
          "yoy_changes": ["..."]
        }
      ],
      "screening_assessment": {      // 全銘柄必須
        "summary": "...",
        "strengths": ["..."],
        "concerns": ["..."]
      },
      "verdict": {                   // 全銘柄必須
        "recommendation": "proceed",  // "proceed" | "skip" の 2 値【固定】
        "reasons": [ { "text": "...", "source_ids": ["news-..."],
                       "basis": "news_catalyst" } ]
      }
    }
  ],
  "no_trade": false,
  "no_trade_reason": null
}
```

### 記入ルール

- `news_summary` / `filing_analyses` は該当テキストが無ければ `null` / `[]`。
- `run_id`、`as_of`、`strategy_key`、`input_digest`は`analysis_input.json`から逐語コピーする。
  `input_digest`は入力本体の canonical JSON（キーソート、UTF-8、安定した日時表現）から
  Python が計算した完全 SHA-256 であり、短縮・再計算・書き換えをしない。
- `screening_assessment` と `verdict` は **全銘柄必須**。
- `symbols[].symbol` は重複不可で、`analysis_input.json` の `candidates[].symbol` と
  **入力と完全一致**させる。入力にある銘柄を落としたり、入力外銘柄を追加したりしない。
- `facts[].source_ids` は **非空**、かつ入力の該当銘柄の `source_id` 集合
  （＋ `context.calendar_events` の ID。これは全銘柄共通で引用可）の部分集合。
- `facts[].evidence_quote` は **必須**（正規化後 12〜300 字）。その fact が引用する
  `source_ids` のいずれかの本文（ニュースは `headline` ＋ `summary`、開示は入力の
  `text`、カレンダーイベントは `title` ＋ `summary`）からの逐語引用でなければ、
  ingest の provenance 検査に落ちる。`verdict.reasons` にはこのフィールドは無い。
- `verdict.reasons[].source_ids`: ニュース／開示／`context.calendar_events`に基づく
  理由は該当 `source_id` を必ず引用。スコア等の決定論的入力のみに基づく理由は空リスト可。
- `verdict.reasons[].basis`: その理由がどの**種類**の根拠に立っているかを表す
  閉集合タグ。次の 6 値のいずれか、または省略（`null`）。
  - `technical_score` — `score_breakdown` の複合スコア・加重内訳・生値
    （RSI14 / SMA50 / SMA200 / ATR14 比率 / 終値 / 平均出来高）に基づく理由
  - `news_catalyst` — ニュース記事が報じた材料に基づく理由
  - `filing_fundamental` — 開示（10-Q / 10-K / 8-K）の内容に基づく理由
  - `risk_sizing` — `risk_constraints` の binding_constraint・株数・warnings に
    基づく理由
  - `market_regime` — `context.market_regime` のゲート・分配日・エクスポージャ
    上限に基づく理由
  - `peer_relative` — 同業他社・セクター全体との相対比較に基づく理由
  1 つの理由が複数種類にまたがるなら、**その理由を分割して 1 つずつ書く**。
  迷ったら省略してよい（`untagged` として集計される）が、根拠が明確なものは
  必ず付ける: このタグだけが「決算根拠の proceed とテクニカルのみ根拠の proceed の
  どちらが当たっているか」を後から測れる唯一の手掛かりであり、ingest 側は
  正しさを検証できない（＝誤ったタグは provenance 検査に掛からず、集計だけを
  歪める）。
- `no_trade=true` のときだけ、非空白の `no_trade_reason` に理由を書く（CON-03 検査対象）。
  `no_trade=false` のときは `no_trade_reason` を必ず `null` にする。

## ingest の検証規則【固定】

1. `schema_version` が `analysis-result-v3` であることを検証し、それ以外
   （`analysis-result-v2` を含む）は run 全体を hard fail とする。P8 の retro
   collect パスに限り、アーカイブ済みの `analysis-result-v2` を読み取り専用で
   解釈できる。
2. 3文書の strict schema と digest を検証し、`run_id`、`as_of`、`strategy_key`、
   `input_digest`が完全一致しなければ hard fail。report と `latest.md` は書き換えない。
3. provenance 検証: 全 `source_ids` が入力の該当銘柄の `source_id`、または
   `context.calendar_events` の `source_id`（全銘柄共通で引用可）の部分集合。
   `facts` の `source_ids` は非空。
4. evidence_quote 検証: `facts[].evidence_quote` が非空で、正規化後 12〜300 字の
   範囲にあり、その fact の `source_ids` のいずれかの本文（ニュースは
   `headline` ＋ `summary`、開示は入力の `text`、カレンダーイベントは `title` ＋
   `summary`）に、Unicode NFKC 正規化・全角/半角記号統一・空白畳み込み・大小無視の
   うえで実在すること。正しい `source_id` を申告しつつ別銘柄の本文から書いた
   fact は、ここで検出される。
5. 数値整合の**警告**（fail-closed ではない）: `facts[].text` と `evidence_quote` の
   双方に単位・通貨の付いた数値がある fact に限り、10 のべき乗（千 / 百万 /
   billion / million / 億 / 万）を跨いで text 側の数値が quote 側の数値へ到達できるかを
   照合する。到達できない数値はログに警告として出るが、当該銘柄は縮退させずそのまま
   描画する（誤検知で分析を落とさないため）。年号・四半期・比率・株数のような単位の
   付かない数値は対象外で、検算責任は分析側（AC16）にある。
6. CON-03 機械検査を、Unicode NFKC 正規化後のユーザー表示テキスト全フィールドに適用
   （`facts[].text`, `interpretation`, `risk_flags`, `red_flags`, `yoy_changes`,
   `screening_assessment.*`, `verdict.reasons[].text`, `no_trade_reason`）。
   売買動詞と命令形・義務表現の組み合わせを禁止し、引用・否定を含む場合も安全側で
   検査対象にする。
7. 違反（3・4・6）は **銘柄単位の fail-closed**。当該銘柄の定性セクションを縮退表示し、
   リトライはしない。5 の警告は縮退させない。
8. result の symbol 集合が input と完全一致しなければ run 全体を hard fail とする。
   部分結果・重複・不足・入力外銘柄を縮退表示で受け入れない。
9. レポートがリンクにする URL は input 側の `http` / `https` だけ。不正・空 URL は
   事実本文を表示しても source attribution を付けない。
10. ingest はネットワークアクセスもスクリーニング再実行もしない。

`copilot-verify-analysis <analysis_result.json>` は上記のうち 1〜4・6〜8 を
レポートを書かずに実行する **ingest の dry-run** である（`report_context.json`
との照合だけは対象外。あれは `copilot-daily` が同じ run で書くコード所有の
ファイルであり、スキルが取り違えうるのは result 側だから）。ingest の代わりには
ならないが、ingest を走らせる前に同じ判定を得られる。

## レポート表示（ingest 側の責務、参考）

- 銘柄ごとに verdict を併記: `⚠ 定性: 見送り推奨（理由要約）` / `✓ 定性: 懸念なし`。
- スクリーニングの決定論的結果（スコア等）は一切書き換えない。
- `no_trade=true` なら冒頭に「本日は取引なし（定性判断）」を明示。
- 最終判断は人間である旨の既存文言・CON-03 準拠は維持される。

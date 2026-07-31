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

```text
<WORKDIR>/
├── analysis_input.json          ← copilot-daily が生成（読み取り専用）
├── analysis_work/               ← 専門家サブエージェントが生成（中間成果物）
│   ├── news-<SYMBOL>.json
│   ├── filings-<SYMBOL>.json
│   └── screening-<SYMBOL>.json
├── analysis_result.json         ← swing-daily が断片をマージして生成
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
  "ac_check": "AC1-AC15 違反なし",   // または懸念のある AC 番号と一言
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

## サブエージェント入力スライス【読み取り専用・作業用】

`analysis_input.json` が大きい場合、統括は専門家ごと・銘柄ごとに必要な範囲だけを
読み取り専用ファイルへ切り出して渡す。これはコンテキスト消費を抑えるための
**作業用の輸送形式**であり、`AnalysisInput` の JSON スキーマでも、成果物でもない。
`<WORKDIR>/analysis_work/` には置かない。

```jsonc
{
  "run_id": "11111111-2222-3333-4444-555555555555", // 元 input から逐語コピー
  "as_of": "2026-07-27",                            // 元 input から逐語コピー
  "input_digest": "<input の値を逐語コピー>",        // 元 input 全体の digest。slice の再計算値ではない
  "context": { "...": "担当分析に必要な run-wide context のみ" },
  "candidate": {
    "symbol": "AAPL",
    "...": "担当専門家に必要な元 candidates[] のフィールドだけ"
  }
}
```

- 元入力の `run_id` / `as_of` / `input_digest` は必ず含め、専門家は断片出力の同名 3 値へ
  逐語コピーする。統括は元の `analysis_input.json` と一致を確認する
- `source_id` と、その専門家が分析する `summary` / `text` は元入力から逐語コピーする。
  担当対象の source object を要約・再採番・省略しない
- ニュース／開示スライスには担当銘柄の該当 source object だけを、スクリーニング
  スライスにはその銘柄の決定論的入力と必要な run-wide context だけを入れる。担当外の
  候補や長文テキストを入れない
- スライスは `analysis_input.json` を置き換えない。digest は元入力全体に対する値なので、
  スライス単体で digest を再計算・検証しない

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
      "filings": [
        { "source_id": "filing-...", "form_type": "10-Q", "filed_at": "...",
          "text": "...", "url": "...",
          "coverage": {
            "original_chars": 180000,
            "exported_chars": 120000,
            "is_truncated": true,
            "selection_mode": "section_priority_partial",
            "sections": [
              { "name": "part_i_item_2", "status": "full",
                "original_chars": 38000, "exported_chars": 38000,
                "omission_shape": null },
              // partial は「章の中間 2,110 字が落ちている」と読む
              { "name": "part_ii_item_1a", "status": "partial",
                "original_chars": 20500, "exported_chars": 18390,
                "omission_shape": "head_and_tail" },
              { "name": "part_ii_item_1", "status": "missing",
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
- `sections[]`の`original_chars` / `exported_chars` / `omission_shape`は**任意**
  （スキーマは`analysis-input-v3`のまま）。`omission_shape`は残した形を表し、
  `head_and_tail`＝章の中間が欠落、`head_only`＝先頭スライスのみで以降が欠落。
  `status: "full"`と`"missing"`には付かない。3値が`null`のときは「未記録」であって
  「欠落なし」ではない（フィールド追加前のアーカイブと、P8がDB行から復元した
  coverageが該当する）。欠落量は`original_chars - exported_chars`で読む。
- news/filings が空の候補も `candidates` に含まれる（screening 評価は行うため）。
- `candidates[].symbol` は文書内で一意、各候補の news と filings を合わせた
  `source_id` も一意にする。重複は strict schema の parse failure になる。

## analysis_result.json（スキルが生成、ingest が検証）

```jsonc
{
  "schema_version": "analysis-result-v2",
  "run_id": "11111111-2222-3333-4444-555555555555", // input を逐語コピー
  "as_of": "2026-07-27",             // input と一致必須（不一致は hard fail）
  "strategy_key": "default",          // input を逐語コピー
  "input_digest": "<input の値を逐語コピー>",
  "generated_by": "swing-daily skill",
  "symbols": [
    {
      "symbol": "AAPL",
      "news_summary": {              // 該当ニュースが無ければ null
        "facts": [ { "text": "...", "source_ids": ["news-..."] } ],
        "interpretation": ["..."],
        "risk_flags": ["..."]
      },
      "filing_analyses": [           // 該当開示が無ければ []
        {
          "source_id": "filing-...",
          "facts": [ { "text": "...", "source_ids": ["filing-..."] } ],
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
        "reasons": [ { "text": "...", "source_ids": ["news-..."] } ]
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
- `verdict.reasons[].source_ids`: ニュース／開示／`context.calendar_events`に基づく
  理由は該当 `source_id` を必ず引用。スコア等の決定論的入力のみに基づく理由は空リスト可。
- `no_trade=true` のときだけ、非空白の `no_trade_reason` に理由を書く（CON-03 検査対象）。
  `no_trade=false` のときは `no_trade_reason` を必ず `null` にする。

## ingest の検証規則【固定】

1. 3文書の strict schema と digest を検証し、`run_id`、`as_of`、`strategy_key`、
   `input_digest`が完全一致しなければ hard fail。report と `latest.md` は書き換えない。
2. provenance 検証: 全 `source_ids` が入力の該当銘柄の `source_id`、または
   `context.calendar_events` の `source_id`（全銘柄共通で引用可）の部分集合。
   `facts` の `source_ids` は非空。
3. CON-03 機械検査を、Unicode NFKC 正規化後のユーザー表示テキスト全フィールドに適用
   （`facts[].text`, `interpretation`, `risk_flags`, `red_flags`, `yoy_changes`,
   `screening_assessment.*`, `verdict.reasons[].text`, `no_trade_reason`）。
   売買動詞と命令形・義務表現の組み合わせを禁止し、引用・否定を含む場合も安全側で
   検査対象にする。
4. 違反は **銘柄単位の fail-closed**。当該銘柄の定性セクションを縮退表示し、
   リトライはしない。
5. result の symbol 集合が input と完全一致しなければ run 全体を hard fail とする。
   部分結果・重複・不足・入力外銘柄を縮退表示で受け入れない。
6. レポートがリンクにする URL は input 側の `http` / `https` だけ。不正・空 URL は
   事実本文を表示しても source attribution を付けない。
7. ingest はネットワークアクセスもスクリーニング再実行もしない。

## レポート表示（ingest 側の責務、参考）

- 銘柄ごとに verdict を併記: `⚠ 定性: 見送り推奨（理由要約）` / `✓ 定性: 懸念なし`。
- スクリーニングの決定論的結果（スコア等）は一切書き換えない。
- `no_trade=true` なら冒頭に「本日は取引なし（定性判断）」を明示。
- 最終判断は人間である旨の既存文言・CON-03 準拠は維持される。

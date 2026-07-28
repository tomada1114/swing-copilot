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

- `analysis_input.json` / `analysis_result.json` は当日のレポート出力先
  ディレクトリ（Markdown レポートと同じ場所）に置く。以下このディレクトリを
  `<WORKDIR>` と呼ぶ。
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
└── <当日の Markdown レポート>    ← copilot-ingest-analysis が再描画
```

## analysis_work 断片【命名・形式固定】

- ファイル名は `news-<SYMBOL>.json` / `filings-<SYMBOL>.json` /
  `screening-<SYMBOL>.json`。`<SYMBOL>` は入力の `symbol` をそのまま使う（大文字）。
- **1 ファイル = 1 銘柄 × 1 専門家。** 複数銘柄を 1 ファイルにまとめない。
- 断片は作業用アーティファクトであり、`analysis_result.json` に**そのまま入れない**。

```jsonc
{
  "as_of": "2026-07-27",        // 入力の as_of をそのままコピー（再入判定に使う）
  "symbol": "AAPL",
  "ac_check": "AC1-AC15 違反なし",   // または懸念のある AC 番号と一言
  "news_summary": { }           // 担当に応じて news_summary / filing_analyses /
                                //   screening_assessment のいずれか 1 キー
}
```

- `as_of` / `ac_check` は**作業用メタデータ**。統括はマージ時にこれらを捨て、
  ペイロードキー（`news_summary` / `filing_analyses` / `screening_assessment`）
  だけを `analysis_result.json` に載せる。ingest は strict 検証（未知フィールド拒否）
  なので、混入すると hard fail する。
- 該当テキストが無い銘柄の断片は、ペイロードを `null`（news）/ `[]`（filings）
  にしたファイルを書く（＝「分析済みで空」と「未分析」を区別できるようにする）。

## analysis_input.json（Python が生成、読み取り専用）

```jsonc
{
  "schema_version": "analysis-input-v1",
  "as_of": "2026-07-27",
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
          "text": "...", "url": "..." }
      ]
    }
  ]
}
```

- `source_id` というキー名は【固定】。
- news/filings が空の候補も `candidates` に含まれる（screening 評価は行うため）。

## analysis_result.json（スキルが生成、ingest が検証）

```jsonc
{
  "schema_version": "analysis-result-v1",
  "as_of": "2026-07-27",             // input と一致必須（不一致は hard fail）
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
- `screening_assessment` と `verdict` は **全銘柄必須**。
- `facts[].source_ids` は **非空**、かつ入力の該当銘柄の `source_id` 集合
  （＋ `context.calendar_events` の ID。これは全銘柄共通で引用可）の部分集合。
- `verdict.reasons[].source_ids`: ニュース／開示／`context.calendar_events`に基づく
  理由は該当 `source_id` を必ず引用。スコア等の決定論的入力のみに基づく理由は空リスト可。
- `no_trade` は全銘柄 skip などの場合に統括が `true` にできる。`true` なら
  `no_trade_reason` に理由を書く（CON-03 検査対象）。

## ingest の検証規則【固定】

1. スキーマ strict 検証（未知フィールド拒否）。壊れた JSON / `as_of` 不一致は hard fail。
2. provenance 検証: 全 `source_ids` が入力の該当銘柄の `source_id`、または
   `context.calendar_events` の `source_id`（全銘柄共通で引用可）の部分集合。
   `facts` の `source_ids` は非空。
3. CON-03 機械検査を、ユーザー表示される全テキストフィールドに適用
   （`facts[].text`, `interpretation`, `risk_flags`, `red_flags`, `yoy_changes`,
   `screening_assessment.*`, `verdict.reasons[].text`, `no_trade_reason`）。
4. 違反は **銘柄単位の fail-closed**。当該銘柄の定性セクションを縮退表示し、
   リトライはしない。
5. input に無い symbol が result にあれば当該 symbol は error 扱い。
   input にあって result に無い symbol は「分析なし」として縮退表示。
6. ingest はネットワークアクセスもスクリーニング再実行もしない。

## レポート表示（ingest 側の責務、参考）

- 銘柄ごとに verdict を併記: `⚠ 定性: 見送り推奨（理由要約）` / `✓ 定性: 懸念なし`。
- スクリーニングの決定論的結果（スコア等）は一切書き換えない。
- `no_trade=true` なら冒頭に「本日は取引なし（定性判断）」を明示。
- 最終判断は人間である旨の既存文言・CON-03 準拠は維持される。

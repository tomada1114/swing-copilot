# 03. 基本設計書（swing-copilot）

## 1. 文書情報

| 項目 | 内容 |
|---|---|
| システム名（仮称） | swing-copilot |
| 対象範囲 | 米国株スイング〜ポジショントレードの意思決定支援システム（日次バッチ）。売買判断・発注は人間が行う。完全自動売買・証券会社API連携はスコープ外（CON-01）。 |
| 読者 | 本システムの詳細設計・実装を行う開発者・実装エージェント（Claude Codeの `/goal` による自律実装を含む） |
| 前提文書 | `docs/00_human_preparation.md`（人間の下準備）、`docs/01_requirements.md`（要件定義書、FR/NFR/CONのID定義元） |
| 後続文書 | `docs/04_detailed_design.md`（詳細設計書。本書のコンポーネント設計をモジュール・クラス・スキーマレベルまで具体化する） |
| バージョン | v1.2 |
| 最終更新日 | 2026-07-22 |

本書は要件定義書（`docs/01_requirements.md`）で定義された要件ID（FR-01〜FR-12、NFR-01〜NFR-07、CON-01〜CON-04）を前提とし、それらを満たすシステム構成・データフロー・データストア・外部インターフェース・エラー処理方針・運用設計・セキュリティ設計を定める。要件そのものの再定義は行わない。

文書の役割は、要件・制約=`docs/01_requirements.md`、アーキテクチャ=`docs/03_basic_design.md`、実装契約=`docs/04_detailed_design.md`、現在のデータ/API形状=`models.py`/`storage/schema.py`/公開シグネチャ、実行状況=Git・テスト・CIとする。`docs/goal-prompts/**`は特定の無人実行を支援する履歴資料であり、恒久設計の正本ではない。正本同士が矛盾した場合は暗黙に一方を選ばず、互換性を保ちながら差異を記録し、古い側を同じ変更で更新する。

---

## 2. システム全体構成

swing-copilotは、利用者がローカルマシンで手動実行するコマンドを起点に、価格・ファンダメンタルズ・テキスト情報を収集し、スクリーニング・リスクチェック・LLM分析を経てCLI日次ブリーフと監査用Markdownを生成する、単一プロセスのバッチパイプラインである。Discord通知はオプションであり、自動発注は行わない。

採用するアーキテクチャパターンは次の通り。

- **モジュラーモノリス**: 配布・実行単位は1つのPythonパッケージ/CLIとする。機能別モジュール境界は保つが、プロセス分割・メッセージブローカー・常駐サービスは導入しない。
- **Functional Core / Imperative Shell**: 指標、Filter、Signal、候補順位、リスク計算、バックテスト約定規則は、`as_of`と入力データを受け取る決定的な純粋ロジックに置く。`pipeline/daily.py`は外部API・clock・ストア・通知を調停する薄いimperative shellとする。
- **Ports & Adapters（境界限定）**: 変更可能性または障害可能性が高い`DataProvider`、テキスト取得、LLM、Notifier、clockだけをProtocolで抽象化する。内部モジュール同士を機械的にinterface化しない。
- **Repository + step Unit of Work**: `MarketStore`/`StateStore`は同じDuckDBを使う論理repositoryとし、日次バッチの各ステップをトランザクション境界にする。Parquetを跨ぐ処理は自然キーupsertと原子的renameで再実行可能にする。
- **明示的composition root**: CLI起動時に設定から具体adapterを組み立てて注入する。import副作用による自動検出やentry-point plugin探索はP1〜P2では行わない。

この構成は、1人保守・ローカル手動実行というNFR-02に対して、外部サービス差し替えとテスト容易性だけを確保し、分散システムの運用負荷を持ち込まないための選択である。

```mermaid
flowchart TD
    subgraph External["外部サービス"]
        YF["株価API<br/>(yfinance試作 / EODHD本番)"]
        EDGARAPI["SEC EDGAR API"]
        FH["Finnhub API<br/>(company-news)"]
        FRED["FRED API<br/>(経済カレンダー)"]
        CLAUDE["Claude API<br/>(モデルはsettings.yamlで選択)"]
        DISCORD["Discord Webhook"]
    end

    subgraph Batch["日次バッチ: src/swing_copilot/pipeline/daily.py"]
        UNIV["universe.py<br/>FR-01"]
        DP["data/*_provider.py<br/>DataProvider (FR-02)"]
        EDG["data/edgar.py<br/>FR-03"]
        SCR["screening/pipeline.py<br/>Filter+Signal (FR-04, FR-05)"]
        REG["regime/*<br/>市場ゲート・DD（P3-13）"]
        RISK["risk/checks.py<br/>FR-06"]
        TXT["text/*<br/>FR-07"]
        LLM["llm/*<br/>FR-08"]
        BRIEF["report/daily_brief.py<br/>FR-09"]
        OUT["terminal_report.py / markdown_report.py"]
        NOTIFY["report/discord_notify.py<br/>FR-09"]
    end

    subgraph Offline["オフライン支援機能"]
        BT["backtest/*<br/>FR-10"]
        PAPER["paper/journal.py<br/>FR-11"]
    end

    subgraph Storage["データストア（2層）"]
        PARQUET[("Parquet<br/>bars/ (日足時系列)")]
        DUCKDB[("DuckDB<br/>分析 + 実行状態 + 監査ログ")]
    end

    UNIV --> DP
    YF --> DP
    DP --> PARQUET
    PARQUET --> DUCKDB
    EDGARAPI --> EDG
    EDG --> DUCKDB
    DUCKDB --> SCR
    SCR --> DUCKDB
    SCR --> RISK
    DP --> REG
    REG --> DUCKDB
    REG --> BRIEF
    RISK --> DUCKDB
    FH --> TXT
    EDGARAPI --> TXT
    FRED --> TXT
    TXT --> LLM
    CLAUDE --> LLM
    LLM --> DUCKDB
    SCR --> BRIEF
    RISK --> BRIEF
    LLM --> BRIEF
    DUCKDB --> BRIEF
    BRIEF --> OUT
    OUT --> STDOUT["stdout"]
    OUT --> MDOUT[("reports/<date>/<run_id>.md")]
    BRIEF --> NOTIFY
    NOTIFY --> DISCORD

    DUCKDB -.-> BT
    BT -.-> DUCKDB
    DUCKDB -.-> PAPER
    PAPER -.-> DUCKDB
```

実行環境はローカルマシンのみであり、利用者が1日1回`uv run copilot-daily`を手動実行する。`data/`（Parquet/DuckDB）と`reports/`（生成Markdown）はローカルへ永続化する。判断は`uv run copilot-decision`で明示的に記録する。記録済みのrun・候補・落選・判断・実績を後から閲覧する読み出し専用CLIとして`uv run copilot-history`がある（roadmap P1-05、`docs/04_detailed_design.md` 3.22節）。

---

## 3. コンポーネント一覧（責務・対応FR）

| コンポーネント | 主な配置 | 責務 | 対応FR/NFR |
|---|---|---|---|
| Universe管理 | `universe.py` | S&P500構成銘柄リストの取得・保存・更新 | FR-01 |
| DataProvider | `data/base.py`, `data/yfinance_provider.py`, `data/eodhd_provider.py` | 日足株価の取得を抽象化し、試作(yfinance)・本番(EODHD)を差し替え可能にする | FR-02, NFR-07, CON-02 |
| EDGARクライアント | `data/edgar.py` | SEC EDGAR公式APIから財務諸表・ファンダメンタルズを取得 | FR-03 |
| Database | `storage/database.py` | 単一DuckDB接続・スキーマ初期化・トランザクション境界 | NFR-02, NFR-05 |
| MarketStore | `storage/market_store.py` | 日足時系列（Parquet）・ファンダメンタルズ・分析クエリ（DuckDB）の読み書き | FR-02, FR-03 |
| StateStore | `storage/state_store.py` | 実行、候補、リスク評価、ポジション、判断、LLM入出力ログを同じDuckDBへ保存 | FR-11, NFR-05 |
| Filter/Signal基盤 | `screening/base.py` | フィルタ・シグナルのABCとプラガブルな登録レジストリ | FR-05, NFR-07 |
| ファンダフィルタ | `screening/fundamental_filters.py` | 第1段: 黒字継続・FCF・自己資本比率によるユニバース絞り込み | FR-04 |
| テクニカルシグナル | `screening/technical_signals.py` | 第2段: pandasで算出するトレンド・押し目シグナル評価 | FR-05 |
| ScreeningPipeline | `screening/pipeline.py` | `strategies.yaml`に従いフィルタ・シグナルをAND合成し、決定的に順位付けした候補を出力 | FR-04, FR-05, NFR-07 |
| 市場レジーム | `regime/gate.py`, `regime/distribution.py` | SPY/QQQ/^VIXの`as_of`までのOHLCVから市場ゲートとDistribution Dayを決定論的に算出し、データ不足時はUNKNOWNへ安全側に倒す | P3-13 |
| RiskChecker | `risk/` | ポジションサイズ・セクター集中度・銘柄間相関等のリスクチェック。Exposure CeilingがCASH_PRIORITYなら新規株数を0、REDUCE_ONLYなら取引リスク枠を縮小する | FR-06, P3-14 |
| テキスト収集 | `text/` | ニュース（Finnhub）・適時開示（EDGAR 8-K/10-Q）・経済カレンダー（FRED）の収集 | FR-07 |
| LLMClient | `llm/client.py` | Claude API呼び出しの共通ラッパー（リトライ・コスト記録） | FR-08, NFR-05, NFR-06 |
| LLM分析（要約） | `llm/summarize.py` | LLMによるニュース要約（事実/推測分離、コード計算済みの市場レジームは信頼済みsystemフィールドへ分離して注入、使用モデルは`settings.yaml`の`llm.models.news_summary`で設定、デフォルトHaiku） | FR-08, P3-15 |
| LLM分析（決算解釈） | `llm/filings_analysis.py` | LLMによる決算書解釈（事実/推測分離、使用モデルは`settings.yaml`の`llm.models.filing_analysis`で設定、デフォルトHaiku。精度重視の場合はSonnet等へ設定変更可） | FR-08 |
| 日次ブリーフ構築 | `report/daily_brief.py` | 市場・候補・リスク・LLM結果を表示非依存の値へ集約 | FR-09 |
| CLI/Markdown出力 | `report/terminal_report.py`, `report/markdown_report.py` | stdout表示とrun ID単位の原子的Markdown保存 | FR-09, NFR-05 |
| Discord通知 | `report/discord_notify.py` | Discord Webhookへの通知送信（オプション機能、デフォルト無効） | FR-09 |
| バックテスト | `backtest/` | 日次ロジックを再利用する複数銘柄ポートフォリオシミュレータ、SPY買い持ちとの比較 | FR-10 |
| ペーパートレード記帳 | `paper/journal.py` | 人間の判断（追随/見送り/修正）と仮想約定の記録 | FR-11, CON-04 |
| 判断記録CLI | `paper/cli.py` | 候補検証、判断upsert、Markdown判断欄の再生成 | FR-11 |
| 日次オーケストレータ | `pipeline/daily.py` | 全ステップの実行順制御・冪等性・フェイルソフト | FR-12, NFR-04 |
| 設定ロード | `config.py` | `settings.yaml`/`strategies.yaml`/環境変数の統合ロード | NFR-06 |

---

## 4. データフロー（日次バッチのシーケンス）

`pipeline/daily.py` は以下の8ステップを固定順で実行する。起動時に一意な`run_id`を発行し、`run_date`は取得済み日足の最新取引日または明示された`--as-of`から決める。ステップ(5)テキスト収集・(6)LLM分析が失敗しても、ステップ(8)はスクリーニング結果のみの縮退版を出力する。同じ`run_date`の再実行でも業務データを重複させず、実行履歴は別`run_id`で残す。

```mermaid
sequenceDiagram
    participant Local as ローカルマシン（手動実行）
    participant D as pipeline/daily.py
    participant DP as DataProvider
    participant EDG as data/edgar.py
    participant SCR as ScreeningPipeline
    participant RC as RiskChecker
    participant TXT as text/*
    participant LLM as llm/*
    participant OUT as report/daily_brief + renderers
    participant DC as report/discord_notify.py
    participant MS as MarketStore(DuckDB+Parquet)
    participant ST as StateStore(DuckDB)

    Local->>D: uv run copilot-daily
    D->>ST: runs初期化（run_id, run_date, config_hash）

    D->>DP: (1) 株価更新: get_daily_bars(universe)
    DP-->>MS: write_bars()（既取得日はスキップ）
    D->>ST: run_steps(step=1, status)

    D->>EDG: (2) ファンダ更新（週1回）
    EDG-->>MS: upsert fundamentals
    D->>ST: run_steps(step=2, status)

    D->>SCR: (3) スクリーニング（Filter→Signal）
    SCR-->>ST: signals テーブルへ記録
    SCR-->>ST: candidates/screening_rejections を同一トランザクションで記録（P1-02）
    D->>ST: run_steps(step=3, status)

    D->>RC: (4) リスクチェック
    RC-->>ST: RiskAssessment 記録
    D->>ST: run_steps(step=4, status)

    D->>MS: レジーム算出（SPY/QQQ/^VIX、date <= as_of）
    D->>ST: regime_snapshotsへ補正upsert

    D->>TXT: (5) テキスト収集（news/filings/calendar）
    alt 失敗
        TXT-->>D: 例外を捕捉
        D->>ST: run_steps(step=5, status=failed, detail)
    else 成功
        D->>ST: run_steps(step=5, status=success)
    end

    D->>LLM: (6) LLM分析（ニュース要約 + 決算解釈、モデルはsettings.yamlで設定）
    alt 失敗 or (5)が失敗
        LLM-->>D: 例外を捕捉 or スキップ
        D->>ST: run_steps(step=6, status=failed/skipped, detail)
    else 成功
        LLM-->>ST: llm_calls テーブルへ記録
        D->>ST: run_steps(step=6, status=success)
    end

    D->>DC: (7) Discord通知（notification.enabled=trueの場合のみ）
    D->>ST: run_steps(step=7, status)

    D->>OUT: (8) DailyBrief構築、Markdown原子保存、stdout表示
    OUT-->>Local: CLI日次ブリーフ
    D->>ST: run_steps(step=8, status)
```

---

## 5. データストア設計（2層の使い分け）

swing-copilotは目的別に2層のデータストアを使い分ける。単一プロセス・単一利用者であるため、SQLiteとDuckDBへ構造化状態を分散させない。

| 層 | 実体 | 役割 | 主な利用者 |
|---|---|---|---|
| ① Parquet | `data/bars/`（`year=YYYY`パーティション） | 日足時系列の生データを列指向で永続化。追記・大量読み出しに強い。 | DataProvider（書き込み）、DuckDB（読み出し元） |
| ② DuckDB | `data/copilot.duckdb` | Parquetへのビュー、ファンダメンタルズ、ユニバース履歴、実行/ステップ、候補、落選理由、リスク評価、LLM入出力、判断/ポジションを保持する唯一の構造化ストア。 | screening/*, risk/*, llm/*, paper/*, report/*, pipeline/*, backtest/* |

使い分けの原則:
- **時系列の大量データ（株価）はParquet**に列指向で永続化し、DuckDBはその上の「クエリビュー」として振る舞う（DuckDB自体に生データを二重persistしない）。
- **ファンダメンタルズとアプリ状態はDuckDBのテーブル**として直接保持し、同じ`run_id`に紐づけてレポート入力を再構成できるようにする。
- DuckDB内の論理責務は`MarketStore`と`StateStore`へ分けるが、物理DBと接続/トランザクション管理は`storage/database.py`へ一元化する。
- ParquetとDuckDBは`data/`配下に配置し、ローカルファイルシステムへ永続化する。Parquet更新とDuckDB更新を跨ぐ分散トランザクションは行わず、ステップ単位の原子的書き込みと再実行可能なupsertで回復する。

---

## 6. 外部インターフェース一覧

### 6.1 データ取得系 外部API（4つ）＋Discord通知

| # | サービス | 用途 | エンドポイント種別 | 認証 | レート制限 |
|---|---|---|---|---|---|
| 1 | 株価API（yfinance試作 / EODHD本番） | 日足OHLCV取得 | yfinance: 非公式ライブラリ経由（キー不要）。EODHD: REST API（$19.99/月プラン） | yfinance: なし。EODHD: APIキー（クエリパラメータ、実装時に要確認） | yfinance: 明示的なSLAなし（過度な連打は避ける）。EODHD: プラン依存（実装時に要確認） |
| 2 | SEC EDGAR API | 財務諸表・ファンダメンタルズ、8-K/10-Q監視 | 公式REST API（edgartools経由） | 不要（ただしUser-Agentヘッダー必須: 氏名/アプリ名＋連絡先メールアドレス） | 10リクエスト/秒上限 |
| 3 | Finnhub API | ニュース収集（company-newsエンドポイント） | 公式REST API | APIキー（無料枠） | 60コール/分 |
| 4 | FRED API | 経済カレンダー・指標 | 公式REST API | APIキー（無料） | 明示的なSLAなし（実装時に要確認、常識的な間隔を空ける） |
| － | Discord Webhook | 日次レポート通知（オプション機能、デフォルト無効） | Webhook POST | Webhook URL自体が認証情報 | Discord側のWebhookレート制限（実装時に要確認） |

### 6.2 LLM API（Claude API）

上記4データAPI＋Discordとは別枠で、LLM分析を担うClaude APIを以下の通り整理する。

| 項目 | 内容 |
|---|---|
| 用途 | ニュース要約（FR-08前段）、決算書解釈（FR-08本体） |
| モデル | ニュース要約・決算書解釈とも`settings.yaml`の`llm.models`（`news_summary`/`filing_analysis`）で指定するモデルIDを使用する（コード変更不要で切替可能）。デフォルトはいずれも`claude-haiku-4-5-20251001`（$1/$5 per MTok）。精度が必要な場合は決算書解釈のモデルIDをSonnet等（例: $2/$10 per MTok、2026-09-01以降 $3/$15）へ設定変更できる |
| 認証 | `ANTHROPIC_API_KEY`（環境変数） |
| 出力形式 | 構造化JSON出力（`llm/schemas.py`のpydanticモデルに準拠） |
| レート制限・リトライ | 具体的なレート制限値はAPIキーのTierに依存するため実装時に要確認。`LLMClient`が指数バックオフ等のリトライを実装する（詳細は`docs/04_detailed_design.md`）。 |
| 監査記録 | 全呼び出しの入出力・トークン数・適用単価・コストをDuckDB `llm_calls`へ記録（NFR-05） |

---

## 7. エラー処理・フェイルソフト方針

- **フェイルソフトの原則（FR-12・NFR-04）**: ステップ(5)・(6)が失敗しても、ステップ(8)は候補とリスクを含む縮退ブリーフを生成する。
- **各ステップの結果記録**: `runs`に実行全体、`run_steps`に8ステップの成否・詳細・所要時間を記録する。(1)〜(4)の失敗は致命的終了とする。
- **冪等性と原子性**: 同じ評価対象日を再実行しても、bars=`(symbol,date)`、fundamentals=`accession_no`、signals=`(run_date,symbol,strategy_key,signal_name)`、text=`source_id`を自然キーとして訂正可能なupsertを行う。成功済みという理由だけでステップ全体を無条件スキップしない。複数行の論理更新は1トランザクションとし、途中失敗時は全件rollbackする。snapshot再保存は消えた構成員も削除する。LLMのみ完全なsystem+user promptから算出した`(model,prompt_hash,schema_version)`一致時に成功レスポンスを再利用する。（**live検証時の訂正（2026-07-22）**: fundamentalsステップ（`pipeline/daily.py` 2番目のステップ）は例外で、`MarketStore.has_fundamentals_fetched_on()`により当日`fetched_at`済みの銘柄はEDGARへの個別ネットワーク取得のみをスキップする。ステップ自体・自然キーupsertロジックは無条件スキップせず毎回実行するため、上記原則には反しない。詳細は`docs/04_detailed_design.md` 3.21節）
- **欠損検知・リトライ（NFR-04）**: DataProvider・LLMClient等、外部I/Oを伴うコンポーネントはtimeout、retry対象例外、総試行上限、backoffを明示する。レート制御は各試行へ適用し、設定/検証/プログラミングエラーはretryしない。個別銘柄の取得失敗はバッチ全体を止めず、失敗銘柄をリストとして返し、後続処理は成功分のみで進める。
- **断定的売買指示の禁止（CON-03）**: LLM出力スキーマは事実（`facts`）と推測（`interpretation`）をフィールドレベルで分離する。プロンプト指示だけに依存せず、gatewayで全ユーザー表示テキストを一元検査し、違反応答をcache/表示しない。cache hitもsource_idとCON-03を再検証する。

---

## 8. 運用設計

### 8.1 実行環境

| 環境 | 用途 | 実行方法 |
|---|---|---|
| ローカルマシン | 開発・デバッグ・日々の運用のすべてを同一方法で実行 | 利用者が任意のタイミングで1日1回`uv run copilot-daily`を手動実行する（`.env`から環境変数をロード） |

### 8.2 永続化

すべてのデータはローカルファイルシステムのパスにそのまま永続化される。実行環境がステートレスに破棄されることはないため、永続化のためのコミット操作は不要である。
- `data/`（Parquet: `bars/`、DuckDB: `copilot.duckdb`）
- `reports/<run_date>/<run_id>.md`（run別生成Markdown）
- `reports/latest.md`（最新runの便宜コピー）

### 8.3 実行時間

NFR-03「35分以内」を満たすため、各ステップの`duration_s`を`run_steps`に記録し、継続的にボトルネックを監視する。S&P500全銘柄（約500銘柄）に対する外部API呼び出しはレート制限（EDGAR 10req/秒、Finnhub 60コール/分）の制約を受けるため、以下の方針で時間短縮を図る（発注者確定仕様）。

- 価格取得: yfinanceの一括ダウンロード（500銘柄バッチ）を用い、銘柄ごとの個別リクエストを避ける。
- ファンダメンタルズ更新: 週1回、かつ前回取得以降に新規filingがある銘柄のみを対象とする増分更新とする。
- ニュース取得・LLM分析: 保有銘柄＋当日のスクリーニング候補銘柄の合計最大30銘柄に対象を限定する。
- SEC EDGARアクセス: 10リクエスト/秒の上限を守るスロットリングを実装する。

実装後の実測に基づく追加チューニング（並列化要否等）は`docs/04_detailed_design.md`の該当モジュール（3.2, 3.4, 3.6, 3.14節）を参照。

### 8.4 監視

- 実行結果は`runs`/`run_steps`に記録し、レポート末尾へ`run_id`、評価対象日、データ鮮度、各ステップの状態と所要時間を表示する。
- Discord通知を有効にしている場合、通知はレポート配信を兼ねた簡易な死活監視としても機能する（通知が来ない＝バッチ未完走のシグナルになる）。ただし通知はオプション機能であり、無効時（デフォルト）はこの用途には使えない。

---

## 9. セキュリティ設計

- **APIキー管理（NFR-06）**: `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`, `DISCORD_WEBHOOK_URL` はすべて環境変数として扱い、ローカルの`.env`（`.gitignore`対象、python-dotenvで読み込み、`.env.example`に項目のみ記載）から読み込む。`DISCORD_WEBHOOK_URL`は通知（オプション機能）を有効にする場合のみ設定する。
- **コードへの秘密情報のハードコード禁止**: `settings.yaml`・`strategies.yaml`等の設定ファイルにはAPIキー・Webhook URLを直接記載しない。`config.py`（pydantic-settings）が環境変数を優先的に読み込む。
- **リポジトリの公開範囲**: GitHubリポジトリを利用する場合（コード管理用、利用自体は任意）はプライベートで運用する（`docs/00_human_preparation.md`項目5に対応）。
- **SEC EDGAR User-Agent**: 規約上必須のUser-Agentヘッダーには氏名またはアプリ名＋連絡先メールアドレスを設定する（個人情報の取り扱いに留意）。
- **LLM入出力ログの取り扱い**: `llm_calls`テーブルに秘密情報を除いたプロンプト・レスポンス全文と参照元IDを記録する（NFR-05）。`data/`はGit管理対象外とし、リポジトリがprivateでもDBファイルをコミットしない。
- **監査性と秘密情報の分離**: `runs`・`run_steps`・`llm_calls`等の監査ログにAPIキー自体が記録されないよう、ログ出力箇所でシークレット値をマスクする実装方針とする（詳細は`docs/04_detailed_design.md`）。

---

## 10. 要件トレーサビリティ表

| 要件ID | 概要 | 対応コンポーネント |
|---|---|---|
| FR-01 | ユニバース管理 | `universe.py` |
| FR-02 | 株価取得・保存（Provider差し替え可） | `data/base.py`, `data/yfinance_provider.py`, `data/eodhd_provider.py`, `storage/market_store.py` |
| FR-03 | EDGARファンダ取得 | `data/edgar.py`, `storage/market_store.py` |
| FR-04 | ファンダ品質フィルタ（第1段） | `screening/fundamental_filters.py`, `screening/pipeline.py` |
| FR-05 | テクニカルシグナル（第2段・プラガブル） | `screening/technical_signals.py`, `screening/base.py`, `screening/pipeline.py` |
| FR-06 | リスク管理チェック | `risk/position_sizing.py`, `risk/checks.py` |
| FR-07 | テキスト収集 | `text/news_finnhub.py`, `text/edgar_filings.py`, `text/calendar_fred.py` |
| FR-08 | LLM分析（事実/推測分離） | `llm/client.py`, `llm/schemas.py`, `llm/summarize.py`, `llm/filings_analysis.py` |
| FR-09 | CLI・Markdown＋Discord通知 | `report/daily_brief.py`, `report/terminal_report.py`, `report/markdown_report.py`, `report/discord_notify.py` |
| FR-10 | バックテスト（対S&P500） | `backtest/strategies.py`, `backtest/runner.py` |
| FR-11 | ペーパートレード記録 | `paper/journal.py`, `paper/cli.py`, `storage/state_store.py`（DuckDB） |
| FR-12 | 日次バッチ（冪等・フェイルソフト） | `pipeline/daily.py` |
| NFR-01 | コスト（試作月$5以内） | `llm/client.py`（コスト記録）、モデル選定（`settings.yaml`の`llm.models`設定、デフォルト全Haiku） |
| NFR-02 | 1人保守 | 全体アーキテクチャ（シンプルな単一バッチ構成、`config.py`による設定一元化） |
| NFR-03 | 35分以内 | `pipeline/daily.py`（`run_steps`による所要時間計測） |
| NFR-04 | 欠損検知・リトライ | `data/*_provider.py`, `llm/client.py`, `pipeline/daily.py`（フェイルソフト） |
| NFR-05 | 監査性（全入出力記録） | `storage/database.py`, `storage/state_store.py`（`runs`, `run_steps`, `signals`, `candidates`, `screening_rejections`, `risk_assessments`, `llm_calls`, `trades_journal`） |
| NFR-06 | キー管理 | `config.py`, `.env`（python-dotenv） |
| NFR-07 | インターフェース分離（Strategy/Filter/Signal/DataProvider/Notifier） | `data/base.py`, `screening/base.py`, `screening/pipeline.py`（Strategy）, `report/discord_notify.py`（Notifier、オプション機能） |
| NFR-08 | テスト品質（カバレッジ95%以上・E2Eスモーク） | テスト戦略全体（`docs/04_detailed_design.md` 8章）、`pyproject.toml`/justfileのカバレッジ設定 |
| CON-01 | 発注自動化なし | アーキテクチャ全体（証券会社API未接続） |
| CON-02 | yfinance試作限定 | `data/yfinance_provider.py`（P1〜P3）、`data/eodhd_provider.py`（P4） |
| CON-03 | 断定的売買指示を出力しない | `llm/schemas.py`（facts/interpretation分離）、LLMプロンプト設計 |
| CON-04 | ペーパートレード検証ゲート | `paper/journal.py` |

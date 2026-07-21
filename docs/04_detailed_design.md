# 04. 詳細設計書（swing-copilot）

## 1. 文書情報

| 項目 | 内容 |
|---|---|
| システム名（仮称） | swing-copilot |
| 目的 | `docs/03_basic_design.md`のコンポーネント設計を、Claude Codeの`/goal`による自律実装エージェントがそのまま実装に着手できる粒度（モジュール構成、主要クラス/関数シグネチャ、データスキーマ、受け入れ基準）まで具体化する |
| 前提文書 | `docs/00_human_preparation.md`, `docs/01_requirements.md`, `docs/03_basic_design.md` |
| 記法凡例 | コード例中の型ヒントは実装意図を示す設計指示であり、実装時のライブラリバージョンにより微修正され得る。「実装時に要確認」の注記がある箇所は、本書執筆時点で仕様を断定せず、実装時に一次情報（公式ドキュメント等）を確認することを指示するものである。 |
| バージョン | v1.0 |

---

## 2. リポジトリ構成

```
swing-copilot/
├── pyproject.toml            # uv管理（/Users/masuyama/ghq/github.com/tomada1114/uv-template をコピーして雛形とする）
├── .env.example
├── config/
│   ├── settings.yaml         # ユニバース設定、閾値、リスクパラメータ
│   └── strategies.yaml       # 有効なフィルタ/シグナルの組み合わせ定義
├── src/swing_copilot/
│   ├── config.py             # 設定ロード（pydantic-settings）
│   ├── universe.py           # FR-01
│   ├── data/
│   │   ├── base.py           # DataProvider ABC（FR-02, NFR-07）
│   │   ├── yfinance_provider.py
│   │   ├── eodhd_provider.py # 本番用（P4で実装、当面スタブ）
│   │   └── edgar.py          # FR-03（edgartools使用）
│   ├── storage/
│   │   ├── market_store.py   # DuckDB+Parquet
│   │   └── state_store.py    # SQLite
│   ├── screening/
│   │   ├── base.py           # Filter ABC / Signal ABC（NFR-07）
│   │   ├── fundamental_filters.py  # FR-04
│   │   ├── technical_signals.py    # FR-05（TA-Lib）
│   │   └── pipeline.py       # strategies.yamlに従い合成
│   ├── risk/
│   │   ├── position_sizing.py
│   │   └── checks.py         # FR-06
│   ├── text/
│   │   ├── news_finnhub.py
│   │   ├── edgar_filings.py  # 8-K/10-Q監視
│   │   └── calendar_fred.py  # FR-07
│   ├── llm/
│   │   ├── client.py         # Anthropic SDKラッパー、リトライ、コスト記録
│   │   ├── schemas.py        # 構造化出力のpydanticモデル
│   │   ├── summarize.py      # ニュース要約（モデルはsettings.yamlのllm.models.news_summaryで設定、デフォルトHaiku）
│   │   └── filings_analysis.py  # 決算解釈（モデルはsettings.yamlのllm.models.filing_analysisで設定、デフォルトHaiku、FR-08）
│   ├── report/
│   │   ├── html_report.py    # Jinja2テンプレート
│   │   ├── chart_data.py     # OHLC+SMAをJSONでテンプレートへ渡す（UI詳細はdocs/05_ui_design.md参照）
│   │   └── discord_notify.py # FR-09（オプション機能）
│   ├── backtest/
│   │   ├── strategies.py     # backtesting.py用Strategy
│   │   └── runner.py         # FR-10
│   ├── paper/
│   │   └── journal.py        # FR-11 ペーパートレード記録
│   └── pipeline/
│       └── daily.py          # FR-12 オーケストレータ（CLI: uv run copilot-daily）
├── templates/report.html.j2
├── data/                     # Parquet/DuckDB/SQLite（ローカルファイルシステムに永続化）
├── reports/                  # 日次HTML出力
└── tests/
```

---

## 3. モジュール別詳細

以下、各モジュールについて「責務」「主要クラス/関数のシグネチャとdocstring」「依存」「エラー処理」を示す。型ヒントはPython 3.12構文（`list[str]`等）を用いる。DataFrameライブラリはPolars（`pl`）を標準とする。

### 3.1 `config.py`

**責務**: `config/settings.yaml`, `config/strategies.yaml`, 環境変数を統合ロードし、型安全な設定オブジェクトを提供する。

```python
from pydantic_settings import BaseSettings
from pydantic import BaseModel

class Secrets(BaseSettings):
    """環境変数から読み込む秘密情報。ローカルの.env（python-dotenvで読み込み、.gitignore対象）由来。"""
    anthropic_api_key: str
    finnhub_api_key: str
    fred_api_key: str
    discord_webhook_url: str | None = None  # 通知（オプション機能）を有効にする場合のみ設定
    edgar_user_agent: str  # 例: "tomada tmasuyama1114@gmail.com"
    eodhd_api_key: str | None = None  # P4まで未使用

class Settings(BaseModel):
    """settings.yamlをパースした設定。閾値・リスクパラメータ等。"""
    universe: "UniverseConfig"
    risk: "RiskConfig"
    fundamental_filters: "FundamentalFilterConfig"
    technical_signals: "TechnicalSignalConfig"
    backtest: "BacktestConfig"
    llm: "LLMConfig"
    budget: "BudgetConfig"
    schedule: "ScheduleConfig"
    notification: "NotificationConfig"  # Discord通知（オプション機能）の有効/無効
    report: "ReportConfig"              # レポート自動表示設定

def load_settings(path: str = "config/settings.yaml") -> Settings:
    """settings.yamlを読み込みSettingsを返す。ファイル不在・スキーマ不整合はpydantic ValidationErrorを送出する。"""

def load_secrets() -> Secrets:
    """環境変数からSecretsを読み込む。必須キー欠落はpydantic ValidationErrorを送出する（起動時に即座に失敗させる）。"""
```

**依存**: `pydantic`, `pydantic-settings`, `pyyaml`
**エラー処理**: 必須環境変数の欠落・設定ファイルの型不整合はバッチ開始前（ステップ0）に即座に検出し、`run_log`に記録せず標準エラー出力＋非ゼロ終了する（後続ステップが実行されない致命的エラーのため）。

### 3.2 `universe.py`（FR-01）

**責務**: S&P500構成銘柄シンボルリスト（GICSセクター付き）の取得・保存・週次更新。

```python
def get_sp500_symbols(force_refresh: bool = False) -> list[str]:
    """
    S&P500構成銘柄のティッカーシンボルとGICSセクターの一覧を返す。
    取得元はWikipediaの "List of S&P 500 companies" ページのテーブルを
    pandas.read_html で取得する（テーブル構造・列名は実装時に要確認）。
    取得結果は config/universe_snapshot.csv にスナップショットとして保存し、
    取得に失敗した場合はこのスナップショットへフォールバックする（NFR-04）。
    force_refresh=Falseの場合はStateStoreに保存済みの最新リストを優先して使う
    （週次更新、FR-01）。settings.yaml の universe.manual_include /
    universe.manual_exclude による手動上書き（銘柄の追加・除外）を、
    取得結果に適用してから返す。
    """

def refresh_universe(state_store: "StateStore") -> list[str]:
    """
    Wikipediaから最新のユニバース（シンボル＋GICSセクター）を再取得し、
    config/universe_snapshot.csv を更新した上でStateStoreへ保存する。
    前回取得日からの差分（追加/除外銘柄）をrun_logに記録する。
    """
```

**依存**: `storage/state_store.py`, `pandas`（`read_html`用）
**エラー処理**: Wikipediaページの取得・パースに失敗した場合、`config/universe_snapshot.csv`（前回保存済みスナップショット）へフォールバックし、`run_log`に`status=failed, detail="fallback to universe snapshot"`を記録する（NFR-04）。

### 3.3 `data/base.py`（FR-02, NFR-07）

**責務**: 株価データ取得の抽象基底クラス。yfinance/EODHD実装を差し替え可能にする。

```python
from abc import ABC, abstractmethod
from datetime import date
import polars as pl

class DataProvider(ABC):
    """日足株価データ取得の抽象基底クラス。実装はyfinance/EODHD等に差し替え可能（NFR-07）。"""

    @abstractmethod
    def get_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """
        指定シンボル・期間の日足OHLCVを取得する。
        戻り値の列: symbol, date, open, high, low, close, adj_close, volume
        取得に失敗した銘柄は結果から除外され、失敗銘柄リストは
        self.last_failed_symbols（list[str]）に格納される。
        """

    @abstractmethod
    def get_universe_prices_latest(self, symbols: list[str]) -> pl.DataFrame:
        """指定シンボルの最新1日分の株価を取得する。列はget_daily_barsと同一。"""
```

### 3.4 `data/yfinance_provider.py`（P1〜P3、CON-02）

```python
class YFinanceProvider(DataProvider):
    """yfinanceを用いた試作用DataProvider実装。本番運用には使用しない（CON-02）。"""

    def get_daily_bars(self, symbols, start, end) -> pl.DataFrame:
        """
        yfinanceの一括ダウンロード機能（複数シンボルをまとめて取得するAPI）を用いて
        銘柄群をバッチ取得する（500銘柄バッチ、NFR-03: 35分以内の実現方針）。
        個別銘柄の取得失敗（例外・空データ）はバッチ結果から除外し、
        self.last_failed_symbolsに追加した上で処理を継続する
        （バッチ全体を停止させない、NFR-04）。
        """
```

**エラー処理**: yfinanceは非公式ラッパーであり明示的なレート制限SLAがないため、連続リクエスト間に短い待機を挟む実装とする（具体的な待機時間は実装時に要確認・調整）。

### 3.5 `data/eodhd_provider.py`（P4、本番用スタブ）

```python
class EODHDProvider(DataProvider):
    """
    EODHD（$19.99/月）を用いた本番用DataProvider実装。
    P1〜P3ではスタブ（NotImplementedError）とし、P4で実装する。
    エンドポイント・レスポンススキーマは実装時にEODHD公式ドキュメントを要確認。
    """

    def get_daily_bars(self, symbols, start, end) -> pl.DataFrame:
        raise NotImplementedError("EODHDProvider is implemented in P4")
```

### 3.6 `data/edgar.py`（FR-03）

**責務**: SEC EDGAR公式APIから財務諸表・ファンダメンタルズを取得する。`edgartools`ライブラリを使用。

```python
class EdgarClient:
    """
    SEC EDGAR公式API（edgartools経由）のラッパー。
    リクエストは10リクエスト/秒を超えないようレート制限し、
    全リクエストにUser-Agentヘッダー（Secrets.edgar_user_agent）を付与する。
    呼び出し側（pipeline/daily.py）は週1回、かつ前回取得以降に新規filingが
    ある銘柄のみを対象にfetch_fundamentals()を呼び出す増分更新とする
    （NFR-03: 35分以内の実現方針）。
    edgartoolsの具体的な関数名・クラス名（例: Company, get_filings等）は
    実装時に公式ドキュメントを要確認。
    """

    def fetch_fundamentals(self, symbol: str) -> "FundamentalsRecord":
        """
        指定銘柄の直近四半期の財務指標（revenue, net_income, fcf, equity, shares）を取得する。
        戻り値はstorage.market_storeのfundamentalsテーブルスキーマに対応するモデル。
        """

    def fetch_recent_filings(self, symbol: str, form_types: list[str]) -> list["FilingRef"]:
        """指定銘柄の直近提出書類（8-K, 10-Q等）の参照情報を返す（FR-07で利用）。"""
```

**依存**: `edgartools`
**エラー処理**: レート制限超過時はリトライ（指数バックオフ）。銘柄単位で取得失敗した場合はスキップしログ記録、バッチは継続する。

### 3.7 `storage/market_store.py`

```python
import duckdb
import polars as pl

class MarketStore:
    """Parquet（bars/）+ DuckDB（分析ビュー・fundamentalsテーブル）の読み書きを担う。"""

    def __init__(self, duckdb_path: str = "data/market.duckdb", parquet_root: str = "data/bars"):
        ...

    def write_bars(self, df: pl.DataFrame) -> None:
        """
        日足OHLCVをyear=YYYYパーティションでParquetへ追記する。
        同一symbol+dateの既存レコードは上書きしない（冪等: 既存日付はスキップ）。
        """

    def read_bars(self, symbols: list[str], start: "date", end: "date") -> pl.DataFrame:
        """DuckDB経由でParquetビューから指定範囲の日足を読み出す。"""

    def upsert_fundamentals(self, records: list["FundamentalsRecord"]) -> None:
        """fundamentalsテーブルへupsertする（symbol, fiscal_period が一意キー）。"""

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """DuckDB接続を返す（screening/backtestからの直接SQL利用向け）。"""
```

### 3.8 `storage/state_store.py`

```python
import sqlite3

class StateStore:
    """SQLite（state.sqlite）による状態管理: positions, trades_journal, llm_calls, run_log, signals。"""

    def __init__(self, db_path: str = "data/state.sqlite"):
        ...

    def init_schema(self) -> None:
        """未作成のテーブルをDDL（本書4章）に従い作成する（既存テーブルには影響しない）。"""

    def record_run_log(self, run_date: "date", step: str, status: str, detail: str | None, duration_s: float) -> None:
        """run_logへ1行追記する。"""

    def record_signals(self, signals: list["SignalHit"], run_date: "date") -> None:
        """signalsへ記録する。(date, symbol, signal_name)の重複はUNIQUE制約によりスキップ（冪等）。"""

    def record_llm_call(self, model: str, input_tokens: int, output_tokens: int, cost_usd: float, prompt_hash: str, response_json: str) -> None:
        """llm_callsへ1行追記する（NFR-05: 監査性）。"""

    def get_open_positions(self, is_paper: bool = True) -> list["Position"]:
        """オープン中のポジション一覧を返す（risk/checks.pyのセクター集中度計算等で使用）。"""
```

**エラー処理**: SQLite書き込みは各呼び出しをトランザクション単位とし、失敗時は呼び出し元に例外を伝播する（run_log自体の記録失敗は標準エラー出力へフォールバック）。

### 3.9 `screening/base.py`（NFR-07）

```python
from abc import ABC, abstractmethod
import polars as pl
from pydantic import BaseModel

class SignalHit(BaseModel):
    symbol: str
    signal_name: str
    direction: str      # "long" | "short"（当面 "long" のみ想定）
    strength: float
    context: dict

class Filter(ABC):
    """第1段: ファンダメンタルズ等によるユニバース絞り込み。"""
    name: str

    @abstractmethod
    def apply(self, df_fundamentals: pl.DataFrame) -> set[str]:
        """条件を満たすシンボル集合を返す。"""

class Signal(ABC):
    """第2段: テクニカル等によるシグナル評価。"""
    name: str

    @abstractmethod
    def evaluate(self, bars: pl.DataFrame) -> list[SignalHit]:
        """条件に合致したシグナルのリストを返す。"""

# 登録レジストリ（デコレータ登録方式）。新しいFilter/Signalはクラス追加＋
# strategies.yamlへの1行追加のみで有効化できることを保証する（発注者の明示要望）。
FILTER_REGISTRY: dict[str, type[Filter]] = {}
SIGNAL_REGISTRY: dict[str, type[Signal]] = {}

def register_filter(key: str):
    """Filterサブクラスを FILTER_REGISTRY に登録するクラスデコレータ。"""

def register_signal(key: str):
    """Signalサブクラスを SIGNAL_REGISTRY に登録するクラスデコレータ。"""
```

### 3.10 `screening/fundamental_filters.py`（FR-04）

```python
@register_filter("profitable_positive_fcf_equity")
class ProfitablePositiveFCFEquityFilter(Filter):
    """
    直近4四半期黒字（net_income>0 全期）AND FCF>0（直近期）AND 自己資本比率>閾値。
    閾値はsettings.yaml: fundamental_filters.min_profitable_quarters,
    require_positive_fcf, min_equity_ratio から取得する。
    """
    name = "profitable_positive_fcf_equity"

    def apply(self, df_fundamentals: pl.DataFrame) -> set[str]:
        ...
```

### 3.11 `screening/technical_signals.py`（FR-05、TA-Lib使用）

```python
@register_signal("trend_sma")
class TrendSMASignal(Signal):
    """トレンド判定: 終値>SMA200 かつ SMA50>SMA200。TA-Lib talib.SMA を使用。"""
    name = "trend_sma"

    def evaluate(self, bars: pl.DataFrame) -> list[SignalHit]: ...

@register_signal("pullback_rsi")
class PullbackRSISignal(Signal):
    """押し目判定: RSI(14)<閾値 かつ 終値がSMA50の±バンド%以内。TA-Lib talib.RSI を使用。"""
    name = "pullback_rsi"

    def evaluate(self, bars: pl.DataFrame) -> list[SignalHit]: ...

@register_signal("volume_min")
class MinAverageVolumeSignal(Signal):
    """出来高フィルタ: 20日平均出来高が閾値を上回ること。"""
    name = "volume_min"

    def evaluate(self, bars: pl.DataFrame) -> list[SignalHit]: ...
```

TA-Libの具体的な関数シグネチャ（`talib.SMA(close, timeperiod=...)`, `talib.RSI(close, timeperiod=...)`等）はTA-Lib公式ドキュメントに準拠する。

### 3.12 `screening/pipeline.py`

```python
class ScreeningPipeline:
    """
    config/strategies.yaml の filters: [...] / signals: [...] を読み、
    FILTER_REGISTRY / SIGNAL_REGISTRY から該当クラスをインスタンス化して合成する。
    """

    def __init__(self, strategies_config: dict, market_store: "MarketStore"):
        ...

    def run(self, run_date: "date") -> list[SignalHit]:
        """
        (1) 有効な全Filterをapply()しシンボル集合の積集合を取る（第1段通過銘柄）。
        (2) 第1段通過銘柄に対し有効な全Signalをevaluate()し、SignalHitのリストを返す（第2段）。
        """
```

**Strategy抽象について（NFR-07）**: NFR-07が求める`Strategy`インターフェースの実体は、`config/strategies.yaml`（フィルタ・シグナルの宣言的な組み合わせ定義）と本`ScreeningPipeline`（それを読み取り合成実行するエンジン）の組み合わせである。すなわち「フィルタ×シグナルの宣言的合成」自体が戦略定義となる。バックテスト（`backtest/strategies.py`）もこの同じ`FILTER_REGISTRY`/`SIGNAL_REGISTRY`（`screening/base.py`）を参照しており、`backtesting.py`ライブラリの`Strategy`クラス（`SwingStrategy`、3.19節）は、このFilter/Signalレジストリをbacktesting.pyの実行モデルに適合させるアダプタとして実装する。

**エラー処理**: `strategies.yaml`に未登録キーが指定された場合はKeyErrorを送出し、バッチ開始前の設定検証で検出する（起動時フェイルファスト）。

### 3.13 `risk/position_sizing.py` / `risk/checks.py`（FR-06）

```python
class CorrelationWarning(BaseModel):
    """FR-06: 保有銘柄との相関に関する警告（ブロックはしない、参考情報）。"""
    warning_type: str = "high_correlation"
    correlated_symbol: str      # 相関が閾値超だった相手銘柄
    correlation: float          # ピアソン相関係数

class RiskAssessment(BaseModel):
    symbol: str
    approved: bool
    max_shares: float
    reasons: list[str]
    warnings: list[CorrelationWarning] = []  # FR-06: 相関警告（approved判定には影響しない）

def calc_position_size(
    account_equity: float, entry_price: float, stop_price: float,
    max_position_pct: float, max_trade_risk_pct: float,
) -> float:
    """
    1トレードのリスク（資金のmax_trade_risk_pct、ストップ幅基準）と
    1銘柄の上限（資金のmax_position_pct）の両方を満たす最大株数を返す。
    """

class RiskChecker:
    """FR-06: サイズ上限・セクター集中度等のリスクチェック。閾値はsettings.yaml: risk.* から取得。"""

    def check(self, candidates: list[SignalHit], portfolio: list["Position"]) -> list[RiskAssessment]:
        """
        各候補について、
        - 1銘柄=資金のmax_position_pct上限
        - 1トレードのリスク=資金のmax_trade_risk_pct上限（ストップ幅基準）
        - 同一セクター上限max_sector_pct
        - 銘柄間相関チェック（FR-06、ブロックしない警告のみ）
        を満たすかを判定する。セクター判定に必要な銘柄→セクターのマッピングは、
        universe.pyが取得・保存するGICSセクター（config/universe_snapshot.csv、
        本書3.2節参照）を用いる。
        """

    def check_correlation(self, candidate_symbol: str, portfolio: list["Position"], market_store: "MarketStore") -> list[CorrelationWarning]:
        """
        FR-06: エントリー候補（candidate_symbol）と既存保有銘柄（portfolio）それぞれについて、
        直近risk.correlation_lookback_days営業日（デフォルト60）の日次リターンの
        ピアソン相関係数を、market_store（Parquet/DuckDBの日足）から算出する。
        いずれかの保有銘柄との相関がrisk.max_correlation（デフォルト0.7）を超える場合、
        CorrelationWarning（warning_type="high_correlation"、相手銘柄・相関値を含む）を
        リストへ追加してRiskAssessment.warningsへ格納する。
        **警告のみでブロックはしない**（意思決定支援の原則、CON-03整合。approvedの判定には
        影響を与えない）。候補または保有銘柄のいずれかで直近60営業日分の日足データが
        揃っていない場合、その銘柄ペアは「計算不能」として警告を付与せず、
        その旨をログ（detail）に記録するに留める。
        """
```

### 3.14 `text/news_finnhub.py` / `text/edgar_filings.py` / `text/calendar_fred.py`（FR-07）

ニュース取得・EDGAR新着開示取得（およびこれらに続くFR-08のLLM分析）の対象銘柄は、保有銘柄＋当日のスクリーニング候補銘柄の合計最大30銘柄に限定する（NFR-03: 35分以内の実現方針）。経済指標カレンダー取得（FRED）は銘柄に依存しないため対象外。

```python
def fetch_company_news(symbol: str, since: "date") -> list["NewsItem"]:
    """Finnhub company-newsエンドポイントから指定銘柄の直近ニュースを取得する。60コール/分を超えないようレート制限する。"""

def fetch_recent_filings_text(symbol: str) -> list["FilingText"]:
    """EdgarClient.fetch_recent_filings()の結果から8-K/10-Qの本文テキストを取得する。"""

def fetch_calendar_events(start: "date", end: "date") -> list["CalendarEvent"]:
    """FRED APIから経済指標カレンダーを取得する。"""
```

**エラー処理**: いずれも銘柄・イベント単位で取得失敗した場合はスキップし処理を継続する。全体が失敗した場合、`pipeline/daily.py`のステップ(5)は`failed`として記録され、ステップ(6)(7)(8)は縮退版で継続する（FR-12）。

### 3.15 `llm/schemas.py`（FR-08、CON-03）

```python
from pydantic import BaseModel

class NewsSummary(BaseModel):
    symbol: str
    period: str
    facts: list[str]           # 事実のみ（推測を含めない）
    interpretation: list[str]  # 推測・解釈（事実と明確に分離）
    sentiment: int              # -1 | 0 | +1
    risk_flags: list[str]
    sources: list[str]          # URL

class FilingAnalysis(BaseModel):
    symbol: str
    filing_type: str
    facts: list[str]
    interpretation: list[str]
    red_flags: list[str]
    yoy_changes: list[str]
    guidance_direction: str  # 例: "positive" | "negative" | "neutral" | "not_disclosed"
```

**設計原則（CON-03）**: `facts`と`interpretation`をフィールドレベルで分離することで、LLM出力に「買い/売り」等の断定的売買指示が混入することをスキーマレベルで抑止する。いずれのフィールドにも「買うべき」「売るべき」等の命令形出力を禁止する制約はプロンプト側（3.17節）で明示する。

### 3.16 `llm/client.py`（FR-08, NFR-05, NFR-06）

```python
from pydantic import BaseModel

class LLMClient:
    """Anthropic SDKのラッパー。リトライ、構造化出力パース、コスト記録を担う。"""

    def __init__(self, api_key: str, state_store: "StateStore"):
        ...

    def analyze(
        self, prompt: str, schema: type[BaseModel], model: str, max_tokens: int,
    ) -> BaseModel:
        """
        Claude APIを呼び出し、schemaに準拠した構造化JSON出力をパースして返す。
        呼び出しごとに入力/出力トークン数・コスト(USD)・プロンプトハッシュ・
        レスポンスJSON全文をstate_store.record_llm_call()経由でllm_callsへ記録する（NFR-05）。
        レート制限・一時的エラーは指数バックオフでリトライする（具体的なリトライ回数・
        待機秒数、レート制限値は実装時に要確認）。
        コスト計算はmodelごとの単価（settings.yaml外、コード内定数または
        設定で管理。Haiku: $1/$5 per MTok、Sonnet: $2/$10 per MTok、
        Sonnetは2026-09-01以降 $3/$15 per MTokに変更されるため日付で切替える）。
        呼び出し元（summarize_news/analyze_filing）は、使用するモデルIDを
        settings.yamlのllm.models.news_summary/llm.models.filing_analysisから
        取得してmodel引数に渡す。LLMClient自体はモデルIDをハードコードせず、
        呼び出し側から受け取った値をそのままAPIリクエストのmodelフィールドに使用する。
        """
```

### 3.17 `llm/summarize.py` / `llm/filings_analysis.py`（FR-08）

```python
def summarize_news(
    client: LLMClient, symbol: str, news_items: list["NewsItem"], model: str,
) -> NewsSummary:
    """llm/client.LLMClientを呼び出し、NewsSummaryを返す。プロンプトは本書6章参照。

    modelは呼び出し元がsettings.yamlのllm.models.news_summary
    （デフォルト: claude-haiku-4-5-20251001）から取得して渡す。関数内でモデルIDをハードコードしない。
    """

def analyze_filing(
    client: LLMClient, symbol: str, filing_text: "FilingText", model: str,
) -> FilingAnalysis:
    """llm/client.LLMClientを呼び出し、FilingAnalysisを返す。プロンプトは本書6章参照。

    modelは呼び出し元がsettings.yamlのllm.models.filing_analysis
    （デフォルト: claude-haiku-4-5-20251001。精度重視の場合はSonnet等へ設定変更可）から
    取得して渡す。関数内でモデルIDをハードコードしない。
    """
```

### 3.18 `report/html_report.py` / `report/discord_notify.py`（FR-09, NFR-07）

```python
def render_report(
    run_date: "date", signals: list[SignalHit], risk_assessments: list[RiskAssessment],
    news_summaries: list[NewsSummary] | None, filing_analyses: list[FilingAnalysis] | None,
) -> str:
    """
    templates/report.html.j2（Jinja2）を用いてHTMLレポートを生成し、
    reports/{run_date}.html へ保存してパスを返す。
    news_summaries/filing_analyses がNone（LLM分析失敗時）の場合は
    スクリーニング結果のみの縮退版として描画する（FR-12フェイルソフト）。
    """

from pathlib import Path
from typing import Protocol

class Notifier(Protocol):
    """
    通知送信の抽象インターフェース（NFR-07）。DiscordNotifierはこの実装の1つである。
    """

    def notify(self, summary: str, report_path: Path | None) -> None:
        """通知を送信する。summaryは通知本文（サマリテキスト）、report_pathは
        言及するレポートファイルへのパス（Noneの場合はレポートへの言及なし）。"""
        ...

class DiscordNotifier:
    """Notifierプロトコルの実装。Discord Webhookへ通知を送信する（FR-09、オプション機能。settings.yamlのnotification.enabled=trueかつWebhook URL設定時のみ呼び出される）。"""

    def __init__(self, webhook_url: str):
        ...

    def notify(self, summary: str, report_path: Path | None) -> None:
        """
        Discord Webhookへレポートの要約（サマリテキスト＋レポートへの言及）を送信する。
        送信失敗時はrun_logにfailedを記録し、例外は送出しない（バッチ全体を止めない）。
        """
```

将来メール通知やSlack通知を追加する場合も、`Notifier`を実装するクラス（例: `EmailNotifier`, `SlackNotifier`）を追加するだけで`pipeline/daily.py`から差し替え可能である（NFR-07）。

### 3.19 `backtest/strategies.py` / `backtest/runner.py`（FR-10）

```python
from backtesting import Strategy

class SwingStrategy(Strategy):
    """
    backtesting.py用戦略クラス。
    エントリー: シグナル翌日寄付。イグジット: ATRトレーリングストップ(2.5×ATR14)
    または保有60営業日経過。init()/next()の実装詳細は実装時にbacktesting.py
    公式ドキュメントを要確認。
    """

def run_backtest(
    symbols: list[str], start: "date", end: "date",
    commission_pct: float = 0.001, slippage_pct: float = 0.001,
    benchmark_symbol: str = "SPY",
) -> "BacktestResult":
    """
    デフォルト戦略（fundamental_filters + technical_signals）でバックテストを実行し、
    SPYバイ&ホールドとの比較結果（リターン・ドローダウン・勝率等）を返す。
    """
```

### 3.20 `paper/journal.py`（FR-11, CON-04）

```python
class PaperJournal:
    """ペーパートレードの記帳。人間の判断（追随/見送り/修正）と仮想約定を記録する。"""

    def record_decision(
        self, signal_id: int, decision: str, reason_memo: str,
        virtual_fill_price: float | None,
    ) -> None:
        """
        decisionは "followed" | "ignored" | "modified"。trades_journalへ記録する。
        CON-04（ペーパートレード検証ゲート）の実績データ元となる。
        """

    def close_position(self, position_id: int, close_date: "date", close_price: float) -> None:
        """オープン中のペーパーポジションをクローズし、positionsを更新する。"""
```

### 3.21 `pipeline/daily.py`（FR-12）

```python
def run_daily(dry_run: bool = False, skip_text: bool = False, skip_llm: bool = False) -> int:
    """
    日次バッチのオーケストレータ。docs/03_basic_design.md 4章の9ステップを
    固定順で実行する。各ステップの成否・詳細・所要時間をrun_logへ記録する。
    最終ステップ(9)では、生成したレポートを webbrowser.open() でデフォルトブラウザに
    自動表示する（settings.yamlのreport.auto_open、デフォルトtrue）。
    dry_run=Trueの場合、テスト・CI検証用としてステップ(9)のブラウザ自動表示をスキップする。
    skip_text/skip_llmはP1段階での動作確認用フラグ。
    戻り値: プロセス終了コード（0=成功、非ゼロ=致命的失敗）。
    CLIエントリポイント: `uv run copilot-daily [--dry-run] [--skip-text] [--skip-llm] [--limit N] [--no-open]`
    （`--limit N`: ユニバースを先頭N銘柄+保有銘柄に制限する検証・スモーク用フラグ。`--no-open`: レポート生成後の自動ブラウザ表示を抑止する。いずれも通常運用では未指定）
    （pyproject.toml の [project.scripts] で copilot-daily = "swing_copilot.pipeline.daily:main" として登録）。
    """

def main() -> None:
    """CLIエントリポイント。argparseで引数をパースしrun_daily()を呼び、sys.exit()する。"""
```

---

## 4. データスキーマ定義

### 4.1 Parquetスキーマ（`data/bars/`）

`year=YYYY`でHiveパーティショニングする。

| 列名 | 型 | 説明 |
|---|---|---|
| symbol | string | ティッカーシンボル |
| date | date | 取引日 |
| open | double | 始値 |
| high | double | 高値 |
| low | double | 安値 |
| close | double | 終値 |
| adj_close | double | 調整済み終値 |
| volume | int64 | 出来高 |

### 4.2 DuckDB

```sql
-- Parquetへのビュー
CREATE VIEW IF NOT EXISTS bars AS
SELECT * FROM read_parquet('data/bars/year=*/*.parquet', hive_partitioning=true);

-- ファンダメンタルズテーブル
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol         VARCHAR NOT NULL,
    fiscal_period  VARCHAR NOT NULL,   -- 例: "2026Q2"
    revenue        DOUBLE,
    net_income     DOUBLE,
    fcf            DOUBLE,
    equity         DOUBLE,
    shares         DOUBLE,
    fetched_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, fiscal_period)
);
```

### 4.3 SQLite（`data/state.sqlite`）

```sql
CREATE TABLE IF NOT EXISTS signals (
    signal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date          DATE NOT NULL,
    symbol        TEXT NOT NULL,
    signal_name   TEXT NOT NULL,
    direction     TEXT NOT NULL,
    strength      REAL,
    context_json  TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, symbol, signal_name)
);

CREATE TABLE IF NOT EXISTS positions (
    position_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    is_paper      BOOLEAN NOT NULL DEFAULT 1,
    entry_date    DATE NOT NULL,
    entry_price   REAL NOT NULL,
    shares        REAL NOT NULL,
    stop_price    REAL,
    status        TEXT NOT NULL CHECK(status IN ('open','closed')),
    close_date    DATE,
    close_price   REAL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades_journal (
    journal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id            INTEGER NOT NULL REFERENCES signals(signal_id),
    position_id          INTEGER REFERENCES positions(position_id),
    decision              TEXT NOT NULL CHECK(decision IN ('followed','ignored','modified')),
    reason_memo           TEXT,
    virtual_fill_price    REAL,
    close_info            TEXT,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL NOT NULL,
    prompt_hash     TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS run_log (
    run_log_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date     DATE NOT NULL,
    step         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(status IN ('success','failed','skipped')),
    detail       TEXT,
    duration_s   REAL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 4.4 pydanticモデル一覧

| モデル | 定義場所 | 用途 |
|---|---|---|
| `Settings` / `Secrets` | `config.py` | 設定・秘密情報 |
| `SignalHit` | `screening/base.py` | シグナル評価結果 |
| `RiskAssessment` | `risk/checks.py` | リスクチェック結果 |
| `CorrelationWarning` | `risk/checks.py` | 銘柄間相関の警告（FR-06、`RiskAssessment.warnings`） |
| `NewsSummary` | `llm/schemas.py` | ニュース要約（FR-08） |
| `FilingAnalysis` | `llm/schemas.py` | 決算書解釈（FR-08） |
| `FundamentalsRecord` | `data/edgar.py` | ファンダメンタルズ1レコード |
| `Position` | `storage/state_store.py` | ポジション（DDLに対応） |

---

## 5. 設定ファイル仕様

### 5.1 `config/settings.yaml`（全項目とデフォルト値）

```yaml
universe:
  index: "sp500"
  refresh_interval_days: 7
  snapshot_path: "config/universe_snapshot.csv"
  manual_include: []          # 手動追加する銘柄シンボルのリスト
  manual_exclude: []          # 手動除外する銘柄シンボルのリスト

risk:
  max_position_pct: 0.10      # 1銘柄=資金の10%上限
  max_trade_risk_pct: 0.01    # 1トレードのリスク=資金の1%（ストップ幅基準）
  max_sector_pct: 0.30        # 同一セクター上限30%
  max_correlation: 0.7               # 保有銘柄との相関がこれを超えたら警告（ブロックしない、FR-06）
  correlation_lookback_days: 60      # 相関計算に用いる直近営業日数（FR-06）

fundamental_filters:
  min_profitable_quarters: 4   # 直近4四半期黒字
  require_positive_fcf: true   # FCF>0
  min_equity_ratio: 0.30       # 自己資本比率>30%

technical_signals:
  trend:
    sma_short: 50
    sma_long: 200
  pullback:
    rsi_period: 14
    rsi_threshold: 45
    sma_band_pct: 0.03
  volume:
    avg_volume_days: 20
    min_avg_volume: 1000000

backtest:
  entry: "next_open"           # シグナル翌日寄付
  exit_atr_multiple: 2.5
  exit_atr_period: 14
  max_hold_days: 60
  commission_pct: 0.001
  slippage_pct: 0.001
  benchmark: "SPY"

llm:
  models:
    news_summary: "claude-haiku-4-5-20251001"    # デフォルト: Haiku（実験用・最安）
    filing_analysis: "claude-haiku-4-5-20251001" # デフォルト: Haiku。精度が欲しくなったらSonnet等のIDに書き換え
  max_tokens: 2048

budget:
  monthly_cap_usd_prototype: 5    # NFR-01
  monthly_cap_usd_production: 25

schedule:
  timeout_minutes: 35              # NFR-03（ローカル手動実行時の所要時間上限）

notification:
  enabled: false                   # Discord通知はオプション機能（デフォルト無効）。trueにする場合は環境変数DISCORD_WEBHOOK_URL（.env）を設定する

report:
  auto_open: true                  # 実行完了後、生成したレポートを webbrowser.open() でデフォルトブラウザに自動表示する
```

### 5.2 `config/strategies.yaml`（初期値）

```yaml
filters:
  - profitable_positive_fcf_equity

signals:
  - trend_sma
  - pullback_rsi
  - volume_min
```

新しいフィルタ/シグナルを追加する場合、`screening/fundamental_filters.py`または`screening/technical_signals.py`に`@register_filter("key")`/`@register_signal("key")`を付けたクラスを追加し、本ファイルの`filters:`/`signals:`にキーを1行追加するだけで有効化できる（NFR-07、発注者の明示要望）。

---

## 6. LLMプロンプト設計

いずれのプロンプトも、CON-03（断定的売買指示を出力しない）を満たすため、システムプロンプトで明示的に禁止事項を宣言し、出力を`llm/schemas.py`の構造化スキーマに強制する（Anthropic SDKの構造化出力機能を用いる。具体的なAPI呼び出し方法は`claude-api`スキル／公式ドキュメントを実装時に要確認）。

### 6.1 ニュース要約プロンプト（`llm/summarize.py`、モデル: `settings.yaml`の`llm.models.news_summary`で指定。デフォルト`claude-haiku-4-5-20251001`）

**システムプロンプト（草案）**:
```
あなたは米国株の個人投資家向け意思決定支援アシスタントです。
与えられたニュース記事群から、対象銘柄に関する情報を要約してください。

厳守事項:
1. 「facts」フィールドには、記事に明記された客観的事実のみを記載してください
   （例: 決算数値、契約締結、経営陣交代等）。あなたの意見・推測を含めないでください。
2. 「interpretation」フィールドには、factsから読み取れる解釈・示唆を記載してください。
   ここでも「買い」「売り」「保有すべき」等の断定的な売買指示は一切出力しないでください。
   あくまで「〜という可能性がある」「〜と読める」という留保付きの表現にとどめてください。
3. 出力は指定されたJSONスキーマに厳密に従ってください。
4. 事実として確認できない内容を断定的に記載しないでください。不明な場合は
   risk_flagsに不確実性を記録してください。
```

**ユーザープロンプト（テンプレート草案）**:
```
対象銘柄: {symbol}
対象期間: {period}

以下は収集したニュース記事一覧です（各記事: タイトル・本文抜粋・URL・公開日）。

{news_items_formatted}

上記からNewsSummaryスキーマに従いJSONを出力してください。
sourcesフィールドには参照した記事のURLをすべて含めてください。
```

### 6.2 決算書解釈プロンプト（`llm/filings_analysis.py`、モデル: `settings.yaml`の`llm.models.filing_analysis`で指定。デフォルト`claude-haiku-4-5-20251001`、精度重視の場合はSonnet等へ設定変更可）

**システムプロンプト（草案）**:
```
あなたは米国株の個人投資家向け意思決定支援アシスタントです。
与えられたSEC提出書類（8-K/10-Q等）の抜粋から、対象銘柄の財務・経営状況を分析してください。

厳守事項:
1. 「facts」フィールドには、書類に明記された数値・記述のみを記載してください
   （例: 売上高、前年同期比、ガイダンス数値、経営陣コメントの引用等）。
2. 「interpretation」フィールドには、factsに基づく解釈を記載してください。
   ここでも「買い」「売り」「今すぐ発注すべき」等の断定的な売買指示・投資助言は
   一切出力しないでください。本システムは意思決定支援ツールであり、最終判断は
   人間が行うことを前提としています（自動発注は行いません）。
3. 「red_flags」には、財務上の懸念点（利益率悪化、キャッシュフロー悪化、
   偶発債務等）があれば記載してください。なければ空リストとしてください。
4. 「yoy_changes」には前年同期比の主要な変化点を記載してください。
5. 「guidance_direction」は経営陣のガイダンスの方向性を
   "positive" | "negative" | "neutral" | "not_disclosed" のいずれかで分類してください。
6. 出力は指定されたJSONスキーマに厳密に従ってください。
```

**ユーザープロンプト（テンプレート草案）**:
```
対象銘柄: {symbol}
書類種別: {filing_type}
提出日: {filing_date}

以下は当該書類の抜粋です。

{filing_text_excerpt}

上記からFilingAnalysisスキーマに従いJSONを出力してください。
```

**補足**: 書類本文が長大な場合のチャンク分割・要約前処理の方針は実装時に要確認（トークン上限との兼ね合い）。

---

## 7. デフォルト戦略仕様（初期実装、全閾値は`settings.yaml`化）

### 7.1 第1段: ファンダメンタルフィルタ

| 条件 | 閾値 | 設定キー |
|---|---|---|
| 直近4四半期黒字 | net_income > 0（全4四半期） | `fundamental_filters.min_profitable_quarters = 4` |
| FCF | FCF > 0 | `fundamental_filters.require_positive_fcf = true` |
| 自己資本比率 | > 30% | `fundamental_filters.min_equity_ratio = 0.30` |

### 7.2 第2段: テクニカルシグナル

| シグナル | 条件 | 設定キー |
|---|---|---|
| トレンド | 終値 > SMA200 かつ SMA50 > SMA200 | `technical_signals.trend.sma_short=50`, `sma_long=200` |
| 押し目 | RSI(14) < 45 かつ 終値がSMA50の±3%以内 | `technical_signals.pullback.rsi_period=14`, `rsi_threshold=45`, `sma_band_pct=0.03` |
| 出来高フィルタ | 20日平均出来高 > 100万株 | `technical_signals.volume.avg_volume_days=20`, `min_avg_volume=1000000` |

### 7.3 バックテスト初期設定

| 項目 | 値 | 設定キー |
|---|---|---|
| エントリー | シグナル翌日寄付 | `backtest.entry="next_open"` |
| イグジット | ATRトレーリングストップ(2.5×ATR14) または60営業日 | `backtest.exit_atr_multiple=2.5`, `exit_atr_period=14`, `max_hold_days=60` |
| 手数料 | 0.1% | `backtest.commission_pct=0.001` |
| スリッページ | 0.1% | `backtest.slippage_pct=0.001` |
| 比較対象 | SPYバイ&ホールド | `backtest.benchmark="SPY"` |

### 7.4 リスクパラメータ

| 項目 | 値 | 設定キー |
|---|---|---|
| 1銘柄上限 | 資金の10% | `risk.max_position_pct=0.10` |
| 1トレードリスク上限 | 資金の1%（ストップ幅基準） | `risk.max_trade_risk_pct=0.01` |
| 同一セクター上限 | 30% | `risk.max_sector_pct=0.30` |
| 保有銘柄との相関警告閾値 | ピアソン相関 0.7 超で警告（ブロックしない） | `risk.max_correlation=0.7` |
| 相関計算の参照期間 | 直近60営業日の日次リターン | `risk.correlation_lookback_days=60` |

---

## 8. テスト戦略

### 8.1 ユニットテスト（モック使用）

- **対象**: `screening/*`, `risk/*`, `llm/schemas.py`, `llm/client.py`（HTTPコールはモック）, `storage/*`（一時ファイル/一時SQLite上で実行）。
- **方針**: 外部API（yfinance, EDGAR, Finnhub, FRED, Claude API, Discord Webhook）は全てモック化し、ネットワークアクセスなしで実行できるようにする。`pytest`の`monkeypatch`/`unittest.mock`を使用する。
- **DataProviderのテスト**: `DataProvider` ABCに対する契約テスト（列名・型が仕様通りであること）を共通化し、`YFinanceProvider`・（将来の）`EODHDProvider`双方に適用できるようにする（NFR-07の担保）。
- **Filter/Signalのテスト**: 既知の入力DataFrame（Polars）に対する期待値ベースのテスト。境界値（例: RSIちょうど45、SMAバンドの境界）を含める。

### 8.2 統合テスト（5銘柄の小規模実データsmoke test）

- **対象**: `pipeline/daily.py`のエンドツーエンド実行。
- **方針**: S&P500全銘柄ではなく、固定の5銘柄（例: AAPL, MSFT, JPM, XOM, JNJ等、セクター分散を考慮して選定。具体的な銘柄選定は実装時に確定）に対し、実際の外部API（またはVCR的な記録済みレスポンス）を用いて`uv run copilot-daily --dry-run`を実行し、正常終了（終了コード0）・`reports/`へのHTML出力・`run_log`への9ステップ記録を検証する。
- **API呼び出しの扱い**: CI環境でのAPIキー不在・レート制限を考慮し、記録済みレスポンス（fixture）を用いたリプレイ方式を基本とし、実APIを叩く統合テストはローカル限定のマーカー（例: `@pytest.mark.live`）で分離する。

### 8.3 fixtures方針

- `tests/fixtures/`に、5銘柄分の株価CSV/Parquet、ファンダメンタルズJSON、ニュースJSON、EDGAR書類抜粋、FRED応答等のサンプルデータを配置する。
- pydanticモデル（`SignalHit`, `RiskAssessment`, `NewsSummary`, `FilingAnalysis`等）のfactoryヘルパーを`tests/factories.py`（または`conftest.py`）にまとめ、テスト間で再利用する。
- SQLite/DuckDBは`tmp_path`（pytest標準fixture）上に都度作成し、テスト間の状態汚染を防ぐ。

### 8.4 カバレッジ基準とE2Eスモークテスト（NFR-08）

- **カバレッジ閾値**: pytest-covによるline+branchカバレッジを全体で95%以上とする。uv-template既定の`justfile`の`test`レシピは`uv run pytest --cov=<package> --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=80`だが、本プロジェクトでは`--cov-fail-under=95`に引き上げる。`pyproject.toml`の`[tool.coverage.run]`（`branch = true`）はuv-templateの設定をそのまま踏襲する。
- **カバレッジ除外ルール**: `# pragma: no cover`の使用は`if __name__ == "__main__":`ブロックとProtocol/ABCの抽象メソッド本体（`@abstractmethod`が付与されたメソッドの本体等）のみに限定する。上記以外の箇所での`# pragma: no cover`追加、およびテストの`@pytest.mark.skip`/`@pytest.mark.xfail`によるカバレッジ回避は禁止する。
- **品質水準の意図**: 数値カバレッジはあくまで手段であり、目的は「実際にアプリを動かしたときにバグがないレベル」の品質を担保することである。そのため数値カバレッジに加えて以下のE2Eスモークテストを必須テストとして課す。
- **E2Eスモークテスト（必須）**: 外部API（yfinance/EODHD, EDGAR, Finnhub, FRED, Claude API, Discord Webhook）を全て記録済みフィクスチャ/モックに差し替えた状態で、`copilot daily`相当のコマンド（`uv run copilot-daily --dry-run`等）を一気通貫実行し、`reports/`配下にHTMLレポートが生成されるまで正常終了（終了コード0）することを検証する。8.2節の統合テスト（5銘柄smoke test）をこのE2Eスモークテストの実装基盤として用いてよいが、外部APIを一切呼ばずフィクスチャ/モックのみで完結する経路を少なくとも1つ、CI/ローカルどちらでも実行可能な形で用意すること（8.2節の実APIを叩く`@pytest.mark.live`テストとは分離する）。

---

## 9. 実装順序と受け入れ基準

### P1: FR-01, FR-02, FR-03, FR-04, FR-05, FR-06, FR-10, FR-12

P1は、`/Users/masuyama/ghq/github.com/tomada1114/uv-template` をプロジェクトディレクトリへコピーして雛形とすることから開始する（`uv init`は使用しない）。以降の実装コードは同テンプレートのruff（広範ルール+google docstring）・mypy（strict）の設定に通ることを受け入れ条件に含む。

| ステップ | 内容 | 受け入れ基準 |
|---|---|---|
| P1-1 | プロジェクト初期化（uv-templateのコピー、`pyproject.toml`, `uv`, `ruff`, `pytest`設定の確認） | `uv run pytest tests/` が終了コード0で実行できる（テスト0件でも可） |
| P1-2 | `config.py` + `settings.yaml`/`strategies.yaml`雛形 | `uv run python -c "from swing_copilot.config import load_settings; load_settings()"` が例外なく成功する |
| P1-3 | `universe.py`（FR-01） | ユニットテストでS&P500シンボルリストの取得・キャッシュ挙動を検証。`uv run pytest tests/test_universe.py` が通る |
| P1-4 | `data/base.py`, `data/yfinance_provider.py`（FR-02） | `YFinanceProvider().get_daily_bars(["AAPL"], start, end)` が仕様通りの列を持つDataFrameを返す。ユニットテスト通過 |
| P1-5 | `storage/market_store.py` | Parquet書き込み→DuckDBビュー経由の読み出しが一致する。ユニットテスト通過 |
| P1-6 | `data/edgar.py`（FR-03） | 5銘柄分のファンダメンタルズ取得がfundamentalsテーブルへupsertされる。ユニットテスト通過（EDGAR呼び出しはモック） |
| P1-7 | `screening/base.py`, `fundamental_filters.py`, `technical_signals.py`, `pipeline.py`（FR-04, FR-05） | `strategies.yaml`のデフォルト設定でScreeningPipeline.run()が既知fixtureに対し期待通りのSignalHitを返す。ユニットテスト通過 |
| P1-8 | `risk/`（FR-06） | RiskChecker.check()がsettings.yamlの閾値通りにapproved/rejectedを判定する。ユニットテスト通過 |
| P1-9 | `backtest/`（FR-10） | `uv run python -m swing_copilot.backtest.runner`（または相当のCLI）が5銘柄fixtureでSPY比較を含む結果を出力し終了コード0 |
| P1-10 | `pipeline/daily.py`（FR-12、テキスト収集/LLM/レポート/通知を除く価格〜リスクチェックまで） | `uv run copilot-daily --dry-run --skip-text --skip-llm` が終了コード0で完走し、`run_log`にステップ1〜4の記録がある |
| P1完了基準 | 全体 | `uv run pytest tests/` が通る。`uv run ruff check .` がエラー0件。`uv run mypy .`（strict）がエラー0件。line+branchカバレッジが全体95%以上（`--cov-fail-under=95`、8.4節）であること。 |

### P2: FR-07, FR-08, FR-09, FR-11

| ステップ | 内容 | 受け入れ基準 |
|---|---|---|
| P2-1 | `text/`（FR-07） | Finnhub/EDGAR filings/FREDの取得関数がユニットテスト（モック）で通る |
| P2-2 | `llm/schemas.py`, `llm/client.py`（FR-08） | `LLMClient.analyze()`のモックテストでリトライ・コスト記録（`llm_calls`挿入）を検証 |
| P2-3 | `llm/summarize.py`, `llm/filings_analysis.py`（FR-08） | 6章のプロンプトでモックレスポンスをNewsSummary/FilingAnalysisへパースできる。facts/interpretationが分離されていることをテストで検証 |
| P2-4 | `report/html_report.py`, `report/discord_notify.py`（FR-09） | `uv run copilot-daily --dry-run` が終了コード0で`reports/`にHTMLを出す。LLM結果あり/なし双方の描画パスをテスト |
| P2-5 | `paper/journal.py`（FR-11, CON-04） | `PaperJournal.record_decision()`/`close_position()`のユニットテストが通る |
| P2-6 | `pipeline/daily.py` 全9ステップ結線 | `uv run copilot-daily --dry-run` が終了コード0で完走し、`run_log`にステップ1〜9すべての記録がある。テキスト収集/LLM分析を意図的に失敗させるテストで、レポート・Discord通知が縮退版で完走することを検証（FR-12フェイルソフト） |
| P2完了基準 | 全体 | `uv run pytest tests/` が通る。`uv run copilot-daily --dry-run` が終了コード0で`reports/`にHTMLを出す。`uv run copilot-daily`（`--dry-run`なし）をローカルで手動実行し、正常完了後にレポートがデフォルトブラウザで自動的に開くことを確認できる。line+branchカバレッジが全体95%以上（`--cov-fail-under=95`）であり、8.4節のE2Eスモークテストに合格していること。 |

P3（ペーパートレード検証運用、CON-04ゲート）・P4（EODHD本番切替）は本書のスコープ外の運用フェーズであり、`docs/00_human_preparation.md`のP3/P4項目と対応する。

---

## 10. 未決事項リスト

実装着手前、または各該当ステップの着手前に確認・決定が必要な事項を以下に列挙する。

1. **解決済み: S&P500構成銘柄リストの取得元（FR-01）**: WikipediaのList of S&P 500 companiesページのテーブルをpandas.read_htmlで取得する。取得結果はconfig/universe_snapshot.csvにスナップショット保存し、取得失敗時はスナップショットへフォールバックする。手動上書き（銘柄の追加・除外リスト）はsettings.yaml（`universe.manual_include`/`universe.manual_exclude`）で可能とする（詳細は本書3.2節）。テーブル構造は実装時に要確認。
2. **解決済み: セクター分類の取得元（FR-06）**: 項目1と同じソース（Wikipediaのユニバーステーブル）のGICS Sector列を使用する（本書3.2節・3.13節参照）。
3. **edgartoolsの具体的なAPI（関数名・クラス名）**: `data/edgar.py`のシグネチャは意図ベースで記述しており、`edgartools`の実際のクラス/関数名は実装時に公式ドキュメントを確認して確定させる。
4. **EODHDの具体的なエンドポイント・認証パラメータ・レート制限**: P4実装時にEODHD公式ドキュメントを確認する（`docs/00_human_preparation.md`項目8のサポート確認結果もあわせて反映）。
5. **Claude APIの正確なレート制限・リトライパラメータ**: APIキーのTierに依存するため、実装時に`claude-api`スキル／公式ドキュメントで確認し、`LLMClient`のリトライ設計に反映する。
6. **解決済み: 35分以内（NFR-03）の実現方針**: 価格取得はyfinanceの一括ダウンロード（500銘柄バッチ）、ファンダメンタルズ更新は週1回・新規filingのみの増分更新、ニュース取得・LLM分析は保有＋候補の最大30銘柄に限定、EDGARアクセスは10リクエスト/秒上限を守るスロットリングを実装する（詳細は本書3.2, 3.4, 3.6, 3.14節および`docs/03_basic_design.md`8.3節参照）。実装後の実測に基づく追加チューニング（並列化要否等）の必要性はP1〜P2の実装時に判断する。
7. **冪等性判定の具体的なキー設計**: 「同日再実行時は取得済みデータをスキップ」の判定方法（日付ベースのマーカー、`run_log`の参照、各テーブルのUNIQUE制約への依存等の組み合わせ）を実装時に確定させる。
8. **統合テスト用5銘柄の具体的な選定**: 本書8.2節ではセクター分散を考慮した例を挙げたが、最終的な銘柄選定は実装時に確定する。
9. **監視・可視化の具体的手段（NFR-03関連）**: `run_log`の内容をどう可視化するか（レポートへの埋め込み、別途のダッシュボード等）はマスター仕様に明記がなく、実装時に簡易な方法を選定してよい。

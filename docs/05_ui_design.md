# 05. UI設計書（swing-copilot）

## 1. 文書情報

| 項目 | 内容 |
|---|---|
| システム名（仮称） | swing-copilot |
| 対象画面 | 日次レポート（`reports/{run_date}.html` / `reports/latest.html`）ただ1画面。これ以外の画面・遷移は存在しない。 |
| 利用文脈 | ユーザー（開発者本人）が朝、`uv run copilot-daily` をローカルで手動実行する。バッチ完走後、`webbrowser.open()` により当該HTMLが自動的にデフォルトブラウザの新規タブで開く。ユーザーはそこから5〜10分程度で候補銘柄（最大10件）を確認し、その日の売買判断を下す。ネットワーク接続がなくても閲覧できる（ローカルファイル＋vendored JSのみで完結）。 |
| 読者 | 本書に基づき`templates/report.html.j2`・`report/html_report.py`・`report/chart_data.py`・`assets/style.css`を実装する開発者・実装エージェント（`/goal`による自律実装を含む） |
| 前提文書 | `docs/03_basic_design.md`（コンポーネント設計）、`docs/04_detailed_design.md`（モジュール・スキーマ詳細） |
| 併走成果物 | `reports/assets/`配下に置く実HTML/CSS/JSの参照見本として、ダミーデータ入りモックアップ `docs/mockups/ui-mockup-morning-briefing.html` を作成済み。デザイントークン・構造は本書と完全一致させること。実装時はこのモックアップを見た目の正として参照する。 |
| バージョン | v1.0 |

---

## 2. デザイン原則（3つ）

### 原則1: 結論ファースト — 5分で判断できる情報階層

ページ最上部30%（ヘッダー＋市場ストリップ＋リスク警告帯＋候補サマリーテーブル）だけを見れば、その日の意思決定の8割は完了できる設計とする。銘柄別詳細カードは「サマリーで気になった銘柄だけを深掘りする」ための第2階層であり、全件を熟読する前提を置かない。LLM要約ブロックも「一言結論」を最初に太字1行で置き、根拠・全文詳細をその下に段階的に開示する（プログレッシブディスクロージャー）。

**受け入れ基準**: サマリーテーブルとリスク警告帯は、ページを開いて最初のスクロールなしの表示領域（概ね1000px高のビューポート、デスクトップブラウザ標準）内、または1回のスクロールで収まる分量に収めること。詳細カードの内容をサマリーテーブルより上に配置しない。

### 原則2: 色は3値セマンティクスのみ

上昇/下落/中立（up/down/neutral）の3値だけを意味的な色として使う。RSIの水準やスコアの高低を多段階のヒートマップ的な配色で表現することは禁止する。理由は2つ: (1)候補は最大10件・詳細指標も多いため、色数が増えるほど「どの色が何を意味するか」を都度参照する必要が生じ、5分判断という原則1に反する。(2)開発者1人が実装・保守する前提（NFR-02）で、配色ルールはシンプルであるほど実装・レビューの負荷が下がる。accent（琥珀）はブランド・警告・フォーカスの3用途に限定し、乱用しないことで「注意すべき箇所」としての希少性を保つ。

**受け入れ基準**: 実装中に「もう1色増やしたい」という誘惑が生じた場合は、既存の3値（up/down/neutral）とアイコン・テキストの組み合わせで表現できないか先に検討すること。数値セルの背景色を段階的に変える実装（ヒートマップテーブル）は行わない。

### 原則3: 静的・自己完結・サーバーレス

レポートはJinja2が生成する静的HTML1枚であり、生成後はサーバーもAPI呼び出しも介さない。外部CDN・Webフォント・トラッキングスクリプトへの参照は一切持たない（オフライン閲覧・長期アーカイブ耐性のため）。唯一の外部相当リソースであるチャートライブラリ（TradingView Lightweight Charts）も`assets/`へvendoredし相対パスで参照する。1日1回使い捨てで生成されるドキュメントである以上、SPA的な状態管理・ルーティング・派手な演出は追求せず、`<details>`の開閉とホバーに限定したごく軽微なインタラクションのみを実装する。

**受け入れ基準**: `templates/report.html.j2`が生成したHTMLをネットワーク切断状態のブラウザで開いても、チャート描画を含め全機能が正常に動作すること（`<script src>`・`<link>`はすべて`assets/`への相対パスであり、`http(s)://`で始まる参照を含まないこと）。

---

## 3. カラーシステム

### 3.1 トークン定義

| トークン | 値 | 用途 |
|---|---|---|
| `--bg` | `#0E1116` | ページ背景（青みがかったニアブラック） |
| `--surface` | `#161B22` | カード・テーブルの背景 |
| `--surface-2` | `#1C232C` | ホバー時・ネストした面（テーブル行ホバー、バッジ背景等） |
| `--border` | `#232A33` | ヘアライン罫線（カード枠・テーブル罫線・区切り線） |
| `--text` | `#E6E8EB` | 本文・主要な数値 |
| `--text-dim` | `#9AA4B2` | 補助テキスト、ラベル、メタ情報、非アクティブ状態 |
| `--up` | `#2EBD85` | 上昇・買いシグナル方向 |
| `--down` | `#EF5350` | 下落・弱シグナル方向 |
| `--neutral` | `#8B949E` | 中立（変化なし相当） |
| `--accent` | `#E8B45A` | ブランド（eyebrow）・警告帯・フォーカスリングのみ |

### 3.2 使用ルール

- **色の意味は3値（up/down/neutral）に固定する**。4値目以降の意味的な色（例: 「やや上昇」「強い下落」を別の色で塗り分ける）は導入しない。強弱は色ではなく数値そのもの・矢印記号・太さで表現する。
- **`--accent`のホワイトリスト**: (1) ヘッダーのeyebrowテキスト「SWING COPILOT」、(2) リスク警告帯の左ボーダーおよびアイコン、(3) フォーカス時のアウトライン、(4) `<details>`のsummaryホバー等ごく軽微な強調。この4箇所以外（本文リンク色、通常ボタン、通常の見出し）には使わない。
- **抽出理由バッジ（例: SMA200上抜け／RSI押し目／決算サプライズ）は常にニュートラル配色**（背景`--surface-2`、文字`--text-dim`、枠`--border`のpill）とする。抽出理由は「方向性」ではなく「カテゴリ」を表す情報のため、up/down色は使わない（原則2参照）。
- **`--up`/`--down`は「値そのものの正負」にのみ連動させる**（前日比%、リターン、ポジション損益等）。判断が難しい派生指標（例: RSI・ATRの絶対値、スコア）はセマンティックカラーを付けず`--text`のプレーン表示とする。

### 3.3 up/down/neutral判定ルール（実装への受け入れ基準）

すべての前日比・変化率セル（市場ストリップ、サマリーテーブルの前日比%列、スパークライン、詳細カードの当日騰落表示）に共通で以下の閾値を適用する。

| 条件 | 適用トークン |
|---|---|
| 変化率 ≥ +0.1% | `--up` |
| 変化率 ≤ −0.1% | `--down` |
| −0.1% 〜 +0.1%未満（絶対値0.1%未満） | `--neutral` |

**受け入れ基準**: サマリーテーブルの前日比%セルは、値が正なら`--up`、負なら`--down`、±0.1%未満なら`--neutral`を適用する。この判定関数（例: `classify_change(pct: float) -> Literal["up","down","neutral"]`）は`report/html_report.py`側に1箇所だけ実装し、市場ストリップ・サマリーテーブル・詳細カード・スパークラインの全箇所から共通利用すること（閾値のハードコードを複数箇所に分散させない）。

**受け入れ基準（VIX等の逆相関資産の扱い）**: VIXのように「下落が強気シグナル」と解釈されうる指数であっても、色判定は当日比の符号のみで機械的に行い、意味論的な反転（VIX下落を`--up`にする等）は行わない。実装をシンプルに保ち、色の意味を「上昇/下落/中立」の1系統に統一するため（原則2）。

---

## 4. タイポグラフィ

### 4.1 フォントスタック

| 用途 | スタック |
|---|---|
| 見出し・本文 | `-apple-system, BlinkMacSystemFont, "Hiragino Sans", "Segoe UI", system-ui, sans-serif` |
| 数値・ティッカーシンボル | `ui-monospace, "SF Mono", "Cascadia Mono", "Menlo", monospace` + `font-variant-numeric: tabular-nums;` |

**受け入れ基準**: 価格・パーセンテージ・RSI/ATR等の数値を表示するすべての要素（テーブルの数値セル、カードのメトリクス値、ティッカーシンボル）は数値・ティッカースタックを適用し、`font-variant-numeric: tabular-nums`を必ず指定すること（桁揃えのため）。日本語・英単語の地の文には適用しない。

### 4.2 タイプスケール

| 役割 | サイズ | ウェイト | 行間 | 字間 | スタック | 備考 |
|---|---|---|---|---|---|---|
| Eyebrow（`SWING COPILOT`） | 11px | 700 | 1.2 | 0.14em | 見出し | 大文字、`--accent`色 |
| ページタイトル（H1） | 22px | 600 | 1.3 | 0 | 見出し | 「Morning Briefing — YYYY-MM-DD (Dow)」 |
| メタ行（実行時刻等） | 12px | 400 | 1.5 | 0 | 数値スタック（時刻・銘柄数を含むため） | `--text-dim` |
| セクションラベル（H2相当） | 12px | 600 | 1.4 | 0.08em | 見出し | 大文字、`--text-dim`。「MARKET」「CANDIDATES」等の帯 |
| ティッカー（カード見出し） | 26px | 700 | 1.1 | 0.01em | 数値スタック | 例: `AAPL` |
| 社名・セクター（カード見出し） | 13px | 400 | 1.4 | 0 | 見出し | `--text-dim` |
| テーブルヘッダー | 11px | 600 | 1.3 | 0.04em | 見出し | 大文字、`--text-dim` |
| テーブル・カード本文数値 | 13px | 500 | 1.4 | 0 | 数値スタック | tabular-nums必須 |
| 本文（LLM要約・根拠等） | 13.5px | 400 | 1.7 | 0 | 見出し | 日本語文章のため行間広め |
| 一言結論（LLM要約の強調行） | 15px | 600 | 1.5 | 0 | 見出し | `--text` |
| 小・メタ（出典URL、フッター） | 11px | 400 | 1.5 | 0 | 見出し | `--text-dim` |
| バッジ文字 | 11px | 600 | 1 | 0.01em | 見出し | pill内 |

---

## 5. スペーシング・レイアウト

### 5.1 8pxグリッド

余白・gapは以下のスケールのみを使用する: `4 / 8 / 12 / 16 / 24 / 32 / 48 / 64` (px)。中間値（例: 10px, 20px）は使わない。

### 5.2 コンテンツ幅

- コンテンツ最大幅: `1200px`、`margin: 0 auto`で中央寄せ。
- ページ左右パディング: デスクトップで`32px`。
- セクション間の縦マージン: `48px`。カード内部の要素間は`16px`〜`24px`。

### 5.3 テーブル

- 候補サマリーテーブルは横スクロールを許容する。テーブル本体に`min-width: 960px`程度を与えた上で、外側を`overflow-x: auto`のラッパー`<div class="table-scroll">`で囲む（ウィンドウを狭めても列が潰れて数値が読めなくなるより、横スクロールの方が数値の可読性を優先する原則2・4と整合する）。
- 行の高さは`44px`前後を目安とし、10行（最大候補数）が並んでも視認性を保つ。

### 5.4 詳細カード内3カラムグリッド

テクニカル / ファンダメンタル / リスク計算の3ブロックは`display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px;`とする。デスクトップ最適化のため、狭幅への収縮対応（1カラム化のブレークポイント）は必須要件としない（任意実装可）。

---

## 6. コンポーネント仕様

### 6.1 バッジ（抽出理由バッジ / シグナルバッジ）

| 状態 | 背景 | 文字色 | 枠線 | 形状 |
|---|---|---|---|---|
| 通常（全種共通） | `--surface-2` | `--text-dim` | 1px `--border` | pill（`border-radius: 999px`）、padding `4px 10px` |

抽出理由バッジの文言は`SignalHit.signal_name`から日本語ラベルへの1:1マッピングとする。マッピング表は`report/html_report.py`内に定数として保持し、`screening/technical_signals.py`・`screening/fundamental_filters.py`へ新しいFilter/Signalが`@register_signal`/`@register_filter`で追加された際（NFR-07）は、このマッピング表にも1エントリ追加することを実装ルールとする。

| `signal_name` | バッジ表示ラベル |
|---|---|
| `trend_sma` | SMA200上抜け |
| `pullback_rsi` | RSI押し目 |
| `volume_min` | （出来高フィルタは基礎条件のためバッジ化しない。サマリー表には表示せず内部フィルタとしてのみ機能） |
| （将来追加、例） | 決算サプライズ 等、`docs/04_detailed_design.md`のFR-08拡張やFilter/Signal追加に応じてマッピング表を追記する |

**受け入れ基準**: バッジは常にテキストラベルを伴い、色のみで意味を伝えない（アクセシビリティ、9章参照）。1銘柄が複数シグナルにヒットした場合、バッジは複数個を横並びで表示する（優先度・上限数は実装時に決めてよいが、最低1つは必ず表示する）。

### 6.2 前日比・変化率セル

| 状態 | 文字色 | 付随表現 |
|---|---|---|
| 上昇（≥+0.1%） | `--up` | `+` 符号を必ず表示（例: `+1.24%`） |
| 下落（≤−0.1%） | `--down` | `−` 符号を必ず表示（例: `−0.68%`） |
| 中立（±0.1%未満） | `--neutral` | `±0.00%`のように符号または`±`を表示 |

色だけに依存せず、符号（`+`/`−`/`±`）を必ず文字として併記する（9章、色覚多様性対応）。

### 6.3 テーブル（候補サマリーテーブル）

- ヘッダー行: `--surface`背景、`--text-dim`文字、下罫線1px `--border`。
- ボディ行: 背景`--surface`、行間罫線1px `--border`（最終行は罫線なし）。
- **ホバー**: 行全体の背景を`--surface-2`に変化（`transition: background-color 120ms ease`）。
- 詳細へのアンカーリンク列（`→`等の記号 + 「詳細」テキスト）は`--text-dim`、ホバー時`--text`。

### 6.4 銘柄別詳細カード

- 背景`--surface`、枠線1px `--border`、`border-radius: 8px`、内部padding `24px`。
- カードのアンカーID（サマリーテーブルからのジャンプ先）は`id="card-{ticker}"`とする。
- カードヘッダー: ティッカー（数値スタック26px 700）、社名・セクター（下段、`--text-dim`）、右側に抽出理由バッジ群。

### 6.5 リスク警告帯

- 左ボーダー4px `--accent`、背景 `--accent`を8%不透明度で敷いた`--surface`との合成（実装値目安: `rgba(232, 180, 90, 0.08)`）、padding `12px 16px`、`border-radius: 8px`（左端は角丸なしでよい）。
- 先頭に警告アイコン（インラインSVGの三角アイコン、`--accent`色）+ 本文（`--text`、13.5px）。
- **表示条件**: `RiskAssessment.warnings`（`CorrelationWarning`）が1件以上存在する場合にのみ、このセクション自体をDOMに出力する。0件の日は`<section>`ごと省略し、`display:none`等での隠蔽は行わない（不要なDOM・空白を残さない）。
- 複数件ある場合は1件ごとに1行、リスト（`<ul>`）として並べる。

### 6.6 スパークライン

- インラインSVG、目安サイズ`96×28px`、直近20営業日の終値を単純な折れ線（`<polyline>`）で描画する。
- 線の色は「直近20日の始点終値→終点終値」の変化率に対し、6.2節と同じup/down/neutral判定を適用する（3値のみ、途中経過のグラデーションは付けない）。
- 軸・目盛・グリッドは描画しない（サマリーテーブル内の補助情報のため最小限に留める）。

### 6.7 `<details>`ブロック（LLM要約の全文・出典）

- ネイティブ`<details>`/`<summary>`要素を使用する（JS実装のアコーディオンは使わない。ブラウザ標準機能で十分かつキーボード操作・アクセシビリティを無償で得られるため）。
- `summary`のスタイル: `cursor: pointer`、`color: --text-dim`、右側に開閉を示すシェブロン（CSSまたはインラインSVG）。開閉時のシェブロン回転のみ`transition: transform 150ms ease`を許容する（アニメーションは最小限、9章参照）。
- 開いた状態のコンテンツ（全文・出典リンク一覧）は`padding-top: 12px`、出典URLは`--text-dim`のリストで表示し、リンクは新規タブ（`target="_blank" rel="noopener"`）で開く。

---

## 7. 画面仕様

日次レポートは以下の順で1画面に構成する。各セクションのデータソース（供給モジュール）を併記する。

### 7.1 ヘッダー

- 内容: eyebrow「SWING COPILOT」（`--accent`、letter-spacing）+ ページタイトル「Morning Briefing — {run_date} ({曜日})」+ メタ行（実行時刻、データ鮮度＝最終株価取得タイムスタンプ、ユニバース銘柄数）。
- データソース: `pipeline/daily.py`の実行メタ（`run_date`、各ステップの`duration_s`合計等から算出する実行時刻）← `run_log`（StateStore）。ユニバース銘柄数 ← `universe.py`（`get_sp500_symbols()`の件数、`config/universe_snapshot.csv`）。

### 7.2 市場全体感ストリップ

- 内容: SPY / QQQ / VIX / US10Yの現在値＋前日比バッジを横並びで表示。
- データソース: `data/*_provider.py`（`DataProvider.get_universe_prices_latest()`相当）← `storage/market_store.py`。VIX・US10Yのシンボル・データ取得方法（株価APIで代替可能なティッカーを使うか、専用ソースを要するか）は`docs/04_detailed_design.md`の未決事項リストに準じ実装時に要確認とする。
- 色判定: 3.3節のup/down/neutralルールを適用。

### 7.3 リスク警告帯（該当時のみ表示）

- データソース: `risk/checks.py`（`RiskChecker.check_correlation()`が返す`CorrelationWarning`、`RiskAssessment.warnings`）。
- 表示ロジック: 6.5節参照。

### 7.4 候補サマリーテーブル

- 列: `#` / ティッカー+社名 / 抽出理由バッジ / 終値 / 前日比% / RSI14 / ATR14 / スコア / スパークライン(直近20日) / 詳細へのアンカーリンク。
- データソース: `screening/pipeline.py`（`ScreeningPipeline.run()`が返す`SignalHit`一覧、`signal_name`・`context`）。RSI14/ATR14は`SignalHit.context`（`screening/technical_signals.py`が計算時に格納する値）から取得する。スコアは銘柄単位に複数`SignalHit`を集約した合成値とし、集約ロジック（重み付け等）は`report/html_report.py`側で定義する（`docs/04_detailed_design.md`にスコア集約の仕様が未記載のため、実装時に確定してよい設計判断ポイントとして明記する）。
- 受け入れ基準: 6.3節参照。行数は最大10件（要件上のユニバース候補上限）。

### 7.5 銘柄別詳細カード

サマリーテーブルの各行から`#card-{ticker}`へアンカージャンプする。カードごとに以下を含む。

| ブロック | データソース |
|---|---|
| カードヘッダー（ティッカー/社名/セクター/抽出理由バッジ） | `screening/pipeline.py`の`SignalHit`、セクターは`universe.py`（GICSセクター、`config/universe_snapshot.csv`） |
| チャート（ローソク足6ヶ月＋SMA50/SMA200＋出来高） | 新設`report/chart_data.py`が`storage/market_store.py`（`MarketStore.read_bars()`）から直近6ヶ月分のOHLCVを読み出し、SMA50/200を算出してJSON化（8章参照） |
| テクニカル指標値 | `SignalHit.context`（RSI14、ATR14、SMA50/200等、`screening/technical_signals.py`） |
| ファンダメンタル値（PER, FCF, 自己資本比率, 直近EPS等） | `data/edgar.py`の`FundamentalsRecord`（`storage/market_store.py`の`fundamentals`テーブル） |
| リスク計算（想定ポジションサイズ、ATRベースストップ目安価格、想定リスク%） | `risk/position_sizing.py`の`calc_position_size()`、`risk/checks.py`の`RiskAssessment` |
| LLM要約ブロック | `llm/summarize.py`の`NewsSummary`（ニュース由来）＋`llm/filings_analysis.py`の`FilingAnalysis`（決算書由来）。一言結論はこの2つの`interpretation`から`report/html_report.py`が要約表示用に1行抽出・整形する（具体的な抽出ロジックは実装時に確定）。出典リンクは`NewsSummary.sources`（URL） |

**受け入れ基準（フェイルソフト整合）**: `news_summaries`/`filing_analyses`が`None`（`docs/03_basic_design.md`のフェイルソフト、ステップ5/6失敗時）の場合、LLM要約ブロックは「本日はニュース・開示分析を取得できませんでした」等の縮退表示とし、カードの他ブロック（テクニカル・ファンダメンタル・リスク・チャート）は通常通り描画する。カード自体を非表示にはしない。

### 7.6 フッター

- 内容: 免責文言「本レポートは情報提供のみを目的とし、投資助言ではありません。最終判断は自身で行ってください」+ TradingView attribution（"Charting by TradingView" テキストリンク、`https://www.tradingview.com/`）+ 生成メタ（バージョン、実行ID）。
- データソース: 免責・attributionは静的文言（テンプレート固定文）。生成メタ（バージョン、実行ID）← `report/html_report.py`（実行ID=`run_log`の`run_date`または一意な`run_log_id`起点の識別子）。
- 前日/翌日レポートへのナビリンク: `reports/`配下に存在するファイル名（日付）を`report/html_report.py`が走査し、存在すれば相対リンクを生成する。存在しない場合はリンク自体を出力しない（無効リンクを残さない）。

---

## 8. チャート仕様（TradingView Lightweight Charts v5）

### 8.1 ライブラリ配置

- `assets/lightweight-charts.standalone.production.js`（Apache License 2.0）を1回だけvendoredし、各日次レポートはこれを相対パス（`<script src="assets/lightweight-charts.standalone.production.js"></script>`）で参照する。インライン埋め込みは行わない（レポートファイル自体の肥大化・リポジトリサイズ増加を防ぐため、CON準拠の`data/`/`reports/`コミット運用と整合）。

### 8.2 チャート構成

各詳細カードにつき1チャート、以下3系列を1つの`createChart`インスタンス上に重ねる。

| 系列 | Lightweight Charts API | 内容 |
|---|---|---|
| ローソク足 | `addCandlestickSeries`（v5の該当API、実装時に正式メソッド名を公式ドキュメントで要確認） | 直近6ヶ月の日足OHLC |
| SMA50 | `addLineSeries` | `screening/technical_signals.py`と同一ロジックで計算した50日単純移動平均 |
| SMA200 | `addLineSeries` | 同200日単純移動平均 |
| 出来高 | `addHistogramSeries`（別スケール、`priceScaleId`をメインと分離し下部に表示） | 日次出来高、当日の陽線/陰線に応じた色 |

### 8.3 ダークテーマ設定値

```js
chart.applyOptions({
  layout: {
    background: { type: 'solid', color: 'transparent' }, // ページ背景(--bg)を透過して見せる
    textColor: '#9AA4B2', // --text-dim
  },
  grid: {
    vertLines: { color: '#1E242C' },
    horzLines: { color: '#1E242C' },
  },
  timeScale: { borderColor: '#232A33' }, // --border
  rightPriceScale: { borderColor: '#232A33' },
});

candleSeries.applyOptions({
  upColor: '#2EBD85',        // --up
  downColor: '#EF5350',      // --down
  wickUpColor: '#2EBD85',
  wickDownColor: '#EF5350',
  borderVisible: false,
});

volumeSeries.applyOptions({
  color: '#8B949E', // --neutral をベースに、実装時は当日陽線/陰線に応じ --up/--down を個別バーに設定
});
```

**SMA線の配色に関する例外（設計判断）**: 3.2節の「セマンティックカラーはup/down/neutralの3値のみ」という原則は、意味を持つ状態表示（バッジ・セル・警告）に適用されるルールである。チャート上でSMA50とSMA200という**2本の異なる系列を視覚的に区別する**ことは、それ自体が「上昇/下落」を意味しないため、この原則の対象外の実装上の必要事項として扱う。区別のため、この2系列専用の非セマンティックな補助トークンを新設する。

| トークン | 値 | 用途 |
|---|---|---|
| `--chart-sma-fast` | `#6FA8DC`（ミュートブルー） | SMA50ライン |
| `--chart-sma-slow` | `#C79FEF`（ミュートパープル） | SMA200ライン |

この2色は`--up`/`--down`/`--accent`と明確に区別できる色相とし、チャートコンポーネント以外（バッジ・テーブル・警告帯等）では使用しない。

### 8.4 テンプレートへ渡すJSONデータ構造

`report/chart_data.py`が銘柄ごとに以下の構造を生成し、Jinja2テンプレート内に`<script type="application/json" id="chart-data-{ticker}">`として埋め込む（インラインスクリプトタグ内のJSONであり、外部fetchは行わない＝原則3の自己完結性を満たす）。

```json
{
  "symbol": "AAPL",
  "ohlcv": [
    { "time": "2026-01-05", "open": 224.10, "high": 226.40, "low": 223.55, "close": 225.80, "volume": 48213000 }
  ],
  "sma50": [
    { "time": "2026-01-05", "value": 221.34 }
  ],
  "sma200": [
    { "time": "2026-01-05", "value": 210.02 }
  ]
}
```

- `time`はLightweight Charts v5が受け付ける`yyyy-mm-dd`形式の営業日文字列とする（日足のため`BusinessDay`型は使わず文字列形式で統一、実装時に公式ドキュメントで型互換を要確認）。
- `sma50`/`sma200`はSMA計算に必要な直近分（200日分の追加バッファ）を`ohlcv`より手前に遡って計算した上で、表示期間（直近6ヶ月）に対応する範囲のみを出力してよい。データ不足でSMA200が計算できない期間（例: 上場から日が浅い銘柄）は該当日の`value`を出力しない（欠落させる。0埋め等のフォールバックはしない）。

**受け入れ基準**: `render_report()`（`report/html_report.py`）は、`report/chart_data.py`が生成したJSON構造を各カードのテンプレートコンテキストに`chart_data: dict[str, ChartData]`（銘柄→構造）として渡す。テンプレート側は銘柄ごとに`<script>`初期化ブロックで`createChart`を呼び出し、対応するチャート用`<div id="chart-{ticker}">`にマウントする。

### 8.5 Attribution表示義務

Lightweight Chartsの利用規約（TradingView製品を利用したチャートである旨の表示義務）を満たすため、フッター（7.6節）に"Charting by TradingView"のテキストリンク（`https://www.tradingview.com/`）を常設する。個々のチャート直下への表示は必須としない（サイト全体で1箇所、フッターに集約する運用とする。本方針は背景情報として確定済みの構成に基づく）。

---

## 9. アニメーション方針・アクセシビリティ

### 9.1 アニメーション方針（最小限）

許容するアニメーションは以下のみとする。

| 対象 | 効果 | 時間 |
|---|---|---|
| テーブル行・カードのホバー | 背景色遷移 | 120ms ease |
| `<details>`のシェブロン開閉 | `transform: rotate()` | 150ms ease |
| フォーカスリング出現 | なし（即時表示、遅延させない） | - |

ページロード時のフェードイン・スクロール連動演出・パララックス等は実装しない（原則3）。

### 9.2 アクセシビリティ

- **コントラスト比**: 本文色`--text`(#E6E8EB)は背景`--bg`(#0E1116)に対し約17:1のコントラスト比を持ち、WCAG AA（通常文字4.5:1以上）を大きく上回る。`--text-dim`(#9AA4B2)は背景`--bg`に対し約8:1、`--surface`(#161B22)に対し約6.8:1であり、いずれもAA基準（4.5:1）を満たす。**受け入れ基準**: `--text-dim`を本文の主要な情報伝達に使う場合（例: 一言結論やfacts等の重要情報）は避け、ラベル・メタ情報用途に限定すること。
- **フォーカス可視**: すべてのインタラクティブ要素（アンカーリンク、`<summary>`、外部リンク）に`:focus-visible`スタイルとして`outline: 2px solid var(--accent); outline-offset: 2px;`を適用する。`outline: none`によるフォーカスリングの削除は行わない。
- **色だけに依存しない**: 6.1節・6.2節の通り、バッジは常にテキストラベルを伴い、前日比セルは常に`+`/`−`/`±`の符号を文字として併記する。up/down/neutralの判別を色のみに頼る箇所を作らない。
- **代替テキスト**: チャート・スパークラインのSVGには`role="img"`と`aria-label`（例: 「ローソク足チャート、6ヶ月分」）を付与する。装飾目的のみのアイコン（警告アイコン等）は`aria-hidden="true"`とする。

---

## 10. 実装への影響（`docs/04_detailed_design.md`への追記事項）

以下は本書の内容を実装可能にするため、詳細設計書（`docs/04_detailed_design.md`）の3章（モジュール別詳細）・2章（リポジトリ構成）に追記すべき事項である。

### 10.1 新規モジュール `report/chart_data.py`

```python
from datetime import date
from pydantic import BaseModel

class OHLCVPoint(BaseModel):
    time: str   # "yyyy-mm-dd"
    open: float
    high: float
    low: float
    close: float
    volume: int

class SMAPoint(BaseModel):
    time: str
    value: float

class ChartData(BaseModel):
    symbol: str
    ohlcv: list[OHLCVPoint]
    sma50: list[SMAPoint]
    sma200: list[SMAPoint]

def build_chart_data(symbol: str, market_store: "MarketStore", as_of: date, lookback_months: int = 6) -> ChartData:
    """
    market_store.read_bars() から SMA200計算に必要な遡及バッファを含めて日足を読み出し、
    直近lookback_monthsのOHLCVと、その期間に対応するSMA50/SMA200を算出してChartDataを返す。
    SMA計算はscreening/technical_signals.pyと同一のTA-Lib呼び出し（talib.SMA）を再利用し、
    スクリーニング結果とチャート表示のSMA値がロジック的に一致することを保証する。
    """
```

**依存**: `storage/market_store.py`、`talib`（`screening/technical_signals.py`と共有）
**呼び出し元**: `report/html_report.py`（`render_report()`が候補銘柄ごとに呼び出し、テンプレートコンテキストへ`chart_data`として渡す）

### 10.2 `report/html_report.py`への追記

- `render_report()`のテンプレートコンテキストに`chart_data: dict[str, ChartData]`（8.4節）、`market_strip: MarketStripData`（7.2節、SPY/QQQ/VIX/US10Y）、`correlation_warnings: list[CorrelationWarning]`（7.3節）を追加する。
- 6.1節のシグナルバッジ日本語マッピング表、3.3節の`classify_change()`（up/down/neutral判定）をこのモジュール内のヘルパー関数として実装する。
- `render_report()`実行後、`reports/{run_date}.html`保存に加え`reports/latest.html`へ同内容をコピーする（ファイル出力構成、下記10.4節）。

### 10.3 `webbrowser.open()`によるローカル自動表示

`pipeline/daily.py`の`run_daily()`内、レポート生成（ステップ7）完了直後に以下を追加する。

```python
import os, webbrowser

def _maybe_open_report(report_path: Path) -> None:
    """
    ローカル実行時（GitHub Actions等のCI環境ではない場合）のみ、
    生成されたレポートをデフォルトブラウザで自動的に開く。
    CI判定は環境変数 GITHUB_ACTIONS の有無で行う（GitHub Actionsが自動設定する変数）。
    """
    if os.environ.get("GITHUB_ACTIONS"):
        return
    webbrowser.open(report_path.resolve().as_uri())
```

**受け入れ基準**: ローカルで`uv run copilot-daily`を実行した場合、レポート生成成功後に自動でブラウザタブが開く。GitHub Actions環境（`GITHUB_ACTIONS=true`が自動設定される）では`webbrowser.open()`を呼び出さない（ヘッドレス環境でのエラー・ハングを避ける）。

### 10.4 ファイル出力構成の確定（`docs/04_detailed_design.md` 2章への追記）

```
reports/
  2026-07-21.html      # 日次レポート（Jinja2生成、report/html_report.py）
  latest.html          # 最新版のコピー（同一内容、固定パスでブックマーク可能にする）
  assets/
    lightweight-charts.standalone.production.js  # vendored、10.5節のscriptで一度だけ配置
    style.css          # 共通スタイル（本書3〜6章のトークン・コンポーネント定義）
```

各レポートHTMLは`assets/lightweight-charts.standalone.production.js`・`assets/style.css`を相対パス（`assets/...`）で参照する。日付ごとのHTMLファイルをインライン化しない理由は、リポジトリが日次でコミットされる運用（`docs/03_basic_design.md` 8.2節）のため、同一アセットの重複コミットによるリポジトリ肥大化を避けるためである。

### 10.5 vendored JS取得の自動化 `scripts/fetch_assets.py`

vendoredライブラリの配置は人間の手作業ではなく、以下のセットアップスクリプトで自動化する。

```python
"""
scripts/fetch_assets.py

TradingView Lightweight Charts v5 の standalone production ビルドを取得し、
reports/assets/lightweight-charts.standalone.production.js として配置する。

実行方法: uv run python scripts/fetch_assets.py
取得元: npm配布物（unpkg等のCDN経由でのダウンロード。具体的なURL・バージョン
ピン方法は実装時にLightweight Charts公式ドキュメント・npmパッケージ情報を
要確認）。取得したファイルの先頭にバージョン番号・ライセンス(Apache License 2.0)・
取得日をコメントとして追記する。
このスクリプトは初回セットアップ時、またはライブラリバージョン更新時にのみ
再実行する（日次バッチの一部ではない）。
"""
```

**受け入れ基準**: `uv run python scripts/fetch_assets.py`を実行すると`reports/assets/lightweight-charts.standalone.production.js`が生成され、`uv run copilot-daily`はこのファイルの存在を前提として動作する（ファイル不在時はレポート生成ステップでチャートが描画できない旨を明示するエラーメッセージを出す）。日次バッチ自体はネットワークからJSをダウンロードしない（原則3の自己完結性、および日次実行時間NFR-03を圧迫しないため）。

---

## 11. 自己QAサマリー

本書執筆にあたり以下を確認した。

- カラートークン10種はすべて背景資料の値と完全一致させた（独自の追加・変更なし。8.3節のSMA専用2色のみ、意図的な範囲限定の追加として明示した）。
- ページ構成6セクションの順序は背景資料の指定順と一致させた。
- 各セクションのデータソースは`docs/03_basic_design.md`・`docs/04_detailed_design.md`の実在モジュール名・スキーマ名（`SignalHit`, `RiskAssessment`, `CorrelationWarning`, `NewsSummary`, `FilingAnalysis`, `FundamentalsRecord`等）とすべて突き合わせ、存在しないモジュールを参照していないことを確認した。
- スコア集約ロジックの詳細、VIX/US10Yの具体的データ取得元の2点は既存設計書に定めがなく、本書で「実装時に確定してよい設計判断ポイント」として明示した（未決事項の隠蔽をしない）。

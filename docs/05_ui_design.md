# 05. CLI・Markdown・ダッシュボード出力設計

## 1. 目的

日次バッチの主表示はブラウザではなくターミナルとする。利用者が朝の5〜10分で候補を確認できることを優先する。日次runがブラウザを自動起動することはなく、run成果物としてのHTMLも生成しない。

蓄積された判定・追跡データを視覚的に振り返るために、読み取り専用のローカルダッシュボード`copilot-dashboard`を併設する（本書8節）。これは日次runの一部ではなく、利用者が必要なときだけ起動する別プロセスのビューアである。

構造化データの正本はDuckDBである。Markdownは人間が後から読み返すための再生成可能な派生成果物であり、手編集したMarkdownをアプリケーションへ取り込まない。

## 2. 出力境界

| 出力 | 用途 | 宛先 |
|---|---|---|
| 進捗ログ | ステップの開始・終了・縮退理由・所要時間 | stderr |
| 日次ブリーフ | 当日の意思決定支援 | stdout |
| Markdown | 人間向け監査スナップショット | `reports/<run_date>/<run_id>.md` |
| 最新版 | 直近runへの固定パス | `reports/latest.md` |
| 分析入力 | 定性分析スキルへの唯一の入力・監査証跡 | `reports/<run_date>/<run_id>/analysis_input.json` |
| 分析結果 | スキルの回答・監査証跡（スキルが書く） | `reports/<run_date>/<run_id>/analysis_result.json` |
| 再描画用context | ingest時に同じブリーフを再現するスナップショット | `reports/<run_date>/report_context.json` |
| 落選記録 | 落選銘柄の明細とcandidate_limit切り捨ての明細 | `reports/<run_date>/<run_id>/rejections.json` |

`--dry-run`は`data/copilot_dry_run.duckdb`と`reports/dry_run/`へ隔離する。通知は送らないが、ターミナル表示とMarkdown生成は通常runと同じ契約で行う。

## 3. 共通表示モデル

`report/daily_brief.py`の`DailyBrief`をターミナルとMarkdownの共通入力にする。各rendererはデータ取得、指標計算、リスク判断を行わない。

```text
MarketStore / StateStore / Pipeline values
                  |
                  v
             DailyBrief
              /      \
             v        v
      terminal renderer  Markdown renderer
```

`DailyBrief`は次を保持する。

- `run_id`, `run_date`, `generated_at`
- SPY、QQQ、VIX、US10Yの値と前日比
- 候補順位、銘柄、終値、前日比、RSI、ATR、シグナル
- ファンダメンタル表示値
- リスク判定、指値、逆指値、1R、理由、warnings
- 定性分析の結論、facts、risk flags、source IDとURL
- 銘柄ごとのverdict（`proceed`／`skip`）とその要約
- 公開対象の`proceed`推奨の追跡状態（現在値、損益、逆指値、残り営業日）
- run全体の`no_trade`フラグと理由
- テキスト・分析・通知の縮退理由（分析未実施、分析対象外、検証不合格を区別する）

追跡一覧は台帳更新後のスナップショットであり、当日runで新しく判定されたverdictは
翌runの`tracking/update`で建玉されてから掲載対象になる。

すべての市場・財務読み取りは`run_date`を明示的な`as_of`として渡し、境界をinclusiveにする。

## 4. ターミナル表示

表示順は次のとおり。

1. 日付、run status、候補数、run ID
2. 市場概況
3. 市場レジーム、exposure ceiling、実行バケット。REDUCE_ONLYは警戒見出しと理由を表示し、CASH_PRIORITYは全候補を「見送り（地合い）」に置く
4. 候補比較テーブル
5. 候補ごとの定性分析結論、verdict行、リスク警告、source ID
6. 追跡中の推奨一覧
7. run全体の警告
8. 詳細レポートパス

候補表は最大10件を前提とし、順位、銘柄、終値、前日比、スコア、1R、ストップ、指値の8列を日本語ヘッダと罫線付きで表示する。指値は翌営業日の計画上限、1Rは指値から逆指値までの距離率である。読者の口座や保有を前提にした株数、Portfolio risk、Circuit Breakerは表示しない。実行状態は実行バケット行で、リスク警告は候補別詳細で表示する。落選サマリはターミナルには表示せず、監査用のMarkdownレポートだけに保持する。出力末尾には詳細レポートのパスと、`analysis_input.json`を書き出した場合はそのパスを表示する。詳細なfactsとURLはMarkdownへ保存し、ターミナルでは結論ファーストにする。

`no_trade`が真のときは、ヘッダ直後に「本日は取引なし（定性判断）」と理由を1行で強調表示する。候補別詳細では「定性」の結論行の直下に、`skip`なら`⚠ 定性: 見送り推奨（理由）`、`proceed`なら`✓ 定性: 懸念なし`のverdict行を出す。分析が未実施・対象外・検証不合格の候補ではverdict行そのものを出さない——沈黙が「懸念なし」と読まれてはならないためである。結論行は状態に応じて「分析待ち（swing-daily スキルで分析を実行してください）」「定性分析なし」「検証不合格のため非表示」を出し分ける。

Richは幅計算・日本語折り返し・TTY色制御にだけ使用する。CLI引数解析はargparseを維持する。非TTYまたはテストでは色を無効化し、安定したプレーンテキストを返す。

## 5. Markdown保存

保存先は日付だけでなく`run_id`を含める。同日再実行は別ファイルになり、`latest.md`のみ直近内容へ置換される。

```text
reports/
├── latest.md
└── 2026-07-22/
    ├── <run-id-1>.md
    └── <run-id-2>.md
```

書き込みは宛先と同じディレクトリの一時ファイルへ全内容を書いた後、`Path.replace()`で原子的に置換する。失敗時は以前の宛先を保ち、一時ファイルを削除する。

Markdown冒頭にはDuckDBが正本であることをコメントで明記する。本文には市場、候補一覧、銘柄別詳細、verdict、追跡中の推奨一覧、定性評価（強み・懸念）、facts、risk flags、開示分析（書類種別と提出日で識別）、source URL、警告、免責文を含める。

各Markdownと同じ`run_id`の監査ファイルは`reports/<run_date>/<run_id>/`に置く。ここには`analysis_input.json`（分析へ渡した入力、schema `analysis-input-v3`。開示ごとのcoverageを含む）、`analysis_result.json`（スキルの回答、schema `analysis-result-v3`）、`report_context.json`（再描画に使ったブリーフのスナップショット、schema `report-context-v4`）を置く。この3ファイルが定性分析の監査証跡であり、`copilot-ingest-analysis`は`run_id`・`as_of`・`strategy_key`・input digestの一致を確認してから同じMarkdownを再生成する（ネットワークアクセスもスクリーニング再計算も行わない）。

同じディレクトリには`rejections.json`（schema `rejections-v1`、`report/rejections.py`）も置く。Markdownの「落選サマリ」がreason_code別の件数しか出さないのに対し、こちらは落選銘柄の明細（`symbol`・`stage`・`reason_code`・`detail`）と、全ステージを通過したのに`candidate_limit`で切り捨てられた銘柄の明細（`truncated_by_candidate_limit`: `symbol`・`rank`・`score`・スコア内訳・実行状態）を残す。後者はDuckDBの`screening_rejections`にも載らない——落選理由コードは閉じたenumであり、順位落ちは落選ではないためで、run成果物としてはこのファイルだけが記録する。定性分析の3ファイルとは異なりdigestで束縛せず、読み戻す経路も持たない診断用の成果物である。

## 6. フェイルソフト

- テキスト収集または分析入力エクスポートが失敗しても、候補・リスクまでのCLI/Markdownを出力する
- 定性分析が一部の銘柄でだけ検証を通った場合、通った銘柄の結果を保持し、通らなかった銘柄だけを縮退表示にする（fail-closed、リトライなし）
- Markdown保存失敗時も構築済み`DailyBrief`があればターミナル表示は可能にする
- 通知失敗はrunを`degraded`にするが、ローカル出力は続行する
- 断定的な売買指示は`copilot-ingest-analysis`でCON-03検査し、renderer任せにしない

## 7. 受け入れ基準

- stdoutとstderrの役割が分離される
- ターミナルとMarkdownが同じ`DailyBrief`を使う
- 0候補、欠損値、分析未実施、一部銘柄のみ検証通過、決算・wide-stop警告を明示できる
- 分析未実施・対象外・検証不合格でverdict行が出ず、「懸念なし」と誤読されない
- `no_trade`が真のときヘッダ直後に取引なしと理由を表示する
- `as_of`直前・同時・直後で未来データが表示されない
- 追跡表示は`proceed`だけを対象とし、建玉中は残り営業日を表示し、手仕舞い済みは設定された営業日数だけ公開する
- Markdownのrun別保存と`latest.md`置換が原子的である
- `copilot-ingest-analysis`の再描画が決定論的フィールドを変えず、定性欄だけを差し替える
- CLI・Markdown・通知にCON-03違反が表示されない

## 8. 閲覧用ローカルダッシュボード

### 8.1 目的と位置づけ

`copilot-dashboard`は、DuckDBに蓄積された「最新runの全体像」「銘柄ごとの判断根拠」「数日〜数週間の推移」をブラウザで俯瞰するための読み取り専用ビューアである。日次判断の主表示はあくまでターミナルとMarkdownであり、ダッシュボードはそれを置き換えない。判断の記録・設定変更・再実行の経路は持たない。

FastAPI + Jinja2のサーバレンダリングで、JavaScriptのチャートライブラリ・CDN・外部フォントを一切読み込まない。チャートはサーバ側で生成するインラインSVGであり、ネットワークが切れていても全画面が同じように描画される。

### 8.2 画面とルート

| ルート | 画面 | 内容 |
|---|---|---|
| `/` | — | 最新runへリダイレクト |
| `/runs/{run_id}` | run概観 | runヘッダ（run_date・mode・status・config_hash短縮）、レジームパネル、候補と判断のテーブル、分析待ちバナー、落選銘柄（stage → reason_codeでグループ化、既定は折りたたみ） |
| `/runs/{run_id}/symbols/{symbol}` | 銘柄詳細 | verdictと根拠（`verdict_reasons`をreason_index順、basisタグとsource数併記）、スコア内訳、テクニカル生値、実行バケット、リスク、追跡状況、当否（5日/20日）、GICSセクター |
| `/history` | 推移 | 判定成績（run_dateごとのHIT/MISS件数をhorizon別・recommendation別の小倍数で）、レジーム変遷（VIX終値の折れ線とドローダウン圧力の帯）、追跡台帳（建玉中一覧と手仕舞い済みの集計） |
| `/tracking` | 追跡中の推奨 | `proceed`だけの公開一覧（推奨日終値、現在値、損益、本日の逆指値、状態、残り営業日）、手仕舞い済みは設定された営業日数だけ表示 |

全ページ共通ヘッダにrun切替のドロップダウン（run_date・mode・statusバッジ）を置く。

### 8.3 読み取り専用の制約

DuckDBのファイルロックは読み書きプロセスと他のすべての間で排他であり、接続を保持したブラウザタブは`just data-pull`／`just data-push`を失敗させ、ローカルコピーを古い世代に取り残す。したがって:

- DuckDBアクセスは`swing_copilot.research`経由のみとし、アクセサが保証する「開く→1クエリ→閉じる」に乗る。ダッシュボード自身は接続もDataFrameもキャッシュしない
- `research.ensure_views()`をこのプロセスから呼ばない（読み書き接続を開くため）。ビュー不在時は`ResearchError`をエラーページに変換し、別シェルで一度`ensure_views()`を実行するよう案内する
- 書き込みルートを持たない。`reports/`ツリーは分析未完了runの検出のために読むだけである

### 8.4 欠損値の表示

蓄積データのNULLは列ごとに意味が異なる（未成熟・verdict未取込・計測導入前・未記録・追跡未開始・該当なし）。ダッシュボードはこれらを区別した表示トークンとして描き、ゼロや`UNKNOWN`と読めるようにはしない。各ページの脚注に、そのページが実際に使ったトークンの定義だけを列挙する。

`verdicts`は次のrunの`copilot-retro collect`で取り込まれるため、最新runにverdict行が無いのは正常である。この状態は「verdict未取込」として表示し、`skip`や空欄にしない。また`tracked_positions`はIssue #190以降`skip`も反実仮想として追跡しているため、履歴台帳と集計は必ず`recommendation`で層別する。公開用の`/tracking`と日次ブリーフは`proceed`だけを表示し、`skip`はここへ混ぜない。

### 8.5 読み方の注記

値だけを並べた画面は「何が良い状態か」を読み手の記憶に委ねてしまう。各セクションには`dashboard/guidance.py`に集約した1〜2行のキャプションを添え、長い定義は折りたたむ。文言はテンプレートに直書きせず、複数ページで同じ定数を共有する。

とくに重要なのが当否の向きである。`retro/evaluate.py`は分類をverdict自身の主張に対して定義しており、`proceed`でも`skip`でもHITは「その判断が正しかった」を意味する（`skip`のHITは、見送った銘柄が実際に下落したケース）。この説明がないと、下落した`skip`が失敗に見える。判定成績のfacetで緑が正解・赤が不正解と両区分そろって読めるのも同じ理由による。

閾値は`postmortem.neutral_threshold_pct`／`postmortem.severe_threshold_pct`のように設定キー名で示す。ダッシュボードは`settings.yaml`を読まないため、数値を焼き込めば黙って古くなる。

### 8.6 起動

```bash
uv run copilot-dashboard
uv run copilot-dashboard --db data/copilot.duckdb --reports-dir reports --port 8787
uv run copilot-dashboard --tracking-retention-days 5
```

既定で`127.0.0.1:8787`にのみバインドする。認証は持たない——書き込み経路がなく、ローカルファイルのローカルビューアだからであり、公開してよいという意味ではない。起動前にDuckDBを1度だけ読んで可読性を確認し、読めなければサーバを立ち上げずにその場で終了する。

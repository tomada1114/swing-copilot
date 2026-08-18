---
paths:
  - "src/swing_copilot/dashboard/**"
  - "tests/dashboard/**"
---

`copilot-dashboard` は蓄積済みデータの閲覧専用ビューア。`.claude/rules/python.md`
と `testing.md` に加えて、この境界だけの規約を守る。

## 読み取り専用

- 書き込み経路を一切作らない。POST/PUT/DELETE ルート、`Database(...)`
  の read-write 生成、DDL のいずれも禁止
- DuckDB へは `swing_copilot.research` 経由でのみ触る。`duckdb.connect()` を
  自分で呼ばない。接続をアプリ・モジュール・キャッシュに保持しない
  （`research` の「開く→1クエリ→閉じる」を通す限り保持は起きない）
- `research.ensure_views()` をダッシュボードから呼ばない。read-write 接続を
  開くため。ビュー不在は `ResearchError` としてエラーページに出し、
  別シェルで `ensure_views()` を実行するよう案内する
- リクエストごとに複数クエリを発行してよい。DataFrame をキャッシュしない
- join を Python や生 SQL で自作しない。`research.query()` を使う場合は
  `v_*` ビューへの SELECT に限る

## レイヤ

| 層 | モジュール | 責務 |
|---|---|---|
| routes | `app.py` | パラメータ検証、view-model の組み立て、描画のみ |
| view-model | `viewmodels/` | DataFrame → frozen dataclass。意味論の解釈はすべてここ |
| queries | `queries.py` | `research` の薄いラッパ。DataFrame を返すだけ |
| templates | `templates/` | ロジックなし。分岐は表示の有無まで |

view-model 層が引き受けるもの: `v_verdict_scorecard` の
(verdict × 成熟 horizon) 粒度の集約、NULL の意味の解決、`recommendation`
での層別。ルートやテンプレートで再導出しない。

## NULL 意味論

- 列ごとに NULL の意味が違う。ゼロ・`UNKNOWN`・`none` と混同しない
- view-model 層で `formatting.Cell` の表示トークンへ解決し、テンプレートには
  生の NULL を渡さない。テンプレートは欠損有無で分岐しない
- 表示トークンと色意味論（tone）の定数は `formatting.py` に 1 箇所だけ置き、
  チャート・バッジ・CSS が同じ語彙を共有する
- 新しい欠損理由が要るときは `NULL_TOKENS` に追加し、対象ページの
  `LEGEND_KEYS` にも載せる（各ページは自分が使うトークンだけを脚注で定義する）

## ドメイン上の注意

- `tracked_positions` は #190 以降 `skip` も反実仮想として追跡している。
  台帳・集計・チャートは必ず `recommendation` で層別する。混ぜた平均を出さない
- `verdicts` は次の run の retro collect で取り込まれる。最新 run に verdict 行が
  無いのは正常であり、`skip` や空欄ではなく「verdict未取込」として表示する

## チャートと静的資産

- チャートはサーバサイド生成のインライン SVG。JS チャートライブラリ、CDN、
  外部フォント、外部画像を読み込まない（完全オフラインで動くこと）
- 色は `static/app.css` の CSS カスタムプロパティ（`var(--tone-*)`）を参照し、
  Python 側に色リテラルを置かない
- ホバー情報は SVG の `<title>` で出す（スクリプト不要）

## テスト

- 2 段構え: view-model 層のユニットテスト（HTTP を介さず、NULL 意味論・集約・
  層別を直接検証）と、`TestClient` によるページテスト（ルーティング・404・
  エラーページ・描画内容の要点）
- DB は `tmp_path` に作り、`tests/dashboard/conftest.py` の `Builder` で行を入れる。
  実 `data/`・実 `reports/` は絶対に触らない
- `Builder` は 1 文ごとに接続を開閉する。read-write 接続を保持すると
  `research` の read-only 接続が開けなくなる

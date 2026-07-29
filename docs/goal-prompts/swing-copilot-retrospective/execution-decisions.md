# 実行ルールとフェーズ別事前判断（ED / E3x）

P8-30〜P8-33 の各ゴールセッションに共通の実行契約と、フェーズ固有の
事前確定判断。設計上の判断は `design.md` と `decisions.md`（D1〜D10）が正。
本書はそれを「無人実行の手順」に落とした層で、再審議しない。
実行不能な指示に当たったら、該当ゴールの STOP RULES に従い停止して報告する。

## ED. 全フェーズ共通

- **ED1 ブランチと終端**: 各フェーズは main から
  `feat/p8-3x-<slug>`（ゴール本文に明記）を切り、コミット → push →
  `gh pr create` で PR を開いて終わる（タイトルは Conventional Commits、
  本文はリポジトリの PR テンプレートに従い summary / test plan を埋める。
  `.claude/skills/create-pr` の慣習に一致させる）。push / PR 作成が
  環境要因で失敗したら 2 回まで再試行し、それでも失敗なら
  ローカルブランチにコミットを残した縮退終端とする（各ゴールの
  DONE WHEN に縮退 sentinel を定義済み）。main へ直接コミットしない。
- **ED2 依存フェーズの preflight**: 各ゴール冒頭の preflight チェック
  （前フェーズ成果が main に存在するかの具体的な grep / import 確認）に
  失敗したら、実装を始めずに `GOAL_STOPPED:` で停止する。前フェーズの
  作り直しは行わない。
- **ED3 TDD とコミット粒度**: 挙動追加・変更は必ず受け入れシナリオ
  （テスト）を先に書き、失敗出力を貼ってから実装して green にする。
  1 論理単位 = 1 コミットで、実装・回帰テスト・必要なドキュメント修正を
  同一コミットに入れる。巨大な一括コミットにしない。
- **ED4 乖離ルール**: design.md と現行コードが矛盾したら、
  「現状の事実」はリポジトリを正とし、「未実装の契約」は design.md を
  正とする。契約が実装不能と判明したら黙って形を変えず、停止して
  乖離内容を報告する（AGENTS.md の conflict handling に従う）。
- **ED5 ドキュメント義務の範囲**: P8-30〜P8-32 では
  `docs/04_detailed_design.md` への昇格は行わない（P8-33 の仕事）。
  各フェーズは roadmap 該当シードの「動作確認」項目を新鮮なテスト出力で
  満たすこと。`docs/reference.md` / README は公開 API（CLI エントリ
  ポイント等）が変わるフェーズのみ、既存記載の粒度に合わせて更新する。
- **ED6 依存とロック**: どのフェーズも新規依存を追加しない。
  `uv.lock` が変化する操作が必要になったらそれは想定外なので停止して
  報告する。`.env*` / `secrets/**` に触れない。`--no-verify` と
  force-push は禁止（guard がブロックする）。
- **ED7 sentinel 規律**: `GOAL_DONE:` / `GOAL_STOPPED:` は各ゴール本文に
  定義された 1 行だけを、新鮮な `just verify` 出力の直後に印字する。
  途中経過の自己申告で代替しない。

## E30. P8-30（storage + collect / evaluate + forward_returns 抽出）

- **E30.1 CLI の範囲**: `retro/cli.py` は `collect` と `evaluate`
  サブコマンドのみ実装する。`prepare` / `export` / `ingest` は後続
  フェーズで追加するので、argparse に未実装サブコマンドを生やさない。
- **E30.2 collect のメタデータ解決**: run_id はディレクトリ名の UUID、
  run の as_of は親ディレクトリ名の日付から得る。recommendation /
  reasons / no_trade / 引用 source_id は `analysis_result.json`、
  strategy_key と source_type（news|filing|calendar）は同ディレクトリの
  `analysis_input.json` から解決する。どちらかのファイルを欠く run
  ディレクトリはスキップして note に残す（fail-soft）。走査 0 件は
  正常終了（R0 参照: 現環境に実ファイルはまだ無い）。
- **E30.3 forward_returns.py の抽出契約**: `_compute_forward_return` と
  取引日カレンダー構築を `pipeline/forward_returns.py` へ純関数として
  移設し、既存逆算は `find_target_trading_day`、新設順算（run_date から
  horizon 営業日先の満期日）は `find_maturity_trading_day` として公開する。
  postmortem は移設先を import する形に変え、挙動は不変
  （`tests/pipeline/test_postmortem.py` 全 green を維持）。同一の
  取引日リスト上で逆算・順算が整合すること（往復で元の日付に戻る等）を
  明示的にテストする。
- **E30.4 source_type 不明の防御**: `analysis_result.json` が引用する
  source_id が `analysis_input.json` 側に見つからない場合、その行は
  取り込まずに note へ記録する（過去 run は ingest 済みで通常起きないが、
  防御的に fail-soft とする）。

## E31. P8-31（export / retro_input.json）

- **E31.1 RetroConfig**: `max_surprises: int = 5 (ge=1)` と
  `approval_mode: Literal["auto", "manual"] = "auto"` の 2 フィールド。
  approval_mode は将来拡張の予約で本フェーズでは未参照（docstring に
  予約である旨を明記）。鮮度データの件数・文字数予算は `analysis.*` の
  既存 config を流用し、新フィールドを作らない（design §5.3）。
- **E31.2 スキーマの裁量範囲**: `retro-input-v1` の内容物は design §5.3 の
  8 項目が契約。フィールドの命名・入れ子構造は executor 裁量だが、
  strict（`extra="forbid"`）、`schema_version` 定数、`input_digest`
  （canonical JSON の SHA-256、`analysis/snapshot.py` の既存実装を手本）は
  必須。
- **E31.3 鮮度データの境界**: 各サプライズ銘柄について「run の as_of 以降
  〜retro の as_of」の窓で既存 text アダプタ（R7）を呼ぶ。timeout /
  retry / rate limit は既存実装のものをそのまま通す。取得失敗は
  当該銘柄の鮮度欄を空にして note に残す fail-soft（export 全体を
  落とさない）。オフラインテストはフェイク注入で行う。
- **E31.4 prepare**: `collect → evaluate → export` を順に呼ぶ umbrella
  サブコマンド `prepare` を本フェーズで追加する。
- **E31.5 人間整合の「実現リターン」**: 新たな実現損益計算は作らない。
  `trades_journal`（decision）× `verdicts` × `verdict_outcomes`
  （forward_return_pct / classification、同ホライズン）の join で構成する
  （design §3.4 の「追加収集不要（既存データの join のみ）」の具体化）。
  `virtual_fill_price` ベースの執行込み損益は初期スコープ外とし、
  既知の限界（design §12）に整合させる。

## E32. P8-32（ingest / 台帳 / 再提案ガード）

- **E32.1 RP-ID 採番**: `RP-001` からの全体連番（3 桁ゼロ埋め）。
  台帳 `docs/retro/proposals.md` の既存最大番号 + 1。台帳ファイルが
  存在しなければ ingest がヘッダ付きで新規生成する（P8-33 の
  「台帳初期化」はこの生成物を空のままコミットするだけにする）。
- **E32.2 proposal_key**: `retro_result.json` 側の必須フィールド
  （非空・正規化された安定文字列。例 `config:screening.rsi_threshold`）。
  再提案ガード（design §5.4）は台帳上の rejected / verification_failed
  行との**完全一致**で判定し、`reopen_justification` の有無で差し戻す。
- **E32.3 出力の置き場**: `retro_report.md` は retro_input.json と
  同じ `reports/retro/<as_of>/` へ原子的描画。台帳と提案全文
  （`docs/retro/proposals/RP-NNN-<slug>.md`）はリポジトリ内へ追記・生成。
  ingest が行う台帳操作は status=proposed の追記のみ（D10）。
- **E32.4 evidence_refs の全空間**: export が retro_input.json に付与した
  集約 ID・サプライズ銘柄・source_id の集合。この部分集合でない
  参照を含む提案・叙述は withhold（fail-closed、リトライなし）。
- **E32.5 必須テストマトリクス**: (1) happy path、(2) `as_of` /
  `input_digest` 不一致は run 全体 hard fail、(3) 捏造 evidence_ref の
  提案だけが withhold され正当な提案は生き残る、(4) CON-03 違反は
  リトライなしで当該項目のみ withhold、(5) 再提案が
  `reopen_justification` 無しで差し戻され・有りで通る、(6) RP-ID 採番が
  台帳の既存最大値から連続する、(7) report / 台帳書き込み失敗時に
  以前の成果物が保存される（原子性）。

## E33. P8-33（swing-retro スキル + 台帳初期化 + 04 昇格）

- **E33.1 スキル構成**: `.claude/skills/swing-retro/` は swing-daily と
  同型（`SKILL.md` + `references/`）。SKILL.md が必ず含むもの:
  design §6 の手順 1〜7（preflight `prepare` → input/台帳読込 →
  並列深掘り fan-out → §7 敗因分類と §8.1 証拠ゲート判定 →
  `ingest` 実行と提示 → L1 即時適用フロー（提案ごとのブランチ →
  verification_plan → `just verify` → PR）→ L2/L3 の
  設計→`AskUserQuestion` 承認→適用→PR フロー）、毎回の自問
  「L2/L3 相当の構造的観察はないか」と無ければ「再点検の上でなし」と
  明記する規律、D10 に従う台帳 status 記録。叙述規約は
  `.claude/skills/swing-daily/references/analysis-conventions.md` を
  リポジトリ内パスで参照し、コピーしない。
- **E33.2 04 昇格と roadmap 更新**: `docs/04_detailed_design.md` に
  **3.23 節**として retro 一式（データモデル・評価セマンティクス・
  スキーマ契約・承認モデル）を昇格させる。design.md §5.2 / D7 が求める
  「`signal_outcomes.as_of` との相違の明記」を含めること。roadmap
  P8-30〜P8-33 シードの完了マークは、完了済みシード（例: P2-11）の
  既存慣習を git 上で確認してそれに合わせる。
- **E33.3 スキルの無人検証範囲**: 実データでのスキル実行はスコープ外。
  受け入れは構造チェックで行う: `just docs-check` green、
  `docs/retro/proposals.md`（空台帳）存在、SKILL.md に design §6 の
  手順 1〜7・L1/L2/L3 の適用分岐・「再点検の上でなし」規律が含まれる
  ことを目視でなく grep で確認して出力を貼る。

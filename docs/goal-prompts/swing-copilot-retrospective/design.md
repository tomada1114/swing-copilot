# 振り返り→改善提案機構 詳細設計（P8）

対象: LLM 定性 verdict の当否検証と、分析ロジック（テクニカル/ファンダ指標・
定性分析・ニュース源・アーキテクチャ）の継続的な見直し提案を生成する仕組み。
本書は実装前の設計正本シード。実装確定後は `docs/04_detailed_design.md` の
3.x 節へ本書の内容を昇格させる（roadmap 運用規則に従う）。

関連: `docs/06_reliability_roadmap.md` §5 P8-30〜P8-33（Issue シード）、
`decisions.md`（事前確定判断 D1〜D9）。

---

## 1. 背景と目的

- 定量シグナルの振り返りは P2-11（`pipeline/postmortem.py` + `signal_outcomes`）で
  存在するが、LLM 定性 verdict（`proceed`/`skip`）の当否検証は未実装。
  verdict は `analysis_result.json`（`reports/` 配下、gitignore 対象）にしか
  存在せず、DuckDB に永続化されていない。
- 目的は 2 つ:
  1. **verdict の当否を決定論的に計測し**、定性レイヤが情報価値を持つかを
     数値で追跡できるようにする（観測専用。自動調整はしない）。
  2. 蓄積した証拠に基づき、**パラメータ調整から設計見直しまでの改善提案**を
     生成し、軽微な調整（L1）は即時適用して PR 化、中規模以上（L2/L3）は
     設計を `AskUserQuestion` で承認してから適用して PR 化する
     （ユーザーは投資の素人前提のため、承認対象は個別数値ではなく
     設計の方向性。§8.2）。

## 2. 全体像

日次フロー（copilot-daily → swing-daily スキル → copilot-ingest-analysis）は
変更しない。振り返りは独立したループとして、ユーザーが毎日〜数日おきに
手動で回す。

```text
[手動起動: 数日おき]
  copilot-retro prepare --as-of YYYY-MM-DD
    ├─ collect  : reports/<date>/<run_id>/analysis_result.json を走査し
    │             verdicts / verdict_sources へ冪等に取り込み（DuckDB）
    ├─ evaluate : 満期を迎えた (run, horizon) の forward return を計算し
    │             verdict_outcomes へ完全置換保存
    └─ export   : 集約指標 + サプライズ銘柄の証拠一式 + 鮮度データを
                  reports/retro/<as_of>/retro_input.json へ原子的に出力
        ↓
  .claude/skills/swing-retro （振り返りスキル、手動呼び出し）
    retro_input.json と提案台帳を読み、サブエージェントで深掘り →
    retro_result.json（strict スキーマ）を書く
        ↓
  copilot-retro ingest reports/retro/<as_of>/
    strict 検証・evidence 参照検証・CON-03 機械検査（fail-closed）→
    retro_report.md 描画 + docs/retro/ の提案台帳へ追記
        ↓
  swing-retro スキル（続き）: 提案の適用
    ├─ L1: 即時適用（config 編集）→ verification_plan 実行 →
    │      just verify → PR 作成（1 提案 1 PR）
    └─ L2/L3: 設計を作成 → AskUserQuestion で設計承認 →
           承認時のみ適用（規模超過なら goal-prompt 化）→ PR 作成
        ↓
  [人間] PR をレビュー・マージ（全変更共通の最終チェックポイント）
```

責務分担の原則は既存どおり「判断はコード、叙述はスキル分析」:
当否分類・集約・閾値判定・検証はすべて Python の決定論コードが行い、
スキルは「なぜ外したか」「何を変えるべきか」の叙述と提案だけを担う。
スキルの出力は一切信用せず、ingest で検証する。

## 3. 評価フレームワーク

### 3.1 verdict の意味論と評価の枠組み

verdict は強気/弱気の方向予測ではなく、スクリーニング通過済み候補への
**追加リスク回避フィルタ**である（`proceed` = 定性情報に追加懸念なし、
`skip` = 見送り推奨）。したがって当否は騰落との単純相関ではなく
次の非対称な枠組みで定義する:

- `proceed` の的中 = その後に重大な逆行がなかったこと（片側の主張）。
- `skip` の的中 = 下落（＝損失回避）。上昇は機会損失であり、
  実損を出す proceed の外れより軽い失敗として扱う。

### 3.2 ホライズンと閾値（推奨値と根拠）

| 項目 | 推奨値 | 根拠 |
|---|---|---|
| 評価ホライズン | 5 / 20 営業日 | スイングの典型保有期間（数日〜数週、`max_hold_days=60` だが実際の exit は大半それ以前）に合致。P2-11 と同一にすることで取引カレンダー実装を共有でき、シグナル成績と同じ窓で比較可能になる。5 日は決算等の即時リスク検証、20 日はテーゼ水準の検証 |
| ノイズ境界 | ±0.5%（`neutral_threshold_pct` 流用） | 日中スプレッド＋滑り相当の変動を「情報なし」として除外する既存判断を踏襲。語彙を分けると同じ現象に2つの閾値が生じる |
| 重大境界 | ±2.0%（`severe_threshold_pct` 流用） | 同上。`max_trade_risk_pct=0.01`・ATR ストップ前提で、5 日 −2% は想定損切り水準に達しうる逆行 |
| ホライズン重み | 5日 0.6 / 20日 0.4（流用） | 既存 postmortem と同一。headline 指標の合成にのみ使用 |

閾値はすべて `settings.postmortem` の既存値を参照する（新設しない）。
これらは既に `(要検証)` であり、本機構自身のレビュー対象に入る。

### 3.3 当否分類（決定論、Python が唯一の判定者）

forward return r（満期営業日の終値ベース、§5.2）に対し:

| recommendation | 条件 | classification |
|---|---|---|
| proceed | r > −0.5% | `HIT` |
| proceed | −2.0% < r ≤ −0.5% | `MISS_MILD` |
| proceed | r ≤ −2.0% | `MISS_SEVERE` |
| skip | r ≤ −0.5% | `HIT`（下落回避） |
| skip | \|r\| < 0.5% | `NEUTRAL` |
| skip | +0.5% ≤ r < +2.0% | `MISS_MILD`（機会損失小） |
| skip | r ≥ +2.0% | `MISS_SEVERE`（機会損失大） |

proceed 側に `NEUTRAL` がないのは意図的（「重大逆行なし」という片側の
主張に対して小幅な変動は主張を否定しないため）。価格に現れない
リスク事象（horizon 外での顕在化等）は決定論分類では拾えないことを
設計上の既知の限界として明記し、スキルの定性再読（§7）で補完する。

### 3.4 集約指標（export が計算、レポートに表示）

| 指標 | 定義 | ウォッチ水準（要検証） | 根拠 |
|---|---|---|---|
| **separation**（最重要） | proceed 群と skip 群の平均 forward return の差（ホライズン別） | n≥40 で ≤ 0 が持続 → L3 検討トリガー | 定性レイヤの存在意義そのもの。skip した銘柄群が proceed 群より良い成績なら、レイヤは情報を足していないか逆選択をしている |
| proceed 重大外し率 | proceed のうち `MISS_SEVERE` の割合（重み合成） | > 15% でフラグ | スクリーニング通過済み集合への追加フィルタとして、7 件に 1 件超の重大逆行見逃しは付加価値を疑う水準。同 run の全候補（skip 含む）の重大逆行率と併記し、**ベースラインより悪い場合は水準未満でも即フラグ** |
| skip 的中率 | skip のうち非 NEUTRAL に占める `HIT` | ベースライン（全候補の下落率）比で判定 | skip は選別的な少数判断なので絶対閾値より同期間ベースライン比較を正とする |
| 人間整合 | `trades_journal`（followed/ignored/modified）× verdict × 実現リターンのクロス集計 | 観測のみ | 「スキル助言に従った/逆らった場合の成績差」。追加収集不要（既存データの join のみ） |
| ソース貢献 | source_type/プロバイダ別の引用回数と、HIT-verdict 引用比率 vs MISS-verdict 引用比率。加えて敗因分類 `information_absent` の件数（§7） | 観測のみ | ニュース源の増減提案の根拠。引用されないソース・MISS に偏るソースは削減候補、`information_absent` の反復は追加候補 |

サンプル床は `preliminary_sample_threshold=20` を流用し、20 件未満は
「暫定」表示。根拠: 二項比率の 95% 信頼区間半幅は n=20 で約 ±22pt、
n=40 で約 ±15pt。n=20 は大きな効果の検出にしか使えないため、
提案レベルの証拠ゲート（§8）はこの2段（20/40）に対応させる。

## 4. データモデル（DuckDB 新テーブル 3 つ）

`signal_outcomes` には相乗りしない（signal は複数同時ヒットの按分が本質、
verdict は 1 symbol 1 run 1 判断で意味論が別物）。既存テーブルは無変更。

```sql
-- 過去 run の verdict の正本化（analysis_result.json からの取り込み）
CREATE TABLE verdicts (
    run_id         UUID NOT NULL,          -- 評価対象 run
    symbol         VARCHAR NOT NULL,
    as_of          DATE NOT NULL,          -- run の as_of
    strategy_key   VARCHAR NOT NULL,
    recommendation VARCHAR NOT NULL CHECK (recommendation IN ('proceed','skip')),
    reasons_json   JSON NOT NULL,          -- VerdictReason 全件
    no_trade       BOOLEAN NOT NULL,       -- run 全体の no_trade フラグ
    PRIMARY KEY (run_id, symbol)
);

-- その銘柄の分析が引用した source_id（facts / filing / verdict.reasons を統合）
-- source_type は analysis_input.json 側（コード所有メタデータ）から解決する
CREATE TABLE verdict_sources (
    run_id      UUID NOT NULL,
    symbol      VARCHAR NOT NULL,
    source_id   VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL CHECK (source_type IN ('news','filing','calendar')),
    PRIMARY KEY (run_id, symbol, source_id)
);

-- 当否分類（signal_outcomes と同型の観測専用テーブル）
CREATE TABLE verdict_outcomes (
    run_id             UUID NOT NULL,
    symbol             VARCHAR NOT NULL,
    horizon_days       INTEGER NOT NULL CHECK (horizon_days IN (5, 20)),
    as_of              DATE NOT NULL,      -- 満期営業日（観測日ではない。§5.2）
    recommendation     VARCHAR NOT NULL,   -- 非正規化コピー
    forward_return_pct DOUBLE NOT NULL,
    classification     VARCHAR NOT NULL
        CHECK (classification IN ('HIT','MISS_MILD','MISS_SEVERE','NEUTRAL')),
    PRIMARY KEY (run_id, symbol, horizon_days)
);
```

書き込み契約（既存不変条件の適用）:

- collect は run 単位の完全置換（DELETE→INSERT、1 トランザクション）。
  natural-key 再実行は訂正を取り込む。`analysis_result.json` が更新されて
  いれば再取り込みで verdicts / verdict_sources が更新される。
- evaluate は `(run_id, horizon_days)` 単位の完全置換（`replace_signal_outcomes`
  と同パターン）。株価訂正後の再実行で分類が更新される。
- 複数行書き込みは全コミットか全ロールバック。テストは 1 文成功後の
  失敗注入で検証する。

`signal_outcomes.as_of` が「観測日」なのに対し `verdict_outcomes.as_of` は
「満期営業日」とする（意図的な相違、§5.2 で理由を述べる）。

## 5. Python 側の責務: 新パッケージ `retro/` と CLI `copilot-retro`

### 5.1 配置

```text
src/swing_copilot/retro/
├── cli.py        # copilot-retro: prepare / collect / evaluate / export / ingest
├── collect.py    # reports/ 走査 → verdicts / verdict_sources 取り込み
├── evaluate.py   # 満期判定・forward return → verdict_outcomes
├── export.py     # 集約 + サプライズ選定 + 鮮度データ取得 → retro_input.json
├── ingest.py     # retro_result.json 検証 → retro_report.md + 台帳追記
└── schemas.py    # retro-input-v1 / retro-result-v1 strict スキーマ
```

`analysis/` に置かない理由: `analysis/` は「ネットワークも DB も触らない
検証専用境界」という憲章を持つ（`analysis/cli.py` docstring）。retro は
DB 読み書きと（export 時の）外部 API 取得を行うため、憲章を壊さず
別パッケージとする。`copilot-ingest-analysis` が DB に触れない不変条件も
維持される。エントリポイントは `pyproject.toml` `[project.scripts]` に
`copilot-retro = "swing_copilot.retro.cli:main"` を 1 行追加（既存慣習）。

共有プリミティブの抽出: `pipeline/postmortem.py` の
`_compute_forward_return`（方向非依存）と取引日カレンダー構築部を
`pipeline/forward_returns.py` へ純関数として移設し、postmortem と retro の
両方が import する。注意: 既存 `_find_target_trading_day` は「`as_of` から
horizon 営業日**前**」の逆算専用実装であり、retro が必要とする
「`run_date` から horizon 営業日**先**の満期日」（§5.2）には流用できない。
移設時に同じ取引日リスト上で前方インデックスする順算関数を新設し、
逆算・順算が同一カレンダーで整合することをテストで担保する。
`signal_outcomes` 側の挙動は不変（回帰テストで担保）。

### 5.2 evaluate の満期セマンティクス

postmortem（毎日「ちょうど 5/20 営業日前の run」を評価）と異なり、retro は
数日おき実行なのでバッチ指向にする:

- 対象: `run_date ∈ [as_of − lookback_window_days − 30, as_of]` の各 run。
- 各 (run, horizon) について、run_date から horizon 営業日**先**の取引日
  （満期日）をベンチマーク bar カレンダーで求め、`満期日 <= as_of` の
  ものだけ評価する。forward return は満期日終値で確定する。
- `verdict_outcomes.as_of` にはこの**満期日**を記録する。いつ retro を
  実行しても同じ行が得られ（決定論・冪等）、実行間隔が空いても
  評価漏れ・二重評価が起きない。
- すべて `date <= as_of` の価格のみ使用（look-ahead 禁止）。bar 欠損は
  当該 (run, symbol, horizon) をスキップし note に残す（fail-soft）。

### 5.3 export と `retro_input.json`（strict スキーマ `retro-input-v1`）

含めるもの:

1. **集約指標**（§3.4 の全指標、ホライズン別・重み合成・暫定フラグ付き）
2. **シグナル成績**: 既存 `compute_signal_performance` の出力をそのまま同梱
   （統合俯瞰。`signal_outcomes` の再解釈はしない）
3. **人間整合クロス集計**: trades_journal × verdicts × 実現リターン
4. **ソース貢献表**: verdict_sources × verdict_outcomes × `text_items` join
5. **サプライズ銘柄一式**（上限 `settings.retro.max_surprises = 5`、要検証。
   スキルのコンテキスト予算と 1 回の振り返りで深掘りできる件数から設定）:
   - 選定基準: `MISS_SEVERE`（proceed の重大逆行・skip の大幅上昇の両方向）。
     超過分は |forward_return| 降順で切り、切った件数を明示（silent cap 禁止）
   - 各銘柄に同梱: 当時の verdict・reasons・引用 facts（verdicts テーブル
     由来）/ 実現パス（5/20 日リターン、期間内最大逆行）/ **鮮度データ**:
     run 以降に公開されたニュース・開示を既存 text アダプタで今取得した
     もの（`analysis.*` の件数・文字数予算を流用、timeout/retry/rate limit の
     既存不変条件に従う）。これが「その時点で API 取得できる情報と
     過去分析との乖離」の材料になる
6. **現行設定スナップショット**: 提案対象になりうる config 値の抜粋と
   config_hash（提案が「どの設定に対する変更か」を一意にするため）
7. 提案台帳のパスと、status=rejected の RP-ID 一覧（再提案ガード用）
8. `input_digest`（SHA-256。result 側に逐語コピーさせ同一性検証する）

出力は一時ファイル + `os.replace` の原子的書き込み（既存規約）。

### 5.4 ingest の検証（fail-closed）

- strict（`extra="forbid"`）スキーマ検証、`as_of` / `input_digest` 不一致は
  run ごと hard fail（`validate_artifact_identity` と同型）。
- **evidence 参照検証**: 提案・叙述の `evidence_refs` は retro_input.json で
  供給した集約 ID・サプライズ ID・source_id の部分集合であること
  （provenance 検証の相似形）。捏造された証拠参照は当該提案を withhold。
- **CON-03 機械検査**: `analysis/safety.py` の `check_display_texts` を
  全ユーザー表示テキストに適用。違反は当該提案/銘柄叙述のみを
  fail-closed で非表示にし、リトライしない（銘柄単位縮退と同じ思想）。
- **再提案ガード**: 台帳で rejected / verification_failed の提案と同一
  `proposal_key` の提案は、当該 RP-ID への言及と新規証拠の説明
  （`reopen_justification`）がなければ ingest が差し戻す。
- 検証通過後: `retro_report.md` を同ディレクトリへ描画し、台帳
  （`docs/retro/proposals.md`）へ status=proposed で追記、提案本文を
  `docs/retro/proposals/RP-NNN-<slug>.md` に生成する。ingest 自体が行う
  台帳操作は追記のみで、proposed 以降の status 遷移（§8.2）は適用段階の
  スキルが記録する。

## 6. スキル側の責務: `.claude/skills/swing-retro/`

銘柄分析スキル群からは独立。手順:

1. **Preflight**: `uv run copilot-retro prepare --as-of <date>` を実行
   （collect → evaluate → export の umbrella）。
2. retro_input.json と提案台帳を読む。
3. **並列深掘り**（サブエージェント fan-out、swing-daily と同型）:
   - サプライズ銘柄ごとの敗因分析（§7）
   - シグナル成績 + verdict 成績の突合レビュー（指標の取捨選択観点）
   - ソース貢献レビュー（ニュース源の増減観点）
4. **統合と自己 QA**: 敗因分類の集計 → 提案案の証拠ゲート判定（§8）。
   毎回「L2/L3 に相当する構造的観察はないか」を明示的に自問し、
   なければ「再点検の上でなし」と結果に明記する（細かい調整への
   偏り防止。ゼロ件は探索不足を疑う既存 QA 原則の適用）。
   rejected 済み提案の再出でないかを台帳と突合する。
5. `retro_result.json` を書き、`uv run copilot-retro ingest <dir>` を実行、
   結果（成績サマリ + 提案一覧 + withhold された項目）をユーザーに提示。
6. **L1 の適用**: 証拠ゲートを満たす L1 提案を提案ごとのブランチで即時
   適用（config 編集）し、verification_plan（バックテスト前後比較等）と
   `just verify` の合格を確認して PR を作成する（`smart-commit` /
   `create-pr` スキルの慣習に従う）。不合格なら適用を取り消し、台帳に
   `verification_failed` として記録する。
7. **L2/L3 の適用**: 提案ごとに設計（変更内容・影響範囲・検証計画・
   代替案）をまとめ、`AskUserQuestion` で設計の承認を得る。承認されたら
   同セッションで適用し PR を作成する。1 セッションに収まらない規模
   （大幅なアーキテクチャ変更等）は、承認後に roadmap P-ID /
   goal-prompt 化して別セッション実装へ引き継ぐ。却下・保留は台帳に
   記録して終了する。

叙述規約（hedge 必須、断定的売買指示禁止、facts と推論の分離）は
`swing-daily` の `references/analysis-conventions.md` を流用する。

## 7. 乖離・予想外れの敗因分類（スキルの定性再読）

サプライズ銘柄ごとに、当時の入力・叙述と鮮度データを突き合わせ、
次の閉じた enum で敗因を 1 つ選ぶ（`retro-result-v1` の必須フィールド）:

| failure_class | 意味 | つながる提案の典型 |
|---|---|---|
| `information_absent` | 判断材料が当時の入力に存在しなかった（後から取得した情報には兆候がある） | ニュース源・データ源の追加（L2） |
| `information_present_missed` | 入力にあったが分析が見落とした | スキル手順・fan-out 構成の改善（L2） |
| `interpretation_error` | 情報は捉えたが読み違えた | 叙述規約・スキーマ語彙の改善（L2/L3） |
| `exogenous` | 当時のいかなる入力からも予見不能な外生イベント | 提案なし（ノイズとして記録） |
| `threshold_artifact` | 判断は妥当だが当否分類の閾値・ホライズンが不適切に「外れ」を作った | 評価フレームワーク自体の調整（L1/L3） |

この分類の反復パターンが、個別の外れを構造的な改善提案へ昇格させる
橋になる（§8 の証拠ゲートが参照する）。

## 8. 改善提案の粒度・証拠ゲート・承認フロー

### 8.1 提案レベル

| レベル | 射程 | 最低証拠（ゲートは上限ではなく床） |
|---|---|---|
| **L1 パラメータ調整** | 既存 config 値の変更（閾値・重み・予算・ウォッチ水準含む） | 該当集約 n≥20 かつ両ホライズンで方向一致、または 2 回以上の振り返りで同方向の再現 |
| **L2 構成変更** | 指標/シグナル/フィルタの追加・削除、ニュース源の増減、analysis スキーマや スキル手順の変更 | 定量: n≥40。または定性: 同一 failure_class が直近 3 回の振り返りで累計 5 件以上。**計測を可能にするための構造変更**（例: confidence フィールド追加）は初回から定性根拠のみで提案可 |
| **L3 設計見直し** | アーキテクチャ、verdict 語彙、評価フレームワーク自体、パイプライン構成の大幅変更 | separation ≤ 0 が n≥40 で持続、L1/L2 を経ても改善しない systemic 欠陥、または構造的欠陥の発見。診断メモと代替案比較（最低 2 案）を必須添付 |

各提案の必須フィールド: `proposal_key` / `level` / 対象（config パス・
モジュール・領域）/ `evidence_refs` / `evidence_basis`
(quantitative | qualitative | mixed) / 主張と期待効果 / **verification_plan** /
リスク。verification_plan は L1/L2 で必須とし、指標・閾値系は
`copilot-backtest` による前後比較手順（コマンドと合否基準）を明記する
（バックテスト系統との接続点。CON-04 の「検証なしに実運用へ進まない」
原則の提案版）。

### 8.2 承認モデルと台帳

承認の考え方（ユーザーは投資の素人前提。個別数値の妥当性判断は求めない）:

- **L1**: 事前承認なし。スキルが即時適用し PR を作成する。人間の
  チェックポイントは PR レビュー・マージに集約される（「人間判断を挟む」
  原則は PR マージとして残る。main 直接コミットは既存 guard どおり不可）。
- **L2/L3**: スキルが設計（変更内容・影響範囲・検証計画・代替案）を
  まとめ、`AskUserQuestion` で**設計の方向性**の承認を得てから適用し
  PR を作成する。却下・保留はその場の回答で確定する。
- 将来、人間が細かく介入する運用へ切り替える余地を残す
  （`settings.retro.approval_mode: auto | manual`。初期値 `auto`。
  `manual` は全レベルで AskUserQuestion を要求する将来拡張であり、
  初期実装は `auto` 固定でよいが config 名だけ予約しておく）。

台帳は**承認の場ではなく、履歴・監査・重複抑止の装置**:

- `docs/retro/proposals.md`: 1 行 1 提案の台帳
  （RP-ID | 日付 | level | タイトル | status | PR/決裁メモ | リンク）。
- `docs/retro/proposals/RP-NNN-<slug>.md`: 提案全文（証拠・検証計画つき）。
- status ライフサイクル（機械管理。スキルが遷移を記録する）:
  `proposed` → `applied`（PR 番号を記録）/ `rejected`（AskUserQuestion での
  却下。理由を記録）/ `deferred` / `verification_failed`（検証不合格で
  適用取り消し）。applied → `merged` / `reverted` は PR の顛末に追従。
- rejected / verification_failed の記録が再提案ガード（§5.4）の入力になる。
  人間が status を手で直すことも妨げない（git 履歴が監査証跡）。

## 9. 既存機構との関係

- **postmortem (P2-11) とは並置**。テーブル・集約は分離し、共有するのは
  取引カレンダー/forward return の純関数と `settings.postmortem` の閾値のみ。
  統合は retro_input.json（証拠 dossier）のレベルで行う。
- **trades_journal / decision_history**: 読み取り join のみ。書き込み経路や
  `analysis_input.json` への decision_history 注入には触れない。
- **copilot-backtest**: コード変更なし。提案の verification_plan の道具として
  接続する（将来、提案付随の一時 config での自動バックテスト実行を
  検討余地として残す。初期スコープ外）。
- **daily レポート**: 初期スコープでは変更しない。「Verdict 成績」節の
  daily レポート追加は P8 完了後の候補（§11 参照）。

## 10. 不変条件との整合チェックリスト

- as-of / point-in-time: evaluate・export は明示 `as_of` を受け、
  `date <= as_of` のみ使用。Clock 注入、wall-clock 直呼びなし。
- ストレージ: 完全置換 + 訂正 upsert + 単一トランザクション（§4）。
- 原子的置換: retro_input.json / retro_report.md は一時ファイル + `os.replace`。
- 外部境界: 鮮度データ取得は既存 text アダプタの timeout / retry /
  rate limit をそのまま使う。オフラインテストは socket guard 維持、
  フェイク注入。
- スキル境界: strict スキーマ双方向、evidence 参照検証、CON-03 中央検査、
  違反は提案/叙述単位で fail-closed・リトライなし。
- 無審査の静黙変更なし: Python 側（copilot-retro）には config / コードを
  書き換える経路がない。変更を行うのはスキルの適用段階のみで、必ず
  提案ごとのブランチ + verification_plan / `just verify` 合格 + PR を経由し、
  L2/L3 は AskUserQuestion の設計承認が先行する。main への直接コミットは
  しない。verdict_outcomes の集計が閾値を直接書き換えるフィードバック
  ループは存在しない。

## 11. 実装フェーズ分割

| フェーズ | 内容 | 依存 |
|---|---|---|
| P8-30 | `verdicts` / `verdict_sources` / `verdict_outcomes` + collect / evaluate + `forward_returns.py` 抽出 | なし（P7 完了が前提） |
| P8-31 | export（集約・サプライズ選定・鮮度データ取得・retro_input.json） | P8-30 |
| P8-32 | ingest（strict 検証・CON-03・レポート描画・台帳生成・再提案ガード） | P8-31 |
| P8-33 | `swing-retro` スキル + 台帳初期化 + `docs/04_detailed_design.md` 3.x 節昇格 | P8-30〜P8-32 |

P8 完了後の候補（本設計のスコープ外、初回の振り返り運用で要否判断）:
daily レポートへの「Verdict 成績」節追加、`analysis_result` への構造化
confidence / 想定パス（thesis）フィールド追加（当否計測の解像度を上げる
L2 提案の有力候補として本機構自身が最初に検討することを想定）、
提案付随バックテストの自動実行。

## 12. 既知の限界

- verdict は二値で confidence がなく、interpretation / risk_flags は自由文の
  ため、初期の当否計測は recommendation の粒度に留まる。自由文の主張
  単位の検証はスキルの定性再読が担い、構造化はスキーマ進化の提案
  （§11 候補）に委ねる。
- 価格ベースの当否は horizon 外・価格外のリスク顕在化を拾えない（§3.3）。
- skip 銘柄は実際に取引されないため、当否は仮想的な forward return 評価で
  あり、執行コストを含む実収益とは一致しない。
- 運用初期はサンプルが小さく、数ヶ月は L1 判断も「暫定」域に留まる。
  この期間の主産出物は定性再読と構造的観察（L2/L3 候補）である。

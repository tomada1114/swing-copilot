---
name: swing-track
description: >
  Review the verdict-tracking ledger: every past `proceed` verdict treated as a
  virtual position bought at that run's close and carried forward under the same
  exit rules as the backtest (2.5×ATR14 trailing stop, 25-session max hold).
  Runs `copilot-track update`, reads the ledger with `list` / `show`, pairs each
  position's current state against the reasons the verdict gave at the time, and
  records the conclusion as a dated note or a manual close.
  Use PROACTIVELY when: 追跡、含み損益、仮想ポジション、手仕舞い、手仕舞い判断、
  ポジション確認、台帳確認、あの銘柄どうなった、判断メモ、
  track, tracking, virtual positions, open positions, unrealized P&L,
  trailing stop, close a position.
---

# 仮想ポジション追跡ふりかえり（日次の戦術ループ）

`swing-daily` が `proceed` と判定した銘柄を「その run の終値で仮想的に買った」とみなし、
backtest と**同一の手仕舞いルール**（2.5×ATR14 トレーリングストップ + 最大保有 25 営業日）で
追跡した台帳を読む。目的は売買の指示ではなく、**「当時の判断はどうだったか」を頻繁にふりかえる**こと。

台帳の数値・手仕舞い判定はすべて Python の決定論コードが出す。
このスキルが担うのは、**台帳と当時の verdict 理由を突き合わせた対話的な読み**と、
その結論を `note` / `close` として台帳に残すことだけ。

## `swing-retro` との棲み分け

| | swing-track（このスキル） | swing-retro |
|---|---|---|
| 周期 | 毎日でも叩ける速い戦術ループ | 満期（5/20 営業日）が溜まってから |
| 見るもの | 手仕舞いルール準拠の日次追跡（含み損益・現在ストップ・残り営業日） | close-to-close 2 点の統計的当否評価・シグナル成績・ソース貢献 |
| 問い | 「この 1 銘柄、当時の読みは今も成り立っているか」 | 「定性判断のレイヤーは全体として価値を出しているか」 |
| 出力 | 判断メモ / 手動クローズ（台帳への追記のみ） | `retro_result.json`・提案台帳・1 提案 1 PR |
| 変更権限 | **なし**（config / コードに触れない） | 提案として PR 経由でのみ変更 |

両者は別レイヤとして併存する。track で溜めた**判断メモが retro の改善提案の一次材料**になる
（「同じ崩れ方を 3 回した」は、track のメモが無ければ後から再構成できない）。
このスキルで構造的な改善アイデアが出たら、実施せず `swing-retro` へ渡す（Step 5）。
定型の台帳読みを超えた自由な切り口の集計（「レジーム別の勝率は」「スコア上位は
当たってるか」等）は `swing-research`（`swing_copilot.research` の読み取り専用
DataFrame）へ。

作業前に必ず読む:

- `.claude/skills/swing-daily/references/analysis-conventions.md` — AC1〜AC16 の共通叙述規約。
  台帳に残すメモにもそのまま適用する（特に AC3〜AC5 の CON-03、AC12 の hedge）。
  **このスキルへコピーせず、上記パスを直接読むこと**

## Inputs

- DuckDB の `verdict_positions` / `verdict_position_marks` / `verdict_position_notes`
  — `copilot-track` 経由でのみ読み書きする。SQL で直接触らない
- `verdicts.reasons_json`（`show` が表示する当時の proceed 理由）
- `config/settings.yaml` の `backtest.exit_atr_multiple` / `backtest.max_hold_days`
  — **読むだけ**。追跡ルールの数値であり、このスキルからは書き換えない

## Outputs

- `copilot-track note` による判断メモ（`(run_id, symbol, note_date)` で 1 日 1 件、同日は上書き）
- `copilot-track close` による手動クローズ（`exit_reason=manual`）
- ユーザーへの要約報告（要注意ポジション / 各銘柄の読み / 記録した内容）

台帳以外への書き込みは一切行わない。

## Step 0: 再入チェック

同一セッション・同日に既に `update` を実行済みなら、再実行せず Step 2 へ進む。
判断がつかない場合は `update` を実行してよい（冪等で、マークは訂正 UPSERT される）。

対象日（`--as-of`）の指定が無ければ省略する（CLI 境界で当日が入る）。
過去日を指定して読み直したい場合はユーザーの指示に従う。

## Step 1: 台帳の更新

```bash
uv run copilot-track update
```

出力は `新規 N 件 / 更新 N 件 / 手仕舞い N 件` と、黄色の data-quality note。

- **黄色の note は捨てない。** 「エントリー価格を解決できないため追跡を開始しない」
  「ATR を算出できずストップ未設定で追跡する」「バーが欠損しているため当日を飛ばした」は、
  台帳の読み方そのものを変える情報である。該当銘柄はストップ列が `—` になり得るし、
  含み損益が最新営業日のものでない可能性がある
- 「エントリー価格を解決できない」は次回 `update` で自然に再試行される。ここで手当てしない
- バー未取得が原因と読める場合は `swing-daily` の実行が先である旨を報告し、
  台帳を無理に読み進めない

## Step 2: 全体把握

```bash
uv run copilot-track list --status open
```

列は symbol / ⚠ / run_id / entry_date / entry / stop / last close / 含み損益 /
保有・上限 / 残（営業日）/ exit_date / 理由 / 確定損益。open 行が entry_date 降順で先、
closed 行が exit_date 降順で後に並ぶ。

`⚠` 列が `no_trade` の行は、銘柄単体の判定は proceed だが run 全体は当日
エントリー非推奨（no_trade）だった中の proceed である。判定の質を測る材料としては
有効だが、「実際に提案された買い」とは区別して読む。

**優先的に取り上げるもの**（★を付けて報告する）:

1. **ストップ割れ間近・割れ**: `last close` が `stop` を下回っている、
   または `(last close − stop) / last close` が 3% 未満
2. **残り営業日 5 日以下**: 最大保有到達が近く、ルール上まもなく終値で手仕舞いになる
3. **前回の note 以降に読みが変わった可能性のあるもの**: 含み損益の符号が変わった、
   ストップがラチェットで entry を上回った（利益が確定側に入った）など

該当が無ければ「★該当なし」と明示する。全 open 行を機械的に列挙するのではなく、
**上位数件に絞って**ユーザーへ提示する。手仕舞い済みの確認が要る場合のみ
`--status closed` / `--status all` を追加で叩く。

## Step 3: 気になる銘柄を突き合わせる

```bash
uv run copilot-track show --symbol <SYM> [--run-id <UUID>]
```

verdict の proceed 理由（当時の読み）・日次マーク履歴（close / stop / 含み損益）・
既存ノートが出る。同一銘柄が複数 run で建っている場合は `--run-id` で 1 本に絞る。

`⚠ no_trade run` と表示された場合は、その proceed が出た run 全体では当日
エントリー非推奨だったことを踏まえて読む。以降の突き合わせでは判定の質の
材料として扱い、「その日実際に買いが提案されていた」という前提では読まない。

**当時の理由と現状を 1 行ずつ突き合わせる。** 価格の善し悪しではなく、
「proceed の根拠が今も立っているか」を見る。判断枠は次の 2 つ:

| 読み | 見え方 | 例 |
|---|---|---|
| **想定内** | 下落・停滞しているが、proceed の根拠自体は崩れていない。ストップという想定内の防御が効いている範囲 | 「決算後の需給悪化と読める動きで、根拠にした受注動向を否定する材料は入力に無い」 |
| **判断が崩れた** | proceed の根拠にした事実そのものが後続の観測で否定された・前提が失効した | 「根拠にしたガイダンス上方修正が撤回された」「根拠が特定顧客との契約継続だったが解消が公表された」 |

- 迷ったら **想定内**（＝台帳のルールに任せる）に倒す。手動クローズはルールの上書きであり、
  常態化すれば「backtest と同一ルールで追跡する」という台帳の意味が失われる
- マーク履歴に無い事実（当時のニュース・開示の中身）まで遡って読みたい場合は、
  このスキルでは扱わず `swing-deepdive` に渡す
- 決定論的な数値（entry_price / stop / 含み損益 / days_held）は**解釈のみ**。
  再計算・訂正・上書きをしない（AC1）

## Step 4: 結論を台帳に残す

書き込む前に `AskUserQuestion` で確認する。**ユーザーの回答なしに書き込まない。**
選択肢の軸は「メモだけ残す（想定内）」「手動クローズする（判断を覆す）」「何もしない」。

想定内 — 日付付きの判断メモを残す:

```bash
uv run copilot-track note --run-id <UUID> --symbol <SYM> --text "<メモ>"
```

判断を覆す — 手動クローズし、理由をメモとして同時に残す:

```bash
uv run copilot-track close --run-id <UUID> --symbol <SYM> --note "<覆した理由>"
```

メモの書き方（`analysis-conventions.md` の AC をそのまま適用）:

- **観測事実と推論を分ける。** 「株価が entry 比 −6%」は事実、「需給要因と読める」は推論
- **推論には hedge を付ける（AC12）。**「〜の可能性がある」「台帳の範囲では〜」
- **断定的売買指示・命令形を書かない（AC3・AC4）。**
  NG:「明日売るべき」／ OK:「根拠にした前提が失効したため、手動クローズとして記録する」
  この指示は本文だけの約束ではない。`note` / `close --note` は保存前に中央の
  CON-03 ガードを通るので、違反したメモはコマンドが非0終了で拒否する
  （`close --note` は検査が先なのでポジションも閉じない）。拒否されたら
  文面を直して再実行する。
- **根拠なき心理・行動診断を書かない（AC5）**
- 1 行 1 論点で短く。後から `show` で時系列に読み返して意味が通ること、
  そして `swing-retro` が材料として拾えることを基準にする

`note` は同日 1 件で上書き。同じ日に追記したい場合は既存メモを読んだうえで
**統合した全文**を書き直す（片方が消えないように）。

## Step 5: 構造的な改善アイデアが出た場合

「ストップ倍率が浅すぎるのでは」「この signal は proceed の根拠として弱いのでは」
「max_hold 25 日は長すぎるのでは」といった**構造的な観察は、このスキルでは実施しない**。

- config / コードを編集しない。バックテストで検証もしない
- 観察を Step 4 のメモとして台帳に残し（後から証拠になる）、
  ユーザーへ「`swing-retro` の提案フロー（証拠ゲート → 提案台帳 → 1 提案 1 PR）に
  回すのが適切」と案内して終える
- 緊急性を理由にこの順序を飛ばさない。台帳の追跡ルールが途中で変わると、
  それ以前のポジションと以後のポジションが比較できなくなる

## Step 6: 報告

- `update` の結果（新規 / 更新 / 手仕舞い件数）と data-quality note
- ★要注意ポジション（ストップ割れ間近・残り営業日僅少）とその読み
- 掘り下げた銘柄ごとの「想定内 / 判断が崩れた」の結論と根拠
- 記録した note / close の一覧（銘柄・run_id・内容）
- `swing-retro` へ回した構造的観察があればその要旨

最終判断は人間である旨を添える。台帳はあくまで**仮想ポジションの記録**であり、
実際の売買・保有とは独立している。

## 禁止事項

- `config/` および `src/` の編集（改善提案は `swing-retro` の PR フロー経由。
  このスキルに変更権限は無い）
- 追跡ルールの決定論的な数値（`exit_atr_multiple` = 2.5、`max_hold_days` = 25、
  ATR 期間 14）の書き換え、および台帳の entry_price / stop / 含み損益 /
  確定損益の再計算・訂正（AC1）
- `copilot-track close` / `note` 以外の書き込み。とくに DuckDB を SQL で直接
  UPDATE / INSERT / DELETE すること（トランザクションと UPSERT 規約を迂回するため）
- `analysis_result.json` / `analysis_work/**` / `reports/**` への書き込み、
  および `docs/retro/proposals.md`・`docs/retro/proposals/` の編集
- `copilot-daily` / `copilot-ingest-analysis` / `copilot-retro` の実行
  （このスキルは追跡台帳だけを扱う。必要なら該当スキルへ渡す）
- `AskUserQuestion` による確認なしの `close` / `note` 実行
- 断定的売買指示、命令形、根拠なき心理・行動診断（AC3〜AC5）
- data-quality note を報告から落とすこと、欠損を「変化なし」と読み替えること

# AGENTS.md

ヤマビヨリ: Open-Meteo の無料API(気象庁MSM + ECMWF)だけで動く登山向け天気予報ツール。山頂標高の値で
「その日その山の稜線は濡れずに歩けるか」をスコア化する。Codex などのエージェントはこのファイルに従うこと。

## 完了条件(必ず守る)
コードを変更したとき:
1. **完了を報告する前に、必ずテストを実行し、その結果を報告に含める。**
   `./venv/Scripts/python.exe -X utf8 -m unittest` の最後の行(`OK` / `FAILED` と件数)をそのまま書く。実行していないなら完了と言わない。
2. **既存テストの期待値を書き換えない。テストを削除・スキップもしない。** 既存テストが落ちたら、既定の動作を変えてしまった
   合図なので自分の変更を見直す。どうしても変更が必要だと考えたら、変える前に理由を示してユーザーに確認する。
   新しい機能のテストを追加するのはよい(`tests/test_offline.py`)。
3. **機能を足すときは既定の動作(引数なしで実行したときの結果)を変えない。** `--region`・`--limit` と同じように
   新しい引数や設定として追加する。既定を変えたいときは、変える前にユーザーに確認する。

公開・共有を頼まれたとき:
4. **まず生成した HTML ファイルを直接送る方法(メールやチャットへの添付)を案内する。** 公開URLが必要な場合は、方法を1つに
   決めず選択肢を示す。実行前に「方法・場所(リポジトリとブランチ)・公開範囲・公開されるファイル」を示し、承認を得る。
5. **GitHub の設定変更(Pages の有効化など)は、ほかの作業とは別に個別の承認を得る。** **main へは直接 push しない。**
   このリポジトリ(ktakemaru/yamabiyori)の main への反映は Claude Code プラグインのマーケットプレイス公開になるため、PR 経由のみ。
レポートを出すとき(HTML・可視化を頼まれたとき。公開・共有でなくても適用):
6. **生成したレポートはローカル(`out/`)に保存し、場所を伝えて終える。** 人に見せる結果は HTML で出す。HTML は自分で書かず、`skills/yamabiyori/references/report_style.md` の手順(`--json-out` で数値を保存 → 天気図を取得して解説を書く → `scripts/render_report.py --data` で変換)に従う。必須要素の欠落はエラーになる。載せられなかった要素は理由を報告に含める。

## 最初に読むもの
- このファイルと README.md の「Codexで試す」「3つのモード」節で足りる。`skills/yamabiyori/SKILL.md`(長い)は**スコア式・閾値・較正に関わる変更をするときだけ**読む。 レポート(HTML)を出すときは `skills/yamabiyori/references/report_style.md`(手順・必須要素)と `weather_map_reading.md` を読む。
- 山・山域をすすめるとき(「家族向け」「デート向け」「初心者向け」など)は `skills/yamabiyori/references/recommend_guide.md` に従う。
- 版ごとの経緯と数値の根拠は `CHANGELOG.md`。山数・閾値などの**数値はここに書かない。コードと CHANGELOG を正とする**(例: `python -X utf8 -c "import mountain_weather_core as c; print(len(c.MOUNTAINS))"`)。

## セットアップ(Python 3.10 以上)
コマンドはリポジトリのルートで実行する。以下は Windows PowerShell 表記。macOS/Linux は `./venv/Scripts/python.exe` を
`./venv/bin/python`、最初の `python` を `python3` に読み替える。venv は activate せず、venv の python を直接呼ぶ。
```powershell
python -m venv venv                       # python が無ければ py -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
./venv/Scripts/python.exe -X utf8 scripts/check_setup.py        # [NG] があれば -> の案内に従う
```
**すべての python 実行に `-X utf8` を付ける。** 本体の3スクリプトは付け忘れても UTF-8 で出力するが、`python -c` や
自作の確認スクリプトは Windows + Python 3.14 以前だと `UnicodeEncodeError`(cp932)で止まる。コードの不具合ではない。

## 実行
### 長い作業の前に伝える
- 30 秒以上かかる見込みの作業の前に、**何をするか**と**時間の目安**を 1 行で利用者に伝える。例: 「全 76 座の予報を取ります。5 分ほどかかります。」(30 秒は仮の値)
- 伝えるのは、最初の長いコマンドを呼ぶ直前に 1 回だけ。途中の実況はしない。短い作業(30 秒未満)には出さない。
- 時間の目安は、下の表から引く。**自分で見積もらない。** 表に無い作業・「未実測」の作業は「時間の目安はまだ測っていません」と言う。
- 条件で時間が大きく変わる作業(展望図)は、先に見積もりのコマンド(`--panorama-estimate`。通信しない)を実行して、その結果の時間を伝える(例: `mountain_weather_detail.py --mountain 大菩薩嶺 --panorama-estimate`。「時間の目安: …」の行)。
- 終わったあとに「○分かかりました」と報告する必要はない(コマンドの出力に実行時間の内訳がある)。

| 作業 | コマンド | 目安 | 出どころ(いつ・どこで測ったか) |
|---|---|---|---|
| 探索モード 全 76 座 | `mountain_weather_mvp.py` | 約 4 分 | 2026-11-18、開発用の PC、空のキャッシュで 2 回: 250・246 秒(ほかに、キャッシュの状態の記録が無い 1 回が 252 秒。`docs/mountain-coordinates-fix-report.md` 4 章)。予報の保存(3 時間)があると短い(62 座が保存済みで 16 秒、同上) |
| 探索モード 一部(`--limit`・`--region`) | `mountain_weather_mvp.py --limit 3` など | 未実測 | — |
| 診断モード 1 座 | `mountain_weather_detail.py --mountain 山` | 未実測 | — |
| 診断モード + 展望図(初回) | `mountain_weather_detail.py --mountain 山 --panorama` | **`--panorama-estimate` の結果** | 標高タイルが 1 枚も無いとき 566 枚・12 分 14 秒 + 予報と図づくり 109 秒(2026-11-20、大菩薩嶺。`docs/panorama-v1.10-finish-report.md`) |
| 診断モード + 展望図(2 回目以降) | 同上 | **`--panorama-estimate` の結果** | すべて保存済みで約 20 秒(20.7 秒・22.4 秒、大菩薩嶺。`docs/panorama-v1.10-report.md`・同上) |
| 計画モード(GPX) | `mountain_weather_detail.py`(対話式) | 未実測 | — |
| HTML レポートの作成 | `scripts/render_report.py` | 未実測 | — |
| 天気図・ひまわりの取得 | `scripts/fetch_synoptic.py` | 未実測 | — |
| 地形の事前計算 | `precompute_terrain.py` | 未実測 | — |
| テスト | `-m unittest` | 約 1 分 | 2026-11-24、開発用の PC、1 回: 187 件・67 秒 |

(参考。PC の値ではない: GitHub のマシンでの探索モード 全 76 座は、229 件・333 秒 / 228 件・319 秒。2026-11-22・23 の 3 回のうち利用者から聞いた値。ビヨリコンシェルの取得の記録)

### コマンド
```powershell
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py --limit 3        # 最初の動作確認(先頭3座だけ、数十秒)
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py                  # 探索モード全山(初回は数分、出力は数百行)
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py --region 伊豆     # 地域で絞る(複数回指定可)
./venv/Scripts/python.exe -X utf8 mountain_weather_detail.py --list        # 山の番号一覧
./venv/Scripts/python.exe -X utf8 mountain_weather_detail.py --mountain 富士山   # 診断モード(番号でも可)
```
- **76座に無い山**は診断モードに `--name 山名 --lat 緯度 --lon 経度 --elevation 標高m`(4つ一緒。`--mountain` とは併用不可)を渡す。座標・標高はWeb検索(Wikipedia・国土地理院など)で調べ、出典を伝える。地形(風上/風下)の表示は付かず、探索モードの対象にはならない。`MOUNTAINS` への追加は別途ユーザーに確認する。
- **展望図(山頂から見た雲の絵)を頼まれたら** `mountain_weather_detail.py --mountain 山 --panorama`(76 座だけ。HTML を `out/panorama/` に書き、最後に「展望図: パス」)。時間は手元の標高タイルの枚数で大きく変わるので、**先に同じ山で `--panorama-estimate` を実行し、出た時間を伝えてから作る**(上の「長い作業の前に伝える」)。展望図が作れなくても診断の出力・終了コードは変わらない。
- 引数なしの `mountain_weather_detail.py` は対話式(GPXルート診断もこちら)。エージェントからは `--mountain` を使う。山の番号はリスト順なので、使う前に `--list` で確認する。2回目以降は `cache/` が効いて速い。
- 診断モードの時間ごとの表(画面・`--json-out` とも同じ列)の **「総雲量%」は空全体の雲量で、地上付近の雲量ではない**。内訳の「低層雲%」「中層雲%」「高層雲%」で、山頂より下の雲か、山の上の高い雲かを読み分ける(読み方は `skills/yamabiyori/references/report_style.md`)。
- **PowerShell では python の出力を `>`・`|`・`$x = ...` で受けない。** Windows PowerShell 5.1 では日本語が化け、`>` の
  ファイルは UTF-16 になる。`| Select-Object -First` は途中でプロセスを止め終了コードも壊す。コマンドはそのまま実行して出力を読む。
  長い出力は `--out out/ファイル名`(UTF-8 BOM付きで保存、画面には1行だけ)で残す。`out/` は gitignore 済み。別の場所に保存したファイルはコミットしない。
  全山の探索は `--out` で保存し、必要な日付・山だけを読むと出力を読む量が減る。`--out` は診断モードでは `--mountain`/`--list` と一緒にだけ使える。
- 全山の探索がコマンドのタイムアウトに当たったら、`--region` で地域ごとに分けて実行する(結果は `cache/` に残る)。

## 成功条件
| 実行 | 成功 |
|---|---|
| `check_setup.py` | 終了コード0、最後に「セットアップは問題ありません」 |
| 探索モード | 終了コード0、出力に `【対象地域:` の行があり、`取得失敗` を含む行が無い(`--out` のときはそのファイルの中身で判定) |
| 診断モード | 終了コード0、出力に `登山向け総合スコア(日別)` の表がある(同上) |
| テスト | 最後の行が `OK`(skipped は正常) |
- **ランキングが空でも正常。** `--limit`・`--region` の軽量実行では基準点(`MIN_SCORE_THRESHOLD`)を超える山が無いことがある。
- 終了コード2 = 引数の誤り、または予報の取得失敗(探索モードは全山失敗のとき)。
- **Open-Meteo の一時的な失敗**(タイムアウト、HTTP 5xx、`取得失敗` 行、終了コード2)は外部要因として報告し、コードは直さない。
- **API レスポンスの形式が変わった**と思われるとき(KeyError 等)は、スコアリングを直さずにユーザーへ報告する。

## ネットワークが拒否されたとき
pip install と Open-Meteo への通信には、エージェントのサンドボックス設定によって承認が必要な場合がある。
拒否されたら**回避策(別のミラー、キャッシュの手作り、コードの書き換え)を取らず**、ネットワーク利用の承認をユーザーに求める。
オフラインでできる確認はテストだけ。

**Open-Meteo の利用上限(HTTP 429)**: 1分の上限(`minutely`)は自動で65秒待って取り直す(1回の実行で5回まで)。
1時間・1日の上限・理由が読めない429は、探索モードでは残りの山の取得をやめ、取れた山だけで結果を出す(画面に種類と reason が出る)。
**手で再実行を重ねない**(上限が延びるだけ)。1時間・1日の上限なら、時間をおいてから1回だけ実行し直す。探索モードは通信の間隔を自動で調整するので、以前より時間がかかる。

## テスト
```powershell
./venv/Scripts/python.exe -X utf8 -m unittest                # オフライン(既定)。pytest でも可
$env:YAMABIYORI_NETWORK_TESTS="1"; ./venv/Scripts/python.exe -X utf8 -m unittest tests.test_network   # Open-Meteo 実通信
```
`tests/test_regression.py` は参照3日をフィクスチャ(`tests/fixtures/refs/`)から再計算し完全一致を確かめる。
手動では `./venv/Scripts/python.exe -X utf8 scratch_validate_refs.py --cache-dir tests/fixtures/refs`。

## 変更してよい領域 / 保護領域
| 変更してよい | 保護(ユーザーの明示的な指示と承認なしに変えない) |
|---|---|
| 探索モードの対象山の選別(`mountain_weather_mvp.py` の `select_pool()`・`REGION_FILTER`・`TOP_N_PER_DAY`)| `mountain_weather_core.py` のスコア式・penalty関数・`SCORE_WEIGHTS`・危険信号の閾値 |
| 表示・表の整形(`print_*` 関数)、引数処理、`main()` の入出力、`cli_output.py` | 雲量較正(`CLOUD_CALIBRATION_*`、`calibrated_summit_cloud_series`)。**毎時に較正してから窓平均**の順序も含む |
| `tests/`・`scripts/`・ドキュメント | 雨天判定(`WET_HOUR_*`、湿潤層・強制上昇・逆転層の蓋)と各種閾値定数 |
| | 標高補間、MSM/ECMWF/ENS の取得と切り替え(各 `fetch_forecast`・`fetch_open_meteo`) |
| | 時間帯評価(`window_scores_by_day`・`compute_day_scores`)、`MIN_SCORE_THRESHOLD` |
- 「特定の山だけで探索」は `MOUNTAINS` を編集せず(順番を変えると診断モードの番号がずれる)、**新しい引数**として `parse_args()`・
  `select_pool()` に条件を足す(座標・地域は各山の dict)。`main()` の `region_label`(`【対象地域:` の行)にも条件を出し、テストを追加する。
- 山の追加・削除はユーザーに確認してから。追加時は `python -X utf8 precompute_terrain.py 山名` を実行し、ドキュメントの山数も直す。
- **山の ID**(`MOUNTAINS` の `"id"`、`m001`〜)は、ほかのビヨリ・ビヨリコンシェルが山を指す変わらない名前。決まり:
  (1) 一度決めたら変えない(山名を直しても、並べ替えても)。(2) 使い回さない(山を外したら欠番にする)。
  (3) 新しい山には、今ある最大の番号の次を付け、`tests/fixtures/mountain_ids.json` の末尾にも1行足す(`tests/test_mountain_ids.py` が一致を確かめる)。
  (4) `m` で始まる ID はこのリポジトリが付ける。利用者が自分で足す山は `u` で始める(例 `u001`。その仕組みはまだ無い)。
- セットアップを通すためだけにスコアリングを変えてはならない。環境の問題は環境側で解決する。
- 設計のルール: Open-Meteo 以外の予報サービスの結果を混ぜない(気象庁の天気図・ひまわりは可)。有料・APIキー必須のAPIを
  追加しない。スコアの根拠を後から追える表示(`M`/`湿n`/`確n`、危険信号の別枠)を崩さない。

## 変更後に実行する検証
1. `./venv/Scripts/python.exe -X utf8 -m unittest` がすべて OK(冒頭の「完了条件」。期待値を書き換えて通すのは不可)。
2. 触ったモードを実際に実行して成功条件を満たす(探索モードは `--limit 3` でよい)。
3. 保護領域を変えた場合(承認済みのときだけ): CHANGELOG に日付・内容・理由と参照3日の新しい値を書き、
   `tests/test_regression.py` の期待値を更新する。詳しくは SKILL.md と CHANGELOG の各エントリ。
4. 仕様変更や重要な判断は `CHANGELOG.md` の `[Unreleased]` に1〜2行で記録する。`.claude-plugin/plugin.json` の `version` は上げない(リリース判断は作者)。

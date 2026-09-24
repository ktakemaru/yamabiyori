# AGENTS.md

ヤマビヨリ: Open-Meteo の無料API(気象庁MSM + ECMWF)だけで動く登山向け天気予報ツール。山頂標高の値で
「その日その山の稜線は濡れずに歩けるか」をスコア化する。Codex などのエージェントはこのファイルに従うこと。

## 最初に読むもの
- このファイルと README.md の「Codexで試す」「3つのモード」節だけで、セットアップ・実行・小さな改造はできる。
- `skills/yamabiyori/SKILL.md`(Claude Code用の設計資料、長い)は**スコア式・閾値・較正に関わる変更をするときだけ**読む。
- 版ごとの経緯と数値の根拠は `CHANGELOG.md`。山数・閾値などの**数値はここに書かない。コードと CHANGELOG を正とする**
  (例: `python -X utf8 -c "import mountain_weather_core as c; print(len(c.MOUNTAINS))"`)。

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
```powershell
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py --limit 3        # 最初の動作確認(先頭3座だけ、数十秒)
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py                  # 探索モード全山(初回は数分、出力は数百行)
./venv/Scripts/python.exe -X utf8 mountain_weather_mvp.py --region 伊豆     # 地域で絞る(複数回指定可)
./venv/Scripts/python.exe -X utf8 mountain_weather_detail.py --list        # 山の番号一覧
./venv/Scripts/python.exe -X utf8 mountain_weather_detail.py --mountain 富士山   # 診断モード(番号でも可)
```
- 引数なしの `mountain_weather_detail.py` は対話式(GPXルート診断もこちら)。エージェントからは `--mountain` を使う。
- 山の番号はリスト順なので、使う前に `--list` で確認する。2回目以降は `cache/` が効いて速い。
- **PowerShell では python の出力を `>`・`|`・`$x = ...` で受けない。** Windows PowerShell 5.1 では日本語が化け、`>` の
  ファイルは UTF-16 になる。`| Select-Object -First` は途中でプロセスを止め終了コードも壊す。コマンドはそのまま実行して出力を読む。
  長い出力を残したいときは `--out ファイル`(UTF-8 BOM付きで保存、画面には `[OK]/[NG] 保存先(行数)` の1行だけ)を使う。
  全山の探索は `--out` で保存し、必要な日付・山だけを読むと出力を読む量が減る。`--out` は診断モードでは `--mountain`/`--list` と一緒にだけ使える。
- 全山の探索がコマンドのタイムアウトに当たったら、`--region` で地域ごとに分けて実行する(結果は `cache/` に残る)。

## 成功条件
| 実行 | 成功 |
|---|---|
| `check_setup.py` | 終了コード0、最後に「セットアップは問題ありません」 |
| 探索モード | 終了コード0、出力に `【対象地域:` の行があり、`取得失敗` を含む行が無い(`--out` のときはそのファイルの中身で判定) |
| 診断モード | 終了コード0、出力に `登山向け総合スコア(日別)` の表がある(同上) |
| テスト | 最後の行が `OK`(skipped は正常) |
- **ランキングが空でも正常。** `--limit`・`--region` の軽量実行では、基準点(`MIN_SCORE_THRESHOLD`)を超える山が無く
  日別の表が1つも出ないことがある。上の成功条件(`【対象地域:` の行 / `取得失敗` なし / 終了コード0)は変わらない。
- 終了コード2 = 引数の誤り、または予報の取得失敗(探索モードは全山失敗のとき)。
- **Open-Meteo の一時的な失敗**(タイムアウト、HTTP 5xx、`取得失敗` 行、終了コード2)は外部要因として報告し、コードは直さない。
- **API レスポンスの形式が変わった**と思われるとき(KeyError 等)は、スコアリングを直さずにユーザーへ報告する。

## ネットワークが拒否されたとき
pip install と Open-Meteo への通信には、エージェントのサンドボックス設定によって承認が必要な場合がある。
拒否されたら**回避策(別のミラー、キャッシュの手作り、コードの書き換え)を取らず**、ネットワーク利用の承認をユーザーに求める。
オフラインでできる確認はテストだけ。

## テスト
```powershell
./venv/Scripts/python.exe -X utf8 -m unittest                # オフライン(既定)。pytest でも可
$env:YAMABIYORI_NETWORK_TESTS="1"; ./venv/Scripts/python.exe -X utf8 -m unittest tests.test_network   # Open-Meteo 実通信
```
`tests/test_regression.py` は参照3日(唐松岳9/6・立山9/5・槍ヶ岳9/5)をフィクスチャ(`tests/fixtures/refs/`)から
再計算し、現行の値と完全一致を確かめる。手動では
`./venv/Scripts/python.exe -X utf8 scratch_validate_refs.py --cache-dir tests/fixtures/refs`。

## 変更してよい領域 / 保護領域
| 変更してよい | 保護(ユーザーの明示的な指示と承認なしに変えない) |
|---|---|
| 探索モードの対象山の選別(`mountain_weather_mvp.py` の `select_pool()`・`REGION_FILTER`・`TOP_N_PER_DAY`)| `mountain_weather_core.py` のスコア式・penalty関数・`SCORE_WEIGHTS`・危険信号の閾値 |
| 表示・表の整形(`print_*` 関数)、引数処理、`main()` の入出力、`cli_output.py` | 雲量較正(`CLOUD_CALIBRATION_*`、`calibrated_summit_cloud_series`)。**毎時に較正してから窓平均**の順序も含む |
| `tests/`・`scripts/`・ドキュメント | 雨天判定(`WET_HOUR_*`、湿潤層・強制上昇・逆転層の蓋)と各種閾値定数 |
| | 標高補間、MSM/ECMWF/ENS の取得と切り替え(各 `fetch_forecast`・`fetch_open_meteo`) |
| | 時間帯評価(`window_scores_by_day`・`compute_day_scores`)、`MIN_SCORE_THRESHOLD` |
- 「特定の山だけで探索」したいときは `MOUNTAINS` を編集せず、探索モードの選別で絞る(`MOUNTAINS` の順番を変えると
  診断モードの番号がずれる)。山の座標(`lat`/`lon`)・地域(`region`)は各山の dict にある。選別条件を足したら、
  `main()` の `region_label`(`【対象地域:` の行)にもその条件が出るようにする。
- 山の追加・削除はユーザーに確認してから。追加時は `python -X utf8 precompute_terrain.py 山名` を実行し、ドキュメントの山数も直す。
- セットアップを通すためだけにスコアリングを変えてはならない。環境の問題は環境側で解決する。
- 設計のルール: Open-Meteo 以外の予報サービスの結果を混ぜない(気象庁の天気図・ひまわりは可)。有料・APIキー必須のAPIを
  追加しない。スコアの根拠を後から追える表示(`M`/`湿n`/`確n`、危険信号の別枠)を崩さない。

## 変更後に実行する検証
1. `./venv/Scripts/python.exe -X utf8 -m unittest` がすべて OK。
2. 触ったモードを実際に実行して成功条件を満たす(探索モードは `--limit 3` でよい)。
3. 保護領域を変えた場合(承認済みのときだけ): CHANGELOG に日付・内容・理由と参照3日の新しい値を書き、
   `tests/test_regression.py` の期待値を更新する。詳しくは SKILL.md と CHANGELOG の各エントリ。
4. 仕様変更や重要な判断は `CHANGELOG.md` の `[Unreleased]` に1〜2行で記録する。
   `.claude-plugin/plugin.json` の `version` は上げない(リリース判断は作者が行う。main への push はそのまま公開になる)。

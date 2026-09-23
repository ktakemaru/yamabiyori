# AGENTS.md

このリポジトリ(ヤマビヨリ / mountain-weather)を clone してコーディングエージェントで
拡張作業を行う人向けの指示ファイル。Codex(ChatGPT)などのエージェントはまずこのファイルに従うこと。

> `skills/yamabiyori/SKILL.md` はClaude Code用の補助ファイル(Skill)であり、Claude Codeが
> このツールをユーザーの代わりに実行する際の詳細設計ドキュメント(スコア式の根拠、既知の
> 落とし穴、検証記録など)。汎用のプロジェクトルールではないので、このAGENTS.mdと役割を
> 混同しないこと。ただし設計の背景を深く知りたい場合の一次資料としては有用(「まず読む」参照)。

## まず読む

1. `README.md` — プロジェクト概要、3モードの実出力例、スコアの仕組み、データソース、精度と限界
2. `skills/yamabiyori/SKILL.md` — 設計判断の経緯、スコア式の各定数の根拠、既知の落とし穴、検証記録(Claude Code用だが情報量が最も多い)
3. `CHANGELOG.md` — 仕様・スコア式の変更履歴(日付・内容・理由)
4. [yamabiyori-backtest](https://github.com/ktakemaru/yamabiyori-backtest) — 本体とは別リポジトリのバックテスト基盤。
   精度検証の結果(`docs/track-b-findings.md`)と本体への推奨(`docs/product-recommendations.md`、R1〜R9)、
   日次収集(アメダス実況・予報スナップショット・ECMWF アンサンブル全メンバー・本体の確信度)。
   本体側の数値(閾値・較正テーブル)を変えるときは、まずこちらの根拠を確認する。

## プロジェクト概要

**「その日、その山の稜線は、濡れずに歩けるか」** を数字で答える、登山特化型の天気予報ツール。
Open-Meteoの無料API(気象庁MSM + ECMWF IFS)だけで動き、山頂標高の気圧面データを補間して
雲量・視程・降水を「登山向け総合スコア」にまとめ、雷・強風・低体温症は別枠の「危険信号」として
警告する。関東甲信・北陸信越・東北南部・伊豆の76座に対応。詳細はREADME.mdの「ヤマビヨリの特徴」を参照。

v1.4.0 / v1.5.0 (2026-09-19)、v1.6.0 (2026-09-23) では、別リポジトリのバックテストで測った結果を製品に反映している(v1.6.1 (2026-09-24) は v1.6.0 に合わせた文書のみの修正)。
山頂雲量の検証は 2026-06-12〜09-17 の 98 ラン(00Z)を麓アメダス日照(白馬・野辺山・奥日光の 3 地点で通して、
安達太良山/鷲倉は 9/11〜18 の 8 日分)と富士山頂の日照(7/10〜8/31)に当てたもので、AUC は 1 日前(MSM) 0.78、
3 日前(ECMWF) 0.77、7 日前 0.61。信用してよいのは 3 日前まで、較正は 6〜9 月のデータのみ、夜間・稜線風は未検証
(README「精度と限界」)。この検証から入った変更:
- **R8 (v1.4.0)**: 雨天時間の閾値 `WET_HOUR_MSM_PRECIP_MM` 0.1→0.5mm/h、アンサンブルの `ENSEMBLE_PRECIP_WET_THRESHOLD_MM` 0.1→0.2mm/h。
  0.1 では「雨」とした時間の 7 割強が実況 0.5mm 未満だったため。
- **R1 (v1.5.0)**: 山頂雲量を毎時 `core.calibrated_summit_cloud_series()` で「実効雲量」(過去の的中率にもとづく晴れ確率、
  同モデル d1-2 の 0% ビンで正規化)に直してから窓平均・PM 持続ピークを取り `cloud_penalty` へ渡す(`core.CLOUD_CALIBRATION_TABLE`)。
  全山頂に適用。表示は「雲量%(較正/生)」の 2 値。スコア水準が下がったため探索モードの目安 `MIN_SCORE_THRESHOLD` を 80→70 に変更。
  較正は非線形なので**窓平均の後ではなく毎時に先に適用**すること(順序を変えると意味が変わる)。
- **R12 (v1.6.0)**: R1 の MSM の行は 900/800hPa 無しの山頂雲量で学習されていたので、MSM の d1-2 行を本体と同じ 7 面の山頂雲量で
  学び直した行に差し替えた。案C の基準値 P_ref は**行ごと**(`CLOUD_CALIBRATION_TABLE` の各行の 5 番目の要素)で、MSM d3-4 行は
  v1.5.0 の 0.719 のまま(値が変わらないように)。d1-2 行だけ差し替えるときに基準値をモデル単位で決め直すと d3-4 行の値まで動くので注意。
  MSM d3-4 行は 5 面学習のまま(2027 年 3 月に作り直し予定)。

## リポジトリ構成

- `mountain_weather_core.py` — 探索/診断モードが共有する土台モジュール。山リスト(`MOUNTAINS`)、
  標高補間、湿潤層/強制上昇ルール、スコア式(`mountain_climb_score`)、Open-Meteoキャッシュ層。
  単体では実行しない。
- `mountain_weather_mvp.py` — **探索モード**本体(76座横断ランキング、非対話)。
- `mountain_weather_detail.py` — **診断モード**(山選択)と**GPXルート診断**の本体。天気図取得・
  ルート地図PNG・ECMWFアンサンブル確信度もここ。
- `mountain_terrain.py` / `precompute_terrain.py` / `terrain_profiles.json` — 地形レイヤー
  (国土地理院DEMから76座分を事前計算済み)。
- `scratch_past_date.py` / `scratch_validate_refs.py` — スコア式変更を検証するための過去日付再計算ツール。
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` — Claude Codeプラグインのマニフェストと
  プライベートマーケットプレイス定義。Codexでの作業には直接関係しないが、`plugin.json`の`version`は
  リリースのバージョン番号と一致させる(「重要」節参照)。

## 起動

Python **3.10以上**が必要(`requirements.txt`のバッジ参照。動作確認は3.15で実施)。

### 初回のみ: 仮想環境と依存パッケージ

**Windows (PowerShell):**
```powershell
python -m venv venv
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

必須依存は `requests` と `numpy`(地形レイヤー)のみ。オプション機能を使う場合は追加インストールする:

| 機能 | 追加パッケージ |
|---|---|
| 気象庁天気図の取得(診断モードの概況説明) | `playwright` を pip install → `playwright install chromium` |
| ルート天気マップPNG、ひまわり画像 | `Pillow` |

### 実行コマンド(3モード)

以下はWindows表記(`./venv/Scripts/python.exe`)。macOS/Linuxでは `./venv/bin/python` に読み替える。

```bash
# 探索モード: 76座横断ランキング(非対話)
./venv/Scripts/python.exe mountain_weather_mvp.py

# 診断モード: 対話的に山を選択(起動後に 1=山選択 / 2=GPXルート診断 を入力)
./venv/Scripts/python.exe mountain_weather_detail.py

# 診断モード: 非対話実行の例(モード1、番号12=富士山(剣ヶ峰)。番号はリスト順なので
# 山を追加・削除した場合は必ず `1: 山を1つ選んで診断` 実行時の一覧で番号を確認し直すこと)
printf "1\n12\n" | ./venv/Scripts/python.exe mountain_weather_detail.py
```

探索モードの主な調整パラメータ(`mountain_weather_mvp.py` 冒頭): `REGION_FILTER`(空=全地域)、
`TOP_N_PER_DAY`、`MIN_SCORE_THRESHOLD`(既定70点。v1.5.0で80から変更)。

## 重要(設計思想 — 具体的な禁止・許可のルール)

- **Open-Meteo以外の予報サービス(てんきとくらす・Windy・SCW・Mountain-Forecast・WeatherNews等)の
  予報結果を、予報結果に混ぜてはならない。** 気象庁の実況・予想天気図とひまわり画像は気象庁自身の
  Open Dataなので例外(禁止対象に含まない)。過去の精度比較記録(開発ログ)としてこれらのサービス名が
  SKILL.mdに残っているのは検証履歴であり、本番の予報ロジックに組み込むこととは別物。
- **商用のクローズドAPI(有料の気象予報API等)を新たに追加しないこと。** このツールは
  無料・APIキー不要のOpen Data(Open-Meteo、気象庁、国土地理院)だけで動く設計を維持する。
- **スコアの数値だけでなく「なぜその予報なのか」を説明できる設計を崩さないこと。** 雨天割合セルの
  `M`/`湿n`/`確n`のような根拠表示、危険信号を別枠にする設計、天気図・衛星画像で数値の裏を取る
  フローなどはこの目的のためにある。新機能を足す際も「数値の根拠を後から追える」ことを保つ。
- **数値や関数名は必ずコードを確認してから変更・言及すること。** 推測で書かない(過去に
  山数・番号・関数名の記載ズレが繰り返し起きている。SKILL.md冒頭の注意書き参照)。
  `grep`や `python -c "import mountain_weather_core as core; print(...)"` で実値を確認してから直す。
- **スコアリングロジック(`mountain_weather_core.py`の`mountain_climb_score`・penalty関数群・
  `SCORE_WEIGHTS`・各種閾値定数)を変更した場合は、必ず`CHANGELOG.md`に日付・変更内容・理由を
  追記すること。** バージョン番号(`.claude-plugin/plugin.json`の`version`)を上げた場合は、
  何が変わったかもCHANGELOGに明記する。

## 初回セットアップ時の注意(「起動」との差分のみ)

- pip installやスクリプト実行でエラーが出た場合は、まず原因を調査すること(環境差異か、
  コード側の問題か切り分ける)。
- 依存パッケージのバージョン不整合など**環境差異に起因する修正以外**でコード変更が必要になった
  場合は、変更前に必ずユーザーへ説明し、合意を得てから直すこと。
- セットアップ後は「起動」節の3コマンドを実際に実行し、正常に動作する(エラーなく表・ランキングが
  出力される)ことを確認する。
- **スコアリングロジックを、セットアップを通すためだけの理由で変更してはならない。** 依存パッケージや
  実行環境の問題は依存関係・環境側で解決し、`mountain_climb_score`や各種閾値定数には触れないこと。

## 開発時

- コードを変更したら、可能な範囲で該当するモード(探索/診断/GPX)を実際に実行し、エラーなく
  動作すること、および出力の値が意図通り変わっていることを確認してから完了とする。
- スコア式・閾値を変更した場合は、`scratch_validate_refs.py`(参照3日の回帰チェック)や
  `scratch_past_date.py`(過去日付の実況照合)で影響を確認することが望ましい(手順はSKILL.md
  「実際の登山記録で育てている」節・CHANGELOG.mdの各エントリを参照)。降水閾値・雲量較正を触る場合は
  `scratch_r8_threshold_effect.py` / `scratch_r1_calibration_effect.py`(探索キャッシュ 76 座×15 日の前後比較。
  0 点をまたいだ山日と、日別の順位相関も見る)を回し、根拠は backtest リポジトリの docs を更新する。

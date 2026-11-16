# 展望図の実証 段階1(実験)

ヤマビヨリ2.0の候補「天気入り展望図」の最初の実証。**地形の計算で、写真に写った山を正しく言い当てられるか**を、
PeakFinder(山名を写真に重ねるアプリ)のラベルを正解として確かめる。題材は大菩薩嶺の雷岩付近。
既定の動作(`mountain_weather_*.py`)には一切関わらない。既存のモジュールは import して使うだけ。

## ファイル
| ファイル | 中身 |
|---|---|
| `geometry.py` | 計算の部品(通信しない)。方位と距離(Vincenty の逆問題)、見上げ角と「余裕」、稜線の輪郭、横位置の当てはめ |
| `stage1_daibosatsu.py` | 今回の実行用。材料を読み、計算し、`out/panorama/daibosatsu/` に書く |
| `../../tests/test_panorama_geometry.py` | 計算の部品のテスト(作った地形で。通信しない) |
| `../../tests/test_panorama_stage1.py` | 1003山の読み込みのテスト(作った GeoJSON で。データが無いときに止まること) |

## 動かし方(リポジトリのルートで)
```
./venv/Scripts/python.exe -X utf8 experiments/panorama/stage1_daibosatsu.py --data-dir C:/temp/daibosatsu
./venv/Scripts/python.exe -X utf8 -m unittest tests.test_panorama_geometry
```
- `--data-dir` に `peakfinder_labels.json`(撮影地点・ラベル)と GPX と写真を置く。**利用者の記録なのでリポジトリに入れない。**
  撮影地点の座標・時刻はコードに書かず、実行時にここから読む。
- 国土地理院「日本の主な山岳標高」の GeoJSON を、**ブラウザで取得して** `cache/panorama/gsi_1003/` に置いておく
  (<https://www.gsi.go.jp/kihonjohochousa/kihonjohochousa41139.html> の GeoJSON の zip を展開した `.geojson`)。
  スクリプトは国土地理院のサイトに接続しない。ファイルが無ければ、この手順を案内して止まる。リポジトリには入れない。
- 標高タイルは `mountain_terrain.DemTileStore` で取り、`terrain_cache/` に保存する。`--max-new-tiles`(既定 400)で
  タイルの総数に上限をかけ、HTTP 429 か同じ失敗 5 回で取得をやめる。`--no-fetch` は手元のタイルだけで計算する。
- 所要: タイルが手元にあれば 4〜5 分(輪郭の計算が大半)。

## 計算の中身
- 方位・距離: WGS84 の Vincenty(逆問題はここに実装、順問題は `mountain_terrain.vincenty_direct`)。
- 見上げ角: `mountain_terrain.apparent_elevation_angle_deg`(地球の丸み+屈折、k=0.13)。目の高さ 1.6m。
- 余裕 = 山頂の見上げ角 − 視点から山頂までの途中の地形の見上げ角の最大。正なら山頂が手前の地形より上に出る。
  途中の地形の刻みは 2km まで 50m・20km まで 200m・その先 500m。視点から 100m 以内と、目印の手前 300m は取らない
  (タイルの粗さで足もとの斜面や山頂そのものを「手前の地形」と数えないため)。
- 標高: 地理院の標高タイル z11(1 セル約 76m)の最寄りのセル。海・無効値は 0m。
- 横位置の当てはめ: `x = cx + f·tan(方位 − 中心の方位)`。3 つを最小二乗で求めるもの、`cx` を画像の中央に固定するもの、
  方位に比例する直線、の 3 通りを出す。

## 出どころ
- 山の座標・標高: 国土地理院「日本の主な山岳標高(1003山)」2026-03-31 版
  (<https://www.gsi.go.jp/kihonjohochousa/kihonjohochousa41139.html>)。手元で使うだけで再配布しない。
  利用条件(国土地理院コンテンツ利用規約)の確認は利用者の承認待ち。無い山はヤマビヨリの `MOUNTAINS` を使う。
- 標高: 国土地理院 標高タイル(`cyberjapandata.gsi.go.jp`)。

## 分かっている限界
- 標高タイルが z11(約 76m)なので、近くの尾根や、山頂付近の細かい地形は丸まる。
- 材料(1003 山+76 座)に無い山は計算に出ない。PeakFinder のラベルの 3 割前後はこれで「未取得」になる(低い山・峠・前山)。
- 同じ名前の山は、ラベルの標高で選ぶ。標高まで同じ候補が複数あれば「不確か」とし、距離の近い方を採る。
- 結果の詳細は `docs/panorama-stage1-report.md`。

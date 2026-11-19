"""ヤマビヨリの展望図(天気入り)。診断モードの --panorama から使う(mountain_weather_detail.py)。

部品(実験 experiments/panorama/ の段階1〜3.9 から、図を作るのに要る関数だけを移した。答え合わせ・個人の記録・
段階1 の大菩薩嶺の実証・報告用の部品は入れていない):
- geometry.py  測地(距離・方位・地点)
- terrain.py   標高タイル・見上げ角・輪郭・見える山・図の地形(figure_parts)
- names.py     山名辞書(data/)・呼び名・格。「日本の主な山岳標高」の表(手元にあれば)
- viewpoint.py 山頂の点・OSM の展望地の選び方
- weather.py   予報の取得(本体の get_with_retry と通信の間隔の調整)と雲の値・1 時刻の場面(compute_scene)
- clouds.py    雲の絵(帯・雲海・かたまり)
- draw.py      山並み・名前・空・HTML(せまい画面・広い画面)
- run.py       1 座の図を作る入口(make_panorama)
- config.py    保存先
"""

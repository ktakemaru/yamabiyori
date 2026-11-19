# 山名辞書(展望図用)

このフォルダの 3 つの JSON は、展望図に出す山の名前と格(日本百名山・ヤマビヨリの 76 座)を決めるための辞書です。座標と標高は入れていません。

- `hyakumeizan.json`: 日本百名山と「日本の主な山岳標高」の山との対応(呼び名つき)
- `mountains76.json`: ヤマビヨリの 76 座(`mountain_weather_core.MOUNTAINS`)と「日本の主な山岳標高」の山との対応
- `similar_names.json`: 似た名前の山の組(名前の取り違えを防ぐため)

出典: 「日本の主な山岳標高」(国土地理院)と `MOUNTAINS` をもとに作成(加工: 百名山・76 座との対応づけ、呼び名)。
作り方は実験の `experiments/panorama/stage2_dictionary_build.py`(段階2)。

「日本の主な山岳標高」の表そのもの(山の座標と標高)はリポジトリに入れていません。展望図は、利用者の手元の
`cache/panorama/gsi_1003/*.geojson` を読みます(無ければ、山頂の点は MOUNTAINS の座標、名前は 76 座だけで図を作ります)。

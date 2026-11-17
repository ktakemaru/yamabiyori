# テスト用フィクスチャ

## refs/ — 参照3日の回帰チェック用

`scratch_validate_refs.py` が読む `*_1d_past5d.json` 6ファイルを、`cache/`(gitignore対象)からそのままコピーしたもの。
clone 直後でも `python -X utf8 scratch_validate_refs.py --cache-dir tests/fixtures/refs` と
`tests/test_regression.py` が動くようにするためにコミットしている。**中身は編集しないこと**(期待値が変わる)。

| ファイル | 山(`MOUNTAINS` の座標) | モデル |
|---|---|---|
| `jma_msm_36.6872_137.7547_1d_past5d.json` / `ecmwf_ifs025_…` | 唐松岳(※) | `jma_msm` / `ecmwf_ifs025` |
| `jma_msm_36.5758_137.6197_1d_past5d.json` / `ecmwf_ifs025_…` | 立山(雄山) | 同上 |
| `jma_msm_36.3419_137.6469_1d_past5d.json` / `ecmwf_ifs025_…` | 槍ヶ岳 | 同上 |

※ 唐松岳の 2 ファイルは、2026-11-18 に、`MOUNTAINS` の今の座標(国土地理院 1003 山の山頂の点 36.68723, 137.75469)で取り直したものに置き換えた。置き換える前のファイルは、直す前の座標(36.7514, 137.7622。唐松岳の北 7.2km、白馬岳の近く)で 2026-09-09 に取ったもの。取り直しでは、古い座標の 9/4〜9/8 の値が置き換える前のファイルと全く同じだったので、取った時期による違いは入っていない。MSM のファイルは、ほかの見本と同じ形にするため `daily` を空にしてある(日の出・日の入りは ECMWF のファイルから読む)。

- 取得日時: 2026-09-09 21:37 JST(各ファイルの `_fetched_at`、UNIX 秒)。唐松岳の 2 ファイルだけ 2026-11-18 05:59 JST
- 取得方法: Open-Meteo Forecast API(`/v1/forecast`)、`past_days=5`・`forecast_days=1`、`timezone=Asia/Tokyo`(唐松岳の 2 ファイルは `past_days=75` で取り、同じ期間を切り出した)、
  毎時変数は `mountain_weather_detail.HOURLY_VARS + core.level_stack_vars()`、日別は `sunrise`/`sunset`
- 期間: 2026-09-04 00:00 〜 2026-09-09 23:00(JST、毎時)
- `_fetched_at` 以外はAPIのレスポンスそのまま(`_fetched_at` は本体のキャッシュ層が付ける取得時刻)。唐松岳の 2 ファイルは、上の期間の切り出しと、MSM の `daily` を空にしたこと(`daily_units` も無し)のほかはそのまま

## 参照日の由来

- 唐松岳 2026-09-06 は、利用者がヤマレコで見た複数の登山者の記録写真を元にした参照日。6:40 八方池から白馬三山がはっきり見え、眼下は雲。8:30 と 9:40 は山頂が晴れて遠くまで見通せ、上空と眼下に雲。11:10 は山頂で小雨と強い風。昼ころから強風と雨で、雨脚が強まり、登山者はみな下山した。そのため午後の山頂の記録は無い。山頂の点での予報は、11時からの湿りと風の強まりは捉えたが、日中の雨は 0mm で、雨の降り出しを外している。投稿者の名前と記録の URL は載せない。

## 出典・ライセンス

Weather data by [Open-Meteo.com](https://open-meteo.com/)。
Open-Meteo の API データは [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) で提供されている。
元のモデルデータは 気象庁 MSM(`jma_msm`)と ECMWF IFS 0.25°(`ecmwf_ifs025`)。

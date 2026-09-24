# テスト用フィクスチャ

## refs/ — 参照3日の回帰チェック用

`scratch_validate_refs.py` が読む `*_1d_past5d.json` 6ファイルを、`cache/`(gitignore対象)からそのままコピーしたもの。
clone 直後でも `python -X utf8 scratch_validate_refs.py --cache-dir tests/fixtures/refs` と
`tests/test_regression.py` が動くようにするためにコミットしている。**中身は編集しないこと**(期待値が変わる)。

| ファイル | 山(`MOUNTAINS` の座標) | モデル |
|---|---|---|
| `jma_msm_36.7514_137.7622_1d_past5d.json` / `ecmwf_ifs025_…` | 唐松岳 | `jma_msm` / `ecmwf_ifs025` |
| `jma_msm_36.5758_137.6197_1d_past5d.json` / `ecmwf_ifs025_…` | 立山(雄山) | 同上 |
| `jma_msm_36.3419_137.6469_1d_past5d.json` / `ecmwf_ifs025_…` | 槍ヶ岳 | 同上 |

- 取得日時: 2026-09-09 21:37 JST(各ファイルの `_fetched_at`、UNIX 秒)
- 取得方法: Open-Meteo Forecast API(`/v1/forecast`)、`past_days=5`・`forecast_days=1`、`timezone=Asia/Tokyo`、
  毎時変数は `mountain_weather_detail.HOURLY_VARS + core.level_stack_vars()`、日別は `sunrise`/`sunset`
- 期間: 2026-09-04 00:00 〜 2026-09-09 23:00(JST、毎時)
- `_fetched_at` 以外はAPIのレスポンスそのまま(`_fetched_at` は本体のキャッシュ層が付ける取得時刻)

## 出典・ライセンス

Weather data by [Open-Meteo.com](https://open-meteo.com/)。
Open-Meteo の API データは [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) で提供されている。
元のモデルデータは 気象庁 MSM(`jma_msm`)と ECMWF IFS 0.25°(`ecmwf_ifs025`)。

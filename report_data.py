"""レポート用の数値データ(JSON)を書き出す小さなヘルパー(探索モード・診断モード共通)。

`--json-out ファイル` で、画面に出す表と同じ数値を、列も行も省かずに JSON で保存する。エージェントが画面出力を
読み写す必要がなくなり、`scripts/render_report.py --data` がそのまま HTML にできる。
予報の計算・スコア・既存のテキスト出力には一切関わらない(結果の dict を詰め替えて書くだけ)。
"""
import json
import os

SCHEMA = "yamabiyori-report-data/1"

# 行 dict のうち JSON に残すキー(探索モード・診断モードで共通の名前)。
ROW_KEYS = (
    "score", "ridge_wind_ms", "cape", "cloud_pct", "cloud_pct_raw", "ridge_visibility",
    "precip_wet_pct", "precip_wet_hours", "precip_total_hours", "precip_msm_hours",
    "precip_moist_hours", "precip_prob_hours", "precip_total_mm", "chill",
    "day_peak_wind_ms", "day_peak_wind_hour", "hazards", "day_total_precip_mm",
    "terrain", "terrain_base",
)


def hazard_class(hazards) -> str:
    """危険信号の色分け: 警戒/危険を含む=warning、注意だけ=caution、無し=none。"""
    levels = [hazards.get(k, "-") for k in ("thunder", "wind", "cold")]
    if any(level in ("警戒", "危険") for level in levels):
        return "warning"
    if any(level != "-" for level in levels):
        return "caution"
    return "none"


def row_to_json(r: dict, cells: dict) -> dict:
    """行 dict の数値(ROW_KEYS)と、画面に出す整形済みの文字列(cells)をまとめる。

    cells: {"hazard": 危険信号の文字列, "wet": 雨天割合の文字列, "cloud": 雲量(較正/生), "terrain": 地形}。
    画面と同じ文字列をそのまま持たせるので、レンダラー側で整形ロジックを再実装しない。"""
    out = {k: r.get(k) for k in ROW_KEYS if k in r}
    out["hazard_class"] = hazard_class(r["hazards"])
    out["cells"] = dict(cells)
    return out


def write_report_json(path: str, data: dict) -> int:
    """data を UTF-8 の JSON で保存し、画面には保存先の1行だけを出す。0=成功、2=書き込み失敗。"""
    data = {"schema": SCHEMA, **data}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)
            f.write("\n")
    except OSError as e:
        print(f"[NG] 数値データ(JSON)を書き込めませんでした: {path} ({e})")
        return 2
    print(f"[OK] レポート用の数値データを保存しました: {os.path.abspath(path)}")
    return 0

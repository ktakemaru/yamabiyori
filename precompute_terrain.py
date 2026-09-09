"""76座の地形プロファイルを一度だけ計算して terrain_profiles.json に書く。
Usage: python precompute_terrain.py [--force] [山名 ...]
  --force  : 既存のプロファイルがあっても計算し直す
  山名     : 指定した山だけ計算(省略時は MOUNTAINS 全部)
地形は変わらないので、山を追加したときと計算仕様(mountain_terrain.py の
TERRAIN_PROFILE_VERSION)を変えたときだけ再実行すればよい。
タイルは terrain_cache/ にキャッシュされ、2回目以降はネットワーク不要。
"""
import json
import os
import sys
import time
from datetime import datetime

import mountain_terrain as mt
from mountain_weather_core import MOUNTAINS, CACHE_DIR


def msm_grid_elevation(lat: float, lon: float):
    """Open-Meteo が返す MSM 格子の標高(参考、キャッシュにあれば)。"""
    for days in (15, 14):
        p = os.path.join(CACHE_DIR, f"jma_msm_{lat:.4f}_{lon:.4f}_{days}d.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f).get("elevation")
            except (OSError, json.JSONDecodeError):
                pass
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    existing = {}
    if os.path.exists(mt.TERRAIN_PROFILES_PATH):
        with open(mt.TERRAIN_PROFILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == mt.TERRAIN_PROFILE_VERSION:
            existing = data.get("profiles", {})
    store = mt.DemTileStore()
    targets = [m for m in MOUNTAINS if not args or m["name"] in args]
    profiles = dict(existing)
    t0 = time.time()
    for i, m in enumerate(targets, 1):
        if m["name"] in profiles and not force:
            print(f"[{i}/{len(targets)}] {m['name']}: 既存を流用")
            continue
        t1 = time.time()
        prof = mt.compute_terrain_profile(m["lat"], m["lon"], m["elevation_m"], store)
        prof["msm_grid_elevation_m"] = msm_grid_elevation(m["lat"], m["lon"])
        prof["computed_at"] = datetime.now().isoformat(timespec="seconds")
        profiles[m["name"]] = prof
        print(f"[{i}/{len(targets)}] {m['name']} {m['elevation_m']}m: DEM山頂{prof['dem_summit_m']}m "
              f"露出{prof['exposure_deg']}° 稜線軸{prof['ridge_axis_deg']}° "
              f"drop最大{max(v for v in prof['drop_m'] if v is not None):.0f}m "
              f"({time.time() - t1:.1f}s, tiles 新規{store.downloaded_count}/キャッシュ{store.cache_hit_count}/失敗{store.failed_count})")
        with open(mt.TERRAIN_PROFILES_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": mt.TERRAIN_PROFILE_VERSION, "attribution": mt.ATTRIBUTION,
                       "params": {"zoom": mt.DEM_ZOOM, "azimuths": len(mt.AZIMUTHS), "ray_step_m": mt.RAY_STEP_M,
                                  "ray_max_m": mt.RAY_MAX_M, "drop_band_m": mt.DROP_BAND_M, "horizon_max_m": mt.HORIZON_MAX_M},
                       "profiles": profiles}, f, ensure_ascii=False, indent=1)
    print(f"done: {len(profiles)} profiles in {time.time() - t0:.0f}s -> {mt.TERRAIN_PROFILES_PATH}")


if __name__ == "__main__":
    main()

"""Reference-day regression check for the scoring formula, not part of the
main pipeline. Recomputes the three reference days the 2026-09 redesign was
tuned against -- 唐松岳9/6 (rain on the descent, must stay < 80), 立山9/5 and
槍ヶ岳9/5 (reported fine days) -- from the cached jma_msm/ecmwf_ifs025
`*_1d_past5d.json` files in cache/ (fetched 2026-09-09; they hold the full
pressure-level stack incl. geopotential heights), bypassing the cache TTL so
the numbers are reproducible before/after a code change.
Expected (2026-09-09 night, after the altitude-interpolation redesign): see
CHANGELOG for the current reference values.
Usage: python -X utf8 scratch_validate_refs.py [-v] [--cache-dir DIR]
  -v prints the hourly rows; --cache-dir reads the past5d files from DIR instead
  of cache/ (tests/fixtures/refs/ holds a committed copy, so this runs right after a clone).
If the past5d cache files are missing, re-fetch them with
core.fetch_open_meteo(lat, lon, hourly=m.HOURLY_VARS + core.level_stack_vars(),
daily=["sunrise","sunset"], days=1, model="jma_msm"/"ecmwf_ifs025", past_days=N)
for the right N.
"""
import argparse
import json
import os

import mountain_weather_core as core
import mountain_weather_detail as m

REFS = [("唐松岳", "2026-09-06"), ("立山(雄山)", "2026-09-05"), ("槍ヶ岳", "2026-09-05")]
PAST_DAYS = 5
_parser = argparse.ArgumentParser(description="参照3日(唐松岳9/6・立山9/5・槍ヶ岳9/5)の回帰チェック")
_parser.add_argument("-v", action="store_true", help="時間別の行も表示する")
_parser.add_argument("--cache-dir", default=core.CACHE_DIR, help="*_1d_past5d.json を読むディレクトリ(既定: cache/)")
_args = _parser.parse_args()
verbose = _args.v


def load(model, lat, lon):
    path = os.path.join(_args.cache_dir, f"{model}_{lat:.4f}_{lon:.4f}_1d_past{PAST_DAYS}d.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


for name, day in REFS:
    mtn = next(x for x in core.MOUNTAINS if x["name"] == name)
    msm = load("jma_msm", mtn["lat"], mtn["lon"])
    ec = load("ecmwf_ifs025", mtn["lat"], mtn["lon"])
    summit_m, wind_var, wind_dir_var = m.wind_vars_for_elevation(mtn["elevation_m"])
    temp_var = m.temp_var_for_elevation(mtn["elevation_m"])
    # Same merge as detail.fetch_forecast(): MSM where present, else ECMWF,
    # then the altitude columns.
    ec_idx = {t: i for i, t in enumerate(ec["hourly"]["time"])}
    hourly = {"time": msm["hourly"]["time"]}
    for var in set(msm["hourly"]) | set(ec["hourly"]):
        if var == "time":
            continue
        mv, fv = msm["hourly"].get(var, []), ec["hourly"].get(var, [])
        merged = []
        for i, t in enumerate(hourly["time"]):
            v = mv[i] if i < len(mv) else None
            if v is None:
                j = ec_idx.get(t)
                v = fv[j] if j is not None and j < len(fv) else None
            merged.append(v)
        hourly[var] = merged
    hourly[core.MSM_PRECIP_VAR] = list(msm["hourly"]["precipitation"])
    targets = {core.band_label(a): a for a in core.FIXED_ALTITUDE_BANDS_M}
    targets[core.SUMMIT_LABEL] = summit_m
    core.add_altitude_columns(hourly, targets)
    hourly[core.CLIMB_LAYER_MOIST_VAR] = core.climb_layer_moist_series(hourly, summit_m)
    # Calibrated summit cloud (2026-09-19 R1). These are past dates, so the
    # lead is 1 (today = the reference day), same as the live pipeline's
    # day-0 rows.
    from datetime import date as _date
    hourly[core.SUMMIT_CLOUD_CAL_VAR] = core.calibrated_summit_cloud_series(hourly, summit_m, today=_date.fromisoformat(day))
    fc = {"hourly": hourly,
          "_daily_sunrise": dict(zip(ec["daily"]["time"], ec["daily"]["sunrise"])),
          "_daily_sunset": dict(zip(ec["daily"]["time"], ec["daily"]["sunset"]))}
    r = m.compute_day_scores(fc, wind_var, summit_m, temp_var)[day]
    print(f"{name} {day}: score={r['score']} cloud={r['cloud_pct']}(raw {r.get('cloud_pct_raw')}) wet={m.wet_fraction_cell(r)} "
          f"vis={r['ridge_visibility']} day_total_mm={r.get('day_total_precip_mm')}")
    if verbose:
        for i, t in enumerate(hourly["time"]):
            if t.startswith(day):
                print(f"  {t[11:16]} mm={hourly['precipitation'][i]} msm={hourly[core.MSM_PRECIP_VAR][i]} "
                      f"prob={hourly['precipitation_probability'][i]} "
                      f"c_summit={hourly[core.SUMMIT_VARS['cloudcover']][i]:.0f} rh_summit={hourly[core.SUMMIT_VARS['relative_humidity']][i]:.0f} "
                      f"moist={hourly[core.CLIMB_LAYER_MOIST_VAR][i]}")

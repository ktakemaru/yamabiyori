"""R1 の影響確認: 探索モードのキャッシュ (76 座 × 15 日; 既定は cache/、引数で別の checkout の cache/ を指定可。読むだけ)
で、山頂雲量の較正 (CLOUD_CALIBRATION_ENABLED) の有無で日スコアを比べる。0 点から動いた山日と 0 点になった山日は別枠で数える。"""
import json, os, sys
import mountain_weather_core as core
import mountain_weather_detail as m

SRC = sys.argv[1] if len(sys.argv) > 1 else core.CACHE_DIR   # 別の checkout の cache/ を読むときは引数で


def load(model, lat, lon):
    with open(os.path.join(SRC, f"{model}_{lat:.4f}_{lon:.4f}_15d.json"), encoding="utf-8") as f:
        return json.load(f)


def scores_for(mtn, enabled):
    core.CLOUD_CALIBRATION_ENABLED = enabled
    msm, ec = load("jma_msm", mtn["lat"], mtn["lon"]), load("ecmwf_ifs025", mtn["lat"], mtn["lon"])
    summit_m, wind_var, _ = m.wind_vars_for_elevation(mtn["elevation_m"])
    temp_var = m.temp_var_for_elevation(mtn["elevation_m"])
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
                j = ec_idx.get(t); v = fv[j] if j is not None and j < len(fv) else None
            merged.append(v)
        hourly[var] = merged
    hourly[core.MSM_PRECIP_VAR] = list(msm["hourly"]["precipitation"])
    targets = {core.band_label(a): a for a in core.FIXED_ALTITUDE_BANDS_M}; targets[core.SUMMIT_LABEL] = summit_m
    core.add_altitude_columns(hourly, targets)
    hourly[core.CLIMB_LAYER_MOIST_VAR] = core.climb_layer_moist_series(hourly, summit_m)
    hourly[core.SUMMIT_CLOUD_CAL_VAR] = core.calibrated_summit_cloud_series(hourly, summit_m)
    fc = {"hourly": hourly, "_daily_sunrise": dict(zip(ec["daily"]["time"], ec["daily"]["sunrise"])),
          "_daily_sunset": dict(zip(ec["daily"]["time"], ec["daily"]["sunset"]))}
    return m.compute_day_scores(fc, wind_var, summit_m, temp_var), hourly


rows = []
n_days = n_changed = 0
import collections
delta = collections.Counter(); revived = []; zeroed = []; high_unchanged = 0; high_total = 0
for mtn in core.MOUNTAINS:
    try:
        before, hourly = scores_for(mtn, False)
        after, _ = scores_for(mtn, True)
    except FileNotFoundError:
        continue
    for day in sorted(before):
        b, a = before[day], after[day]
        if b["score"] is None:
            continue
        n_days += 1
        if mtn["elevation_m"] > core.CLOUD_CALIBRATION_RAW_SUMMIT_M:
            high_total += 1
            high_unchanged += (b["score"] == a["score"])
            continue
        d = a["score"] - b["score"]
        if d == 0:
            continue
        n_changed += 1
        if b["score"] == 0 and a["score"] > 0:
            revived.append((mtn["name"], day, b["score"], a["score"], b["cloud_pct"], a["cloud_pct"], m.wet_fraction_cell(a)))
        elif a["score"] == 0 and b["score"] > 0:
            zeroed.append((mtn["name"], day, b["score"], a["score"], b["cloud_pct"], a["cloud_pct"]))
        else:
            delta["<-20" if d < -20 else "-20..-10" if d < -10 else "-10..-5" if d < -5 else "-5..0" if d < 0 else "0..+5" if d <= 5 else "+5..+10" if d <= 10 else ">+10"] += 1
        rows.append((mtn["name"], mtn["elevation_m"], day, b["score"], a["score"], b["cloud_pct"], a["cloud_pct"]))
print(f"scored mountain-days: {n_days}; summits >{core.CLOUD_CALIBRATION_RAW_SUMMIT_M:.0f}m: {high_total} days, unchanged {high_unchanged}")
print(f"changed (summit <= {core.CLOUD_CALIBRATION_RAW_SUMMIT_M:.0f}m): {n_changed}; delta buckets (excluding 0-point crossings): {dict(delta)}")
print(f"revived from 0 (before 0 -> after >0): {len(revived)}")
for r in revived:
    print("   ", r)
print(f"dropped to 0 (before >0 -> after 0): {len(zeroed)}")
for r in zeroed:
    print("   ", r)
print("name elev day score_raw -> score_cal | cloud_raw -> cloud_cal (largest drops first)")
for r in sorted(rows, key=lambda r: r[4] - r[3])[:25]:
    print(f"{r[0]:12s} {r[1]:5d} {r[2]} {r[3]:6.1f} -> {r[4]:6.1f} | {r[5]:5.1f} -> {r[6]:5.1f}")
print("... largest rises:")
for r in sorted(rows, key=lambda r: r[4] - r[3])[-8:]:
    print(f"{r[0]:12s} {r[1]:5d} {r[2]} {r[3]:6.1f} -> {r[4]:6.1f} | {r[5]:5.1f} -> {r[6]:5.1f}")

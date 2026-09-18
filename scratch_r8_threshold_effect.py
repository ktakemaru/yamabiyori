"""R8 の影響確認: 探索モードのキャッシュ (76 座 × 15 日; 既定は cache/、引数で別の checkout の cache/ を指定可。読むだけ)
で、WET_HOUR_MSM_PRECIP_MM 0.1 と 0.5 の日スコアを比べる。MSM 範囲内 (MSM 降水量が取れる日) だけが変わる。"""
import json, os, sys
import mountain_weather_core as core
import mountain_weather_detail as m

SRC = sys.argv[1] if len(sys.argv) > 1 else core.CACHE_DIR   # 別の checkout の cache/ を読むときは引数で


def load(model, lat, lon):
    with open(os.path.join(SRC, f"{model}_{lat:.4f}_{lon:.4f}_15d.json"), encoding="utf-8") as f:
        return json.load(f)


def scores_for(mtn, th):
    core.WET_HOUR_MSM_PRECIP_MM = th
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
    fc = {"hourly": hourly, "_daily_sunrise": dict(zip(ec["daily"]["time"], ec["daily"]["sunrise"])),
          "_daily_sunset": dict(zip(ec["daily"]["time"], ec["daily"]["sunset"]))}
    return m.compute_day_scores(fc, wind_var, summit_m, temp_var), hourly


rows = []
n_days = n_changed = 0
for mtn in core.MOUNTAINS:
    try:
        before, hourly = scores_for(mtn, 0.1)
        after, _ = scores_for(mtn, 0.5)
    except FileNotFoundError:
        continue
    for day in sorted(before):
        b, a = before[day], after[day]
        if b["score"] is None:
            continue
        n_days += 1
        if b["score"] != a["score"] or m.wet_fraction_cell(b) != m.wet_fraction_cell(a):
            n_changed += 1
            # その日の活動時間中の MSM 降水量 (0.1〜0.4 の時間数と最大値)
            mm = [hourly[core.MSM_PRECIP_VAR][i] for i, t in enumerate(hourly["time"]) if t.startswith(day) and hourly[core.MSM_PRECIP_VAR][i] is not None]
            band = sum(1 for v in mm if 0.1 <= v < 0.5); big = sum(1 for v in mm if v >= 0.5)
            rows.append((mtn["name"], day, b["score"], a["score"], m.wet_fraction_cell(b), m.wet_fraction_cell(a), band, big, max(mm) if mm else None, sum(mm) if mm else None))
print(f"scored mountain-days: {n_days}, changed: {n_changed}")
print("name day score_0.1 score_0.5 wet_0.1 wet_0.5 | hours 0.1-0.4 / >=0.5 (whole day, MSM) | max mm/h | day sum mm")
for r in sorted(rows, key=lambda r: r[3] - r[2], reverse=True):
    print(f"{r[0]:12s} {r[1]} {r[2]:6.1f} -> {r[3]:6.1f}  {r[4]:22s} -> {r[5]:22s} | {r[6]:2d} / {r[7]:2d} | {r[8]} | {r[9]:.1f}")

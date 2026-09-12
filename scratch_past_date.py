"""One-off past-date lookup, not part of the main pipeline.
Usage: python scratch_past_date.py

Replicates detail.fetch_forecast()'s MSM(preferred)+ECMWF(fallback) merge but
with past_days instead of a forward-looking days=N, so a date within MSM's
archived range (jma_msm's past_days data is available at least a few days
back, confirmed 2026-09-12) is judged by the real wet-hour rule (MSM
precip/moisture-layer) instead of falling back to the ECMWF-probability rule
meant for beyond-MSM-range dates. Passing model=None here would silently
switch every hour to that fallback rule -- see SKILL.md known pitfall 8.
"""
import mountain_weather_core as core
import mountain_weather_detail as m
import mountain_terrain as terrain

TARGET_DATE = "2026-09-10"
MOUNTAIN_NAME = "西穂高岳"
PAST_DAYS = 3

mtn = next(x for x in m.MOUNTAINS if x["name"] == MOUNTAIN_NAME)
summit_m, wind_speed_var, wind_dir_var = m.wind_vars_for_elevation(mtn["elevation_m"])
temp_var = m.temp_var_for_elevation(mtn["elevation_m"])
all_vars = m.HOURLY_VARS + core.level_stack_vars()


def fetch(model, with_sunrise=False):
    daily_vars = ["sunrise", "sunset"] if with_sunrise else None
    return m.fetch_open_meteo(mtn["lat"], mtn["lon"], hourly=all_vars, daily=daily_vars,
                               days=1, model=model, past_days=PAST_DAYS)


msm = fetch("jma_msm")
fallback = fetch("ecmwf_ifs025", with_sunrise=True)
freezing = m.fetch_open_meteo(mtn["lat"], mtn["lon"], hourly=[m.FREEZING_LEVEL_VAR],
                               days=1, model=None, past_days=PAST_DAYS)
visibility = m.fetch_open_meteo(mtn["lat"], mtn["lon"], hourly=[m.VISIBILITY_VAR],
                                 days=1, model=None, past_days=PAST_DAYS)

fallback_index = {t: i for i, t in enumerate(fallback["hourly"]["time"])}
freezing_index = {t: i for i, t in enumerate(freezing["hourly"]["time"])}
visibility_index = {t: i for i, t in enumerate(visibility["hourly"]["time"])}
hourly = {"time": msm["hourly"]["time"]}
for var in all_vars:
    msm_values = msm["hourly"].get(var, [])
    fb_values = fallback["hourly"].get(var, [])
    merged = []
    for i, t in enumerate(hourly["time"]):
        v = msm_values[i] if i < len(msm_values) else None
        if v is None:
            j = fallback_index.get(t)
            v = fb_values[j] if j is not None and j < len(fb_values) else None
        merged.append(v)
    hourly[var] = merged

freezing_series = freezing["hourly"].get(m.FREEZING_LEVEL_VAR, [])
hourly[m.FREEZING_LEVEL_VAR] = [
    freezing_series[j] if (j := freezing_index.get(t)) is not None and j < len(freezing_series) else None
    for t in hourly["time"]
]
visibility_series = visibility["hourly"].get(m.VISIBILITY_VAR, [])
hourly[m.VISIBILITY_VAR] = [
    visibility_series[j] if (j := visibility_index.get(t)) is not None and j < len(visibility_series) else None
    for t in hourly["time"]
]
# Raw MSM precipitation, un-merged -- the wet-hour judgment needs to know
# which hours MSM actually covered (see core.is_wet_hour/wet_hour_weight).
hourly[core.MSM_PRECIP_VAR] = list(msm["hourly"].get("precipitation", []))

targets = {core.band_label(a): a for a in core.FIXED_ALTITUDE_BANDS_M}
targets[core.SUMMIT_LABEL] = summit_m
core.add_altitude_columns(hourly, targets)
hourly[core.CLIMB_LAYER_MOIST_VAR] = core.climb_layer_moist_series(hourly, summit_m)
profile = terrain.terrain_for(MOUNTAIN_NAME)
if profile:
    core.add_altitude_columns(hourly, {m.CLIMB_BASE_LABEL: summit_m - core.CLIMB_LAYER_DEPTH_M},
                              ["windspeed", "winddirection"])
    hourly[m.UPSLOPE_VAR] = terrain.upslope_lift_series(
        hourly, profile, core.SUMMIT_VARS["winddirection"], core.SUMMIT_VARS["windspeed"])
    hourly[m.UPSLOPE_BASE_VAR] = terrain.upslope_lift_series(
        hourly, profile, core.altitude_col("winddirection", m.CLIMB_BASE_LABEL),
        core.altitude_col("windspeed", m.CLIMB_BASE_LABEL))

daily_sunrise = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunrise"]))
daily_sunset = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunset"]))
forecast = {"hourly": hourly, "_daily_sunrise": daily_sunrise, "_daily_sunset": daily_sunset}

day_scores = m.compute_day_scores(forecast, wind_speed_var, summit_m, temp_var)
if TARGET_DATE in day_scores:
    m.print_day_score_table(mtn, {TARGET_DATE: day_scores[TARGET_DATE]})

rows = m.build_hourly_rows(forecast, wind_speed_var, wind_dir_var, summit_m=summit_m, temp_var=temp_var)
rows = [r for r in rows if r["date"] == TARGET_DATE]

if not rows:
    print(f"{TARGET_DATE} のデータが取得できませんでした(過去{92}日以内か確認してください)。")
else:
    m.print_hourly_table(mtn, rows)
    transition = m.detect_weather_transition(rows)
    if transition:
        print(f"\n※{TARGET_DATE}: {m.transition_note(transition)}")

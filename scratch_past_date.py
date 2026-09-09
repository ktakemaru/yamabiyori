"""One-off past-date lookup, not part of the main pipeline.
Usage: python scratch_past_date.py
"""
import mountain_weather_core as core
import mountain_weather_detail as m

TARGET_DATE = "2026-09-05"
MOUNTAIN_NAME = "槍ヶ岳"
PAST_DAYS = 2

mtn = next(x for x in m.MOUNTAINS if x["name"] == MOUNTAIN_NAME)
summit_m, wind_speed_var, wind_dir_var = m.wind_vars_for_elevation(mtn["elevation_m"])
temp_var = m.temp_var_for_elevation(mtn["elevation_m"])
hourly_vars = m.HOURLY_VARS + core.level_stack_vars() + [m.FREEZING_LEVEL_VAR]

raw = m.fetch_open_meteo(mtn["lat"], mtn["lon"], hourly=hourly_vars, daily=["sunrise", "sunset"],
                          days=1, model=None, past_days=PAST_DAYS)

daily_sunrise = dict(zip(raw["daily"]["time"], raw["daily"]["sunrise"]))
daily_sunset = dict(zip(raw["daily"]["time"], raw["daily"]["sunset"]))
hourly = dict(raw["hourly"])
# Same per-altitude columns fetch_forecast() would synthesize (bands + summit
# + climb-layer moisture). No MSM column here (model=None) -> every hour is
# judged on the probability rule.
targets = {core.band_label(a): a for a in core.FIXED_ALTITUDE_BANDS_M}
targets[core.SUMMIT_LABEL] = summit_m
core.add_altitude_columns(hourly, targets)
hourly[core.CLIMB_LAYER_MOIST_VAR] = core.climb_layer_moist_series(hourly, summit_m)
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

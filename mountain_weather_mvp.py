"""
Mountain Weather Dashboard - MVP script
----------------------------------------
Fetches hourly forecast data (cloud cover, precip, wind, temperature, CAPE)
from the free Open-Meteo API for a shortlist of mountains, and ranks them by
a climbing-oriented score combining thunderstorm risk, ridge wind, ridge
visibility, precipitation, and wind-chill across three time windows: AM
(climb), ridge/summit dwell, and PM (descent, weighted toward afternoon
convective/thunderstorm risk). See `mountain_climb_score()`.

Shares its mountain pool, cache layer, and scoring formula with
mountain_weather_detail.py via mountain_weather_core.py (see that module's
docstring for exactly what's shared vs. kept separate).

Requirements:
    pip install requests

Usage:
    python mountain_weather_mvp.py
"""

import requests
from datetime import date, datetime, timedelta

from mountain_weather_core import (
    MOUNTAINS, pad,
    FIXED_ALTITUDE_BANDS_M, ALTITUDE_BAND_HPA, BAND_VARS, WIND_SPEED_BAND_VARS,
    nearest_band, KMH_TO_MS, wind_chill_c, safe_avg, safe_avg_or_none,
    WIND_PEAK_WARNING_MS, SCORE_WEIGHTS, mountain_climb_score,
    format_date_with_weekday,
    PRECIP_TIMING_UNCERTAIN_DAYS_OUT, PRECIP_TIMING_BOUNDARY_LOW,
    PRECIP_TIMING_BOUNDARY_HIGH, PRECIP_TIMING_NOTE,
    fetch_open_meteo,
)

# ---------------------------------------------------------------------------
# Step 2: Filters — edit these to narrow the output
#   REGION_FILTER: list of regions to include, e.g. ["関東甲信"]. Empty = all.
#   TOP_N_PER_DAY: how many mountains to show per date
# ---------------------------------------------------------------------------
REGION_FILTER: list[str] = []
TOP_N_PER_DAY = None  # None = show all mountains that clear MIN_SCORE_THRESHOLD; or set an int to cap it
MIN_SCORE_THRESHOLD = 80.0  # see mountain_climb_score(): thunder/wind/cloud/precip/temp blend; below this, exclude

# ---------------------------------------------------------------------------
# Pressure levels / fixed altitude bands / cache layer: shared with
# mountain_weather_detail.py, see mountain_weather_core.py (imported above).
# ---------------------------------------------------------------------------

# Three evaluation windows per day, matching the phases of a typical day
# hike rather than just "the morning":
#   AM (登り)      -- sunrise-relative, as before. Precip during the climb.
#   RIDGE (稜線帯滞在) -- fixed clock hours. Wind/visibility/wind-chill at the
#                       altitude band nearest the summit, i.e. what you'd
#                       actually feel while up there.
#   PM (下山/対流リスク) -- fixed clock hours, later than RIDGE. Convective
#                       thunderstorm risk (CAPE) typically builds through
#                       the afternoon; scoring this window is what makes the
#                       classic "early start, early finish" advice show up
#                       as a lower score for days where afternoon storms are
#                       likely, even if the AM/ridge periods look clear.
TRIP_START_OFFSET_HOURS = -1
TRIP_DURATION_HOURS = 6
RIDGE_START_HOUR = 8
RIDGE_END_HOUR = 11
PM_START_HOUR = 12
PM_END_HOUR = 17

# Model priority: we query jma_msm and best_match separately, then merge
# per-hour, preferring jma_msm wherever it has data (roughly the first few
# days) and falling back to best_match beyond that or wherever MSM is null.
# precipitation (mm) and cape (convective available potential energy, a
# standard thunderstorm-risk proxy) are new vs. the original MVP scoring.
HOURLY_VARS = ["cloudcover", "cloudcover_low", "cloudcover_mid",
               "cloudcover_high", "precipitation_probability",
               "precipitation", "cape"]

# Visibility (m) isn't served for the named jma_msm/ecmwf_ifs025 models
# (comes back all-null) -- only for Open-Meteo's blended "best_match", the
# same restriction mountain_weather_detail.py already works around for
# freezing_level_height. So it's fetched via a third, separate call
# (model=None) in fetch_forecast() and merged in by time, rather than being
# part of HOURLY_VARS above.
VISIBILITY_VAR = "visibility"

# Temperature at the same fixed 1000/2000/3000m bands used for cloud cover
# (BAND_VARS, from core) -- kept on the same fixed-band grid (rather than
# each mountain's own exact elevation, as detail.py does) so every mountain
# can still be fetched with one shared variable set. mvp.py-only: detail.py
# derives temperature from the selected mountain's own nearest pressure
# level instead (see temp_var_for_elevation() there).
TEMP_BAND_VARS = {alt: f"temperature_{hpa}hPa" for alt, hpa in ALTITUDE_BAND_HPA.items()}


def fetch_single_model(lat: float, lon: float, model, days: int, with_sunrise: bool = False,
                        extra_hourly: list = None) -> dict:
    hourly_vars = (HOURLY_VARS + list(BAND_VARS.values())
                   + list(WIND_SPEED_BAND_VARS.values()) + list(TEMP_BAND_VARS.values()))
    if extra_hourly:
        hourly_vars = hourly_vars + extra_hourly
    daily_vars = ["sunrise"] if with_sunrise else None
    return fetch_open_meteo(lat, lon, hourly=hourly_vars, daily=daily_vars, days=days, model=model)


def fetch_forecast(lat: float, lon: float, days: int = 15) -> dict:
    """Fetch hourly surface cloud cover/precip/cape plus cloud cover, wind
    speed and temperature at the fixed 1000/2000/3000m altitude bands (same
    bands for every mountain) -- merging JMA MSM (preferred, where available)
    with ECMWF IFS 0.25° (fallback for hours MSM doesn't cover, up to 15
    days). Also fetches daily sunrise times (astronomical, not model-
    dependent) from the ECMWF call, since it covers the full day range we
    need. Visibility is fetched via a third, separate best_match call
    (model=None) since jma_msm/ecmwf_ifs025 return null for it -- see
    VISIBILITY_VAR's comment. All three calls go through fetch_single_model
    -> fetch_open_meteo, so repeat runs within CACHE_TTL_SECONDS hit the
    on-disk cache/ instead of the network -- see that function's docstring."""
    all_vars = (HOURLY_VARS + list(BAND_VARS.values())
                + list(WIND_SPEED_BAND_VARS.values()) + list(TEMP_BAND_VARS.values()))

    msm = fetch_single_model(lat, lon, "jma_msm", days)
    fallback = fetch_single_model(lat, lon, "ecmwf_ifs025", days, with_sunrise=True)
    best = fetch_single_model(lat, lon, None, days, extra_hourly=[VISIBILITY_VAR])

    # Merge per-timestamp: prefer MSM's value when present and not null,
    # otherwise use best_match's value for that same timestamp.
    fallback_index = {t: i for i, t in enumerate(fallback["hourly"]["time"])}
    merged_hourly = {"time": msm["hourly"]["time"]}
    for var in all_vars:
        merged_values = []
        msm_values = msm["hourly"].get(var, [])
        fb_values = fallback["hourly"].get(var, [])
        for i, t in enumerate(msm["hourly"]["time"]):
            v = msm_values[i] if i < len(msm_values) else None
            if v is None:
                j = fallback_index.get(t)
                v = fb_values[j] if j is not None and j < len(fb_values) else None
            merged_values.append(v)
        merged_hourly[var] = merged_values

    best_index = {t: i for i, t in enumerate(best["hourly"]["time"])}
    vis_values = best["hourly"].get(VISIBILITY_VAR, [])
    merged_hourly[VISIBILITY_VAR] = [
        vis_values[j] if (j := best_index.get(t)) is not None and j < len(vis_values) else None
        for t in msm["hourly"]["time"]
    ]

    daily_sunrise = dict(zip(fallback["daily"]["time"], fallback["daily"]["sunrise"]))
    return {"hourly": merged_hourly, "_daily_sunrise": daily_sunrise}


def window_scores_by_day(forecast: dict) -> dict:
    """Compute per-day AM / ridge / PM window summaries, returned as
    {date_str: {"am": {...}, "ridge": {...}, "pm": {...}, "day_peak": {...}}}.

    - am: precip probability/amount during the sunrise-relative climb
      window (TRIP_START_OFFSET_HOURS/TRIP_DURATION_HOURS).
    - ridge: cloud cover, wind speed and temperature at each fixed altitude
      band, averaged over the fixed RIDGE_START_HOUR-RIDGE_END_HOUR clock
      window -- what the ridge/summit dwell actually feels like.
    - pm: peak (not average) CAPE over the fixed PM_START_HOUR-PM_END_HOUR
      clock window. Peak, not average, because thunderstorm risk is a
      threshold hazard -- a 2-hour spike matters even if the window's mean
      looks tame.
    - day_peak: per band, the single highest wind speed (km/h) and the hour
      it occurred at, over the wider AM-start..PM-end span (not just the
      ridge window). ridge_wind_ms above is an 8-11 average and can smooth
      away a real gust that happens outside that window -- e.g. a pre-dawn
      squall a 4am early-starter would actually walk into. This doesn't
      feed mountain_climb_score (the ridge-window design is intentional,
      see wind_penalty's docstring); main() uses it only to flag
      WIND_PEAK_WARNING_MS+ gusts sitting outside the scored window.
    - day_total_precip_mm (2026-09 addition): the full calendar day's
      (00:00-23:59 local) summed precipitation, unconditionally -- NOT
      windowed to AM/PM/ridge like everything else here. am's precip_mm
      (what the score itself uses, via precip_penalty) only looks at the AM
      climb window by design -- the right scope for "can I climb in this"
      -- but a reader glancing only at am precip_prob/the score can come
      away thinking a day is fine when heavy rain is actually falling most
      of the day outside that window (confirmed against a real Windy/MSM
      comparison, 2026-09: an AM precip probability of ~50% coexisted with
      a ~114mm calendar-day total). Same reasoning as day_peak above -- a
      hazard the score's own window doesn't cover still deserves to be
      visible somewhere, without folding it into mountain_climb_score
      itself (unchanged weights/inputs). Identical field, same rationale,
      as detail.py's compute_day_scores()."""
    times = forecast["hourly"]["time"]
    precip_prob_series = forecast["hourly"]["precipitation_probability"]
    precip_mm_series = forecast["hourly"]["precipitation"]
    cape_series = forecast["hourly"]["cape"]
    cloud_band_series = {alt: forecast["hourly"][var] for alt, var in BAND_VARS.items()}
    wind_band_series = {alt: forecast["hourly"][var] for alt, var in WIND_SPEED_BAND_VARS.items()}
    temp_band_series = {alt: forecast["hourly"][var] for alt, var in TEMP_BAND_VARS.items()}
    visibility_series = forecast["hourly"][VISIBILITY_VAR]
    daily_sunrise = forecast["_daily_sunrise"]

    am_by_day: dict = {}
    ridge_by_day: dict = {}
    pm_by_day: dict = {}
    day_by_day: dict = {}
    day_precip_by_day: dict = {}

    for idx, t in enumerate(times):
        day_str = t.split("T")[0]
        t_dt = datetime.fromisoformat(t)

        # Unconditional (no window gate) -- every hour of the calendar day,
        # unlike am_by_day/ridge_by_day/pm_by_day below.
        if precip_mm_series[idx] is not None:
            day_precip_by_day[day_str] = day_precip_by_day.get(day_str, 0.0) + precip_mm_series[idx]

        sunrise_iso = daily_sunrise.get(day_str)
        if sunrise_iso is not None:
            am_start = datetime.fromisoformat(sunrise_iso) + timedelta(hours=TRIP_START_OFFSET_HOURS)
            am_end = am_start + timedelta(hours=TRIP_DURATION_HOURS)
            if am_start <= t_dt < am_end:
                entry = am_by_day.setdefault(day_str, {"precip_prob": [], "precip_mm": []})
                entry["precip_prob"].append(precip_prob_series[idx])
                entry["precip_mm"].append(precip_mm_series[idx])

            # Wider span for day_peak wind detection: AM start through
            # PM_END_HOUR the same day (covers early starts *and* the
            # descent), independent of the narrower RIDGE window used for
            # scoring. No daily sunset is fetched in this file, so PM_END_HOUR
            # (a fixed clock hour, like RIDGE/PM already are) stands in for
            # "end of the climbing day" rather than actual sunset.
            day_end = t_dt.replace(hour=PM_END_HOUR, minute=0, second=0, microsecond=0)
            if am_start <= t_dt < day_end:
                entry = day_by_day.setdefault(day_str, {f"wind_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M})
                for alt in FIXED_ALTITUDE_BANDS_M:
                    entry[f"wind_{alt}m"].append((wind_band_series[alt][idx], t_dt.hour))

        if RIDGE_START_HOUR <= t_dt.hour < RIDGE_END_HOUR:
            entry = ridge_by_day.setdefault(
                day_str,
                {**{f"cloud_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                 **{f"wind_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                 **{f"temp_{a}m": [] for a in FIXED_ALTITUDE_BANDS_M},
                 "visibility": []},
            )
            for alt in FIXED_ALTITUDE_BANDS_M:
                entry[f"cloud_{alt}m"].append(cloud_band_series[alt][idx])
                entry[f"wind_{alt}m"].append(wind_band_series[alt][idx])
                entry[f"temp_{alt}m"].append(temp_band_series[alt][idx])
            # Visibility is a single surface-ish value at the mountain's own
            # coordinates (Open-Meteo doesn't serve it per pressure-level
            # band like cloud/wind/temp), so it isn't split by altitude band.
            entry["visibility"].append(visibility_series[idx])

        if PM_START_HOUR <= t_dt.hour < PM_END_HOUR:
            pm_by_day.setdefault(day_str, {"cape": []})["cape"].append(cape_series[idx])

    summary = {}
    for day_str in set(am_by_day) | set(ridge_by_day) | set(pm_by_day):
        am_vals = am_by_day.get(day_str, {"precip_prob": [], "precip_mm": []})
        ridge_vals = ridge_by_day.get(day_str, {})
        pm_vals = pm_by_day.get(day_str, {"cape": []})

        ridge = {}
        for alt in FIXED_ALTITUDE_BANDS_M:
            ridge[f"cloud_{alt}m"] = safe_avg(ridge_vals.get(f"cloud_{alt}m", []))
            ridge[f"wind_{alt}m"] = safe_avg(ridge_vals.get(f"wind_{alt}m", []))
            ridge[f"temp_{alt}m"] = safe_avg(ridge_vals.get(f"temp_{alt}m", []))
        ridge["visibility"] = safe_avg_or_none(ridge_vals.get("visibility", []))

        day_vals = day_by_day.get(day_str, {})
        day_peak = {}
        for alt in FIXED_ALTITUDE_BANDS_M:
            clean = [(w, h) for w, h in day_vals.get(f"wind_{alt}m", []) if w is not None]
            if clean:
                wind_kmh, hour = max(clean, key=lambda pair: pair[0])
                day_peak[alt] = {"wind_ms": wind_kmh * KMH_TO_MS, "hour": hour}
            else:
                day_peak[alt] = {"wind_ms": None, "hour": None}

        cape_clean = [v for v in pm_vals["cape"] if v is not None]
        summary[day_str] = {
            "am": {
                "precip_prob": safe_avg(am_vals["precip_prob"]),
                "precip_mm": safe_avg(am_vals["precip_mm"]),
            },
            "ridge": ridge,
            "pm": {"cape": max(cape_clean) if cape_clean else None},
            "day_peak": day_peak,
            "day_total_precip_mm": round(day_precip_by_day[day_str], 1) if day_str in day_precip_by_day else None,
        }
    return summary


def fmt(val, ndigits: int = 1) -> str:
    """None-safe rounded string for table cells."""
    if val is None:
        return "-"
    return str(round(val, ndigits))


def wind_peak_warning_cell(r) -> str:
    """'⚠HH時 n.nm/s' when a day_peak gust hit WIND_PEAK_WARNING_MS+ outside
    the scored ridge window (see main()'s wind_warning), else '-'."""
    if not r["wind_warning"]:
        return "-"
    return f"⚠{r['day_peak_wind_hour']:02d}時{fmt(r['day_peak_wind_ms'])}m/s"


def precip_timing_note(day_index: int, rows: list):
    """PRECIP_TIMING_NOTE if this day is PRECIP_TIMING_UNCERTAIN_DAYS_OUT+
    out (day_index is 0 for today), or any row's AM precip probability
    falls inside the ambiguous 40-60% band, else None."""
    if day_index >= PRECIP_TIMING_UNCERTAIN_DAYS_OUT:
        return PRECIP_TIMING_NOTE
    for r in rows:
        prob = r["am_precip_prob"]
        if prob is not None and PRECIP_TIMING_BOUNDARY_LOW <= prob <= PRECIP_TIMING_BOUNDARY_HIGH:
            return PRECIP_TIMING_NOTE
    return None


def print_ranking_table(rows):
    header = (
        pad("順位", 5) + pad("山", 26) + pad("アクセス", 24)
        + pad("スコア", 8) + pad("稜線風速m/s", 12)
        + pad("雷リスクCAPE", 14) + pad("稜線雲量%", 10) + pad("視程m(稜線)", 10)
        + pad("降水確率%(登り)", 16) + pad("体感温度℃(稜線)", 16)
        + pad("強風注意(圏外)", 16) + pad("降水量mm(全日)", 14)
    )
    print(header)
    for i, r in enumerate(rows, start=1):
        row = (
            pad(str(i), 5) + pad(r["mountain"], 26) + pad(r["access"], 24)
            + pad(str(r["score"]), 8) + pad(fmt(r["ridge_wind_ms"]), 12)
            + pad(fmt(r["cape"], 0), 14) + pad(fmt(r["ridge_cloud"]), 10)
            + pad(fmt(r["ridge_visibility"], 0), 10)
            + pad(fmt(r["am_precip_prob"]), 16) + pad(fmt(r["chill"]), 16)
            + pad(wind_peak_warning_cell(r), 16)
            + pad(fmt(r.get("day_total_precip_mm"), 0), 14)
        )
        print(row)


def main():
    today = date.today()
    target_dates = [str(today + timedelta(days=i)) for i in range(15)]  # today .. +14

    pool = MOUNTAINS
    if REGION_FILTER:
        pool = [m for m in MOUNTAINS if m["region"] in REGION_FILTER]

    # date -> list of per-mountain results for that date
    by_date: dict[str, list] = {d: [] for d in target_dates}

    for mtn in pool:
        try:
            forecast = fetch_forecast(mtn["lat"], mtn["lon"])
        except requests.exceptions.RequestException as e:
            print(f"  ({mtn['name']}: 取得失敗のためスキップ - {e})")
            continue
        by_day = window_scores_by_day(forecast)
        for d in target_dates:
            if d in by_day:
                windows = by_day[d]
                band = nearest_band(mtn["elevation_m"])
                ridge, am, pm = windows["ridge"], windows["am"], windows["pm"]
                day_peak = windows["day_peak"][band]
                ridge_wind_ms = ridge[f"wind_{band}m"] * KMH_TO_MS
                chill = wind_chill_c(ridge[f"temp_{band}m"], ridge[f"wind_{band}m"])
                score = mountain_climb_score(
                    ridge_wind_ms=ridge_wind_ms,
                    ridge_cloud_pct=ridge[f"cloud_{band}m"],
                    ridge_visibility_m=ridge["visibility"],
                    chill_c=chill,
                    am_precip_prob=am["precip_prob"],
                    am_precip_mm=am["precip_mm"],
                    pm_cape=pm["cape"],
                )
                # Flag a day_peak gust only when it's both above the danger
                # threshold AND sitting outside the scored ridge window --
                # inside the window it's already reflected in ridge_wind_ms/
                # the score, so a separate flag there would just be noise.
                wind_warning = (
                    day_peak["wind_ms"] is not None
                    and day_peak["wind_ms"] >= WIND_PEAK_WARNING_MS
                    and not (RIDGE_START_HOUR <= day_peak["hour"] < RIDGE_END_HOUR)
                )
                by_date[d].append(
                    {
                        "mountain": f'{mtn["name"]}({mtn["elevation_m"]}m)',
                        "access": mtn["access"],
                        "region": mtn["region"],
                        "score": score,
                        "ridge_wind_ms": round(ridge_wind_ms, 1),
                        "cape": pm["cape"],
                        "ridge_cloud": round(ridge[f"cloud_{band}m"], 1),
                        "ridge_visibility": round(ridge["visibility"]) if ridge["visibility"] is not None else None,
                        "am_precip_prob": round(am["precip_prob"], 1),
                        "chill": round(chill, 1) if chill is not None else None,
                        "day_peak_wind_ms": round(day_peak["wind_ms"], 1) if day_peak["wind_ms"] is not None else None,
                        "day_peak_wind_hour": day_peak["hour"],
                        "wind_warning": wind_warning,
                        "day_total_precip_mm": windows["day_total_precip_mm"],
                    }
                )

    region_label = "、".join(REGION_FILTER) if REGION_FILTER else "全地域"
    count_label = "すべて" if TOP_N_PER_DAY is None else f"上位{TOP_N_PER_DAY}件"
    print(f"\n【対象地域: {region_label} / 1日あたり{count_label} / "
          f"score{MIN_SCORE_THRESHOLD}以上のみ / "
          f"登山向け総合スコア(雷{int(SCORE_WEIGHTS['thunder']*100)}%・風{int(SCORE_WEIGHTS['wind']*100)}%・"
          f"稜線雲量{int(SCORE_WEIGHTS['cloud']*100)}%・視程{int(SCORE_WEIGHTS['visibility']*100)}%・"
          f"降水{int(SCORE_WEIGHTS['precip']*100)}%・"
          f"体感温度{int(SCORE_WEIGHTS['temp']*100)}%) / "
          f"AM:日の出{TRIP_START_OFFSET_HOURS:+.0f}h〜{TRIP_DURATION_HOURS}時間 "
          f"稜線帯:{RIDGE_START_HOUR}-{RIDGE_END_HOUR}時 PM(雷):{PM_START_HOUR}-{PM_END_HOUR}時】")

    for week_label, day_range in (("=== 今週 (0-6日先) ===", range(0, 7)),
                                   ("=== 来週以降 (7-14日先) ===", range(7, 15))):
        print(f"\n{week_label}")
        for i in day_range:
            d = target_dates[i]
            qualifying = [r for r in by_date[d] if r["score"] >= MIN_SCORE_THRESHOLD]
            ranked = sorted(qualifying, key=lambda r: r["score"], reverse=True)
            day_rows = ranked if TOP_N_PER_DAY is None else ranked[:TOP_N_PER_DAY]
            if not day_rows:
                continue
            print(f"\n-- {format_date_with_weekday(d)} --")
            print_ranking_table(day_rows)
            note = precip_timing_note(i, day_rows)
            if note:
                print(note)


if __name__ == "__main__":
    main()

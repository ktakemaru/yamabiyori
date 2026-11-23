"""1 座の展望図を作る入口(診断モード mountain_weather_detail.py の --panorama から呼ぶ)。

流れ(指示書 9・10 章):
1. 山名の材料: 「日本の主な山岳標高」(1003 山)の表が config.gsi_dir() にあれば使う(そのほかの山の名前も出る)。
   無ければ、リポジトリの山頂の点 panorama/data/summits.json(日本百名山と 76 座に当たる点だけ)と MOUNTAINS
   (図に「名前は日本百名山とヤマビヨリの 76 座です」と書く)。表は自分では取りに行かない。
   表に当たる点の無い 76 座(11 座)は MOUNTAINS の座標を山頂の点にし、その山が視点の図に「山頂の点は目安です」と書く。
2. 視点: 山頂の点(1003 山の点、無ければ MOUNTAINS の座標)。OSM の展望地(山頂から 300m 以内・山頂より 50m を超えて
   低くない・名前のある点を優先・同じなら近い点)があれば、展望地の図も作る。OSM は Overpass に問い合わせて保存(失敗したら 5 秒あけて 1 回だけ取り直す)。
   取れなければ山頂の図だけ。
3. 標高タイル: その山に要る分だけ、1 枚ごとに 1 秒あけて取る(terrain_cache/)。取れないタイルがあれば図を作らない。
4. 図の地形(輪郭・見える山・名前の出る山の点): 計算して config.parts_dir() に保存。作ったときの条件(コードの版・辞書の版・
   視点の座標・1003 山の表)が合わなければ作り直す。
5. 予報: 視点ごとの方位 × 距離の点(108 点)・視点・名前の出る山の点(段階3.7 までの取り方)。山頂の図と展望地の図は、
   同じ山頂を中心にした点を使う(展望地は山頂から 300m 以内なので)。取れなければ「地形だけ」の図。
6. 図: 6・8・10・12 時と「地形だけ」。開いたときは 8 時。
"""
from __future__ import annotations

import glob
import hashlib
import html
import json
import math
import os
import pickle
import sys
import time
from datetime import date, timedelta

import numpy as np

import mountain_weather_core as core
import time_estimates

from . import config, draw, geometry, names, terrain, viewpoint, weather

PARTS_VERSION = "v1.10-2"         # 図の地形の計算を変えたら上げる(保存した図の地形を作り直す合図)
FUJI_ID = "m012"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_UA = "yamabiyori-panorama (github.com/ktakemaru/yamabiyori)"
OVERPASS_TIMEOUT_S = 60
OVERPASS_RETRIES = 1              # 502 など・つながらないとき、5 秒あけて 1 回だけ取り直す(指示書 v1.10 その2 の 7 章)
OVERPASS_RETRY_WAIT_S = 5.0
TILE_SEC_PER = time_estimates.PANORAMA_TILE_SEC    # 1 枚の秒数(実測 566 枚・12 分 14 秒。見積もりと同じ値)
PROGRESS_TILES = 50               # 標高タイルの進み具合を出す間隔(枚。仮の値)
PROGRESS_SEC = 60.0               # 同(秒。どちらか早いほう。仮の値)
HOURS = (6, 8, 10, 12)
DEFAULT_HOUR = 8


class PanoramaUnavailable(Exception):
    """展望図を作れない(理由は文で)。"""


class NotSupported(PanoramaUnavailable):
    """76 座に無い山。"""


def log_print(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# 1. 山名の材料と辞書
# ---------------------------------------------------------------------------
def dictionary_version() -> str:
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(config.DATA_DIR, "*.json"))):
        with open(p, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def summits_version() -> str:
    p = names.summits_path()
    if not os.path.exists(p):
        return "none"
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


SUMMIT_ROUGH_TEXT = "山頂の点は目安です(「日本の主な山岳標高」に当たる点が無いので、ヤマビヨリの山の一覧の座標を使っています)"


def build_context():
    """返り値: (ctx, gsi_file or None)。ctx は terrain.figure_parts に渡す形。"""
    led = terrain.TileLedger()
    ctx = terrain.build_ctx(None, led)
    ctx["materials_by_id"] = {m["id"]: m for m in ctx["materials"]}
    gsi = [m for m in ctx["materials"] if m.get("src") == "gsi1003"]
    gsi_file = None
    if gsi:
        paths = sorted(glob.glob(os.path.join(config.gsi_dir(), "*.geojson")))
        gsi_file = os.path.basename(paths[0]) if paths else None
    return ctx, gsi_file


# ---------------------------------------------------------------------------
# 2. 視点
# ---------------------------------------------------------------------------
def summit_viewer(mtn, ctx):
    """山頂の視点(段階2.5 の決まり): 1003 山の点がある山はその点、無ければ MOUNTAINS の座標。"""
    marks = ctx["marks"]
    q = [x for x in ctx["materials"] if mtn["id"] in marks.get(x["id"], {}).get("m76_ids", []) and x["src"] == "gsi1003"]
    rec = {"kind": "summit", "id": mtn["id"], "name": mtn["name"], "mountains_ele_m": float(mtn["elevation_m"]),
           "osm_used": False}
    if q:
        p = q[0]
        d, _ = geometry.vincenty_inverse(mtn["lat"], mtn["lon"], p["lat"], p["lon"])
        rec.update(lat=p["lat"], lon=p["lon"], source=viewpoint.SRC_SUMMIT, center_name=p["name"],
                   nominal_ele_m=float(p["ele_m"]), mt_offset_m=round(float(d), 1), summit_point=True)
    else:
        rec.update(lat=mtn["lat"], lon=mtn["lon"], source=viewpoint.SRC_MOUNTAINS, center_name=mtn["name"],
                   nominal_ele_m=float(mtn["elevation_m"]), mt_offset_m=0.0, summit_point=False)
    return rec


class LocalElev:
    """手元の標高タイルだけを引く(z14 → z11)。展望地と山頂の標高差を測るため。"""

    def __init__(self, z11, z14):
        self.dem = {14: z14, 11: z11}

    def pair(self, lat0, lon0, lat1, lon1):
        for z in (14, 11):
            v = self.dem[z].sample(np.array([lat0, lat1]), np.array([lon0, lon1]), fetch=False)
            if not np.isnan(v).any():
                return float(v[0]), float(v[1]), z
        return None, None, None


def osm_query(lat, lon, radius_m=viewpoint.VP_RADIUS_M):
    a = f"around:{radius_m:.0f},{lat:.6f},{lon:.6f}"
    return f"[out:json][timeout:25];(node[tourism=viewpoint]({a});way[tourism=viewpoint]({a}););out center tags;"


def osm_elements(mtn_id, lat, lon, stats):
    """その山の周り 300m の展望地(Overpass に 1 回。保存があれば読む)。返り値: (要素 or None, 理由)。"""
    path = os.path.join(config.osm_dir(), f"viewpoints_{mtn_id}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)["elements"], "保存済み"
        except (OSError, ValueError, KeyError):
            pass
    import requests
    r, why = None, None
    for attempt in range(1 + OVERPASS_RETRIES):
        if attempt:
            time.sleep(OVERPASS_RETRY_WAIT_S)
        stats["overpass_calls"] += 1
        try:
            r = requests.post(OVERPASS_URL, data={"data": osm_query(lat, lon)}, headers={"User-Agent": OVERPASS_UA},
                              timeout=OVERPASS_TIMEOUT_S)
        except requests.RequestException as e:
            r, why = None, f"Overpass に接続できない({type(e).__name__})"
            continue
        if r.status_code != 200:
            why = f"Overpass の応答 HTTP {r.status_code}"
            continue
        break
    if r is None or r.status_code != 200:
        return None, why + ("(取り直しても同じ)" if OVERPASS_RETRIES else "")
    try:
        data = r.json()
    except ValueError:
        return None, f"Overpass の応答が JSON でない(先頭: {r.text[:60]!r})"
    if "elements" not in data or data.get("remark"):
        return None, f"Overpass の応答に要素が無い({str(data.get('remark') or '')[:80]})"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"elements": data["elements"], "osm_base": (data.get("osm3s") or {}).get("timestamp_osm_base"),
                   "attribution": viewpoint.ATTRIBUTION}, fh, ensure_ascii=False)
    return data["elements"], "取得"


def choose_viewpoint(summit, elements, elev):
    """段階2.5 の決まり(viewpoints.build と同じ)。返り値: (展望地の視点 or None, 山頂の視点(名前を借りることがある))。"""
    hits = viewpoint.within(summit, elements, viewpoint.VP_RADIUS_M)
    cands = [viewpoint.vp_record(h, summit, elev) for h in hits]
    best, ok = viewpoint.choose(cands)
    if best is None:
        return None, summit
    if viewpoint.is_same_place(best):
        s = dict(summit, borrowed_name={"name": best["name"], "dist_m": best["dist_m"]}, osm_used=True)
        return None, s
    vp = {"kind": "viewpoint", "id": summit["id"], "name": summit["name"], "lat": best["lat"], "lon": best["lon"],
          "source": viewpoint.SRC_OSM, "label": best["name"] if best["has_name"] else "名前のない展望地",
          "has_name": best["has_name"], "dist_m": best["dist_m"], "az_deg": best["az_deg"], "az_name": best["az_name"],
          "dem_diff_m": best["dem_diff_m"], "direction_raw": best["direction_raw"],
          "center_is_summit": summit["summit_point"], "nominal_ele_m": summit["nominal_ele_m"], "osm_used": True}
    return vp, summit


# ---------------------------------------------------------------------------
# 3. 標高タイル
# ---------------------------------------------------------------------------
class TileProgress:
    """標高タイルの取得の進み具合(標準エラー)。PROGRESS_TILES 枚ごと、または PROGRESS_SEC 秒ごとの早いほうで 1 行。"""

    def __init__(self, total, clock=time.monotonic, out=None):
        self.total, self.clock, self.out = total, clock, out or time_estimates.announce
        self.done = 0
        self.last_n, self.last_t = 0, clock()

    def step(self):
        self.done += 1
        if self.done >= self.total:
            return
        now = self.clock()
        if self.done - self.last_n >= PROGRESS_TILES or now - self.last_t >= PROGRESS_SEC:
            self.last_n, self.last_t = self.done, now
            left = time_estimates.about((self.total - self.done) * TILE_SEC_PER)
            self.out(f"標高タイル {self.done} / {self.total} 枚(残り {left})")


def missing_tiles(viewers, z11, z14):
    """視点に要る標高タイルと、手元に無いもの。返り値: (要る z11, 要る z14, 無い z11, 無い z14)。"""
    need11, need14 = set(), set()
    for v in viewers:
        t11, t14 = terrain.tiles_needed(z11, z14, v)
        need11 |= t11
        need14 |= t14
    return (need11, need14, sorted(t for t in need11 if not z11.have(*t)), sorted(t for t in need14 if not z14.have(*t)))


def ensure_tiles(viewers, stats, log=log_print, extra=False):
    """要るタイルを数え、無い分を 1 枚ごとに 1 秒あけて取る。取れないタイルが残れば PanoramaUnavailable。
    extra: 展望地のために足りない分を足して取るとき(知らせの文だけ違う)。数は stats に足していく。"""
    led = terrain.TileLedger(limits={11: 10 ** 6, 14: 10 ** 6})
    z11, z14 = terrain.Dem(led, 11, allow_fetch=True), terrain.Dem(led, 14, allow_fetch=True)
    need11, need14, m11, m14 = missing_tiles(viewers, z11, z14)
    n = len(m11) + len(m14)
    stats["tiles_needed"] = max(stats.get("tiles_needed", 0), len(need11) + len(need14))
    if n:
        mins = max(1, math.ceil(n * TILE_SEC_PER / 60))
        msg = (f"(展望図: 展望地のために標高タイルを {n} 枚足して取ります。約 {mins} 分かかります)" if extra else
               f"(展望図: 初回は標高タイル {n} 枚を取ります。約 {mins} 分かかります)")
        log(msg)
        if log is log_print and sys.stdout is not sys.__stdout__ and sys.__stderr__ is not None:
            print(msg, file=sys.__stderr__, flush=True)    # --out で出力をファイルに送っているときも、画面に知らせる
    t0 = time.time()
    progress = TileProgress(n)
    for dem, miss in ((z14, m14), (z11, m11)):
        for t in miss:                 # dem.ensure(miss) と同じ順・同じ取り方(1 枚ごとに進み具合を数える)
            dem.tile(*t)
            progress.step()
    stats["tiles_fetched"] = stats.get("tiles_fetched", 0) + z11.fetched + z14.fetched
    stats["tiles_failed"] = stats.get("tiles_failed", 0) + z11.failed + z14.failed
    stats["tiles_sec"] = round(stats.get("tiles_sec", 0.0) + time.time() - t0, 1)
    left = [t for t in need11 if not z11.have(*t)] + [t for t in need14 if not z14.have(*t)]
    if left:
        why = led.stopped.get(14) or led.stopped.get(11) or "取得に失敗した"
        raise PanoramaUnavailable(f"標高タイルが {len(left)} 枚取れない({why})")
    return z11, z14


# ---------------------------------------------------------------------------
# 4. 図の地形(保存つき)
# ---------------------------------------------------------------------------
def parts_conditions(v, gsi_file):
    return {"version": PARTS_VERSION, "dictionary": dictionary_version(), "summits": summits_version(),
            "lat": round(v["lat"], 7), "lon": round(v["lon"], 7), "gsi": gsi_file, "kind": v["kind"]}


def parts_path(v):
    return os.path.join(config.parts_dir(), f"{v['id']}{'v' if v['kind'] == 'viewpoint' else ''}.pkl")


def load_or_compute_parts(v, ctx, gsi_file, stats):
    cond = parts_conditions(v, gsi_file)
    path = parts_path(v)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                saved = pickle.load(fh)
            if saved.get("conditions") == cond:
                stats["parts_cached"] += 1
                return saved["parts"]
            stats["parts_rebuilt"] += 1
        except Exception:  # noqa: BLE001  壊れた保存は作り直す
            stats["parts_rebuilt"] += 1
    t0 = time.time()
    parts = compute_parts(v, ctx)
    stats["parts_sec"] += round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump({"conditions": cond, "parts": parts}, fh)
    os.replace(tmp, path)
    return parts


def compute_parts(v, ctx):
    """段階3.5 の compute_parts と同じ(図の地形と、名前の出る山の予報の地点)。"""
    parts = terrain.figure_parts(v, ctx)
    named = [p for p in parts["peaks"] if p["tier"] in ("大", "中")]
    pts = {}
    for p in named:
        row = next(r for r in parts["rows"] if r["id"] == p["row_id"])
        pts[p["row_id"]] = terrain.peak_point(row, ctx["materials_by_id"])
    parts["points"] = pts
    parts.pop("res", None)
    return parts


# ---------------------------------------------------------------------------
# 5・6. 予報と図
# ---------------------------------------------------------------------------
def scenes_for(parts, day, states, grid_center):
    obs = parts["viewer"]["ele_m"] + terrain.EYE_M
    ll = (parts["v"]["lat"], parts["v"]["lon"])
    return weather.scene_set(ll[0], ll[1], day, states, obs, grid_center, weather.figure_mountains(parts))


def figure(parts, scenes, states, default, path, title_base, intro, sunrise, seed_key, viewer_is_fuji, gsi_partial):
    """段階3.9 の figure39 と同じ組み立て(雲は段階3.9 の既定)。"""
    ok = {st: sc for st, sc in scenes.items() if sc is not None and sc["viewer"]["summary"] is not None}
    if not ok and default != weather.TERRAIN:
        default = weather.TERRAIN
    svg, ch, st_stats, banners = draw.render_figure(parts, ok, states, default, title_base, note_extra="", seed_key=seed_key)
    notes = draw.state_notes(states, ok, sunrise)
    suffix = {st: (f"・{int(h[-5:-3])} 時" if h else "・地形だけ") for st, _, h in states}
    legend_html = draw.legend(clouds_start_pct())
    info = draw.write_page(path, svg=svg, ch=ch, peaks=parts["peaks"], states=states, default=default, notes=notes,
                             banners=banners, title_base=title_base, title_suffix=suffix, intro=intro,
                             legend_html=legend_html, osm_used=bool(parts["v"].get("osm_used")),
                             viewer_is_fuji=viewer_is_fuji, gsi_partial=gsi_partial)
    info["question_marks"] = st_stats.get("_question_marks")
    return info


def clouds_start_pct():
    from . import clouds
    return clouds.START_PCT


def tomorrow():
    return (date.today() + timedelta(days=1)).isoformat()


def default_out_path(mtn_id, kind="summit"):
    return os.path.join(config.out_dir(), f"{mtn_id}{'v' if kind == 'viewpoint' else ''}.html")


def make_panorama(mtn, day=None, out_path=None, main_days=None, allow_fetch=True, max_calls=None, log=log_print):
    """1 座の展望図(山頂、展望地があれば展望地も)を作る。返り値: {"files": [パス], "stats": {...}, "notes": [文]}。
    76 座に無い山は NotSupported。作れないときは PanoramaUnavailable(理由)。"""
    t_all = time.time()
    if not any(m["id"] == mtn.get("id") for m in core.MOUNTAINS):
        raise NotSupported("展望図は 76 座だけです")
    day = day or tomorrow()
    stats = {"overpass_calls": 0, "parts_cached": 0, "parts_rebuilt": 0, "parts_sec": 0.0, "tiles_fetched": 0,
             "tiles_needed": 0, "tiles_sec": 0.0, "tiles_failed": 0, "openmeteo_calls": 0, "openmeteo_weight": 0.0}
    notes = []
    ctx, gsi_file = build_context()
    gsi_partial = gsi_file is None
    if gsi_partial:
        notes.append("「日本の主な山岳標高」の表が手元に無いので、名前は日本百名山とヤマビヨリの 76 座だけ")
    summit = summit_viewer(mtn, ctx)
    if not summit["summit_point"]:
        notes.append(SUMMIT_ROUGH_TEXT)
    # 標高タイル(山頂のまわり。展望地は 300m 以内なので同じタイルでほぼ足りる。足りない分は下でもう一度)
    z11, z14 = ensure_tiles([summit], stats, log)
    elements, why = osm_elements(mtn["id"], summit["lat"], summit["lon"], stats)
    vp = None
    if elements is None:
        notes.append(f"展望地は調べられなかった({why})。山頂の図だけ")
    else:
        vp, summit = choose_viewpoint(summit, elements, LocalElev(z11, z14))
    viewers = [summit] + ([vp] if vp else [])
    if vp:
        ensure_tiles(viewers, stats, log, extra=True)
    parts_list = [(v, load_or_compute_parts(v, ctx, gsi_file, stats)) for v in viewers]
    # 予報
    states = weather.make_states(day, HOURS)
    default = weather.state_of(f"{day}T{DEFAULT_HOUR:02d}:00")
    core.configure_open_meteo_pacing(True)
    weather.CTX = weather.FetchContext(allow_fetch=allow_fetch, max_calls=max_calls,
                                       main_point=(mtn["lat"], mtn["lon"]), main_days=main_days)
    grid_center = (summit["lat"], summit["lon"])
    scenes_by = {}
    weather_error = None
    for v, parts in parts_list:
        try:
            scenes_by[id(parts)] = scenes_for(parts, day, states, grid_center)
        except weather.WeatherUnavailable as e:
            weather_error = str(e)
            scenes_by[id(parts)] = {}
    if weather_error:
        notes.append(f"予報が取れなかったので「地形だけ」の図({weather_error})")
    stats["openmeteo_calls"] = weather.CTX.calls
    stats["openmeteo_weight"] = round(weather.CTX.weight, 1)
    stats["weather_from_cache"] = weather.CTX.from_cache
    stats["weather_from_main_cache"] = weather.CTX.from_main
    files = []
    for v, parts in parts_list:
        kind = v["kind"]
        path = out_path if (out_path and kind == "summit") else None
        if out_path and kind == "viewpoint":
            b, e = os.path.splitext(out_path)
            path = b + "v" + (e or ".html")
        path = path or default_out_path(mtn["id"], kind)
        where = mtn["name"] if kind == "summit" else f"{mtn['name']}・{v['label']}"
        title = f"{where}からの展望と雲({day})"
        intro = (f"<p>{html.escape(viewpoint.source_text(v))}。{day} の予報(Open-Meteo。気象庁 MSM、無い時間は ECMWF)。"
                 f"予報にもとづく目安です。</p>")
        if not summit["summit_point"]:
            intro += f"<p>{html.escape(SUMMIT_ROUGH_TEXT)}。</p>"
        sunrise = weather.sunrise_from_main_cache(v["lat"], v["lon"], day)
        info = figure(parts, scenes_by.get(id(parts)) or {}, states, default, path, title, intro, sunrise,
                      seed_key=f"{mtn['id']}{'v' if kind == 'viewpoint' else ''}",
                      viewer_is_fuji=(mtn["id"] == FUJI_ID and kind == "summit"), gsi_partial=gsi_partial)
        files.append({"path": path, "kind": kind, "size": info["size"], "open": info["open"]})
    stats["sec"] = round(time.time() - t_all, 1)
    return {"files": files, "stats": stats, "notes": notes, "day": day}


# ---------------------------------------------------------------------------
# 時間の見積もり(--panorama-estimate。通信しない)
# ---------------------------------------------------------------------------
def forecast_saved(parts, day, mtn, main_days, grid_center):
    """図に要る予報(視点・格子の点・名前の出る山の点)が、どれも手元にあるか。実際に読む関数を、通信しない設定で呼ぶ。"""
    hours = [h for _, _, h in weather.make_states(day, HOURS) if h is not None]
    pts = [(parts["v"]["lat"], parts["v"]["lon"])]
    pts += [(g["lat"], g["lon"]) for g in weather.grid_points(*grid_center)]
    pts += [(m["lat"], m["lon"]) for m in weather.figure_mountains(parts) if m["lat"] is not None]
    before = weather.CTX
    weather.CTX = weather.FetchContext(allow_fetch=False, main_point=(mtn["lat"], mtn["lon"]), main_days=main_days)
    try:
        for lat, lon in pts:
            weather.get_fc(lat, lon, day, hours)
        return True
    except weather.WeatherUnavailable:
        return False
    finally:
        weather.CTX = before


def saved_parts(v, gsi_file):
    """保存してある図の地形(作ったときの条件が合うものだけ)。無ければ None。"""
    path = parts_path(v)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            saved = pickle.load(fh)
        return saved["parts"] if saved.get("conditions") == parts_conditions(v, gsi_file) else None
    except Exception:  # noqa: BLE001  壊れた保存は、無いのと同じ
        return None


def estimate(mtn, day=None, main_days=None):
    """展望図の時間の見積もり(通信しない・図を作らない)。山頂の視点だけ(展望地は調べるのに通信が要るので入れない)。"""
    if not any(m["id"] == mtn.get("id") for m in core.MOUNTAINS):
        raise NotSupported("展望図は 76 座だけです")
    day = day or tomorrow()
    ctx, gsi_file = build_context()
    summit = summit_viewer(mtn, ctx)
    led = terrain.TileLedger()
    z11, z14 = terrain.Dem(led, 11, allow_fetch=False), terrain.Dem(led, 14, allow_fetch=False)
    need11, need14, m11, m14 = missing_tiles([summit], z11, z14)
    miss = len(m11) + len(m14)
    parts = saved_parts(summit, gsi_file)
    weather_ok = parts is not None and forecast_saved(parts, day, mtn, main_days, (summit["lat"], summit["lon"]))
    # 地形の計算と予報の片方だけ保存があるときの実測は無いので、長いほう(初回の値)を使う
    draw_sec = (time_estimates.PANORAMA_SAVED_SEC if parts is not None and weather_ok
                else time_estimates.PANORAMA_FIRST_SEC)
    tiles_sec = miss * time_estimates.PANORAMA_TILE_SEC
    return {"zoom": {11: {"needed": len(need11), "missing": len(m11)}, 14: {"needed": len(need14), "missing": len(m14)}},
            "needed": len(need11) + len(need14), "missing": miss, "parts_saved": parts is not None,
            "weather_saved": weather_ok, "tiles_sec": tiles_sec, "draw_sec": draw_sec, "sec": tiles_sec + draw_sec}


def estimate_lines(mtn, est):
    """見積もりの出力(決まった形。機械も読める)。"""
    def z(n):
        d = est["zoom"][n]
        return f"ズーム{n}: 要る {d['needed']}・手元 {d['needed'] - d['missing']}・足りない {d['missing']}"
    about = time_estimates.about
    tiles = f"タイルの取得 {about(est['tiles_sec'])}" if est["missing"] else "タイルの取得 なし"
    return [f"展望図の見積もり: {mtn['name']}({mtn['id']})",
            f"標高タイル: 要る {est['needed']} 枚 / 手元にある {est['needed'] - est['missing']} 枚 / 足りない {est['missing']} 枚"
            f"({z(11)}、{z(14)})",
            f"時間の目安: {about(est['sec'])}({tiles} + 予報と図づくり {about(est['draw_sec'])})",
            "展望地の図があるときは、少し長くなります"]


def summary_lines(res):
    """診断の出力の最後に足す行(展望図: パス と、通信の量)。"""
    s = res["stats"]
    out = [f"展望図: {f['path']}" for f in res["files"]]
    out.append(f"(展望図の通信: 標高タイル {s['tiles_fetched']} 枚・Open-Meteo {s['openmeteo_calls']} 回(重さ "
               f"{s['openmeteo_weight']:g})・Overpass {s['overpass_calls']} 回。かかった時間 {s['sec']:g} 秒"
               f"(うちタイル {s.get('tiles_sec', 0):g} 秒・図の地形の計算 {s.get('parts_sec', 0):g} 秒))")
    for n in res["notes"]:
        out.append(f"(展望図: {n})")
    return out

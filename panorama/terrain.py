"""展望図の部品: 標高タイル(1 枚ごとに 1 秒あけて取る)・見上げ角・輪郭・見える山の判定・図の地形(figure_parts)。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import math
import mountain_terrain as mt
import mountain_weather_core as core
import numpy as np
import shutil
import time

from . import config, geometry, names  # noqa: E402


# ---- 元: experiments/panorama/stage2_common.py ----

ROOT = config.ROOT

TILE_LIMITS = {11: 1500, 14: 1500}

STAGE2_EXTRA_DELAY_SEC = 1.0

MIN_FREE_GB = 1.0          # 新しいタイルを取るのは、ディスクの空きがこれ以上のときだけ(実験は 10GB。利用者の手元向けに下げた)

def free_gb(path: str = ROOT) -> float:
    return shutil.disk_usage(path).free / 1e9

class TileLedger:
    """1 回の実行で新しく取ったタイルの数(ズームごと)と上限。実験の台帳ファイルは使わない。"""

    def __init__(self, limits: dict = None):
        self.limits = dict(limits or TILE_LIMITS)
        self.new = {z: 0 for z in self.limits}
        self.stopped = {}

    def used(self, zoom: int) -> int:
        return self.new.get(zoom, 0)

    def report(self) -> dict:
        return {z: {"new": self.used(z), "limit": self.limits[z], "stopped": self.stopped.get(z)} for z in self.limits}


class Dem:
    """1 つのズームの標高。tiles を先に ensure() で手元にそろえ、sample() でまとめて引く。"""

    def __init__(self, ledger: TileLedger = None, zoom: int = 14, allow_fetch: bool = True):
        self.ledger = ledger if ledger is not None else TileLedger()
        self.zoom = zoom
        self.session = names.StatusSession()
        self.store = mt.DemTileStore(cache_dir=config.terrain_dir(), session=self.session, zoom=zoom)
        self.allow_fetch = allow_fetch
        self.fail_streak = {}
        self.fetched = 0
        self.failed = 0
        self.refused = set()

    @property
    def stopped(self):
        return self.ledger.stopped.get(self.zoom)

    def have(self, x: int, y: int) -> bool:
        return (x, y) in self.store._mem or self.store._disk_path(x, y).exists()

    def tile(self, x: int, y: int):
        if self.have(x, y):
            return self.store.get_tile(x, y)
        if not self.allow_fetch or self.stopped:
            self.refused.add((x, y))
            return None
        if self.ledger.used(self.zoom) >= self.ledger.limits[self.zoom]:
            self.ledger.stopped[self.zoom] = f"上限 {self.ledger.limits[self.zoom]} 枚に達した"
            self.refused.add((x, y))
            return None
        if free_gb() < MIN_FREE_GB:
            self.ledger.stopped[self.zoom] = f"ディスクの空きが {MIN_FREE_GB:g}GB を切った"
            self.refused.add((x, y))
            return None
        before = len(self.session.statuses)
        t = self.store.get_tile(x, y)
        time.sleep(STAGE2_EXTRA_DELAY_SEC)
        st = self.session.statuses[before:] or ["?"]
        last = st[-1]
        if t is None:
            self.failed += 1
            self.fail_streak[last] = self.fail_streak.get(last, 0) + 1
            if last in (429, 403):
                self.ledger.stopped[self.zoom] = f"HTTP {last}"
            elif self.fail_streak[last] >= 5:
                self.ledger.stopped[self.zoom] = f"同じ種類の失敗が5回 ({last})"
        else:
            self.fetched += 1
            self.ledger.new[self.zoom] = self.ledger.new.get(self.zoom, 0) + 1
            self.fail_streak = {}
        return t

    def ensure(self, tiles) -> dict:
        tiles = sorted(set(tiles))
        miss = [t for t in tiles if not self.have(*t)]
        for t in miss:
            self.tile(*t)
        have = sum(1 for t in tiles if self.have(*t))
        return {"needed": len(tiles), "missing_before": len(miss), "have": have}

    def tile_ids(self, lats, lons):
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        n = 2.0 ** self.zoom
        xf = (lons + 180.0) / 360.0 * n
        lr = np.radians(lats)
        yf = (1.0 - np.log(np.tan(lr) + 1.0 / np.cos(lr)) / math.pi) / 2.0 * n
        return xf, yf

    def tiles_for(self, lats, lons) -> set:
        xf, yf = self.tile_ids(lats, lons)
        return set(zip(np.floor(xf).astype(int).ravel().tolist(), np.floor(yf).astype(int).ravel().tolist()))

    def sample(self, lats, lons, fetch: bool = False):
        """まとめて引く。海・無効値は 0m(既存と同じ)。タイルが無いところは nan。"""
        xf, yf = self.tile_ids(lats, lons)
        shape = xf.shape
        xf, yf = xf.ravel(), yf.ravel()
        tx, ty = np.floor(xf).astype(np.int64), np.floor(yf).astype(np.int64)
        px = np.minimum(((xf - tx) * 256).astype(np.int64), 255)
        py = np.minimum(((yf - ty) * 256).astype(np.int64), 255)
        out = np.full(xf.shape, np.nan)
        key = tx * 1_000_000 + ty
        order = np.argsort(key, kind="stable")
        ks = key[order]
        cuts = np.flatnonzero(np.diff(ks)) + 1
        starts = np.concatenate([[0], cuts])
        ends = np.concatenate([cuts, [len(ks)]])
        for s, e in zip(starts, ends):
            if s == e:
                continue
            k = int(ks[s])
            x, y = k // 1_000_000, k % 1_000_000
            t = self.tile(x, y) if fetch else (self.store.get_tile(x, y) if self.have(x, y) else None)
            if t is None:
                continue
            sel = order[s:e]
            v = t[py[sel], px[sel]]
            out[sel] = np.where(np.isnan(v), 0.0, v)
        return out.reshape(shape)

    def point(self, lat: float, lon: float):
        v = self.sample(np.array([lat]), np.array([lon]), fetch=True)[0]
        return None if np.isnan(v) else float(v)

def disc_tiles(dem: Dem, lat: float, lon: float, radius_m: float, *, n_az: int = 180, n_r: int = 60) -> set:
    """半径 radius_m の円の中にかかるタイル(方位 n_az × 距離 n_r の標本で拾う)。"""
    az = np.linspace(0, 360, n_az, endpoint=False)
    rr = np.linspace(0, radius_m, n_r + 1)[1:]
    A, R = np.meshgrid(az, rr)
    la, lo = dest_points(lat, lon, A.ravel(), R.ravel())
    s = dem.tiles_for(la, lo)
    s |= dem.tiles_for(np.array([lat]), np.array([lon]))
    return s

R_EARTH = 6371008.8

def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)

def _latlon(v):
    x, y, z = v[..., 0], v[..., 1], v[..., 2]
    return np.degrees(np.arctan2(z, np.hypot(x, y))), np.degrees(np.arctan2(y, x))

def dest_points(lat, lon, az_deg, dist_m):
    """球面の順問題(配列)。方位ごとの輪郭用。"""
    az = np.radians(np.asarray(az_deg, dtype=float))
    dr = np.asarray(dist_m, dtype=float) / R_EARTH
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = np.arcsin(math.sin(la1) * np.cos(dr) + math.cos(la1) * np.sin(dr) * np.cos(az))
    lo2 = lo1 + np.arctan2(np.sin(az) * np.sin(dr) * math.cos(la1), np.cos(dr) - math.sin(la1) * np.sin(la2))
    return np.degrees(la2), (np.degrees(lo2) + 540.0) % 360.0 - 180.0

def angle_deg(obs_m, h, d, k: float = mt.REFRACTION_K):
    """mountain_terrain.apparent_elevation_angle_deg を配列で。"""
    drop = (1 - k) * d * d / (2 * mt.EARTH_RADIUS_M)
    return np.degrees(np.arctan2(h - obs_m - drop, d))


# ---- 元: experiments/panorama/visibility.py ----

INF = float("inf")

FINE_NEAR_STEPS = ((3000.0, 10.0), (20000.0, 200.0), (INF, 500.0))  # 3km まで 10m(z14 の 1 マス約 10m に合わせる)

TAIL_M = 15000.0

TAIL_STEP_M = 50.0

SUMMIT_SEARCH_M = 300.0


# ---- 元: experiments/panorama/stage2_draw.py ----

SKY_STEPS = ((3000.0, 10.0), (30000.0, 50.0), (70000.0, 150.0), (float("inf"), 300.0))   # 3〜30km を 100m にすると近い山の頂を飛ばした

DEFAULT_BANDS = [3000.0, 10000.0, 20000.0, 35000.0, 60000.0, 100000.0, 150000.0, 200000.0]

NEAR_BAND_M = 3000.0

RIDGE_DIST_FRAC = (0.15, 0.05)   # その山までの距離の手前 15%〜奥 5%(奥を広く取ると、奥の高い地形で稜線が埋まる)

RIDGE_HALF_DEG = 3.0

BURIED_DEG = 0.02          # 自分の層の輪郭より、これ以上下にある頂は「埋もれる」

SNAP_MAX_DEG = 0.15        # 点を輪郭に合わせるのは、この差までの浮き(それより大きければ動かさず記録)

SNAP_MAX_M = 40.0          # または、浮きを山頂の距離で m に直して 40m 以内(近い山。材料の標高と標高タイルのマスの差の範囲)


# ---- 元: experiments/panorama/stage2_view.py ----

EYE_M = geometry.EYE_HEIGHT_M

NEAR_MAX_M = 3000.0

FRAC = 0.05

MARGINAL_DEG = 0.05


CONFIGS = {   # 段階1.5 の T3(そろえた条件)の 3 つ。どれも z14・視点の標高は z14
    "A_fine_skip30": dict(steps=FINE_NEAR_STEPS, near_skip_m=30.0),
    "B_fine_skip100": dict(steps=FINE_NEAR_STEPS, near_skip_m=100.0),
    "C_u100_skip30": dict(steps=((NEAR_MAX_M, 10.0), (INF, 100.0)), near_skip_m=30.0),
}

PRIMARY = "A_fine_skip30"

SADDLE = (20.0, 200.0)     # 鞍部までたどる測り方: 遊び 20m・深さ 200m(段階1.5 の saddle_d200)

def _steps_array(max_m, steps, start_m):
    return np.asarray(geometry.sample_distances(max_m, steps, start_m), dtype=float)

class Elev:
    """z14(near_max_m 以内)と z11 を、距離で切り替えて引く。"""

    def __init__(self, z11: Dem, z14: Dem, near_max_m: float = NEAR_MAX_M):
        self.z11, self.z14, self.near_max_m = z11, z14, near_max_m

    def at(self, lats, lons, d):
        out = np.full(np.shape(lats), np.nan)
        near = d <= self.near_max_m
        if near.any():
            out[near] = self.z14.sample(lats[near], lons[near])
        if (~near).any():
            out[~near] = self.z11.sample(lats[~near], lons[~near])
        return out

def _positions(viewer, tlat, tlon, frac):
    """視点 → 各山頂の大円の上の位置。tlat/tlon は標本ごとの山頂(配列)、frac は割合(配列)。"""
    a = _xyz(viewer["lat"], viewer["lon"])
    b = _xyz(tlat, tlon)
    cosom = np.clip(b @ a, -1.0, 1.0)
    om = np.arccos(cosom)
    s = np.sin(om)
    s = np.where(s < 1e-15, 1.0, s)
    wa = np.sin((1 - frac) * om) / s
    wb = np.sin(frac * om) / s
    v = wa[:, None] * a[None, :] + wb[:, None] * b
    return _latlon(v)

def target_geometry(viewer, targets):
    dist = np.empty(len(targets))
    az = np.empty(len(targets))
    for i, t in enumerate(targets):
        dist[i], az[i] = geometry.vincenty_inverse(viewer["lat"], viewer["lon"], t["lat"], t["lon"])
    return dist, az

def profiles(viewer, targets, elev: Elev, cfg: dict, dist=None):
    """各山の断面(main の標本)。返り値: dict(idx, d, h, ang, dist, summit_angle)。標本は山ごとに続けて並ぶ。"""
    if dist is None:
        dist, _ = target_geometry(viewer, targets)
    obs_m = viewer["ele_m"] + EYE_M
    ds, idx = [], []
    for i, D in enumerate(dist):
        arr = _steps_array(D - 1e-6, cfg["steps"], cfg["near_skip_m"])
        ds.append(arr)
        idx.append(np.full(len(arr), i, dtype=np.int64))
    d = np.concatenate(ds) if ds else np.zeros(0)
    idx = np.concatenate(idx) if idx else np.zeros(0, dtype=np.int64)
    tlat = np.array([t["lat"] for t in targets])[idx]
    tlon = np.array([t["lon"] for t in targets])[idx]
    la, lo = _positions(viewer, tlat, tlon, d / dist[idx])
    h = elev.at(la, lo, d)
    ang = angle_deg(obs_m, h, d)
    t_ele = np.array([t["ele_m"] for t in targets], dtype=float)
    summit = angle_deg(obs_m, t_ele, dist)
    return {"idx": idx, "d": d, "h": h, "ang": ang, "dist": dist, "summit_angle": summit, "n": len(targets)}

def visible_frac(p, frac=FRAC):
    """距離の割合で外す見えている高さ(度)と、それを決めた地形の距離。欠けた標本の数も返す。"""
    n = p["n"]
    limit = p["dist"][p["idx"]] * (1 - frac)
    use = p["d"] < limit
    ang = np.where(use & ~np.isnan(p["ang"]), p["ang"], -np.inf)
    best = np.full(n, -np.inf)
    np.maximum.at(best, p["idx"], ang)
    miss = np.zeros(n, dtype=np.int64)
    np.add.at(miss, p["idx"], (use & np.isnan(p["ang"])).astype(np.int64))
    vh = p["summit_angle"] - best
    vh = np.where(np.isfinite(best), vh, np.nan)
    # 決めた地形の距離
    at = np.full(n, np.nan)
    hit = use & (ang == best[p["idx"]]) & np.isfinite(ang)
    order = np.where(hit)[0]
    at[p["idx"][order[::-1]]] = p["d"][order[::-1]]
    return vh, miss, at

def saddle_visible(viewer, targets, elev: Elev, p_primary, tol_m=SADDLE[0], depth_m=SADDLE[1], dist=None):
    """鞍部までたどる測り方(visibility.saddle_distance と同じたどり方)の見えている高さ(度)。"""
    if dist is None:
        dist = p_primary["dist"]
    n = len(targets)
    # 山頂の手前 TAIL_M を 50m 刻みで別に取る
    ts, tidx = [], []
    for i, D in enumerate(dist):
        t0 = max(CONFIGS[PRIMARY]["near_skip_m"], D - TAIL_M)
        arr = np.arange(t0, D + 1e-9, TAIL_STEP_M)
        ts.append(arr)
        tidx.append(np.full(len(arr), i, dtype=np.int64))
    td = np.concatenate(ts)
    ti = np.concatenate(tidx)
    tlat = np.array([t["lat"] for t in targets])[ti]
    tlon = np.array([t["lon"] for t in targets])[ti]
    la, lo = _positions(viewer, tlat, tlon, td / dist[ti])
    th = elev.at(la, lo, td)
    out = np.full(n, np.nan)
    pidx, pd_, ph, pang = p_primary["idx"], p_primary["d"], p_primary["h"], p_primary["ang"]
    starts = np.searchsorted(pidx, np.arange(n), side="left")
    ends = np.searchsorted(pidx, np.arange(n), side="right")
    tstarts = np.searchsorted(ti, np.arange(n), side="left")
    tends = np.searchsorted(ti, np.arange(n), side="right")
    for i in range(n):
        D = dist[i]
        t0 = max(CONFIGS[PRIMARY]["near_skip_m"], D - TAIL_M)
        md, mh, ma = pd_[starts[i]:ends[i]], ph[starts[i]:ends[i]], pang[starts[i]:ends[i]]
        sel = md < t0
        seq_d = np.concatenate([md[sel], td[tstarts[i]:tends[i]]])
        seq_h = np.concatenate([mh[sel], th[tstarts[i]:tends[i]]])
        ok = ~np.isnan(seq_h)
        seq_d, seq_h = seq_d[ok], seq_h[ok]
        if len(seq_d) == 0:
            continue
        near_summit = np.where(seq_d >= D - SUMMIT_SEARCH_M)[0]
        start = int(near_summit[np.argmax(seq_h[near_summit])]) if len(near_summit) else len(seq_d) - 1
        top_h = seq_h[start]
        min_h, min_d = seq_h[start], seq_d[start]
        sd = CONFIGS[PRIMARY]["near_skip_m"]
        for j in range(start - 1, -1, -1):
            h = seq_h[j]
            if h < min_h:
                min_h, min_d = h, seq_d[j]
            elif h > min_h + tol_m:
                if min_h <= top_h - depth_m:
                    sd = min_d
                    break
                min_h, min_d = h, seq_d[j]
        # 鞍部より手前の main の標本の見上げ角の最大
        use = (md < sd) & ~np.isnan(ma)
        if use.any():
            out[i] = p_primary["summit_angle"][i] - ma[use].max()
    return out

def classify(vals: list, missing: list, split: bool = False) -> str:
    """vals: 3 条件の見えている高さ(度)。missing: 3 条件の欠けた標本の数。split: 2 つの測り方で割れたか。"""
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals) or any(m > 0 for m in missing):
        return "判定できない"
    if all(v <= 0 for v in vals):
        return "きわどい" if split else "隠れる"
    if all(v >= MARGINAL_DEG for v in vals) and not split:
        return "見える"
    return "きわどい"

def evaluate_point(viewer, targets, elev: Elev, *, with_saddle=True, configs=None):
    """1 つの視点(標高は呼ぶ側が z14 のマスの値で与える)から、全部の山の区分。"""
    configs = configs or CONFIGS
    dist, az = target_geometry(viewer, targets)
    res = {"dist": dist, "az": az, "vh": {}, "miss": {}, "at": {}}
    prim = None
    for name, cfg in configs.items():
        p = profiles(viewer, targets, elev, cfg, dist=dist)
        vh, miss, at = visible_frac(p)
        res["vh"][name], res["miss"][name], res["at"][name] = vh, miss, at
        if name == PRIMARY:
            prim = p
            res["summit_angle"] = p["summit_angle"]
    if with_saddle and prim is not None:
        res["saddle"] = saddle_visible(viewer, targets, elev, prim, dist=dist)
    cls = []
    for i in range(len(targets)):
        vals = [float(res["vh"][k][i]) for k in configs]
        miss = [int(res["miss"][k][i]) for k in configs]
        split = False
        if with_saddle:
            sv, fv = res["saddle"][i], res["vh"][PRIMARY][i]
            split = (not math.isnan(sv)) and ((sv > 0) != (fv > 0))
        cls.append(classify(vals, miss, split))
    res["class"] = cls
    return res

def ring_points(viewer, radius_m=25.0, n=8):
    la, lo = dest_points(viewer["lat"], viewer["lon"], np.arange(n) * (360.0 / n), np.full(n, radius_m))
    return [{"lat": float(a), "lon": float(b), "az_from_center": i * (360.0 / n)} for i, (a, b) in enumerate(zip(la, lo))]

def evaluate_place(center: dict, targets, elev: Elev, z14: Dem, *, radius_m=25.0, n_ring=8):
    """展望地を「場所」として扱う: 代表点 + 半径 radius_m の円の上の n_ring 点。どれか 1 点で「見える」なら見える。
    代表点は全部の山を計算し、周りの点は代表点で「見える」にならなかった山だけを計算する(結果は同じで速い)。"""
    v0 = dict(center)
    v0["ele_m"] = z14.point(center["lat"], center["lon"])
    r0 = evaluate_point(v0, targets, elev)
    final = list(r0["class"])
    how = ["代表点で見える" if k == "見える" else None for k in r0["class"]]
    ring = []
    rest = [i for i, k in enumerate(r0["class"]) if k != "見える"]
    for pt in ring_points(center, radius_m, n_ring):
        pt = dict(pt)
        pt["ele_m"] = z14.point(pt["lat"], pt["lon"])
        sub = [targets[i] for i in rest]
        if not sub:
            ring.append({"az_from_center": pt["az_from_center"], "ele_m": pt["ele_m"], "n_visible_extra": 0})
            continue
        rr = evaluate_point(pt, sub, elev, with_saddle=True)
        extra = 0
        for j, i in enumerate(rest):
            k = rr["class"][j]
            if k == "見える" and final[i] != "見える":
                final[i] = "見える"
                how[i] = "動けば見える"
                extra += 1
            elif k == "きわどい" and final[i] == "隠れる":
                final[i] = "きわどい"
                how[i] = "動けばきわどい"
        ring.append({"az_from_center": pt["az_from_center"], "ele_m": pt["ele_m"], "n_visible_extra": extra})
    for i, k in enumerate(final):
        if how[i] is None:
            how[i] = {"隠れる": "どの点でも隠れる", "きわどい": "代表点できわどい",
                      "判定できない": "判定できない"}.get(k, k)
    return {"center": r0, "class": final, "how": how, "ring": ring, "viewer_ele_m": v0["ele_m"]}


# ---- 元: experiments/panorama/stage2_place.py ----

MAX_M = 200000.0

MIN_M = 100.0

SKY_AZ_STEP = 0.1

GAP_DEG = 8.0                 # 案1: 名前の無い区間がこの方位の幅以上あいたら、1 つ足す

NEAR_PROMINENT_KM = 30.0      # 案2: 近い(30km 以内)

PROMINENT_DEG = 0.5           # 案2: 目立つ = 見えている高さ(距離の 5%)が 0.5° 以上

ON_SKYLINE_DEG = 0.03         # 案2: 輪郭を作っている = 山頂の見上げ角が、その方位の輪郭の高さ − 0.03° 以上

MINOR_MIN_SEP_DEG = 1.0       # 案2: 小どうし、名前を出す山から 1° 以内は足さない

def candidates(viewer, materials, exclude_ids=(), max_m=MAX_M, min_m=MIN_M):
    out = []
    for m in materials:
        if m["id"] in exclude_ids:
            continue
        dd, _ = geometry.vincenty_inverse(viewer["lat"], viewer["lon"], m["lat"], m["lon"])
        if min_m <= dd <= max_m:
            out.append(m)
    return out

def rows_for(place, targets, marks):
    r0 = place["center"]
    rows = []
    for i, m in enumerate(targets):
        mk = marks.get(m["id"], {})
        vh = {k: float(r0["vh"][k][i]) for k in CONFIGS}
        rows.append({
            "id": m["id"], "material": m["name"], "src": m["src"], "ele_m": m["ele_m"], "pref": m.get("pref"),
            "tier": mk.get("tier"), "display": mk.get("display") or names.strip_decor(m["name"]),
            "own_name": mk.get("own_name"), "alt_name": mk.get("alt_name"),
            "hyakumeizan": mk.get("hyakumeizan"), "m76_ids": mk.get("m76_ids", []),
            "uncertain": mk.get("uncertain", False),
            "az": float(r0["az"][i]), "dist_km": float(r0["dist"][i]) / 1000.0,
            "summit_angle": float(r0["summit_angle"][i]),
            "vh_deg": vh[PRIMARY], "vh_m": float(r0["dist"][i]) * math.tan(math.radians(vh[PRIMARY])),
            "vh_by_config": vh, "saddle_vh_deg": None if np.isnan(r0["saddle"][i]) else float(r0["saddle"][i]),
            "blocker_km": None if np.isnan(r0["at"][PRIMARY][i]) else float(r0["at"][PRIMARY][i]) / 1000.0,
            "center_class": r0["class"][i], "class": place["class"][i], "how": place["how"][i],
            "missing": {k: int(r0["miss"][k][i]) for k in CONFIGS}})
    return rows

def in_range(az, a0, a1):
    if a1 - a0 >= 360:
        return True
    return a0 <= az <= a1

def az_in_fig(az, a0, a1):
    """図の横の位置に使う方位(0〜360 の図で、方位をそのまま)。"""
    if a1 - a0 >= 360:
        return az % 360.0
    return az

def choose_minor(rows, named, a0, a1, sky, scheme):
    """小の山を選ぶ。named: 名前を出す大・中の行。返り値: 選んだ小の行のリスト(順に)。"""
    full = a1 - a0 >= 360
    pool = [r for r in rows if r["tier"] is None and r["class"] == "見える" and in_range(r["az"], a0, a1)]
    chosen = []
    if scheme == 1:
        closed = set()
        while True:
            azs = sorted(az_in_fig(r["az"], a0, a1) for r in named + chosen)
            if full:
                bounds = azs + [azs[0] + 360.0] if azs else [0.0, 360.0]
            else:
                bounds = [a0] + azs + [a1]
            gaps = []
            for lo, hi in zip(bounds, bounds[1:]):
                if hi - lo >= GAP_DEG and (round(lo, 3), round(hi, 3)) not in closed:
                    gaps.append((hi - lo, lo, hi))
            if not gaps:
                break
            gaps.sort(reverse=True)
            _, lo, hi = gaps[0]
            inside = [r for r in pool if r not in chosen and lo < (az_in_fig(r["az"], a0, a1) + (360.0 if full and az_in_fig(r["az"], a0, a1) < lo else 0.0)) < hi]
            if not inside:
                closed.add((round(lo, 3), round(hi, 3)))
                continue
            best = max(inside, key=lambda r: r["vh_deg"])
            chosen.append(best)
        return chosen
    # 案2: 輪郭を作っている、近くの目立つ山
    az = sky["az"] % 360.0
    top = sky["ang"][:, -1]
    cand = []
    for r in pool:
        if r["dist_km"] > NEAR_PROMINENT_KM or r["vh_deg"] < PROMINENT_DEG:
            continue
        j = int(np.argmin(np.abs(((az - r["az"]) + 180.0) % 360.0 - 180.0)))
        if r["summit_angle"] >= top[j] - ON_SKYLINE_DEG:
            cand.append(r)
    cand.sort(key=lambda r: -r["vh_deg"])
    for r in cand:
        near = [x for x in named + chosen if abs(((x["az"] - r["az"]) + 180.0) % 360.0 - 180.0) < MINOR_MIN_SEP_DEG]
        if not near:
            chosen.append(r)
    return chosen


# ---- 元: experiments/panorama/stage25_summits.py ----

PX360 = 24.0

FAR_M = 200000.0

NEAR_TILE_M = 3100.0

OSM_CREDIT = "、© OpenStreetMap contributors(ODbL)"

def tiles_needed(z11, z14, v, far_m=FAR_M):
    t11 = disc_tiles(z11, v["lat"], v["lon"], far_m, n_az=360, n_r=80)
    t14 = disc_tiles(z14, v["lat"], v["lon"], NEAR_TILE_M, n_az=72, n_r=10)
    return t11, t14

def own_material_ids(mid, materials, marks):
    return {m["id"] for m in materials if mid in marks.get(m["id"], {}).get("m76_ids", []) or m["id"] == mid}


# ---- 元: experiments/panorama/stage25_draw.py ----

SKIPS_M = (30.0, 50.0, 100.0, 200.0)

def bilinear_sample(dem: Dem, lats, lons):
    """dem(z14)の双線形補間。4 マスのどれかのタイルが手元に無い・欠ける点は nan(呼ぶ側でマスの値に戻す)。"""
    xf, yf = dem.tile_ids(lats, lons)
    xf, yf = np.ravel(xf), np.ravel(yf)
    gx = xf * 256.0 - 0.5
    gy = yf * 256.0 - 0.5
    ix0 = np.floor(gx).astype(np.int64)
    iy0 = np.floor(gy).astype(np.int64)
    wx = gx - ix0
    wy = gy - iy0
    out = np.zeros(len(gx))
    bad = np.zeros(len(gx), dtype=bool)
    tiles = {}

    def val(ix, iy):
        tx, ty = ix // 256, iy // 256
        px, py = ix % 256, iy % 256
        v = np.full(len(ix), np.nan)
        key = tx * 1_000_000 + ty
        for k in np.unique(key):
            sel = key == k
            x, y = int(k // 1_000_000), int(k % 1_000_000)
            if (x, y) not in tiles:
                tiles[(x, y)] = dem.store.get_tile(x, y) if dem.have(x, y) else None
            t = tiles[(x, y)]
            if t is None:
                continue
            vv = t[py[sel], px[sel]]
            v[sel] = np.where(np.isnan(vv), 0.0, vv)     # 海・無効値は 0m(段階2 と同じ)
        return v
    for dx, dy, w in ((0, 0, (1 - wx) * (1 - wy)), (1, 0, wx * (1 - wy)), (0, 1, (1 - wx) * wy), (1, 1, wx * wy)):
        v = val(ix0 + dx, iy0 + dy)
        bad |= np.isnan(v)
        out += np.nan_to_num(v) * w
    out[bad] = np.nan
    return out.reshape(np.shape(lats))

def split_skyline(viewer, elev, az_list, bands=DEFAULT_BANDS, steps=SKY_STEPS, skips=SKIPS_M):
    """band_skyline と同じ標本で、層ごとの累積の輪郭(ang・dist)に加えて、
    far_top(3km より先だけの最大)と near_top[skip](skip m 〜 3km の最大)を返す。"""
    start = min(skips)
    max_m = bands[-1]
    ds = np.asarray(geometry.sample_distances(max_m, steps, start))
    az = np.asarray(az_list, dtype=float)
    obs_m = viewer["ele_m"] + EYE_M
    band_of = np.searchsorted(np.asarray(bands), ds, side="left")
    n_b = len(bands)
    ang_out = np.full((len(az), n_b), np.nan)
    dist_out = np.full((len(az), n_b), np.nan)
    far_top = np.full(len(az), np.nan)
    far_dist = np.full(len(az), np.nan)
    near_top = {s: np.full(len(az), np.nan) for s in skips}
    near_dist = {s: np.full(len(az), np.nan) for s in skips}
    missing = 0
    near_mask = ds <= NEAR_BAND_M
    CH = max(1, 400000 // len(ds))
    for s0 in range(0, len(az), CH):
        A = az[s0:s0 + CH]
        AA, DD = np.meshgrid(A, ds, indexing="ij")
        la, lo = dest_points(viewer["lat"], viewer["lon"], AA.ravel(), DD.ravel())
        h = elev.at(la, lo, DD.ravel()).reshape(AA.shape)
        missing += int(np.isnan(h).sum())
        ang = angle_deg(obs_m, h, DD)
        ang = np.where(np.isnan(ang), -np.inf, ang)
        rows = np.arange(len(A))
        for b in range(n_b):
            sub = np.where(band_of[None, :] <= b, ang, -np.inf)
            j = np.argmax(sub, axis=1)
            v = sub[rows, j]
            ang_out[s0:s0 + len(A), b] = np.where(np.isfinite(v), v, np.nan)
            dist_out[s0:s0 + len(A), b] = np.where(np.isfinite(v), ds[j], np.nan)
        sub = np.where(~near_mask[None, :], ang, -np.inf)
        j = np.argmax(sub, axis=1)
        v = sub[rows, j]
        far_top[s0:s0 + len(A)] = np.where(np.isfinite(v), v, np.nan)
        far_dist[s0:s0 + len(A)] = np.where(np.isfinite(v), ds[j], np.nan)
        for sk in skips:
            m = near_mask & (ds >= sk)
            sub = np.where(m[None, :], ang, -np.inf)
            j = np.argmax(sub, axis=1)
            v = sub[rows, j]
            near_top[sk][s0:s0 + len(A)] = np.where(np.isfinite(v), v, np.nan)
            near_dist[sk][s0:s0 + len(A)] = np.where(np.isfinite(v), ds[j], np.nan)
    return {"az": np.round(az, 4), "ang": ang_out, "dist": dist_out, "missing": missing,
            "far_top": far_top, "far_dist": far_dist, "near_top": near_top, "near_dist": near_dist}


# ---- 元: experiments/panorama/stage3_draw.py ----

class DrawElev3(Elev):
    """描くための標高: 3km 以内は z14 の双線形補間、3km より先は z11 の双線形補間(どちらも欠ける所はマスの値)。"""

    def __init__(self, z11, z14, near_max_m=NEAR_MAX_M, far_bilinear=True):
        super().__init__(z11, z14, near_max_m)
        self.far_bilinear = far_bilinear
        self.fallback_near = 0
        self.fallback_far = 0

    @property
    def fallback(self):
        return self.fallback_near + self.fallback_far

    def at(self, lats, lons, d):
        out = np.full(np.shape(lats), np.nan)
        near = d <= self.near_max_m
        if near.any():
            b = bilinear_sample(self.z14, lats[near], lons[near])
            cell = self.z14.sample(lats[near], lons[near])
            miss = np.isnan(b)
            self.fallback_near += int(miss.sum())
            out[near] = np.where(miss, cell, b)
        far = ~near
        if far.any():
            cell = self.z11.sample(lats[far], lons[far])
            if self.far_bilinear:
                b = bilinear_sample(self.z11, lats[far], lons[far])
                miss = np.isnan(b)
                self.fallback_far += int(miss.sum())
                out[far] = np.where(miss, cell, b)
            else:
                out[far] = cell
        return out

def sky_distances(bands=DEFAULT_BANDS, steps=SKY_STEPS, start=min(SKIPS_M)):
    """図の輪郭(stage25_draw.split_skyline)と同じ距離の標本。"""
    return np.asarray(geometry.sample_distances(bands[-1], steps, start))

def ridge_line_v3(viewer, elev, az0, dist_m, half_deg=RIDGE_HALF_DEG, frac=RIDGE_DIST_FRAC,
                  step=SKY_AZ_STEP, ds_all=None):
    """stage2_draw.ridge_line と同じ窓(方位 ±half_deg、距離の手前 frac[0]〜奥 frac[1])で、標本を図の輪郭と同じもの
    (距離は輪郭と同じ刻み、方位は輪郭と同じ 0.1° の格子の上)にした稜線。返り値: [(方位, 見上げ角)]。
    窓に標本が無ければ空。段階2 の稜線は方位 0.05° 刻みで、輪郭の標本の間の方位で輪郭を少し超えることがあった。"""
    ds_all = sky_distances() if ds_all is None else np.asarray(ds_all)
    lo_d, hi_d = dist_m * (1 - frac[0]), dist_m * (1 + frac[1])
    ds = ds_all[(ds_all >= lo_d) & (ds_all <= hi_d)]
    if not len(ds):
        return []
    k0 = int(np.ceil((az0 - half_deg) / step - 1e-9))
    k1 = int(np.floor((az0 + half_deg) / step + 1e-9))
    azs = np.round(np.arange(k0, k1 + 1) * step, 4)
    AA, DD = np.meshgrid(azs, ds, indexing="ij")
    la, lo = dest_points(viewer["lat"], viewer["lon"], AA.ravel(), DD.ravel())
    h = elev.at(la, lo, DD.ravel()).reshape(AA.shape)
    ang = angle_deg(viewer["ele_m"] + EYE_M, h, DD)
    ang = np.where(np.isnan(ang), -np.inf, ang)
    top = ang.max(axis=1)
    return [(float(a), float(t)) for a, t in zip(azs, top) if np.isfinite(t)]

def draw_info_v3(viewer, elev, sky, row, bands=DEFAULT_BANDS):
    """stage2_place.draw_info と同じ決まりで、稜線だけ ridge_line_v3 にしたもの。"""
    band = int(np.searchsorted(np.asarray(bands), row["dist_km"] * 1000.0, side="left"))
    band = min(band, len(bands) - 1)
    azs = np.asarray(sky["az"], dtype=float)
    a = row["az"] % 360.0
    col = sky["ang"][:, band]
    ok = np.isfinite(col)
    own = float(np.interp(a, azs[ok], col[ok]))
    top = float(np.interp(a, azs[ok], np.nan_to_num(sky["ang"][:, -1][ok], nan=-90.0)))
    sa = row["summit_angle"]
    info = {"band": band, "own_outline": own, "top_outline": top, "buried": False, "ridge": None,
            "snap_deg": 0.0, "draw_angle": sa}
    if sa > own:
        diff = sa - own
        diff_m = row["dist_km"] * 1000.0 * math.tan(math.radians(diff))
        info["float_m"] = diff_m
        if diff <= SNAP_MAX_DEG or diff_m <= SNAP_MAX_M:
            info["snap_deg"] = -diff
            info["draw_angle"] = own
        else:
            info["float_unsnapped_deg"] = diff
    elif own - sa > BURIED_DEG:
        info["buried"] = True
        rl = ridge_line_v3(viewer, elev, row["az"], row["dist_km"] * 1000.0)
        info["ridge"] = rl
        if rl:
            near = min(rl, key=lambda p: abs(p[0] - row["az"]))
            if near[1] <= sa + BURIED_DEG:
                info["snap_deg"] = near[1] - sa
                info["draw_angle"] = near[1]
            else:
                info["ridge_above_summit_deg"] = near[1] - sa
    return info

def figure_peaks_v3(viewer, elev, sky, rows, a0, a1, minor_rows, label_fn):
    """stage2_place.figure_peaks と同じ選び方で、呼び名は label_fn(図に出す行のリスト → 呼び名のリスト)で決め、
    稜線は draw_info_v3 で描く。label_fn は図ごとに呼ぶ(括弧は同じ図に同じ名前が 2 つ以上のときだけ。stage3_names)。"""
    named = [r for r in rows if r["tier"] in ("大", "中") and r["class"] in ("見える", "きわどい") and in_range(r["az"], a0, a1)]
    allp = named + [r for r in minor_rows if r not in named]
    labs = label_fn(allp)
    out = []
    for r, lab in zip(allp, labs):
        info = draw_info_v3(viewer, elev, sky, r)
        title = f"{lab}(材料: {r['material']} {r['ele_m']:.0f}m)方位 {r['az']:.1f}° 距離 {r['dist_km']:.1f}km"
        if r["class"] == "きわどい":
            title += " 見えるかどうかが境目"
        out.append({"label": lab, "tier": r["tier"] or "小", "marginal": r["class"] == "きわどい",
                    "az": az_in_fig(r["az"], a0, a1), "angle": r["summit_angle"], "dist_km": r["dist_km"],
                    "prio": r["vh_deg"], "title": title, "row_id": r["id"], **info})
    return out


# ---- 元: experiments/panorama/stage3_summits.py ----

def build_ctx(vp_all, led):
    materials, _ = names.load_materials()
    D = names.PeakDictionary()
    marks25, _ = names.apply(D.attach(materials), materials, D)
    marks, changed = names.apply_overrides(marks25)
    z11, z14 = Dem(led, 11, allow_fetch=False), Dem(led, 14, allow_fetch=False)
    return {"materials": materials, "marks": marks, "elev": Elev(z11, z14), "z11": z11, "z14": z14,
            "vp_all": vp_all, "name_changes": changed}


# ---- 元: experiments/panorama/stage3_weather_run.py ----

def figure_parts(v, ctx):
    materials, marks, elev, z11, z14 = ctx["materials"], ctx["marks"], ctx["elev"], ctx["z11"], ctx["z14"]
    place = {"lat": v["lat"], "lon": v["lon"]}
    excl = own_material_ids(v["id"], materials, marks)
    targets = candidates(place, materials, exclude_ids=excl, max_m=FAR_M)
    res = evaluate_place(place, targets, elev, z14)
    rows = rows_for(res, targets, marks)
    viewer = dict(place, ele_m=res["viewer_ele_m"])
    delev = DrawElev3(z11, z14)
    azs = np.round(np.arange(0.0, 360.0 + 1e-9, SKY_AZ_STEP), 4)
    sky = split_skyline(viewer, delev, azs % 360.0)
    sky["az"] = azs
    named = [r for r in rows if r["tier"] in ("大", "中") and r["class"] in ("見える", "きわどい")]
    minor = choose_minor(rows, named, 0.0, 360.0, sky, 1)
    peaks = figure_peaks_v3(viewer, delev, sky, rows, 0.0, 360.0, minor, names.figure_labels)
    return {"v": v, "viewer": viewer, "rows": rows, "sky": sky, "peaks": peaks, "res": res}

def peak_point(row, materials_by_id):
    """名前を出す山(赤・青)の予報の地点。76 座は MOUNTAINS の座標と標高、それ以外の百名山は 1003 山の点と標高。"""
    if row.get("m76_ids"):
        m = core.mountain_by_id(row["m76_ids"][0])
        return {"lat": m["lat"], "lon": m["lon"], "elev_m": float(m["elevation_m"]), "source": "MOUNTAINS"}
    mat = materials_by_id[row["id"]]
    return {"lat": mat["lat"], "lon": mat["lon"], "elev_m": float(mat["ele_m"]), "source": "1003"}

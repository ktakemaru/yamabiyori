"""展望図の部品: 図の組み立て(山並み・名前・空・雲を重ねた SVG)と HTML(せまい画面・広い画面)。

段階1〜3.9 の実験(experiments/panorama/)から、図を作るのに要る関数だけを移した(「# ---- 元:」の行が元のファイル)。
"""
from __future__ import annotations

import html
import json
import math
import mountain_weather_core as core
import numpy as np
import os
import re

from . import terrain, names, weather, clouds, svgfmt  # noqa: E402


# ---- 元: experiments/panorama/stage15_draw.py ----

def text_width(text: str, font_px: float) -> float:
    """CJK は 1 文字 = 文字の大きさ、ASCII・数字は 0.6 倍として幅を見積もる。"""
    return sum(font_px if ord(ch) > 0x2E7F else font_px * 0.6 for ch in text)

def boxes_overlap(a: dict, b: dict, gap: float = 3.0) -> bool:
    return not (a["x1"] + gap <= b["x0"] or b["x1"] + gap <= a["x0"] or a["y1"] <= b["y0"] or b["y1"] <= a["y0"])

def count_overlaps(boxes: list) -> int:
    n = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes_overlap(boxes[i], boxes[j], gap=0.0):
                n += 1
    return n


# ---- 元: experiments/panorama/stage15_draw_b.py ----

LEAD_HALF_W = 0.35     # 引き出し線の太さ(0.7px)の半分。線が点に「触れる」の判定に使う

def _orient(a, b, c_):
    v = (b[0] - a[0]) * (c_[1] - a[1]) - (b[1] - a[1]) * (c_[0] - a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

def segments_cross(p1, p2, q1, q2) -> bool:
    """2 本の線分が交わるか(端の点どうしが触れるだけ・平行で重なるものは数えない)。"""
    o1, o2, o3, o4 = _orient(p1, p2, q1), _orient(p1, p2, q2), _orient(q1, q2, p1), _orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0

def point_segment_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))

def segment_hits_box(a, b, box) -> bool:
    """線分が名前の枠(x0,x1,y0,y1)の中を通るか。"""
    x0, x1, y0, y1 = box["x0"], box["x1"], box["y0"], box["y1"]
    if any(x0 < p[0] < x1 and y0 < p[1] < y1 for p in (a, b)):
        return True
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return any(segments_cross(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))

def correspondence(geom: list) -> dict:
    """geom: render_panel の check["label_geom"]。名前と頂の対応が分かりにくくなる形を数える。
    - 線の交差: 2 つの名前の引き出し線が交わる
    - 線がほかの名前を通る: 引き出し線が、ほかの名前の文字の枠の中を通る
    - 線がほかの頂に触れる: 引き出し線が、ほかの山の頂の点に触れる(点の半径+線の太さの半分より近い)
    - 点の重なり: 2 つの頂の点が重なる(中心の距離 < 半径の和)
    - 寝た線: 引き出し線が水平に近い(横のずれ > 縦の長さ)。どの頂を指すか読みにくい
    """
    n = len(geom)
    segs = []
    for e in geom:
        px, py = e["peak"]
        segs.append(((e["lead_from"][0], e["lead_from"][1]), (px, py - 3.0)))
    crossings, through_label, touch_dot, dot_overlap, flat = [], [], [], [], []
    pairs = []
    for i in range(n):
        a, b = segs[i]
        if abs(a[0] - b[0]) > abs(a[1] - b[1]):
            flat.append(geom[i]["display"])
        for j in range(n):
            if i == j:
                continue
            if j > i and segments_cross(a, b, *segs[j]):
                crossings.append([geom[i]["display"], geom[j]["display"]])
            if segment_hits_box(a, b, geom[j]["box"]):
                through_label.append([geom[i]["display"], geom[j]["display"]])
            if point_segment_dist(geom[j]["peak"], a, b) < geom[j]["dot_r"] + LEAD_HALF_W:
                touch_dot.append([geom[i]["display"], geom[j]["display"]])
            if j > i:
                dist = math.hypot(geom[i]["peak"][0] - geom[j]["peak"][0], geom[i]["peak"][1] - geom[j]["peak"][1])
                pairs.append((dist, geom[i]["display"], geom[j]["display"]))
                if dist < geom[i]["dot_r"] + geom[j]["dot_r"]:
                    dot_overlap.append([geom[i]["display"], geom[j]["display"]])
    pairs.sort()
    lens = [math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in segs]
    return {"n_labels": n, "lead_crossings": crossings, "lead_through_other_label": through_label,
            "lead_touches_other_peak": touch_dot, "dot_overlaps": dot_overlap, "flat_leads": flat,
            "closest_peak_pairs_px": [[round(p[0], 1), p[1], p[2]] for p in pairs[:5]],
            "lead_len_px": {"min": round(min(lens), 1), "max": round(max(lens), 1)} if lens else None}


# ---- 元: experiments/panorama/stage2_draw.py ----

FONT = {"大": 16, "中": 13, "小": 11}

MIN_FONT = {"大": 13, "中": 11, "小": 9}

WEIGHT = {"大": 700, "中": 600, "小": 400}

DOT_R = {"大": 2.8, "中": 2.2, "小": 1.7}

MAX_SHIFT = {"大": 90.0, "中": 90.0, "小": 24.0}

DIR8 = {0: "北", 45: "北東", 90: "東", 135: "南東", 180: "南", 225: "南西", 270: "西", 315: "北西", 360: "北"}

CREDIT = "国土地理院(標高タイル、日本の主な山岳標高)をもとに作成"

DRAFT_NOTE = "試作"

MARGINAL_MARK = "◇"

def band_color(i, n, dark):
    """距離の層 i(0 が手前)の色。手前ほど濃く、遠いほど薄い(どの方位でも同じ順)。"""
    t = i / max(1, n - 1)
    if dark:
        l0, l1 = 62.0, 24.0       # 暗い画面: 手前ほど明るく(前に出る)、遠いほど背景に近い
    else:
        l0, l1 = 30.0, 84.0
    lum = l0 + (l1 - l0) * t
    return f"hsl(208 {22 + 8 * (1 - t):.0f}% {lum:.0f}%)"

def layout(items, width, row_ys, *, gap=3.0):
    """items: [{"text","x","tier","prio"}]。格の高い順に置く。各段で、まず真上、だめなら左右にずらす。
    なお置けなければ文字を 1px ずつ小さくする。大・中で置けなかったものは unplaced、小は dropped。"""
    placed, unplaced, dropped = [], [], []
    order = sorted(items, key=lambda it: (0 if it["tier"] == "大" else 1 if it["tier"] == "中" else 2, -it["prio"]))

    def seg(p):
        return ((p["text_x"], p["box"]["y1"] + 1), (p["x"], p["py"] - 3))

    def clean(box, cx, it):
        """引き出し線が、ほかの線と交わらず、ほかの名前を通らず、ほかの線が新しい名前を通らない。"""
        if "py" not in it:
            return True
        s = ((cx, box["y1"] + 1), (it["x"], it["py"] - 3))
        for p in placed:
            ps = seg(p)
            if segments_cross(s[0], s[1], ps[0], ps[1]) or segment_hits_box(s[0], s[1], p["box"]) \
                    or segment_hits_box(ps[0], ps[1], box):
                return False
        return True
    for it in order:
        done = False
        for strict in (True, False):
            font = FONT[it["tier"]]
            while True:
                w = text_width(it["label"], font) + (font * 0.6 if it.get("marginal") else 0)
                shifts = [0.0]
                k = 6.0
                while k <= MAX_SHIFT[it["tier"]]:
                    shifts += [k, -k]
                    k += 6.0
                # 横にずらすより先に、上の段へ積む(引き出し線が寝ないように)。ずらす量を少しずつ増やす
                for sh in shifts:
                    for r, y in enumerate(row_ys):
                        cx = it["x"] + sh
                        x0 = min(max(cx - w / 2, 2.0), width - 2.0 - w)
                        box = {"x0": x0, "x1": x0 + w, "y0": y - font, "y1": y + 3}
                        if all(not boxes_overlap(box, p["box"], gap) for p in placed) and                                 (not strict or clean(box, x0 + w / 2, it)):
                            placed.append({**it, "row": r, "font": font, "box": box, "text_x": x0 + w / 2, "text_y": y,
                                           "shift": round(x0 + w / 2 - it["x"], 1), "strict": strict})
                            done = True
                            break
                    if done:
                        break
                if done or font - 1 < MIN_FONT[it["tier"]]:
                    break
                font -= 1
            if done:
                break
            if it["tier"] == "小":      # 小は、交わらない置き場所が無ければ出さない
                break
        if not done:
            (dropped if it["tier"] == "小" else unplaced).append(it)
    return placed, unplaced, dropped

def style(n_bands, bands=terrain.DEFAULT_BANDS):
    lv = "".join(f"--B{i}:{band_color(i, n_bands, False)};--R{i}:{band_color(max(0, i - 2), n_bands, False)};" for i in range(n_bands))
    dv = "".join(f"--B{i}:{band_color(i, n_bands, True)};--R{i}:{band_color(max(0, i - 2), n_bands, True)};" for i in range(n_bands))
    rules = "".join(f"path.B{i}{{fill:var(--B{i})}}polyline.R{i}{{stroke:var(--R{i})}}" for i in range(n_bands))
    dark_vars = ("--bg:#11161c;--fg:#e6ebf0;--mut:#9aa7b4;--card:#18202a;--line:#2a3542;"
                 "--main:#ff7a7f;--sub:#8cb8ff;--side:#c2ccd6;--nearbg:#7d6a52;--nearln:#5c4d3b;" + dv)
    base = (":root{--bg:#f7f8fa;--fg:#1d2733;--mut:#5b6876;--card:#fff;--line:#d8dee6;--main:#b4232c;--sub:#1f4f8a;"
            "--side:#3d4a57;--nearbg:#6b5a45;--nearln:#4a3d2e;" + lv + "}\n"
            '@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){' + dark_vars + "}}\n"
            ':root[data-theme="dark"]{' + dark_vars + "}\n" + rules + "\n")
    base += """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif;line-height:1.6}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 4px} h2{font-size:16px;margin:20px 0 6px}
p{margin:4px 0;color:var(--mut);font-size:14px}
table{border-collapse:collapse;font-size:13px;margin:8px 0;width:100%} td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}
a{color:var(--sub)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
.scroll svg{display:block;max-width:none}
path.near{fill:url(#hatch)} rect.nearbg{fill:var(--nearbg)} line.nearln{stroke:var(--nearln);stroke-width:2}
polyline.ridge{fill:none;stroke-width:1.6}
line.tick{stroke:var(--mut);stroke-width:1} text.tickt{fill:var(--mut);font-size:11px} text.tickt.dir{fill:var(--fg);font-weight:600}
text.nm{fill:var(--side)} text.nm.大{fill:var(--main)} text.nm.中{fill:var(--sub)} tspan.mk{fill:var(--mut)}
line.lead{stroke:var(--mut);stroke-width:.7;opacity:.8} line.lead.大{stroke:var(--main);opacity:.9}
circle.dot{fill:var(--side)} circle.dot.大{fill:var(--main)} circle.dot.中{fill:var(--sub)}
text.credit{fill:var(--mut);font-size:10px} text.nearlab{fill:#fff;font-size:11px;paint-order:stroke;stroke:#0006;stroke-width:3px}
.legend span{display:inline-block;margin-right:14px;font-size:13px}
.k{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px;vertical-align:-1px}
"""
    return base


# ---- 元: experiments/panorama/stage25_draw.py ----

CLIP_MARK = "上は省略"

def top_angle(far_top, peaks) -> float:
    """図の上端の見上げ角: 3km より先の地形のいちばん高い点と、名前を出す山の点の高い方。"""
    cands = [float(np.nanmax(far_top))] + [p["draw_angle"] for p in peaks]
    return max(cands)

def clip_runs(az, near_ang, amax, min_width=0.5):
    over = np.isfinite(near_ang) & (near_ang > amax + 1e-9)
    runs, cur = [], None
    for a, o in zip(az, over):
        if o and cur is None:
            cur = [a, a]
        elif o:
            cur[1] = a
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    return [(float(a), float(b)) for a, b in runs if b - a >= min_width]

def render_v2(sky, peaks, *, az0, az1, px_per_deg, v_exag, title, bands=terrain.DEFAULT_BANDS, near_label=None,
              min_rows=3, max_rows=12, note="", credit_extra=""):
    az = np.asarray(sky["az"])
    ang = np.asarray(sky["ang"])
    n_b = ang.shape[1]
    W = (az1 - az0) * px_per_deg + 40

    def X(a):
        return 20 + (a - az0) * px_per_deg
    amax_all = float(np.nanmax(ang[:, -1]))
    amax = top_angle(sky["far_top"], peaks)
    top_far = ang[:, -1]
    far_min = float(np.nanmin(top_far))
    peak_min = min([p["draw_angle"] for p in peaks] or [far_min])
    amin = min(max(far_min - 0.6, amax - 4.0), peak_min - 0.25)
    px_v = px_per_deg * v_exag
    res = None
    for rows_n in range(min_rows, max_rows + 1):
        label_h = 24 * rows_n + 14
        row_ys = [label_h - 6 - i * 24 for i in range(rows_n)]
        ts = label_h + 8
        items = [{"label": p["label"], "x": X(p["az"]), "py": ts + (amax - p["draw_angle"]) * px_v, "tier": p["tier"],
                  "prio": p.get("prio", 0.0), "marginal": p.get("marginal", False), "ref": p} for p in peaks]
        placed, unplaced, dropped = layout(items, W, row_ys)
        loose = sum(1 for p_ in placed if not p_["strict"]) + len(dropped)
        if res is None or (len(unplaced), loose) < (len(res[3]), res[5]):
            res = (rows_n, label_h, placed, unplaced, dropped, loose)
        if not unplaced and loose == 0:
            break
        if not res[3] and rows_n - res[0] >= 3:
            break
    rows_n, label_h, placed, unplaced, dropped, _ = res
    top_sky = label_h + 8
    H = top_sky + (amax - amin) * px_v + 56

    def Y(a):
        return top_sky + (amax - a) * px_v
    bottom = H - 44
    parts = ['<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
             '<rect width="6" height="6" class="nearbg"/><line x1="0" y1="0" x2="0" y2="6" class="nearln"/></pattern></defs>']
    for bi in range(n_b - 1, -1, -1):
        ys = np.where(np.isfinite(ang[:, bi]), np.clip(Y(ang[:, bi]), top_sky, bottom), bottom)
        poly = [(X(az[0]), bottom)] + [(X(a), y) for a, y in zip(az, ys)] + [(X(az[-1]), bottom)]
        cls = "near" if bands[bi] <= terrain.NEAR_BAND_M else f"B{bi}"
        parts.append(f'<path d="{svgfmt.polygon_path(poly)}" class="{cls}"/>')   # 地形の帯(相対座標の path。weather.POLY_RE で探す)
    for p in peaks:
        if p.get("ridge"):
            pts = " ".join(f"{X(a):.1f},{min(max(Y(v), top_sky), bottom):.1f}" for a, v in p["ridge"])
            parts.append(f'<polyline points="{pts}" class="ridge R{p["band"]}"/>')
    # 上は省略(足もとの地形が上端を超える所)
    cuts = clip_runs(az, ang[:, -1], amax)
    for a_from, a_to in cuts:
        parts.append(f'<line x1="{X(a_from):.1f}" y1="{top_sky:.1f}" x2="{X(a_to):.1f}" y2="{top_sky:.1f}" class="cut"/>')
        if a_to - a_from >= 3.0:
            parts.append(f'<text x="{X((a_from + a_to) / 2):.1f}" y="{top_sky + 13:.1f}" text-anchor="middle" class="nearlab">{CLIP_MARK}</text>')
    for a in range(int(math.ceil(az0)), int(math.floor(az1)) + 1):
        if a % 5:
            continue
        x = X(a)
        lab = DIR8.get(a % 360 if a != 360 else 360)
        parts.append(f'<line x1="{x:.1f}" y1="{bottom:.1f}" x2="{x:.1f}" y2="{bottom + (9 if lab else 4):.1f}" class="tick"/>')
        if a % 10 == 0 or lab:
            text = f'{html.escape(lab) + " " if lab else ""}{a % 360}°'
            anchor = "middle"
            if a == int(math.ceil(az0)):
                anchor, x = "start", x - 2
            elif a == int(math.floor(az1)):
                anchor, x = "end", x + 2
            parts.append(f'<text x="{x:.1f}" y="{bottom + 21:.1f}" class="tickt{" dir" if lab else ""}" text-anchor="{anchor}">{text}</text>')
    geom = []
    for pl in placed:
        p = pl["ref"]
        x, yp = X(p["az"]), Y(p["draw_angle"])
        t = p["tier"]
        mark = (f'<tspan class="mk" style="font-size:{pl["font"] * 0.6:.1f}px">{MARGINAL_MARK}</tspan>'
                if p.get("marginal") else "")
        tip = html.escape(p.get("title", p["label"]))
        parts.append(f'<g><title>{tip}</title><line x1="{pl["text_x"]:.1f}" y1="{pl["box"]["y1"] + 1:.1f}" x2="{x:.1f}" '
                     f'y2="{yp - 3:.1f}" class="lead {t}"/><circle cx="{x:.1f}" cy="{yp:.1f}" r="{DOT_R[t]}" class="dot {t}"/>'
                     f'<text x="{pl["text_x"]:.1f}" y="{pl["text_y"]:.1f}" text-anchor="middle" class="nm {t}" '
                     f'style="font-size:{pl["font"]}px;font-weight:{WEIGHT[t]}">{html.escape(p["label"])}{mark}</text></g>')
        geom.append({"display": p["label"], "tier": t, "box": pl["box"], "lead_from": [pl["text_x"], pl["box"]["y1"] + 1],
                     "peak": [x, yp], "dot_r": DOT_R[t]})
    for a_from, a_to, text in (near_label or []):
        parts.append(f'<text x="{X((a_from + a_to) / 2):.1f}" y="{bottom - 8:.1f}" text-anchor="middle" class="nearlab">{html.escape(text)}</text>')
    cred = f"出典: {CREDIT}{credit_extra}。{DRAFT_NOTE}。縦は横の {v_exag:g} 倍に強調。{note}"
    parts.append(f'<text x="12" y="{H - 8:.1f}" class="credit">{html.escape(cred)}</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" width="{W:.0f}" height="{H:.0f}" '
           f'role="img" aria-label="{html.escape(title)}">' + "".join(parts) + "</svg>")
    corr = correspondence(geom) if geom else {}
    boxes = [pl["box"] for pl in placed]
    check = {"title": title, "width": round(W), "height": round(H), "rows": rows_n, "v_exag": v_exag,
             "px_per_deg": px_per_deg, "angle_range": [round(amin, 3), round(amax, 3)],
             "angle_max_all_terrain": round(amax_all, 3), "clipped_runs": [[round(a, 1), round(b, 1)] for a, b in cuts],
             "n_names": len(placed), "by_tier": {t: sum(1 for pl in placed if pl["tier"] == t) for t in FONT},
             "overlaps": count_overlaps(boxes),
             "unplaced_big_mid": [it["label"] for it in unplaced], "dropped_small": [it["label"] for it in dropped],
             "shrunk": [{"label": pl["label"], "font": pl["font"]} for pl in placed if pl["font"] != FONT[pl["tier"]]],
             "shifted": [{"label": pl["label"], "px": pl["shift"]} for pl in placed if abs(pl["shift"]) > 1],
             "labels_out_of_frame": [pl["label"] for pl in placed if pl["box"]["x0"] < 0 or pl["box"]["x1"] > W],
             "peaks_below_frame": [p["label"] for p in peaks if Y(p["draw_angle"]) > bottom],
             "credit_fits": 12 + text_width(cred, 10) <= W,
             "correspondence": {k: (len(v) if isinstance(v, list) else v) for k, v in corr.items()},
             "correspondence_detail": {k: v for k, v in corr.items() if isinstance(v, list) and v and k != "closest_peak_pairs_px"}}
    return svg, check

EXTRA_CSS = "line.cut{stroke:var(--fg);stroke-width:1.2;stroke-dasharray:4 3}"


# ---- 元: experiments/panorama/stage36_run.py ----

TIER_NAMES = ("真上", "20km 先", "50km 先", "100km 先")

def state_label(row, start_pct):
    """名前の横の文字: 「山名 雲 NN%」だけ(山頂の高さの雲量が描き始め以上のとき)。予報が無い山は名前だけ。"""
    lab = row["label"]
    if row.get("summit_cloud") is not None and row["summit_cloud"] >= start_pct:
        lab += f" 雲 {weather.r0(row['summit_cloud'])}%"
    return lab

def insert_by_depth(svg, items, n_b=len(terrain.DEFAULT_BANDS)):
    """items: [(層の番号 bi or -1, svg)]。bi の多角形の直前に入れる(-1 は最後の多角形の直後)。
    render_v2 は遠い層(n_b − 1)から近い層(0)の順に多角形を描く。同じ場所に入れるものは items の順。"""
    polys = list(weather.POLY_RE.finditer(svg))
    pos = {n_b - 1 - i: mo.start() for i, mo in enumerate(polys)}
    end = polys[-1].end() if polys else svg.rfind("</svg>")
    buckets = {}
    for bi, s in items:
        p = pos.get(bi, end) if bi >= 0 else end
        buckets.setdefault(p, []).append(s)
    out, last = [], 0
    for p in sorted(buckets):
        out.append(svg[last:p])
        out.append("".join(buckets[p]))
        last = p
    out.append(svg[last:])
    return "".join(out)


# ---- 元: experiments/panorama/stage37_run.py ----

POLYLINE_RE = re.compile(r'<polyline points="([^"]*)" class="ridge R(\d+)"/>')

PEAK_G_RE = re.compile(r'<g><title>([^<]*)</title><line x1="([\d.\-]+)"(.*?)/><circle(.*?)/>(<text .*?</text>)</g>', re.S)

TEXT_RE = re.compile(r'<text x="([\d.\-]+)" y="([\d.\-]+)" text-anchor="middle" class="([^"]+)" '
                     r'style="font-size:([\d.]+)px;font-weight:(\d+)">(.*?)</text>', re.S)

HASH_RE = re.compile(r"#([0-9a-f]+)$")

def wx_group(st, default, body, extra_cls=""):
    if not body:
        return ""
    disp = "" if st == default else ' display="none"'
    return f'<g class="wx{extra_cls}" data-st="{st}"{disp}>{body}</g>'

def _name_text(n, label, op):
    op_s = "" if op >= 0.999 else f"opacity:{op:.3f};"
    return (f'<text x="{n["x0"]:.1f}" y="{n["y"]}" text-anchor="start" class="{n["cls"]}" '
            f'style="{op_s}font-size:{n["fs"]}px;font-weight:{n["fw"]}">{html.escape(label)}{n["mark"]}</text>')

def merge_sky_cells(runs):
    """空の区画(方位 1° ごとの縦の帯の中の区画)のうち、となりの方位で上下の端と色が同じものを横につなぐ。
    返り値: [(方位の始め, 方位の数, y0, y1, 色)]。上下の端は図に書く値(小数 1 桁)で比べる。"""
    cols = {}
    for a, y0, y1, col in runs:
        cols.setdefault(a, []).append((f"{y0:.1f}", f"{y1 - y0:.1f}", y0, y1, col))
    out, open_ = [], {}
    for a in sorted(cols):
        nxt = {}
        for k0, kh, y0, y1, col in cols[a]:
            key = (k0, kh, col)
            cell = open_.get(key)
            if cell is not None and cell[0] + cell[1] == a:
                cell[1] += 1
            else:
                cell = [a, 1, y0, y1, col]
                out.append(cell)
            nxt[key] = cell
        open_ = nxt
    return [tuple(c) for c in out]

def sky_paths(runs, X, px):
    """空の区画を、色ごとに 1 つの path にまとめる(区画の形・色・重なりの幅は段階3.6 の長方形と同じ)。
    となりの方位で上下の端と色が同じ区画は、1 つの長方形につなぐ(案 D。塗る所は同じ)。"""
    by = {}
    for a, n, y0, y1, col in merge_sky_cells(runs):
        w = n * px + 0.6
        by.setdefault(col, []).append(f"M{X(a):.1f} {y0:.1f}h{w:.1f}v{y1 - y0:.1f}h-{w:.1f}z")
    return "".join(f'<path class="skyc" d="{"".join(v)}" fill="{k}"/>' for k, v in by.items())

def _sky_runs(scene, sky, X, Y, top_sky, bottom, px):
    azs = np.asarray(sky["az"], dtype=float)
    top = np.asarray(sky["ang"])[:, -1]
    out, n_none, n_sub = [], 0, 8
    for a in range(0, 360):
        j = int(np.argmin(np.abs(azs - (a + 0.5))))
        ys = Y(top[j]) if np.isfinite(top[j]) else bottom
        ys = min(max(ys, top_sky + 8), bottom)
        rh = (ys - top_sky) / 4.0
        run = None
        for s in range(4 * n_sub):
            y0 = top_sky + s * rh / n_sub
            rf = (s + 0.5) / n_sub - 0.5
            v = weather.sky_value_at(scene["grid"], a + 0.5, rf)
            col = weather.NO_FORECAST_COLOR if v is None else weather.sky_color(v)
            n_none += v is None
            y1 = y0 + rh / n_sub + 0.6 if s < 4 * n_sub - 1 else bottom
            if run is not None and run[3] == col:
                run[2] = y1
            else:
                if run is not None:
                    out.append(tuple(run))
                run = [a, y0, y1, col]
        out.append(tuple(run))
    return out, n_none

def _sea_pats(seed_key, bi, period, scale=1.0):
    k = clouds.ARC_W_SCALE * scale
    return {"up": clouds.ArcPattern(clouds.seed_of("sea-up", seed_key, bi), clouds.UP_ARC_PX, clouds.UP_ARC_H, period, scale=k),
            # 雲海のうすい雲は、弧を下げない(高さの幅が大きく、下げた所が櫛のような長方形に見えたため)
            "thin": clouds.ArcPattern(clouds.seed_of("sea-thin", seed_key, bi), clouds.THIN_ARC_PX, clouds.UP_ARC_H, period, scale=k),
            "shadow": clouds.ArcPattern(clouds.seed_of("sea-sh", seed_key, bi), clouds.UP_ARC_PX, clouds.UP_ARC_H, period, scale=k)}

def _upper_pats(seed_key, hpa, period):
    return {"up": clouds.ArcPattern(clouds.seed_of("up-up", seed_key, hpa), clouds.UP_ARC_PX, clouds.UP_ARC_H, period),
            "lo": clouds.ArcPattern(clouds.seed_of("up-lo", seed_key, hpa), clouds.LO_ARC_PX, clouds.LO_ARC_H, period),
            "thin": clouds.ArcPattern(clouds.seed_of("up-thin", seed_key, hpa), clouds.THIN_ARC_PX, clouds.UP_ARC_H, period, dip_ratio=0.45),
            "shadow": clouds.ArcPattern(clouds.seed_of("up-sh", seed_key, hpa), clouds.UP_ARC_PX, clouds.UP_ARC_H, period)}

def _wrap(geom, period):
    out = []
    if geom["a0"] < 0:
        out.append(period)
    if geom["a1"] > 360.0:
        out.append(-period)
    return out

def _legend_base(start_pct):
    stops = "".join(f'<span style="display:inline-block;width:28px;height:12px;background:{weather.sky_color(v)}"></span>'
                    for v in (0, 25, 50, 75, 100))

    def swatch(alpha, shadow=False):
        inner = (f'<span style="display:block;width:34px;height:8px;background:rgba(255,255,255,{alpha:.2f})"></span>'
                 f'<span style="display:block;width:34px;height:4px;background:{clouds.SHADOW_FILL};opacity:{alpha:.2f}"></span>'
                 if shadow else f'<span style="display:block;width:34px;height:12px;background:rgba(255,255,255,{alpha:.2f})"></span>')
        return f'<span style="display:inline-block;width:34px;height:12px;background:{weather.sky_color(20)}">{inner}</span>'
    sw = "".join(f"{swatch(clouds.opacity(c), True)} {c}% " for c in (10, 40, 70, 100))
    return (f"<p>ボタン: 「地形だけ」は天気を重ねない図(空は雲量 0 の色)。時刻のボタンは、その時刻の予報の雲・空・名前の雲量。"
            f"山並み・名前の位置・引き出し線・方位の目盛りは、どのボタンでも同じ。</p>"
            f"<p>空の背景: 方位 10° ごと × 縦 4 段(上から 真上・20km 先・50km 先・100km 先)の点の、視点より上にある雲すべての最大"
            f"(視点のすぐ下の面に雲があり、すぐ上の面に雲が無い点は、下の面の雲量 × {clouds.SKY_UNKNOWN_RATIO:g}(仮)を足す)"
            f"{stops} 0%(青)〜100%(白)。境目は補間。"
            f'<span style="display:inline-block;width:28px;height:12px;background:{weather.NO_FORECAST_COLOR}"></span> その点の予報なし。</p>'
            f"<p>雲海の帯: 視点より低い雲(気圧面の雲量 {start_pct:g}%(仮)以上)を、山並みの層の区切り(3・10・20・35・60・100・150km)"
            f"ごとに 1 本。上の縁はその区切りの距離で雲の上面を見た高さで、雲の上面より高い山は帯の上に頭を出す。"
            f"雲の帯: 視点の高さをまたぐ雲は、段階3.6 のまま 50km 先に置いた記号。濃さと厚みは雲量に比例、下のほうはうすい影の色 {sw}。</p>"
            f"<p>{swatch(clouds.THIN_RATIO * clouds.opacity(70))} うすい雲: 雲があるか、予報の高さの刻みでは分からない範囲。</p>"
            f"<p>山にかかる雲: 名前の出る山の位置の予報で、山頂 − {clouds.CLUMP_BELOW_SUMMIT_M:g}m(仮)より上の面に雲があり、"
            f"帯が包んでいないとき。名前の「雲 NN%」は山頂の高さの雲量({start_pct:g}% 以上のとき)。名前の濃さは、"
            f"山頂を隠す雲の量(手前の雲海の帯と、山頂の高さの雲量の大きいほう)に比例してうすくなる"
            f"(不透明度 = 1 − {clouds.NAME_FADE_SPAN:g} × 雲量/100、仮)。雲の帯の奥の稜線の線は、帯が濃い所では描かない。"
            f"途中の雲は表と facts.md にだけ。</p>")

def state_notes(states, scenes, sunrise):
    notes = {}
    for st, _, hour in states:
        if hour is None:
            continue
        if scenes.get(st) is None:
            notes[st] = "予報なし"
        elif sunrise and hour < sunrise:
            notes[st] = "日の出前"
    return notes


# ---- 元: experiments/panorama/stage38_run.py ----

def legend(start_pct):
    s = _legend_base(start_pct)
    s = s.replace("ごとに 1 本。", f"ごとに 1 本({clouds.NEAR_THIN_KM:g}km 以下の区切りは、手前の山並みが透けるうすい雲で)。", 1)
    return s + (f"<p>雲の端: 雲量は方位 10° の格子の間を線形につなぎ、雲が終わる所は {clouds.FADE_DEG:g}°(仮)かけて、"
                f"濃さと厚みをゆっくり 0 まで下げる。</p>"
                f"<p>図の縦の範囲: 視点から {clouds.NEAR_TOP_KM:g}km より遠い地形と山で決める。足もとの地形が上の端を超える所は"
                f"「{clouds.NEAR_CLIP_TEXT}」。</p>")


# ---- 元: experiments/panorama/stage39_layout.py ----

FS_SCREEN = (13.0, 11.5, 10.5)      # 百名山・76 座・そのほか(画面上の px。仮)

PAD_SCREEN = 7.0                    # 名前の左右の空き(仮)

LEAD_HALF_SCREEN = 3.0              # 引き出し線のために空けておく幅の半分

SIDE_OFF_SCREEN = 5.0               # 名前を点の左右に振るときの、点からのずれ

BASE_ROWS = 4

EXTRA_ROWS = 6                      # 百名山・76 座が置けないとき、まずここまで段を増やす(仮)

MAX_ROWS = 8                        # 段の上限(横向きの画面は LAND_MAX_ROWS)。これでも置けなければ名前どうしの重なりがいちばん短い所(仮)

ZOOMS = (90, 45, 20)                # 「広く」「標準」「拡大」(画面の幅に入る方位の幅。仮)

ZOOM_LABELS = {90: "広く", 45: "標準", 20: "拡大"}

WIDTH_BUCKETS = (320, 360, 390, 430, 480, 560, 640, 720, 800, 844)

WIDE_MIN_PX = 900                   # これ以上は広い画面(全周を横に送る形。仮)

OPEN_WIDE_ZOOM_MIN_PX = 560         # せまい画面で、これ以上なら「広く」で開く(仮)

LAND_MAX_ROWS = 5                   # 横向きの画面の段の上限(図の高さが約 200〜250px しかないため。仮)

LAND_RIDGE_MAX_PX = 246            # 横向きでは、図の山並みの下の端を上からこの px までに収める(実機の枠で使える高さ 約 200〜250px。仮)

FIT_MIN_RATIO = 0.6                 # 高さに収めるための縮小の下限: その縮尺の本来の倍率のこの倍まで(仮)

FIT_MIN_RIDGE_PX = 70               # 同じく下限: 山並みの高さ(空の上の端から地形の下の端まで)がこの px より小さくならない(仮)。
                                    # 2 つの大きいほうで止め、収まらない分はページを縦に送る

FRAME_GUTTER_PX = 16                # せまい画面の、図の枠の左右の余白の合計

ANCHORS = ("middle", "start", "end")

def text_w(text: str, font_px: float) -> float:
    """CJK 1 文字 = 1.02 × 大きさ、ASCII・数字は 0.6 倍(段階1.5 の見積もりより少し広め)。"""
    return sum(font_px * 1.02 if ord(ch) > 0x2E7F else font_px * 0.6 for ch in text)

def name_box(x_scr: float, w: float, anchor: str):
    if anchor == "middle":
        return x_scr - w / 2.0, x_scr + w / 2.0
    if anchor == "start":
        return x_scr - SIDE_OFF_SCREEN, x_scr - SIDE_OFF_SCREEN + w
    return x_scr + SIDE_OFF_SCREEN - w, x_scr + SIDE_OFF_SCREEN

def _hit(intervals, a, b):
    return any(a < q1 and b > q0 for q0, q1 in intervals)

FREE_HEIGHT_ZOOM = 20              # 横向きの「拡大」は画面の幅に 20° を守り、高さに収めない(ページを縦に送って見る)

OMIT_NOTE = "名前を省いた山があります。『拡大』で出ます"

STRICT_ZOOMS = (90, 45)             # 「広く」「標準」: 置ける分だけ(名前どうし・名前と引き出し線が重ならない所だけ)

WIDE_FS = (16.0, 13.0, 11.0)        # 広い画面(幅 WIDE_MIN_PX 以上)の名前の大きさ(px。前からの図の FONT と同じ)

WIDE_ROW_H = 24.0                   # 広い画面の名前の段の高さ(px。前からの図と同じ)

def name_order(items):
    """名前を置く順番(2026-11-20 の決定): 百名山 → 76 座 → そのほか。同じ格の中では視点に近い山を先に、
    距離が同じなら標高の高い山を先に。富士山は、富士山が見える図では必ず名前を出すので、いちばん先に置く。"""
    def key(i):
        it = items[i]
        fuji = 0 if (it["tier"] == 0 and it.get("label") == FUJI) else 1
        return (it["tier"], fuji, it.get("dist_km", 0.0), -it.get("ele_m", 0.0), it["x"])
    return sorted(range(len(items)), key=key)

def layout_names(items, deg, width_px: float = 0.0, px_per_deg: float = 24.0, max_rows: int = None, *,
                 k: float = None, fonts=FS_SCREEN):
    """items: [{"x": 図の x(SVG 単位), "tier": 0/1/2, "texts": [全部の時刻の文字], "dist_km", "ele_m", "label"}]。
    返り値: {"rows": [段(0 が下)or -1(名前を出さない)], "anc": [置き方], "n_rows": 使った段の数, "k": 縮尺}。
    「広く」「標準」(STRICT_ZOOMS): 名前どうしが重ならず、名前と引き出し線が重ならない所にだけ置く。置けない山は点だけ。
    「拡大」: 見えている百名山と 76 座は全部出す(段を増やして重なりを減らす。それでも置けなければ重なりがいちばん短い所)。
    そのほかの山は、どの縮尺でも置ける分だけ(「広く」では出さない)。
    deg=None は広い画面(幅 WIDE_MIN_PX 以上。k と fonts を渡す): 「標準」と同じ決まり(置ける分だけ。置けない山は点だけ)。"""
    if k is None:
        k = (width_px - FRAME_GUTTER_PX) / (deg * px_per_deg)
    mx = MAX_ROWS if max_rows is None else max_rows
    ex = min(EXTRA_ROWS, mx)
    base = min(BASE_ROWS, mx)
    strict_only = deg is None or deg in STRICT_ZOOMS
    n = len(items)
    rows_out, anc_out = [-1] * n, ["middle"] * n
    names = {}                       # 段 → [(a, b)]
    corridors = {}                   # 段 → [(a, b)](上の段の名前の引き出し線が通る所)
    for i in name_order(items):
        it = items[i]
        t = it["tier"]
        if deg is not None and deg >= 90 and t == 2:
            continue
        fs = fonts[t]
        w = max((text_w(s, fs) for s in it["texts"]), default=fs)
        xs = it["x"] * k

        def overlap_names(r, a, b):
            return _hit(names.get(r, []), a, b)

        def crossings(r, a, b):
            n = sum(1 for q0, q1 in corridors.get(r, []) if a < q1 and b > q0)
            for u in range(r):
                n += sum(1 for q0, q1 in names.get(u, []) if q0 < xs < q1)
            return n

        def search(rows, strict):
            best = None
            for r in rows:
                for anchor in ANCHORS:
                    a, b = name_box(xs, w, anchor)
                    a, b = a - PAD_SCREEN, b + PAD_SCREEN
                    if overlap_names(r, a, b):
                        continue
                    c = crossings(r, a, b)
                    if strict and c == 0:
                        return r, anchor
                    if not strict and (best is None or c < best[0]):
                        best = (c, r, anchor)
            return None if strict or best is None else best[1:]

        if strict_only:
            got = search(range(mx if t < 2 else base), True)
        else:
            got = search(range(base), True)
            if got is None and t < 2:
                got = search(range(base, mx), True)
                if got is None:
                    got = search(range(ex), False)
                if got is None:
                    got = search(range(ex, mx), False)
                if got is None:
                    # 段は MAX_ROWS まで。それでも置けなければ、名前どうしの重なりがいちばん短い所に出す(出さないよりよい)
                    best = None
                    for r in range(mx):
                        for anchor in ANCHORS:
                            a, b = name_box(xs, w, anchor)
                            ov = sum(max(0.0, min(b, q1) - max(a, q0)) for q0, q1 in names.get(r, []))
                            key = (ov, crossings(r, a, b), r)
                            if best is None or key < best[0]:
                                best = (key, r, anchor)
                    got = best[1:]
        if got is None:
            continue
        r, anchor = got
        a, b = name_box(xs, w, anchor)
        names.setdefault(r, []).append((a - PAD_SCREEN / 2.0, b + PAD_SCREEN / 2.0))
        for u in range(r):
            corridors.setdefault(u, []).append((xs - LEAD_HALF_SCREEN, xs + LEAD_HALF_SCREEN))
        rows_out[i], anc_out[i] = r, anchor
    used = [r for r in rows_out if r >= 0]
    return {"rows": rows_out, "anc": anc_out, "n_rows": (max(used) + 1) if used else 0, "k": k, "fs": tuple(fonts)}

def layout_wide(items):
    """広い画面の名前の置き方(図は縮めずに 1 単位 = 1px で出す)。返り値は layout_names と同じ形。
    百名山 → 76 座 → そのほかの順に、名前どうし・名前と引き出し線が重ならない所にだけ置く。置けない山は点だけ。"""
    return layout_names(items, None, k=1.0, fonts=WIDE_FS)

def layout_reference(items, deg: float, width_px: float, px_per_deg: float = 24.0):
    """参考の見本(phone_reference_build.py の JS)の置き方を、比べるために Python に写したもの。段は 4 まで。
    置けない名前は出さない(百名山でも)。"""
    k = (width_px - FRAME_GUTTER_PX) / (deg * px_per_deg)
    fsz = [13.0, 11.5, 10.5]
    n = len(items)
    rows = [[] for _ in range(4)]
    rows_out, anc_out = [-1] * n, ["middle"] * n
    order = sorted(range(n), key=lambda i: (items[i]["tier"], i))
    pad = 7.0
    for i in order:
        it = items[i]
        if deg >= 90 and it["tier"] == 2:
            continue
        xs = it["x"] * k
        w = max(sum((0.58 if ord(c) < 256 else 1.02) * fsz[it["tier"]] for c in s) for s in it["texts"])
        opts = [("middle", xs - w / 2, xs + w / 2), ("start", xs - 5, xs + w), ("end", xs - w, xs + 5)]
        done = -1
        anc = "middle"
        for p in range(2 if it["tier"] == 0 else 1):
            if done >= 0:
                break
            for r in range(4):
                if done >= 0:
                    break
                for o in opts:
                    a, b = o[1] - pad, o[2] + pad
                    ok = not _hit(rows[r], a, b)
                    if p == 0 and ok:
                        for u in range(r):
                            if any(xs > q0 and xs < q1 for q0, q1 in rows[u]):
                                ok = False
                                break
                    if ok:
                        rows[r].append((a, b))
                        for u2 in range(r):
                            rows[u2].append((xs - 3, xs + 3))
                        done, anc = r, o[0]
                        break
        rows_out[i], anc_out[i] = done, anc
    used = [r for r in rows_out if r >= 0]
    return {"rows": rows_out, "anc": anc_out, "n_rows": (max(used) + 1) if used else 0, "k": k, "font": fsz,
            "ascii": 0.58}

def count_conflicts(items, lay, ascii_ratio=0.6, cjk_ratio=1.02):
    """名前どうしの重なり(同じ段で箱が重なる組)と、名前と引き出し線の重なり(上の段の名前の引き出し線が、
    下の段の名前の箱を横切る組)を数える。箱は文字の幅だけ(空きを含めない)。"""
    k = lay["k"]
    boxes = []
    for i, it in enumerate(items):
        r = lay["rows"][i]
        if r < 0:
            continue
        fs = lay.get("fs", FS_SCREEN)[it["tier"]]
        w = max(sum(fs * (cjk_ratio if ord(c) > 0x2E7F else ascii_ratio) for c in s) for s in it["texts"])
        a, b = name_box(it["x"] * k, w, lay["anc"][i])
        boxes.append((r, a, b, it["x"] * k, i))
    nn = nl = 0
    for p in range(len(boxes)):
        r1, a1, b1, x1, _ = boxes[p]
        for q in range(p + 1, len(boxes)):
            r2, a2, b2, x2, _ = boxes[q]
            if r1 == r2 and a1 < b2 and a2 < b1:
                nn += 1
        for q in range(len(boxes)):
            r2, a2, b2, x2, _ = boxes[q]
            if q != p and r2 > r1 and a1 < x2 < b1:
                nl += 1
    return {"name_name": nn, "name_lead": nl}

def missing_required(items, lay):
    """百名山・76 座で名前を出していない数(0 であること)。"""
    return sum(1 for i, it in enumerate(items) if it["tier"] < 2 and lay["rows"][i] < 0)

FUJI = "富士山"

FUJI_MARGIN_SHARE = 0.1     # 富士山を画面の端から幅の 10% より内に入れる(名前が切れないように。仮)

def _count_in(hy, k, window_deg):
    n = 0
    for a in hy:
        d = ((a - k) + 180.0) % 360.0 - 180.0
        if abs(d) <= window_deg / 2.0:
            n += 1
    return n

def _middle_of_best_run(ks, step):
    """ks: 方位(昇順)。全周でつながる連続の区間のうち、いちばん長いもの(同じなら方位の小さいほう)の真ん中。"""
    if not ks:
        return None
    runs, cur = [], [ks[0]]
    for a in ks[1:]:
        if abs(a - cur[-1] - step) < 1e-6:
            cur.append(a)
        else:
            runs.append(cur)
            cur = [a]
    runs.append(cur)
    if len(runs) > 1 and abs(runs[0][0] - 0.0) < 1e-6 and abs(runs[-1][-1] + step - 360.0) < 1e-6:
        runs[0] = runs[-1] + runs[0]
        runs.pop()
    best = max(runs, key=lambda r: (len(r), -min(r)))
    return best[len(best) // 2] % 360.0

def fuji_visible(peaks) -> bool:
    return any(p.get("label") == FUJI for p in peaks)

def open_direction(peaks, window_deg: float, viewer_is_fuji: bool = False, step: float = 1.0):
    """返り値: (方位, その範囲の百名山の数, 富士山が入るか, 決まり)。
    - 富士山が見える(図に名前が出る)視点: 富士山を画面の端から 10% より内に入れたまま、百名山がいちばん多く入る方位。
      同じ数の方位が続くときは、その続きの真ん中(富士山を真ん中に寄せるのではない)。
    - 富士山が見えない視点・富士山そのものが視点: これまでの決まり(段階3.8 の open_direction と同じ)。"""
    hy = [p["az"] % 360.0 for p in peaks if p.get("tier") == "大"]
    fu = [p["az"] % 360.0 for p in peaks if p.get("label") == FUJI]
    if fu and not viewer_is_fuji:
        f = fu[0]
        half = window_deg / 2.0 * (1.0 - 2 * FUJI_MARGIN_SHARE)
        cands = []
        k = 0.0
        while k < 360.0 - 1e-9:
            d = ((f - k) + 180.0) % 360.0 - 180.0
            if abs(d) <= half:
                cands.append((k, _count_in(hy, k, window_deg)))
            k += step
        nmax = max(n for _, n in cands)
        az = _middle_of_best_run([k for k, n in cands if n == nmax], step)
        return az, nmax, True, "富士山を入れる"
    best = None
    k = 0.0
    while k < 360.0 - 1e-9:
        n = 0
        has_f = False
        for p in peaks:
            if p.get("tier") != "大":
                continue
            d = ((p["az"] % 360.0 - k) + 180.0) % 360.0 - 180.0
            if abs(d) <= window_deg / 2.0:
                n += 1
                has_f = has_f or p.get("label") == FUJI
        key = (n, has_f)
        if best is None or key > best[0]:
            best = (key, k)
        k += step
    (n, has_f), az = best
    return az, n, has_f, ("富士山が視点" if viewer_is_fuji else "百名山がいちばん多い")

def dir_name(az):
    names = ("北", "北東", "東", "南東", "南", "南西", "西", "北西")
    return names[int(((az % 360.0) + 22.5) // 45.0) % 8]


# ---- 元: experiments/panorama/stage39_clouds.py ----

def render_figure(parts, scenes, states, default, title, start_pct=clouds.START_PCT, note_extra="", seed_key="", opts=None):
    opts = dict(clouds.OPTS, **(opts or {}))
    sky = parts["sky"]
    rows_by = {st: {x["key"]: x for x in sc["rows"]} for st, sc in scenes.items()}
    variants = {}
    peaks = []
    for p in parts["peaks"]:
        h = weather.short_hash(p["row_id"])
        var = {weather.TERRAIN: p["label"]}
        for st in scenes:
            x = rows_by[st].get(p["row_id"])
            var[st] = state_label(x, start_pct) if x is not None else p["label"]
        font = FONT[p["tier"]]
        longest = max(var.values(), key=lambda s: text_width(s, font))
        variants[h] = {"var": var, "longest": longest, "name": p["label"], "row_id": p["row_id"],
                       "marginal": p.get("marginal", False)}
        peaks.append(dict(p, label=longest, title=p["title"] + f" #{h}"))
    peaks, top_deg, clamped = clouds.clamp_near_peaks(peaks, float(np.nanmax(sky["far_top"])))
    note = (f"雲は描き始め {start_pct:g}%(仮)。雲の高さは気圧面(1000〜600hPa の 7 面)のジオポテンシャル高度。"
            f"雲海の帯は図の層の区切りごと({clouds.NEAR_THIN_KM:g}km 以下の区切りはうすい雲)、視点の高さをまたぐ雲の帯は 50km 先に置いた記号。"
            f"かすみ(視程)・木・格子より細かい雲・面の間の雲の高さは入っていません。{note_extra}")
    svg, ch = render_v2(sky, peaks, az0=0.0, az1=360.0, px_per_deg=terrain.PX360, v_exag=1.5, title=title,
                            credit_extra="、" + weather.CREDIT35 + (terrain.OSM_CREDIT if parts["v"].get("osm_used") else ""),
                            note=note)
    svg = svg.replace(f">{CLIP_MARK}</text>", f">{clouds.NEAR_CLIP_TEXT}</text>")
    ch["near_peaks_clamped"] = clamped
    px, v_ex = terrain.PX360, 1.5
    top_sky = 24 * ch["rows"] + 14 + 8
    amin, amax = ch["angle_range"]
    H, W = ch["height"], ch["width"]
    bottom = H - 44
    obs = parts["viewer"]["ele_m"] + terrain.EYE_M

    def X(a):
        return 20 + a * px

    def Y(a):
        return top_sky + (amax - a) * px * v_ex

    def angle_fn(alt, d_m):
        return float(terrain.angle_deg(obs, alt, d_m))
    ridges = [(m.group(1), int(m.group(2))) for m in POLYLINE_RE.finditer(svg)]
    RIDGE_TOKEN = "<!--RIDGES-->"
    first = [True]

    def rid(mo):
        if first[0]:
            first[0] = False
            return RIDGE_TOKEN
        return ""
    svg = POLYLINE_RE.sub(rid, svg)
    names = []

    def peak_rep(mo):
        title_, x1, line_rest, circ, text = mo.groups()
        hm = HASH_RE.search(title_)
        tm = TEXT_RE.match(text)
        if not hm or not tm or hm.group(1) not in variants:
            return mo.group(0)
        v = variants[hm.group(1)]
        tx, ty, cls, fs, fw, content = tm.groups()
        fs_f = float(fs)
        wbox = text_width(v["longest"], fs_f) + (fs_f * 0.6 if v["marginal"] else 0)
        x0 = float(tx) - wbox / 2.0
        name_w = text_width(v["name"], fs_f)
        mark = ""
        mk = re.search(r'(<tspan class="mk".*?</tspan>)', content)
        if mk:
            mark = mk.group(1)
        names.append({"h": hm.group(1), "x0": x0, "y": ty, "cls": cls, "fs": fs, "fw": fw, "mark": mark, **v})
        lead_x = x0 + name_w / 2.0
        return f'<g><title>{title_}</title><line x1="{lead_x:.1f}"{line_rest}/><circle{circ}/></g>'
    svg = PEAK_G_RE.sub(peak_rep, svg)
    sky_parts, ridge_parts, name_parts = [], [], []
    banners = {}
    depth_items = {}
    stats = {}
    clip = (f'<clipPath id="cloudclip"><rect x="0" y="{top_sky:.1f}" width="{W:.1f}" height="{bottom - top_sky:.1f}"/>'
            f'</clipPath>')
    rep_m = clouds.REP_DIST_KM * 1000.0
    rep_bi = weather.depth_index(rep_m)
    max_bottom = float(weather.alt_at_angle(obs, amax, rep_m))
    period = 360.0 * px
    all_states = [st for st, _, _ in states]
    for st in all_states:
        sc = scenes.get(st)
        if st == weather.TERRAIN:
            sky_parts.append(wx_group(st, default, f'<rect x="20" y="{top_sky:.1f}" width="{period:.1f}" '
                                                      f'height="{bottom - top_sky:.1f}" fill="{weather.sky_color(weather.TERRAIN_SKY)}"/>'))
            ridge_parts.append(wx_group(st, default, "".join(f'<polyline points="{pts}" class="ridge R{L}"/>'
                                                                for pts, L in ridges)))
            name_parts.append(wx_group(st, default, "".join(_name_text(n, n["name"], 1.0) for n in names), " wxn"))
            stats[st] = {"names_faded": 0}
            continue
        if sc is None:
            continue
        st_stats = {"sea_bands": 0, "sea_bands_thin": 0, "sea_cols": 0, "upper_bands": 0, "clumps": 0, "thin_clumps": 0,
                    "ridge_hidden_pts": 0, "ridge_pts": 0, "clumps_merged_groups": 0, "clumps_in_merged": 0,
                    "band_merge_cols": 0, "band_cap_cols": 0}
        runs, n_none = _sky_runs(sc, sky, X, Y, top_sky, bottom, px)
        body = sky_paths(runs, X, px)
        st_stats["sky_cells_no_forecast"] = n_none
        azs = np.asarray(sky["az"], dtype=float)
        top = np.asarray(sky["ang"])[:, -1]
        j0 = int(np.argmin(np.abs(azs - 0.5)))
        ys0 = min(max(Y(top[j0]) if np.isfinite(top[j0]) else bottom, top_sky + 8), bottom)
        rh0 = (ys0 - top_sky) / 4.0
        if rh0 >= 9:
            for k, nm in enumerate(TIER_NAMES):
                body += (f'<text x="23" y="{top_sky + rh0 * (k + 0.5) + 3.5:.1f}" class="tier" '
                         f'style="font-size:9.5px;fill:#3d5266">{nm}</text>')
        sky_parts.append(wx_group(st, default, body))
        alpha_by_layer = []
        # 雲海の帯: 先に形を全部作り、5 章の係数を掛けてから描く
        sea_list = []
        for sb in sc["sea_bands"]:
            D = sb["D_m"]
            near_thin = D / 1000.0 <= clouds.NEAR_THIN_KM
            for k, run in enumerate(sb["runs"]):
                sc_ = clouds.sea_scale(D / 1000.0)
                pats = _sea_pats(seed_key, sb["bi"], period, sc_)
                geom = clouds.sea_band_geometry38(run, lambda alt, D=D: Y(angle_fn(alt, D)), X, px, bottom, pats, scale=sc_,
                                              near_thin=near_thin)
                sea_list.append({"bi": sb["bi"], "D": D, "k": k, "run": run, "geom": geom, "near_thin": near_thin,
                                 "wrap": _wrap(geom, period)})
        if opts["bands"] and len(sea_list) > 1:
            fac, n_m, n_c = clouds.band_factors(sea_list, X, px, period)
            st_stats["band_merge_cols"], st_stats["band_cap_cols"] = n_m, n_c
            for i, b in enumerate(sea_list):
                b["geom"] = clouds.apply_factor(b["geom"], fac[i], X, px, period)
        for b in sea_list:
            geom, D, run, near_thin, wrap = b["geom"], b["D"], b["run"], b["near_thin"], b["wrap"]
            if max((s[1] for s in geom["stops"]), default=0.0) <= 1e-4 and not geom["thin_upper"]:
                continue
            tt = html.escape(f"雲海 {D / 1000:g}km の区切り{'(うすい雲で表示)' if near_thin else ''} "
                             f"方位 {geom['a0'] % 360:.0f}°〜{geom['a1'] % 360:.0f}° "
                             f"上面 約{min(c['top'] for c in run['cols']):.0f}〜{max(c['top'] for c in run['cols']):.0f}m "
                             f"雲量 最大 {weather.r0(max(c['c'] for c in run['cols']))}%")
            svg_b = clouds.band_svg37(geom, f"{st}s{b['bi']}_{b['k']}", wrap, tt, "cloudclip")
            if near_thin:
                svg_b = svg_b.replace('class="cloud-band cloud-sea"', 'class="cloud-band cloud-sea cloud-sea-thin"', 1)
                st_stats["sea_bands_thin"] += 1
            depth_items.setdefault((b["bi"], st), []).append(svg_b)
            alpha_by_layer.append((b["bi"], clouds.band_alpha_fn(geom, wrap)))
            st_stats["sea_bands"] += 1
            st_stats["sea_cols"] += len(run["cols"])
        ubands = clouds.build_bands_filtered(sc["profile"], start_pct, max_bottom, clouds.upper_segments_filter(obs))
        clouds.attach_neighbors_band(ubands, sc["profile"])
        for k, b in enumerate(ubands):
            pats = _upper_pats(seed_key, b["hpas"][0], period)
            geom = clouds.upper_band_geometry38(b, lambda alt: Y(angle_fn(alt, rep_m)), X, px, pats)
            wrap = _wrap(geom, period)
            tt = html.escape(f"雲の帯 {'・'.join(str(h) for h in b['hpas'])}hPa 方位 {geom['a0'] % 360:.0f}°〜{geom['a1'] % 360:.0f}° "
                             f"雲量 最大 {weather.r0(max(c['c'] for c in b['cols']))}%")
            depth_items.setdefault((rep_bi, st), []).append(clouds.band_svg37(geom, f"{st}u{k}", wrap, tt, "cloudclip"))
            alpha_by_layer.append((rep_bi, clouds.band_alpha_fn(geom, wrap)))
            st_stats["upper_bands"] += 1
        fig_mts = {mm["key"]: mm for mm in weather.figure_mountains(parts)}
        specs = []
        for x in sc["rows"]:
            if not x.get("levels") or x.get("elev_m") is None:
                continue
            lv = [(alt, c, hpa) for alt, c, hpa, _ in x["levels"]]
            fm = fig_mts.get(x["key"])
            d_m = x["dist_km"] * 1000.0
            behind_band = rep_bi >= 0 and d_m > terrain.DEFAULT_BANDS[rep_bi]
            cover = (list(x["upper_cover"]) if behind_band else []) + list(x["sea_cover"])
            groups = clouds.clump_levels(lv, x["elev_m"], start_pct, cover or None, core.LEVEL_STACK_HPA)
            xx = X(fm["fig_az"] if fm else x["az"])
            if not groups:
                th = clouds.thin_at_summit(lv, x["elev_m"], start_pct, core.LEVEL_STACK_HPA)
                if th is not None and not behind_band and not clouds.covered(cover, x["elev_m"]):
                    wpx = clouds.width_deg(th["c"], x["dist_km"]) * px
                    yc = Y(angle_fn(x["elev_m"], d_m))
                    tt = html.escape(f"{x['label']} の山頂: うすい雲(雲があるか、予報の高さの刻みでは分からない範囲)")
                    specs.append({"thin": True, "bi": weather.depth_index(d_m), "x": xx, "y": yc, "c": th["c"], "w": wpx,
                                  "h": wpx * clouds.CLUMP_MAX_ASPECT, "seed": clouds.seed_of("thin", seed_key, x["key"]),
                                  "key": x["key"], "tt": tt})
                continue
            # 山頂にかかるかたまりの大きさと濃さは、山頂の雲量(はさむ 2 つの面の大きいほう)より小さくしない
            groups = clouds.with_summit_cloud(groups, lv, x["elev_m"], x.get("summit_cloud"))
            wpx = clouds.width_deg(groups[0]["c"], x["dist_km"]) * px
            ab, hpx, gp = clouds.clump_box(groups, angle_fn, d_m, wpx, px * v_ex)
            if ab > amax or Y(ab) - hpx > bottom:
                continue
            a_sum = angle_fn(x["elev_m"], d_m)
            yc = Y(a_sum) if gp["bottom"] < x["elev_m"] else Y(ab) - hpx / 2.0
            tt = html.escape(f"{x['label']} にかかる雲 {'・'.join(str(h) for h in gp['hpas'])}hPa "
                             f"約{gp['bottom']:.0f}〜{gp['top']:.0f}m 雲量 {weather.r0(gp['c'])}%")
            specs.append({"thin": False, "bi": weather.depth_index(d_m), "x": xx, "y": yc, "c": gp["c"], "w": wpx, "h": hpx,
                          "seed": clouds.seed_of("clump", seed_key, x["key"]), "key": x["key"], "tt": tt})
        shape_objs = []
        n_c = 0
        if opts["clumps"]:
            for thin in (False, True):
                by_bi = {}
                for q in specs:
                    if q["thin"] == thin:
                        by_bi.setdefault(q["bi"], []).append(q)
                for bi, lst in by_bi.items():
                    for grp in clouds.group_neighbors(lst, px):
                        if len(grp) == 1:
                            q = grp[0]
                            s_, info = clouds.clump_svg39(q["x"], q["y"], q["c"], q["w"], q["h"], q["seed"], q["tt"], f"{st}c{n_c}",
                                                   thin=thin)
                        else:
                            s_, info = clouds.merged_clump_svg(grp, f"{st}c{n_c}", thin=thin)
                            st_stats["clumps_merged_groups"] += 1
                            st_stats["clumps_in_merged"] += len(grp)
                        depth_items.setdefault((bi, st), []).append(s_)
                        shape_objs.append(dict(info, x=sum(q["x"] for q in grp) / len(grp), bi=(bi, thin)))
                        n_c += 1
        else:
            for q in specs:
                s_ = clouds.clump_svg37(q["x"], q["y"], q["c"], q["w"], q["h"], q["seed"], q["tt"], f"{st}{'t' if q['thin'] else 'c'}{n_c}",
                                    thin=q["thin"])
                depth_items.setdefault((q["bi"], st), []).append(s_)
                shape = clouds.clump_shape(q["seed"], q["w"], q["h"])
                shape_objs.append({"x": q["x"], "w": max(cx + r_ for cx, cy, r_ in shape) - min(cx - r_ for cx, cy, r_ in shape),
                                   "h": max(cy + r_ for cx, cy, r_ in shape) - min(cy - r_ for cx, cy, r_ in shape),
                                   "n": len(shape), "bi": (q["bi"], q["thin"])})
                n_c += 1
        st_stats["clumps"] = sum(1 for q in specs if not q["thin"])
        st_stats["thin_clumps"] = sum(1 for q in specs if q["thin"])
        st_stats["clump_objects"] = len(shape_objs)
        st_stats["same_shape_runs"] = clouds.same_shape_runs(shape_objs, px)
        rl = []
        for pts, L in ridges:
            P = [tuple(map(float, q.split(","))) for q in pts.split()]
            fns = [fn for bi, fn in alpha_by_layer if bi < L]
            vis = clouds.visible_ridge_runs(P, fns)
            st_stats["ridge_pts"] += len(P)
            st_stats["ridge_hidden_pts"] += len(P) - sum(len(v) for v in vis)
            rl += [f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in v)}" class="ridge R{L}"/>' for v in vis]
        ridge_parts.append(wx_group(st, default, "".join(rl)))
        rb = rows_by[st]
        txt, n_f = [], 0
        for n in names:
            row = rb.get(n["row_id"])
            op = 1.0 if row is None else row["name_opacity"]
            n_f += op < 1.0
            txt.append(_name_text(n, n["var"][st], op))
        name_parts.append(wx_group(st, default, "".join(txt), " wxn"))
        st_stats["names_faded"] = n_f
        banners[st] = (sc.get("viewer_state") or {}).get("text") or ""
        stats[st] = st_stats
    svg = svg.replace("</defs>", clip + "</defs>" + "".join(sky_parts), 1)
    items = []
    for (bi, st), lst in depth_items.items():
        items.append((bi, all_states.index(st), wx_group(st, default, "".join(lst))))
    items.sort(key=lambda t: t[1])
    svg = insert_by_depth(svg, [(bi, s) for bi, _, s in items])
    svg = svg.replace(RIDGE_TOKEN, "".join(ridge_parts), 1) if RIDGE_TOKEN in svg else svg
    ci = svg.rfind('<text x="12" y="')
    svg = svg[:ci] + "".join(name_parts) + svg[ci:]
    stats["_question_marks"] = len(re.findall(r"<text[^>]*>[^<]*[?？]", svg))
    stats["_names"] = len(names)
    return svg, ch, stats, banners


# ---- 元: experiments/panorama/stage39_page.py ----

CREDIT_TILES = "地理院タイル(標高タイル)を加工して作成"

CREDIT_1003 = "「日本の主な山岳標高」(国土地理院)を加工して作成"

CREDIT_1003_PARTIAL = "名前は日本百名山とヤマビヨリの 76 座です"

CREDIT_WEATHER = "気象データ: Open-Meteo(気象庁 MSM ほか)"

CREDIT_OSM = "© OpenStreetMap contributors"

CREDIT_NOTE = "予報にもとづく目安です(答え合わせは少数)。縦は横の 1.5 倍"

CREDIT_RE = re.compile(r'<text x="12" y="[\d.]+" class="credit">.*?</text>', re.S)

PEAK_RE = re.compile(r'<g><title>([^<]*)</title><line ([^>]*?)class="lead ([^"]*)"/><circle ([^>]*?)class="dot ([^"]*)"/></g>')

WXN_RE = re.compile(r'(<g class="wx wxn" data-st="[^"]+"[^>]*>)(.*?)(</g>)', re.S)

NAME_T_RE = re.compile(r'<text ([^>]*)>(.*?)</text>', re.S)

TAG_RE = re.compile(r"<[^>]+>")

ATTR_RE = r'{}="([\d.\-]+)"'

TITLE_RE = re.compile(r" (\d+)m\)方位 [\d.]+° 距離 ([\d.]+)km")

def credit_lines(osm_used: bool, gsi_partial: bool = False):
    a = [CREDIT_TILES, CREDIT_1003] + ([CREDIT_1003_PARTIAL] if gsi_partial else []) + [CREDIT_WEATHER]
    if osm_used:
        a.append(CREDIT_OSM)
    return [" / ".join(a), CREDIT_NOTE]

def _attr(s, name):
    m = re.search(ATTR_RE.format(name), s)
    return float(m.group(1)) if m else None

def annotate_svg(svg: str):
    """図の SVG に、名前と点の対応(data-i)を付け、SVG の中の出典の行を外す。
    返り値: (svg, items)。items は stage39_layout.layout_names に渡す形と、点の位置。"""
    svg = CREDIT_RE.sub("", svg)
    dots = []

    def pk(mo):
        title, line_attrs, lcls, circ_attrs, ccls = mo.groups()
        i = len(dots)
        tm = TITLE_RE.search(html.unescape(title))
        dots.append({"x": _attr(circ_attrs, "cx"), "y": _attr(circ_attrs, "cy"), "cls": ccls,
                     "label": html.unescape(title).split("(材料")[0],
                     "ele_m": float(tm.group(1)) if tm else 0.0, "dist_km": float(tm.group(2)) if tm else 0.0})
        return (f'<g class="pk" data-i="{i}"><title>{title}</title><line {line_attrs}class="lead {lcls}"/>'
                f'<circle {circ_attrs}class="dot {ccls}"/></g>')
    svg = PEAK_RE.sub(pk, svg)
    texts = [[] for _ in dots]

    def grp(mo):
        head, body, tail = mo.groups()
        n = [0]

        def tx(m2):
            i = n[0]
            n[0] += 1
            if i < len(texts):
                texts[i].append(html.unescape(TAG_RE.sub("", m2.group(2))))
            return f'<text data-i="{i}" {m2.group(1)}>{m2.group(2)}</text>'
        return head + NAME_T_RE.sub(tx, body) + tail
    svg = WXN_RE.sub(grp, svg)
    items = []
    for i, d in enumerate(dots):
        tier = 0 if "大" in d["cls"] else 1 if "中" in d["cls"] else 2
        items.append({"x": d["x"], "y": d["y"], "tier": tier, "texts": texts[i] or [""], "label": d["label"],
                      "ele_m": d["ele_m"], "dist_km": d["dist_km"]})
    return svg, items

def layouts_for(items):
    """{段の上限: {縮尺: {幅の区切り: [[段, 置き方の番号], ...]}}}。段の上限は縦向き(MAX_ROWS)と横向き(LAND_MAX_ROWS)。"""
    out = {}
    for mx in (MAX_ROWS, LAND_MAX_ROWS):
        out[str(mx)] = {}
        for deg in ZOOMS:
            out[str(mx)][str(deg)] = {}
            for wb in WIDTH_BUCKETS:
                L = layout_names(items, deg, wb, max_rows=mx)
                out[str(mx)][str(deg)][str(wb)] = [[r, ANCHORS.index(a)] for r, a in zip(L["rows"], L["anc"])]
    return out

def open_dirs(peaks, viewer_is_fuji, wide_px=1280):
    res = {}
    for key, win in (("90", 90.0), ("45", 45.0), ("20", 20.0),
                     ("wide", (wide_px - 32) / 24.0)):
        az, n, has_f, rule = open_direction(peaks, win, viewer_is_fuji)
        res[key] = {"az": round(az, 1), "n": n, "fuji": has_f, "rule": rule}
    return res

CSS39 = """
main.p39{max-width:none;padding:6px 8px 16px}
.ctl{display:block}
.ctl .stbar,.ctl .navbar{display:flex;flex-wrap:nowrap;gap:4px;margin:0 0 4px}
.ctl button{flex:1 1 0;min-width:0;min-height:38px;padding:2px 4px;border:1px solid var(--line);border-radius:8px;
 background:var(--card);color:var(--fg);font-size:14px;line-height:1.1;cursor:pointer;white-space:nowrap;overflow:hidden}
.ctl button small{display:block;font-size:10px;color:var(--mut)}
.ctl button[aria-pressed=true]{background:var(--sub);border-color:var(--sub);color:#fff}
.ctl button[aria-pressed=true] small{color:#fff}
.ctl button:disabled{opacity:.45;cursor:not-allowed}
.ctl .sep{flex:0 0 4px}
#frame{position:relative;background:var(--card);border:1px solid var(--line);border-radius:8px;margin:0 0 6px;overflow:hidden}
#vp{overflow:hidden;touch-action:pan-y;user-select:none;-webkit-user-select:none}
#vp svg{display:block}
body.narrow #vp svg{width:100%;height:auto}
body.narrow #vp text.tier{display:none}
body.wide #vp{overflow-x:auto;touch-action:auto}
body.wide #vp svg{max-width:none}
body.wide .zoomb{display:none}
body.wide .ctl{display:flex;flex-wrap:wrap;gap:10px}body.wide .ctl button{flex:0 0 auto;min-width:60px;padding:2px 12px;min-height:40px}
.credit39{font-size:10.5px;line-height:1.35;color:var(--mut);padding:1px 6px 3px;border-top:1px solid var(--line);background:var(--card)}
.credit39 div{overflow-wrap:anywhere}
.vstate{margin:2px 0;font-size:13px;color:var(--fg);overflow-wrap:anywhere}
.omitnote{margin:0 0 4px;font-size:12px;color:var(--fg)}
.opendir{font-size:12px;color:var(--mut);margin:0 0 4px}
h1.t39{font-size:17px;margin:6px 0 2px}
details.howto{margin:8px 0}details.howto summary{cursor:pointer;font-size:14px;color:var(--sub);min-height:40px;display:flex;align-items:center}
@media (orientation:landscape) and (max-height:500px){
 body.narrow .ctl{display:flex;gap:6px;align-items:stretch}
 body.narrow .ctl .stbar,body.narrow .ctl .navbar{margin:0 0 3px}
 body.narrow .ctl .stbar{flex:5 1 0}body.narrow .ctl .navbar{flex:8 1 0}
 body.narrow .ctl button{min-height:34px;font-size:13px}
 body.narrow .ctl button small{display:none}
 main.p39{padding:4px 8px 12px}
}
"""

JS39 = r"""
(function(){
var D=JSON.parse(document.getElementById('p39data').textContent);
var svg=document.querySelector('#vp svg'),vp=document.getElementById('vp'),body=document.body;
var PX=D.px,X0=D.x0,FULL=D.full,SKY=D.sky,BASE=D.base,H=D.H;
var st=D.def,deg=null,az=null,mode=null;
var pk=[].slice.call(svg.querySelectorAll('g.pk')).map(function(g){var c=g.querySelector('circle'),l=g.querySelector('line');
 return {g:g,c:c,l:l,i:+g.getAttribute('data-i'),o:{r:c.getAttribute('r'),x1:l.getAttribute('x1'),x2:l.getAttribute('x2'),y1:l.getAttribute('y1'),y2:l.getAttribute('y2'),sw:l.style.strokeWidth}}});
var txt=[].slice.call(svg.querySelectorAll('g.wxn text[data-i]')).map(function(t){return {t:t,i:+t.getAttribute('data-i'),
 o:{x:t.getAttribute('x'),y:t.getAttribute('y'),a:t.getAttribute('text-anchor'),fs:t.style.fontSize,d:t.style.display}}});
var ticks=[].slice.call(svg.querySelectorAll('text.tickt')).map(function(t){return {t:t,o:{y:t.getAttribute('y'),fs:t.style.fontSize}}});
var near=[].slice.call(svg.querySelectorAll('text.nearlab')).map(function(t){return {t:t,o:{fs:t.style.fontSize,sw:t.style.strokeWidth}}});
var ov={w:svg.getAttribute('width'),h:svg.getAttribute('height'),vb:svg.getAttribute('viewBox')};
var FS=[13,11.5,10.5],ROWH=16,ANC=['middle','start','end'],TOPPAD=20;   // いちばん上の段の名前の上の余白(◇の付く名前が枠の外に出ないよう 14→20)
function bucket(W){var b=D.buckets[0];D.buckets.forEach(function(x){if(x<=W)b=x});return b}
function vw(){return vp.clientWidth||document.documentElement.clientWidth||360}
function restore(){svg.setAttribute('width',ov.w);svg.setAttribute('height',ov.h);svg.setAttribute('viewBox',ov.vb);
 pk.forEach(function(p){p.c.setAttribute('r',p.o.r);['x1','x2','y1','y2'].forEach(function(a){p.l.setAttribute(a,p.o[a])});p.l.style.strokeWidth=p.o.sw;p.l.style.display=''});
 txt.forEach(function(x){x.t.setAttribute('x',x.o.x);x.t.setAttribute('y',x.o.y);x.t.setAttribute('text-anchor',x.o.a);x.t.style.fontSize=x.o.fs;x.t.style.display=x.o.d});
 ticks.forEach(function(x){x.t.setAttribute('y',x.o.y);x.t.style.fontSize=x.o.fs;x.t.style.display=''});
 near.forEach(function(x){x.t.style.fontSize=x.o.fs;x.t.style.strokeWidth=x.o.sw})}
function layout(){
 var W=window.innerWidth||document.documentElement.clientWidth,m=W>=D.wideMin?'wide':'narrow';
 if(m!==mode){mode=m;body.classList.toggle('wide',m==='wide');body.classList.toggle('narrow',m==='narrow');
  if(m==='wide'){restore();wideNames();omit(null);if(az===null)az=D.open.wide.az;go();return}
  svg.removeAttribute('width');svg.removeAttribute('height');svg.setAttribute('preserveAspectRatio','xMinYMin meet');
  if(deg===null)deg=W>=D.openWideZoom?90:45;}
 if(mode==='wide'){go();return}
 if(az===null)az=D.open[String(deg)].az;
 var w=vw(),span=deg*PX,k=w/span,ldeg=deg,L,nr,bw=W;
 var land=window.innerWidth>window.innerHeight,free=land&&deg===D.freeZoom,MX=String(land&&!free?D.landRows:D.maxRows);
 function pick(){L=D.layouts[MX][String(ldeg)][String(bucket(bw))];nr=0;L.forEach(function(q){if(q[0]+1>nr)nr=q[0]+1})}
 pick();
 var ctlE=document.querySelector('.ctl'),ctlB=ctlE.offsetTop+ctlE.offsetHeight,crH=document.querySelector('.credit39').getBoundingClientRect().height;
 var kmin=Math.max(D.fitMinRatio*w/(deg*PX),D.fitMinRidge/(BASE-SKY));   // 縮小の下限。収まらない分はページを縦に送る
 var tgt=window.innerHeight-crH-28;if(land)tgt=Math.min(tgt,D.landH);
 for(var it=0;it<(free?0:2);it++){var lab=7+(Math.max(nr,1)-1)*ROWH+TOPPAD,kh=Math.max((tgt-ctlB-2-lab)/(BASE-SKY),kmin);
  if(k>kh){k=kh;span=w/k;var de=span/PX;ldeg=90;[20,45,90].forEach(function(z){if(z>=de&&z<ldeg)ldeg=z});if(de<=20)ldeg=20;bw=de>ldeg?W*ldeg/de:W;pick()}}   // 「広く」より広くなったら、その分せまい画面の置き方を使う
 D.kcap=(span!==deg*PX);omit(L);
 var rowH=ROWH/k;
 pk.forEach(function(p){var q=L[p.i],r=q[0],x=+D.items[p.i].x,y=+D.items[p.i].y,ty=SKY-7/k-r*rowH,a=ANC[q[1]];
  p.c.setAttribute('r',r<0?Math.max(1.6,1.6/k):Math.max(2.8,2.6/k));
  if(r<0){p.l.style.display='none'}else{p.l.style.display='';p.l.setAttribute('x1',x);p.l.setAttribute('x2',x);p.l.setAttribute('y1',ty+3/k);p.l.setAttribute('y2',y-3/k);p.l.style.strokeWidth=((D.items[p.i].t==0)?1:0.8)/k}
  p.ty=ty;p.a=a;p.r=r;p.x=x});
 txt.forEach(function(x){var p=pk[x.i];if(!p)return;var it=D.items[x.i];
  if(p.r<0){x.t.style.display='none';return}x.t.style.display='';
  x.t.setAttribute('x',p.a==='start'?p.x-5/k:p.a==='end'?p.x+5/k:p.x);x.t.setAttribute('y',p.ty);x.t.setAttribute('text-anchor',p.a);
  x.t.style.fontSize=(FS[it.t]/k)+'px'});
 var step=span/PX>=80?30:10;
 ticks.forEach(function(x){var a=Math.round((+x.t.getAttribute('x')-X0)/PX);x.t.style.display=(a%step===0)?'':'none';x.t.style.fontSize=(10/k)+'px';x.t.setAttribute('y',BASE+13/k)});
 near.forEach(function(x){x.t.style.fontSize=(10/k)+'px';x.t.style.strokeWidth=(3/k)+'px'});
 svg._top=SKY-7/k-(Math.max(nr,1)-1)*rowH-TOPPAD/k;svg._h=BASE+18/k-svg._top;svg._span=span;svg._ldeg=ldeg;
 clampAz();pan();
 if(vw()!==w&&!layout.again){layout.again=1;layout();layout.again=0}}
function wideNames(){var L=D.wide,nr=0,RH=D.wideRowH,FW=D.wideFs,y0=SKY-14;L.forEach(function(q){if(q[0]+1>nr)nr=q[0]+1});
 pk.forEach(function(p){var q=L[p.i],r=q[0],x=+D.items[p.i].x,y=+D.items[p.i].y,ty=y0-r*RH;p.ty=ty;p.a=ANC[q[1]];p.r=r;p.x=x;
  if(r<0){p.l.style.display='none'}else{p.l.style.display='';p.l.setAttribute('x1',x);p.l.setAttribute('x2',x);p.l.setAttribute('y1',ty+3);p.l.setAttribute('y2',y-3)}});
 txt.forEach(function(x){var p=pk[x.i];if(!p)return;var it=D.items[x.i];if(p.r<0){x.t.style.display='none';return}x.t.style.display='';
  x.t.setAttribute('x',p.a==='start'?p.x-5:p.a==='end'?p.x+5:p.x);x.t.setAttribute('y',p.ty);x.t.setAttribute('text-anchor',p.a);x.t.style.fontSize=FW[it.t]+'px'});
 var top=y0-(Math.max(nr,1)-1)*RH-TOPPAD*FW[0]/FS[0],hh=+ov.h-top;   // 図の上の端を、いちばん上の段の名前に合わせる
 svg.setAttribute('height',hh.toFixed(0));svg.setAttribute('viewBox','0 '+top.toFixed(1)+' '+ov.w+' '+hh.toFixed(1))}
function omit(L){var om=document.getElementById('omitnote');if(!om)return;var o=false;
 if(L)D.items.forEach(function(it,i){if(it.t<2&&L[i][0]<0)o=true});om.style.display=o?'':'none'}
function clampAz(){if(mode!=='narrow')return;var h=(svg._span?svg._span/PX:deg)/2,lo=h-X0/PX,hi=(FULL-X0)/PX-h;az=Math.max(lo,Math.min(hi,az))}
function pan(){var x=X0+az*PX-svg._span/2;svg.setAttribute('viewBox',x.toFixed(1)+' '+svg._top.toFixed(1)+' '+svg._span.toFixed(1)+' '+svg._h.toFixed(1));D.cur={az:az,deg:deg,mode:mode,span_deg:Math.round(svg._span/PX*10)/10,layout_deg:svg._ldeg}}
function go(){if(mode==='wide'){vp.scrollLeft=Math.max(0,X0+az*PX-vp.clientWidth/2);D.cur={az:az,deg:null,mode:mode}}else{clampAz();pan()}}
function show(s){var b=document.querySelector('.stbar button[data-st="'+s+'"]');if(!b||b.disabled)return;st=s;
 document.querySelectorAll('.stbar button[data-st]').forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')});
 document.querySelectorAll('.wx[data-st],.wxt[data-st]').forEach(function(e){e.style.display=(e.getAttribute('data-st')===s)?(e instanceof SVGElement?'inline':''):'none'})}
function zoom(d){deg=d;document.querySelectorAll('button[data-deg]').forEach(function(b){b.setAttribute('aria-pressed',+b.getAttribute('data-deg')===d?'true':'false')});layout()}
function curAz(){if(mode==='wide')return (vp.scrollLeft+vp.clientWidth/2-X0)/PX;return az}
document.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-st'))show(b.getAttribute('data-st'));
 else if(b.hasAttribute('data-az')){az=+b.getAttribute('data-az');go()}
 else if(b.hasAttribute('data-mv')){var half=mode==='wide'?vp.clientWidth/2/PX:svg._span/PX/2;az=curAz()+(+b.getAttribute('data-mv'))*half;go()}
 else if(b.hasAttribute('data-deg')){az=curAz();zoom(+b.getAttribute('data-deg'))}});
var drag=null;
vp.addEventListener('pointerdown',function(e){if(mode!=='narrow')return;drag={x:e.clientX,az:az};try{vp.setPointerCapture(e.pointerId)}catch(_){}});
vp.addEventListener('pointermove',function(e){if(!drag)return;az=drag.az-(e.clientX-drag.x)*(svg._span/PX)/vw();clampAz();pan()});
['pointerup','pointercancel'].forEach(function(n){vp.addEventListener(n,function(){drag=null})});
window.addEventListener('resize',function(){var a=curAz();layout();az=a;go()});
var h=location.hash,m=h.match(/st=([a-z0-9]+)/);if(m&&document.querySelector('.stbar button[data-st="'+m[1]+'"]'))st=m[1];
m=h.match(/deg=([0-9]+)/);if(m)deg=+m[1];
layout();m=h.match(/az=([0-9.]+)/);if(m){az=+m[1];go()}
show(st);
document.querySelectorAll('button[data-deg]').forEach(function(b){b.setAttribute('aria-pressed',+b.getAttribute('data-deg')===deg?'true':'false')});
window.__p39=D;
})();
"""

def nav_bar():
    dirs = "".join(f'<button type="button" data-az="{az:g}">{n}</button>'
                   for n, az in (("北", 360.0), ("東", 90.0), ("南", 180.0), ("西", 270.0)))
    zooms = "".join(f'<button type="button" class="zoomb" data-deg="{d}" aria-pressed="false">{ZOOM_LABELS[d]}</button>'
                    for d in ZOOMS)
    return (f'<div class="navbar" role="group" aria-label="方角と縮尺">'
            f'<button type="button" data-mv="-1" aria-label="左へ">◀</button>{dirs}'
            f'<button type="button" data-mv="1" aria-label="右へ">▶</button><span class="sep zoomb"></span>{zooms}</div>')

def time_bar(states, default, notes):
    out = []
    for st, label, _ in states:
        note = notes.get(st, "")
        dis = " disabled" if note == "予報なし" else ""
        sm = f"<small>{html.escape(note)}</small>" if note else ""
        lab = label.replace(" 時", "時")
        out.append(f'<button type="button" data-st="{st}" aria-pressed="{"true" if st == default else "false"}"{dis}'
                   f' title="{html.escape(label + (" " + note if note else ""))}">{html.escape(lab)}{sm}</button>')
    return f'<div class="stbar" role="group" aria-label="表示の切り替え">{"".join(out)}</div>'

def write_page(path, *, svg, ch, peaks, states, default, notes, banners, title_base, title_suffix, intro, legend_html,
                 osm_used, viewer_is_fuji=False, gsi_partial=False, terrain_key=None, extra_how=""):
    """返り値: ページの情報(大きさ・開いたときの方角・名前の置き方の数)。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    svg2, items = annotate_svg(svg)
    W = ch["width"]
    H = ch["height"]
    top_sky = 24 * ch["rows"] + 14 + 8
    bottom = H - 44
    layouts = layouts_for(items)
    WL = layout_wide(items)
    opens = open_dirs(peaks, viewer_is_fuji)
    data = {"px": 24.0, "x0": 20.0, "full": W, "sky": top_sky, "base": bottom, "H": H, "def": default,
            "items": [{"x": round(it["x"], 1), "y": round(it["y"], 1), "t": it["tier"]} for it in items],
            "layouts": layouts, "buckets": list(WIDTH_BUCKETS), "open": opens, "wideMin": WIDE_MIN_PX,
            "openWideZoom": OPEN_WIDE_ZOOM_MIN_PX, "landH": LAND_RIDGE_MAX_PX, "maxRows": MAX_ROWS,
            "landRows": LAND_MAX_ROWS, "freeZoom": FREE_HEIGHT_ZOOM, "fitMinRatio": FIT_MIN_RATIO,
            "fitMinRidge": FIT_MIN_RIDGE_PX, "wideFs": list(WIDE_FS), "wideRowH": WIDE_ROW_H,
            "wide": [[r, ANCHORS.index(a)] for r, a in zip(WL["rows"], WL["anc"])]}
    lines = credit_lines(osm_used, gsi_partial)
    credit = "".join(f"<div>{html.escape(x)}</div>" for x in lines)
    vs = "".join(
        f'<p class="vstate wxt" data-st="{st}"{"" if st == default else " style=" + chr(34) + "display:none" + chr(34)}>'
        f'<b>視点の状態:</b> {html.escape(banners.get(st) or "とくになし")}</p>'
        for st, _, h in states if h and st in banners)
    if any(h is None for _, _, h in states):
        tst = [st for st, _, h in states if h is None][0]
        vs += (f'<p class="vstate wxt" data-st="{tst}"{"" if default == tst else " style=" + chr(34) + "display:none" + chr(34)}>'
               f'地形だけ(天気を重ねない図)</p>')
    spans = "".join(f'<span class="wxt" data-st="{st}"{"" if st == default else " style=" + chr(34) + "display:none" + chr(34)}>'
                    f'{html.escape(title_suffix.get(st, ""))}</span>' for st, _, _ in states)
    o45, o90 = opens["45"], opens["90"]
    od = (f"開いたときの方角: 標準 {dir_name(o45['az'])}(方位 {o45['az']:.0f}°)・広く {dir_name(o90['az'])}"
          f"(方位 {o90['az']:.0f}°)。{o45['rule']}。")
    how = (f'<details class="howto"><summary>図の見方(凡例・注記・出典)</summary>'
           f'<p>操作: 時刻のボタンで雲が替わります(山と名前は動きません)。「◀」「▶」は画面の半分ずつ、図を指で横になぞっても動きます。'
           f'「広く」は画面の幅に 90°、「標準」は 45°、「拡大」は 20°(幅 {WIDE_MIN_PX}px 以上の画面では、全周を横に送る形)。'
           f'赤 = 日本百名山、青 = ヤマビヨリの 76 座。「広く」「標準」では、重ならずに置ける名前だけを出します(百名山 → 76 座 → そのほかの順。近い山を先に)。'
           f'置けない山は点だけで、「拡大」で見えている百名山と 76 座は全部出ます。「広く」ではそのほかの山は点だけ。'
           f'幅 {WIDE_MIN_PX}px 以上の画面も「標準」と同じ決まりで、置けない山は点だけ(点にマウスを当てると名前が出ます)。'
           f'横向きの画面の「拡大」と、画面の高さがとても小さいときは、図が画面の高さを超えるのでページを縦に送って見ます。</p>'
           f'{legend_html}<p>山頂の雲量は、山頂をはさむ 2 つの高さの予報のうち大きいほうです。</p>{extra_how}'
           f'<p style="font-size:12px">出典: 国土地理院の標高タイル(地理院タイル)を加工して作成(視点から見上げた角度に変換)。'
           f'山名と標高は「日本の主な山岳標高」(国土地理院)をもとに作成(百名山・76 座との対応づけと呼び名を加工)。'
           f'気象データは Open-Meteo(気象庁 MSM、無い時間は ECMWF IFS 0.25)。'
           f'{"展望地は © OpenStreetMap contributors(ODbL)。" if osm_used else ""}'
           f'試作。縦は横の 1.5 倍に強調。計算で手前の山に隠れる山は名前を出していない。木・建物は計算に入らない。</p></details>')
    head = (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title_base)}</title>'
            f'<style>{style(len(terrain.DEFAULT_BANDS))}{EXTRA_CSS}{CSS39}</style></head>')
    body = (f'<body class="narrow"><main class="p39">'
            f'<div class="ctl">{time_bar(states, default, notes)}{nav_bar()}</div>'
            f'<div id="frame"><div id="vp">{svg2}</div><div class="credit39">{credit}</div></div>'
            f'<p class="omitnote" id="omitnote" style="display:none">{OMIT_NOTE}</p>'
            f'{vs}<h1 class="t39">{html.escape(title_base)}{spans}</h1><p class="opendir">{html.escape(od)}</p>{intro}{how}'
            f'<script type="application/json" id="p39data">{json.dumps(data, ensure_ascii=False)}</script>'
            f'<script>{JS39}</script></main></body></html>')
    s = head + body
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(s)
    ref = {}
    mine = {}
    for deg in ZOOMS:
        for wb in (390, 844):
            L = layout_names(items, deg, wb)
            R = layout_reference(items, deg, wb)
            mine[f"{deg}_{wb}"] = dict(count_conflicts(items, L), missing=missing_required(items, L),
                                       rows=L["n_rows"])
            ref[f"{deg}_{wb}"] = dict(count_conflicts(items, R, ascii_ratio=0.58), missing=missing_required(items, R),
                                      rows=R["n_rows"])
    return {"file": os.path.basename(path), "size": os.path.getsize(path), "open": opens, "layout": mine,
            "layout_reference": ref, "n_items": len(items)}

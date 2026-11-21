"""展望図の SVG の数の書き方(短く書くための決まり)。図の形・色は変えない。

- num: 数を短く書く(20.0 → 20、0.5 → .5、-0.5 → -.5)。桁数(nd)で丸めた値は変えない。
- circle: 雲のかたまりの丸。中心は 1 画素(整数)に丸める(案 B。画面の写真で差が見えないことを確かめた要素だけ)。
"""
from __future__ import annotations


def num(v: float, nd: int = 1) -> str:
    """v を小数 nd 桁に丸め、末尾の 0・小数点・先頭の 0 を省いた文字。"""
    s = f"{v:.{nd}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", "", "-"):
        return "0"
    if s.startswith("0."):
        return s[1:]
    if s.startswith("-0."):
        return "-" + s[2:]
    return s


def circle(x: float, y: float, r: float) -> str:
    """雲のかたまりの丸(中心は整数、半径は小数 1 桁)。"""
    return f'<circle cx="{num(x, 0)}" cy="{num(y, 0)}" r="{num(r)}"/>'


def _collinear(a, b, c) -> bool:
    """整数の点 a, b, c が一直線で、b が a と c のあいだにある(b を落としても形が変わらない)。"""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    return ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax) == 0
            and (bx - ax) * (cx - bx) >= 0 and (by - ay) * (cy - by) >= 0)


def join_nums(ns) -> str:
    """数の文字を並べる。次が - で始まるとき、前に . があって次が . で始まるときは区切りを省く。"""
    out = []
    for i, n in enumerate(ns):
        if i and not (n.startswith("-") or (n.startswith(".") and "." in ns[i - 1])):
            out.append(" ")
        out.append(n)
    return "".join(out)


SYNC_EVERY = 32       # 相対座標の足し算の誤差がたまらないよう、この点の数ごとに絶対座標(L)を挟む


def polygon_path(points, nd: int = 1, sync: int = SYNC_EVERY) -> str:
    """閉じた多角形を path の d(相対座標)にする。点は小数 nd 桁に丸めてから差を取り(ずれがたまらない)、
    丸めたあとで厳密に一直線に並ぶ点は落とす(形は同じ)。"""
    k = 10 ** nd
    # 丸めは、これまでの書き方(f"{v:.1f}")と同じ値になるよう、文字にしてから整数にする
    q = [(round(float(f"{x:.{nd}f}") * k), round(float(f"{y:.{nd}f}") * k)) for x, y in points]
    keep = [q[0]]
    for i in range(1, len(q) - 1):
        if q[i] == keep[-1] or _collinear(keep[-1], q[i], q[i + 1]):
            continue
        keep.append(q[i])
    if q[-1] != keep[-1]:
        keep.append(q[-1])
    d = [f"M{num(keep[0][0] / k, nd)} {num(keep[0][1] / k, nd)}"]
    rel = []
    for i, ((ax, ay), (bx, by)) in enumerate(zip(keep, keep[1:])):
        if sync and i % sync == sync - 1:
            if rel:
                d.append("l" + join_nums(rel))
                rel = []
            d.append(f"L{num(bx / k, nd)} {num(by / k, nd)}")
            continue
        rel += [num((bx - ax) / k, nd), num((by - ay) / k, nd)]
    if rel:
        d.append("l" + join_nums(rel))
    return "".join(d) + "Z"


def stops(pairs) -> str:
    """雲のぼかしのグラデーションの <stop>。pairs: [(offset, 不透明度)](offset の順)。
    値は今までと同じ桁(offset 小数 4 桁・不透明度 3 桁)で、書き方だけ短くする(案 E):
    先頭と末尾の 0 を省く、色(白)は CSS の stop{stop-color:#fff} に任せる、
    前後と同じ不透明度が続く途中の stop は省く(線形の補間なので、塗りは同じ)。"""
    q = [(num(o, 4), num(a, 3)) for o, a in pairs]
    out = []
    for i, (o, a) in enumerate(q):
        if 0 < i < len(q) - 1 and q[i - 1][1] == a == q[i + 1][1]:
            continue
        out.append(f'<stop offset="{o}" stop-opacity="{a}"/>')
    return "".join(out)

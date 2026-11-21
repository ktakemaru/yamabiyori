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

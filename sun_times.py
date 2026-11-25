"""日の出・日没の時刻(緯度経度と日付から計算。標準ライブラリだけ・通信しない)。

太陽の位置の近似式(NOAA の太陽計算と同じ形。均時差と赤緯)で、太陽の上端が水平線にかかる時刻(大気の屈折と
太陽の半径で高度 -0.833°)を求める。平らな地平線(標高 0m の水平線)での目安で、山や尾根で日が隠れる分・
山頂の高さで早く見える分は考えない。予報の計算・スコア・危険信号には使わない(表示の項目だけ)。
"""
import math
from datetime import date, timedelta

HORIZON_DEG = -0.833              # 太陽の上端が水平線にかかるときの、太陽の中心の高度
JST_OFFSET_MIN = 9 * 60
NOTE = "日の出・日没は、平らな地平線での目安です(計算値。山や尾根で日が隠れる分は含みません)"


def _sun(day: date, minutes_utc: float):
    """その時刻の (赤緯[rad], 均時差[分])。"""
    jd = day.toordinal() + 1721424.5 + minutes_utc / 1440.0
    t = (jd - 2451545.0) / 36525.0
    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0)
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = ((1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(m) + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    true_long = math.degrees(l0) + c
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    obl = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    obl = math.radians(obl + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obl) * math.sin(lam))
    y = math.tan(obl / 2.0) ** 2
    eqt = (y * math.sin(2 * l0) - 2 * e * math.sin(m) + 4 * e * y * math.sin(m) * math.cos(2 * l0)
           - 0.5 * y * y * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m))
    return decl, 4.0 * math.degrees(eqt)


def sun_times(lat: float, lon: float, day_str: str):
    """(日の出, 日没) を日本時間の "HH:MM" で返す。日が昇らない・沈まない緯度では (None, None)。"""
    day = date.fromisoformat(day_str)
    out = []
    for sign in (1, -1):                                  # 1: 日の出(南中より前)、-1: 日没
        local = (5 if sign == 1 else 19) * 60.0           # 日本時間の分での最初の見当。2回で収まる
        for _ in range(3):
            utc = local - JST_OFFSET_MIN
            d = day
            while utc < 0:
                d, utc = d - timedelta(days=1), utc + 1440
            decl, eqt = _sun(d, utc)
            cos_h = ((math.sin(math.radians(HORIZON_DEG)) - math.sin(math.radians(lat)) * math.sin(decl))
                     / (math.cos(math.radians(lat)) * math.cos(decl)))
            if not -1.0 <= cos_h <= 1.0:
                return None, None
            h_deg = math.degrees(math.acos(cos_h))
            local = 720.0 - 4.0 * (lon + sign * h_deg) - eqt + JST_OFFSET_MIN
        out.append(_hhmm(local))
    return out[0], out[1]


def _hhmm(minutes: float) -> str:
    n = int(math.floor(minutes + 0.5)) % 1440
    return f"{n // 60:02d}:{n % 60:02d}"

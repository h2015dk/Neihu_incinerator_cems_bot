"""單一小時高斯煙流篩選估算（非 AERMOD）。
- 穩定度：Turner 法簡化（無雲量資料，日間假設中等日照）
- 擴散係數：Briggs 都市型 σy、σz（McElroy-Pooler）
- 煙流上升：Briggs 最終上升高度
- 地面反射；不含混合層反射、地形、建物下洗
用途是「這一小時煙往哪裡吹、大概落在多遠」的即時示意，不能取代法定 AERMOD 模擬。"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

G = 9.81
P_EXP = {"A": 0.15, "B": 0.15, "C": 0.20, "D": 0.25, "E": 0.30, "F": 0.30}  # 都市風速剖面指數


def stability_class(u10: float, hour: int, night_cloudy: bool = False) -> str:
    day = 6 <= hour < 18
    if day:  # 中等日照
        if u10 < 2: return "B"
        if u10 < 5: return "C" if u10 >= 3 else "B"
        return "C" if u10 < 6 else "D"
    if night_cloudy:
        return "E" if u10 < 3 else "D"
    if u10 < 3: return "F"
    return "E" if u10 < 5 else "D"


def sigmas_urban(x: np.ndarray, cls: str):
    x = np.maximum(x, 1.0)
    if cls in ("A", "B"):
        return 0.32 * x * (1 + 0.0004 * x) ** -0.5, 0.24 * x * (1 + 0.001 * x) ** 0.5
    if cls == "C":
        return 0.22 * x * (1 + 0.0004 * x) ** -0.5, 0.20 * x
    if cls == "D":
        return 0.16 * x * (1 + 0.0004 * x) ** -0.5, 0.14 * x * (1 + 0.0003 * x) ** -0.5
    return 0.11 * x * (1 + 0.0004 * x) ** -0.5, 0.08 * x * (1 + 0.0015 * x) ** -0.5


def plume_rise(vs, d, ts_k, ta_k, u, cls):
    if ts_k <= ta_k:
        return 3 * d * vs / max(u, 0.5)  # 動量上升（冷煙）
    fb = G * vs * d * d / 4 * (ts_k - ta_k) / ts_k
    if cls in ("E", "F"):
        dthdz = 0.02 if cls == "E" else 0.035
        s = G / ta_k * dthdz
        return 2.6 * (fb / (u * s)) ** (1 / 3)
    return (21.425 * fb ** 0.75 if fb < 55 else 38.71 * fb ** 0.6) / u


@dataclass
class Source:
    q_g_s: float          # 排放率（任意質量單位/s；本程式用 ng I-TEQ/s）
    hs: float             # 煙囪高度 m
    d: float              # 出口內徑 m
    vs: float             # 出口流速 m/s
    ts_c: float           # 排氣溫度 °C


def run(src: Source, ws10: float, wd_from: float, hour: int, ta_c: float = 25.0,
        anemo_h: float = 10.0, half_km: float = 15.0, step: float = 100.0,
        night_cloudy: bool = False, min_ws: float = 0.5,
        receptors_xy: list[tuple[float, float]] | None = None) -> dict:
    """receptors_xy：敏感點相對煙囪的 (東, 北) 公尺座標；格點範圍會自動擴大到涵蓋它們。"""
    receptors_xy = receptors_xy or []
    need = max([math.hypot(x, y) for x, y in receptors_xy] + [0]) / 1000 + 2
    half_km = max(half_km, need)
    cls = stability_class(ws10, hour, night_cloudy)
    u10 = max(ws10, min_ws)
    uh = u10 * (src.hs / anemo_h) ** P_EXP[cls]
    dh = plume_rise(src.vs, src.d, src.ts_c + 273.15, ta_c + 273.15, uh, cls)
    he = src.hs + dh

    n = int(half_km * 1000 / step)
    ax = np.arange(-n, n + 1) * step
    X, Y = np.meshgrid(ax, ax)               # X 東、Y 北（m）
    to_rad = math.radians((wd_from + 180) % 360)  # 煙往哪吹（氣象風向是「從哪來」）
    ex, ey = math.sin(to_rad), math.cos(to_rad)

    def conc(x, y):
        xd = x * ex + y * ey                 # 下風距離
        yc = -x * ey + y * ex                # 側風距離
        sy, sz = sigmas_urban(xd, cls)
        c = (src.q_g_s / (2 * math.pi * uh * sy * sz) * np.exp(-yc ** 2 / (2 * sy ** 2))
             * 2 * np.exp(-he ** 2 / (2 * sz ** 2)))
        return np.where(xd > 0, c, 0.0), xd

    c, _ = conc(X, Y)
    rec = []
    for x, y in receptors_xy:
        cr, xdr = conc(np.array([x]), np.array([y]))
        rec.append({"conc": float(cr[0]), "downwind_m": float(xdr[0])})
    i = np.unravel_index(np.argmax(c), c.shape)
    return {"X": X, "Y": Y, "C": c, "cls": cls, "u_stack": uh, "dh": dh, "he": he,
            "max": float(c[i]), "max_xy": (float(X[i]), float(Y[i])),
            "max_dist_m": float(math.hypot(X[i], Y[i])), "calm": ws10 < min_ws,
            "receptors": rec}


def xy_to_lonlat(x, y, lat0, lon0):
    return lon0 + x / (111320 * math.cos(math.radians(lat0))), lat0 + y / 111320


def lonlat_to_xy(lon, lat, lat0, lon0):
    return (lon - lon0) * 111320 * math.cos(math.radians(lat0)), (lat - lat0) * 111320


def compass(deg):
    names = ["北", "北北東", "東北", "東北東", "東", "東南東", "東南", "南南東",
             "南", "南南西", "西南", "西南西", "西", "西北西", "西北", "北北西"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]

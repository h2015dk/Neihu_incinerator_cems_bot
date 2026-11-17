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


def solar_elevation(when, lat: float, lon: float, tz_hours: float = 8.0) -> float:
    """太陽仰角（度）。NOAA 近似公式，誤差約 1° 以內；when 為當地時間（臺灣 UTC+8）。"""
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqtime + 4 * lon - 60 * tz_hours          # 真太陽時（分）
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cosz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cosz))))


def stability_class(u10: float, hour: int, night_cloudy: bool = False,
                    sun_alt: float | None = None) -> str:
    """Turner 法簡化（無雲量資料）。
    sun_alt 有值時依太陽仰角判斷：≤0° 夜間；0～15° 日出日落過渡期，視為中性 D；
    15～35° 弱日照；35～60° 中等日照；>60° 強日照。sun_alt 為 None 時沿用舊的 6～18 點判斷。"""
    if sun_alt is None:
        sun_alt = 45.0 if 6 <= hour < 18 else -10.0
    if sun_alt <= 0:  # 夜間
        if night_cloudy:
            return "E" if u10 < 3 else "D"
        if u10 < 3: return "F"
        return "E" if u10 < 5 else "D"
    if sun_alt < 15:  # 日出後、日落前的過渡時段
        return "D"
    if sun_alt > 60:  # 強日照
        if u10 < 2: return "A"
        if u10 < 5: return "B"
        return "C"
    if sun_alt >= 35:  # 中等日照
        if u10 < 3: return "B"
        if u10 < 6: return "C"
        return "D"
    # 弱日照
    if u10 < 2: return "B"
    if u10 < 5: return "C"
    return "D"


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
        receptors_xy: list[tuple[float, float]] | None = None,
        when=None, lat: float | None = None, lon: float | None = None,
        terrain_fn=None, stack_base_m: float | None = None, f_horizontal=None) -> dict:
    """receptors_xy：敏感點相對煙囪的 (東, 北) 公尺座標；格點範圍會自動擴大到涵蓋它們。
    terrain_fn(x, y)：給相對煙囪的 (東, 北) 公尺座標，回傳地面標高（m）；None＝平坦地形。
    地形處理參考 AERMOD 的雙狀態概念（簡化版）：
      水平狀態：煙流維持原本的海拔高度，地面升高多少，煙離地面就少多少（最低 0）
      地形跟隨：煙流隨地形起伏，離地高度不變
      地面濃度＝f × 水平狀態＋(1－f) × 地形跟隨；f 依穩定度取值（越穩定，越傾向水平撞山）"""
    receptors_xy = receptors_xy or []
    need = max([math.hypot(x, y) for x, y in receptors_xy] + [0]) / 1000 + 2
    half_km = max(half_km, need)
    sun_alt = solar_elevation(when, lat, lon) if when is not None and lat is not None else None
    cls = stability_class(ws10, hour, night_cloudy, sun_alt)
    u10 = max(ws10, min_ws)
    uh = u10 * (src.hs / anemo_h) ** P_EXP[cls]
    dh = plume_rise(src.vs, src.d, src.ts_c + 273.15, ta_c + 273.15, uh, cls)
    he = src.hs + dh

    n = int(half_km * 1000 / step)
    ax = np.arange(-n, n + 1) * step
    X, Y = np.meshgrid(ax, ax)               # X 東、Y 北（m）
    to_rad = math.radians((wd_from + 180) % 360)  # 煙往哪吹（氣象風向是「從哪來」）
    ex, ey = math.sin(to_rad), math.cos(to_rad)

    fh = (f_horizontal or {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5, "E": 0.6, "F": 0.7})[cls]
    zs = None
    if terrain_fn is not None:
        zs = stack_base_m if stack_base_m is not None else float(terrain_fn(np.array([0.0]),
                                                                           np.array([0.0]))[0])

    def conc(x, y):
        xd = x * ex + y * ey                 # 下風距離
        yc = -x * ey + y * ex                # 側風距離
        sy, sz = sigmas_urban(xd, cls)
        base = src.q_g_s / (2 * math.pi * uh * sy * sz) * np.exp(-yc ** 2 / (2 * sy ** 2))

        def vert(h):  # 地面反射的垂直項
            return 2 * np.exp(-h ** 2 / (2 * sz ** 2))

        if terrain_fn is None:
            c = base * vert(he)
        else:
            dz = terrain_fn(x, y) - zs       # 受體地面相對煙囪基地的高差
            h_horiz = np.maximum(he - dz, 0.0)
            c = base * (fh * vert(h_horiz) + (1 - fh) * vert(he))
        return np.where(xd > 0, c, 0.0), xd

    c, _ = conc(X, Y)
    Z = terrain_fn(X, Y) if terrain_fn is not None else None
    rec = []
    for x, y in receptors_xy:
        cr, xdr = conc(np.array([x]), np.array([y]))
        rec.append({"conc": float(cr[0]), "downwind_m": float(xdr[0])})
    i = np.unravel_index(np.argmax(c), c.shape)
    return {"X": X, "Y": Y, "C": c, "cls": cls, "u_stack": uh, "dh": dh, "he": he,
            "max": float(c[i]), "max_xy": (float(X[i]), float(Y[i])),
            "max_dist_m": float(math.hypot(X[i], Y[i])), "calm": ws10 < min_ws,
            "receptors": rec, "sun_alt": sun_alt, "Z": Z, "stack_base_m": zs,
            "f_horizontal": fh if terrain_fn is not None else None}


def xy_to_lonlat(x, y, lat0, lon0):
    return lon0 + x / (111320 * math.cos(math.radians(lat0))), lat0 + y / 111320


def lonlat_to_xy(lon, lat, lat0, lon0):
    return (lon - lon0) * 111320 * math.cos(math.radians(lat0)), (lat - lat0) * 111320


def compass(deg):
    names = ["北", "北北東", "東北", "東北東", "東", "東南東", "東南", "南南東",
             "南", "南南西", "西南", "西南西", "西", "西北西", "西北", "北北西"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]

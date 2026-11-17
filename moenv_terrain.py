"""環境部空氣品質模式支援中心「AERMOD 地形資料」（2022.12.30 版）。

資料格式：每檔 10 km × 10 km，TWD97 二度分帶（TM2，EPSG:3826）座標，每 100 m 一點，
欄位為 x、y、地面高程（m）、山丘高度尺度（m），即 AERMAP 的輸出結果，可直接交給 AERMOD。
檔名結尾為該塊左下角座標（例：-3102770.TER ＝ x 310 km、y 2770 km），放在 terrain_moenv/。

AERMOD 座標使用「以煙囪為原點」的 TM2 公尺差值；TM2 與局部平面的差異（子午線收斂角約 0.3°、
尺度差約萬分之一）對 10 km 內的模擬可忽略。"""
from __future__ import annotations

import glob
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "terrain_moenv")
SOURCE = "環境部空品模式支援中心 AERMOD 地形資料（100 m）"
_TM2 = None


def to_tm2(lon, lat):
    global _TM2
    if _TM2 is None:
        from pyproj import Transformer
        _TM2 = Transformer.from_crs("EPSG:4326", "EPSG:3826", always_xy=True)
    return _TM2.transform(lon, lat)


def load(data_dir: str = DATA_DIR) -> dict:
    """讀入所有 .TER，組成規則網格。回傳 {"x": 一維, "y": 一維, "elev": 2D, "hill": 2D}（列＝y）。"""
    files = sorted(glob.glob(os.path.join(data_dir, "*.TER")) +
                   glob.glob(os.path.join(data_dir, "*.ter")))
    if not files:
        raise FileNotFoundError(f"{data_dir} 內沒有 .TER 地形檔")
    a = np.vstack([np.loadtxt(f, skiprows=1) for f in files])
    xs, ys = np.unique(a[:, 0]), np.unique(a[:, 1])
    ix = np.searchsorted(xs, a[:, 0])
    iy = np.searchsorted(ys, a[:, 1])
    elev = np.full((len(ys), len(xs)), np.nan)
    hill = np.full_like(elev, np.nan)
    elev[iy, ix] = a[:, 2]
    hill[iy, ix] = a[:, 3]
    return {"x": xs, "y": ys, "elev": elev, "hill": hill, "files": [os.path.basename(f) for f in files]}


def _nearest(g, x, y):
    i = int(np.clip(np.round((x - g["x"][0]) / (g["x"][1] - g["x"][0])), 0, len(g["x"]) - 1))
    j = int(np.clip(np.round((y - g["y"][0]) / (g["y"][1] - g["y"][0])), 0, len(g["y"]) - 1))
    return float(g["elev"][j, i]), float(g["hill"][j, i])


def prepare(fac: dict, receptors_lonlat: list, half_km: float = 6.0, step: int = 100) -> dict:
    """產生 AERMOD 的受體檔（RE）與污染源位置（SO）。
    回傳 {"rou","sou","base","grid": {x0,y0,nx,ny,dx,dy},"discrete": [(x,y)],
           "Z","ZX","ZY"（地圖等高線用，相對煙囪座標）,"note","key"}。"""
    g = load()
    sx, sy = to_tm2(fac["stack_lon"], fac["stack_lat"])
    dxg = g["x"][1] - g["x"][0]
    stride = max(1, int(round(step / dxg)))
    # 取煙囪周圍 ±half_km 的資料點（沿用資料本身的 100 m 格點，不內插）
    selx = np.where((g["x"] >= sx - half_km * 1000) & (g["x"] <= sx + half_km * 1000))[0][::stride]
    sely = np.where((g["y"] >= sy - half_km * 1000) & (g["y"] <= sy + half_km * 1000))[0][::stride]
    if len(selx) < 3 or len(sely) < 3:
        raise ValueError("地形檔沒有涵蓋煙囪周圍，請確認 .TER 檔案")
    E = g["elev"][np.ix_(sely, selx)]
    Hh = g["hill"][np.ix_(sely, selx)]
    if np.isnan(E).any():
        raise ValueError("煙囪周圍的地形資料有缺漏，請補上相鄰的 .TER 檔案")
    x0, y0 = float(g["x"][selx[0]] - sx), float(g["y"][sely[0]] - sy)
    d = float(dxg * stride)
    nx, ny = len(selx), len(sely)
    base, _ = _nearest(g, sx, sy)

    lines = ["   ELEVUNIT  METERS",
             "   GRIDCART  G1 STA",
             f"   GRIDCART  G1 XYINC {x0:.1f} {nx} {d:.1f} {y0:.1f} {ny} {d:.1f}"]
    for j in range(ny):
        for k in range(0, nx, 10):
            lines.append(f"   GRIDCART  G1 ELEV {j + 1} " + " ".join(f"{v:.1f}" for v in E[j, k:k + 10]))
        for k in range(0, nx, 10):
            lines.append(f"   GRIDCART  G1 HILL {j + 1} " + " ".join(f"{v:.1f}" for v in Hh[j, k:k + 10]))
    lines.append("   GRIDCART  G1 END")
    disc = []
    for lon, lat in receptors_lonlat:
        rx, ry = to_tm2(lon, lat)
        ze, zh = _nearest(g, rx, ry)
        disc.append((round(rx - sx, 1), round(ry - sy, 1)))
        lines.append(f"   DISCCART  {rx - sx:.1f} {ry - sy:.1f} {ze:.1f} {max(zh, ze):.1f}")
    XX, YY = np.meshgrid(g["x"][selx] - sx, g["y"][sely] - sy)
    return {"rou": "\n".join(lines) + "\n",
            "sou": f"   LOCATION  STK1 POINT 0.0 0.0 {base:.1f}\n",
            "base": base, "grid": {"x0": x0, "y0": y0, "nx": nx, "ny": ny, "dx": d, "dy": d},
            "discrete": disc, "Z": E, "ZX": XX, "ZY": YY,
            "note": f"已納入（{SOURCE}），煙囪基地標高 {base:.0f} m",
            "key": f"moenv-{int(sx)}-{int(sy)}-{nx}x{ny}"}

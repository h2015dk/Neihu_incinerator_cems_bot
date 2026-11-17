"""第 3 步：用 EPA AERMAP 計算受體與煙囪的地形高程、山丘高度尺度，供 AERMOD 使用。

- 地形來源：AWS Terrain Tiles（terrain.py），重新取樣成經緯度等間距網格，寫成 GeoTIFF
  （16 位元整數、NAD83 地理座標、垂直單位公尺），交給 AERMAP 的 NED（GeoTIFF）讀取器
- 座標：AERMOD 使用「相對煙囪」的公尺座標，以 ANCHORXY 對應到 UTM 第 51 區
- 地形與格點都不會變，結果存在 aermap_cache/，只需計算一次；設定改變才重算

用法：python aermap_run.py --prepare   （在 aermod-setup 執行，預先算好並存入快取）"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
AERMAP_EXE = os.environ.get("AERMAP_EXE", os.path.join(HERE, "bin", "aermap"))
CACHE = os.path.join(HERE, "aermap_cache")
SCRIPT_VERSION = "2026-11-17a"


# ---------------- UTM（WGS84／NAD83，兩者在臺灣差異約 1 m，可忽略） ----------------
def to_utm(lat: float, lon: float, zone: int | None = None):
    zone = zone or int((lon + 180) // 6) + 1
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    n = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r) ** 2
    c = ep2 * math.cos(lat_r) ** 2
    aa = math.cos(lat_r) * (lon_r - lon0)
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat_r
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat_r)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat_r)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * lat_r))
    x = k0 * n * (aa + (1 - t + c) * aa ** 3 / 6
                  + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120) + 500000.0
    y = k0 * (m + n * math.tan(lat_r) * (aa ** 2 / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                          + (61 - 58 * t + t * t + 600 * c - 330 * ep2)
                                          * aa ** 6 / 720))
    return x, y, zone


# ---------------- GeoTIFF（不需額外套件，直接寫出 TIFF 結構） ----------------
def write_geotiff(path: str, elev: np.ndarray, lon_w: float, lat_n: float, dlon: float, dlat: float):
    """elev：二維陣列（第 0 列為最北），單位公尺；lon_w／lat_n 為左上角像素「邊緣」的經緯度。"""
    data = np.round(elev).astype("<i2")
    h, w = data.shape
    raw = data.tobytes()
    geokeys = [1, 1, 0, 6,
               1024, 0, 1, 2,        # GTModelType = 地理座標
               1025, 0, 1, 1,        # GTRasterType = PixelIsArea
               2048, 0, 1, 4269,     # GeographicType = NAD83
               2054, 0, 1, 9102,     # 角度單位 = 度
               4096, 0, 1, 5103,     # 垂直座標系統（大地起伏高）
               4099, 0, 1, 9001]     # 垂直單位 = 公尺
    scale = [dlon, dlat, 0.0]
    tie = [0.0, 0.0, 0.0, lon_w, lat_n, 0.0]
    # (tag, type, values)；type：3=SHORT 4=LONG 12=DOUBLE
    entries = [(256, 4, [w]), (257, 4, [h]), (258, 3, [16]), (259, 3, [1]), (262, 3, [1]),
               (273, 4, [0]), (277, 3, [1]), (278, 4, [h]), (279, 4, [len(raw)]),
               (284, 3, [1]), (339, 3, [2]),
               (33550, 12, scale), (33922, 12, tie), (34735, 3, geokeys)]
    size = {3: 2, 4: 4, 12: 8}
    fmt = {3: "H", 4: "I", 12: "d"}
    n = len(entries)
    ifd_off = 8
    extra_off = ifd_off + 2 + n * 12 + 4
    extra = b""
    packed = []
    data_off_placeholder = None
    for tag, typ, vals in entries:
        nbytes = size[typ] * len(vals)
        payload = struct.pack("<" + fmt[typ] * len(vals), *vals)
        if nbytes <= 4:
            packed.append([tag, typ, len(vals), payload.ljust(4, b"\0"), None])
        else:
            off = extra_off + len(extra)
            extra += payload
            if len(extra) % 2:
                extra += b"\0"
            packed.append([tag, typ, len(vals), struct.pack("<I", off), None])
        if tag == 273:
            data_off_placeholder = len(packed) - 1
    data_off = extra_off + len(extra)
    packed[data_off_placeholder][3] = struct.pack("<I", data_off)
    out = b"II" + struct.pack("<HI", 42, ifd_off) + struct.pack("<H", n)
    for tag, typ, cnt, val, _ in packed:
        out += struct.pack("<HHI", tag, typ, cnt) + val
    out += struct.pack("<I", 0) + extra + raw
    with open(path, "wb") as f:
        f.write(out)


# ---------------- 執行 AERMAP ----------------
def cache_key(fac: dict, grid: dict, discrete: list, dem_step: float) -> str:
    s = json.dumps({"v": SCRIPT_VERSION, "lat": round(fac["stack_lat"], 6),
                    "lon": round(fac["stack_lon"], 6), "grid": grid,
                    "disc": [[round(x, 1), round(y, 1)] for x, y in discrete],
                    "dem": dem_step}, sort_keys=True)
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def load_cached(key: str) -> dict | None:
    d = os.path.join(CACHE, key)
    if all(os.path.exists(os.path.join(d, f)) for f in ("aermap.rou", "aermap.sou", "info.json")):
        with open(os.path.join(d, "aermap.rou")) as f:
            rou = f.read()
        with open(os.path.join(d, "aermap.sou")) as f:
            sou = f.read()
        with open(os.path.join(d, "info.json")) as f:
            info = json.load(f)
        return {"rou": rou, "sou": sou, **info}
    return None


def run(fac: dict, grid: dict, discrete: list, dem_step: float = 0.0005,
        margin_km: float = 3.0, zoom: int = 12, keep_dir: str | None = None) -> dict:
    """grid＝{"x0","y0","nx","ny","dx","dy"}（相對煙囪的公尺座標）。
    回傳 {"rou": 受體檔內容, "sou": 污染源檔內容, "base": 煙囪基地高程, "key": 快取鍵}。"""
    key = cache_key(fac, grid, discrete, dem_step)
    hit = load_cached(key)
    if hit:
        return hit
    if not os.path.exists(AERMAP_EXE):
        raise FileNotFoundError(f"找不到 AERMAP 執行檔：{AERMAP_EXE}")
    import terrain as terrainmod
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    xs = [grid["x0"], grid["x0"] + (grid["nx"] - 1) * grid["dx"]] + [x for x, _ in discrete]
    ys = [grid["y0"], grid["y0"] + (grid["ny"] - 1) * grid["dy"]] + [y for _, y in discrete]
    half_km = max(max(abs(v) for v in xs + ys) / 1000 + margin_km, 2.0)
    ter = terrainmod.Terrain(lat0, lon0, half_km + 1, zoom=zoom)
    # 經緯度等間距網格（像素中心）
    dlat_deg = (half_km + 0.5) / 111.32
    dlon_deg = (half_km + 0.5) / (111.32 * math.cos(math.radians(lat0)))
    lat_n, lat_s = lat0 + dlat_deg, lat0 - dlat_deg
    lon_w, lon_e = lon0 - dlon_deg, lon0 + dlon_deg
    ny = int(round((lat_n - lat_s) / dem_step))
    nx = int(round((lon_e - lon_w) / dem_step))
    lats = lat_n - (np.arange(ny) + 0.5) * dem_step
    lons = lon_w + (np.arange(nx) + 0.5) * dem_step
    LON, LAT = np.meshgrid(lons, lats)
    elev = ter(LON, LAT)

    work = keep_dir or tempfile.mkdtemp(prefix="aermap_")
    os.makedirs(work, exist_ok=True)
    write_geotiff(os.path.join(work, "dem.tif"), elev, lon_w, lat_n, dem_step, dem_step)
    ux, uy, zone = to_utm(lat0, lon0)
    dom = (half_km - 0.5) * 1000
    disc = "".join(f"   DISCCART  {x:.1f} {y:.1f}\n" for x, y in discrete)
    inp = f"""CO STARTING
   TITLEONE  CEMS bot terrain
   TERRHGTS  EXTRACT
   DATATYPE  NED
   DATAFILE  dem.tif
   DOMAINXY  {ux - dom:.1f} {uy - dom:.1f} {zone} {ux + dom:.1f} {uy + dom:.1f} {zone}
   ANCHORXY  0.0 0.0 {ux:.1f} {uy:.1f} {zone} 4
   RUNORNOT  RUN
CO FINISHED

SO STARTING
   LOCATION  STK1 POINT 0.0 0.0
SO FINISHED

RE STARTING
   GRIDCART  G1 STA
             G1 XYINC {grid['x0']:.1f} {grid['nx']} {grid['dx']:.1f} {grid['y0']:.1f} {grid['ny']} {grid['dy']:.1f}
             G1 END
{disc}RE FINISHED

OU STARTING
   RECEPTOR  aermap.rou
   SOURCLOC  aermap.sou
OU FINISHED
"""
    with open(os.path.join(work, "aermap.inp"), "w") as f:
        f.write(inp)
    p = subprocess.run([AERMAP_EXE], cwd=work, capture_output=True, text=True, timeout=3000)
    outp = os.path.join(work, "aermap.out")
    log_txt = open(outp, errors="replace").read() if os.path.exists(outp) else ""
    rou_p, sou_p = os.path.join(work, "aermap.rou"), os.path.join(work, "aermap.sou")
    if not (os.path.exists(rou_p) and os.path.exists(sou_p)) or "Successfully" not in (
            p.stdout + log_txt):
        errs = [ln for ln in log_txt.splitlines() if " E" in ln[:10] or "ERROR" in ln.upper()]
        raise RuntimeError("AERMAP 執行失敗：\n" + "\n".join(errs[-20:] or log_txt.splitlines()[-40:])
                           + f"\n{p.stdout[-1500:]}")
    with open(rou_p) as f:
        rou = f.read()
    with open(sou_p) as f:
        sou = f.read()
    base = 0.0
    for ln in sou.splitlines():
        v = ln.split()
        if len(v) >= 7 and v[0] == "SO" and v[1] == "LOCATION":
            base = float(v[6])
    info = {"base": base, "key": key, "dem_shape": [ny, nx], "zone": zone}
    d = os.path.join(CACHE, key)
    os.makedirs(d, exist_ok=True)
    for name, txt in (("aermap.rou", rou), ("aermap.sou", sou)):
        with open(os.path.join(d, name), "w") as f:
            f.write(txt)
    with open(os.path.join(d, "info.json"), "w") as f:
        json.dump(info, f)
    if not keep_dir:
        shutil.rmtree(work, ignore_errors=True)
    return {"rou": rou, "sou": sou, **info}


def prepare():
    """依 config.yaml 預先計算（與機器人使用相同的格點），存入快取。"""
    import yaml
    import plume
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    print("aermap_run.py 版本：", SCRIPT_VERSION)
    for fac in cfg["facilities"]:
        g, disc = grid_for(fac, cfg)
        print(f"{fac['name']}：格點 {g['nx']}×{g['ny']}，間距 {g['dx']:.0f} m，敏感點 {len(disc)} 個")
        work = os.path.join(HERE, "aermap_test")
        shutil.rmtree(work, ignore_errors=True)
        try:
            res = run(fac, g, disc, keep_dir=work,
                      dem_step=cfg.get("aermod", {}).get("dem_step_deg", 0.0005))
            print(f"  AERMAP 完成，快取鍵 {res['key']}，煙囪基地高程 {res['base']:.1f} m")
            lines = [ln for ln in res["rou"].splitlines() if "DISCCART" in ln]
            for ln in lines:
                print("  " + ln.strip())
        except Exception as e:  # noqa: BLE001
            print("  AERMAP 失敗：", e)
            sys.exit(1)


def grid_for(fac: dict, cfg: dict):
    """與 bot.run_aermod 一致的格點定義（地形模式）。"""
    import plume
    acfg = cfg.get("aermod", {})
    rxy = [plume.lonlat_to_xy(r["lon"], r["lat"], fac["stack_lat"], fac["stack_lon"])
           for r in cfg.get("receptors", [])]
    step = acfg.get("terrain_grid_step_m", 200)
    half = acfg.get("terrain_grid_half_width_km", 6) * 1000
    half = max(half, max([math.hypot(x, y) for x, y in rxy] + [0]) + 1000)
    half = math.ceil(half / step) * step
    n = int(2 * half / step) + 1
    return {"x0": -half, "y0": -half, "nx": n, "ny": n, "dx": step, "dy": step}, \
        [(round(x, 1), round(y, 1)) for x, y in rxy]


if __name__ == "__main__":
    if "--prepare" in sys.argv:
        prepare()

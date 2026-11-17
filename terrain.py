"""數值地形：AWS Terrain Tiles（Terrarium 格式，免金鑰）。
臺灣地區資料主要來自 SRTM（約 30 m），zoom 12 的像素約 34 m，對 100 m 的計算格點足夠。
圖磚會存在 terrain_cache/，GitHub Actions 以 cache 保存，地形不會變，只需下載一次。
【待核：上線後以 dry-run 確認 log 出現「地形：已載入」】"""
from __future__ import annotations

import io
import logging
import math
import os

import numpy as np
import requests
from PIL import Image

log = logging.getLogger(__name__)
URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
HERE = os.path.dirname(os.path.abspath(__file__))


def _tile_xy(lon, lat, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
    return x, y


def fetch_tile(z: int, x: int, y: int, cache_dir: str) -> np.ndarray:
    path = os.path.join(cache_dir, f"{z}_{x}_{y}.png")
    if os.path.exists(path):
        img = Image.open(path)
    else:
        r = requests.get(URL.format(z=z, x=x, y=y), timeout=30,
                         headers={"User-Agent": "incinerator-cems-bot/1.0"})
        r.raise_for_status()
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
        img = Image.open(io.BytesIO(r.content))
    a = np.asarray(img.convert("RGB"), dtype=np.float64)
    return a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768  # Terrarium 解碼（公尺）


class Terrain:
    """給經緯度陣列，回傳地面標高（m），雙線性內插。"""

    def __init__(self, lat0, lon0, half_km, zoom=12, cache_dir=None):
        self.z = zoom
        cache_dir = cache_dir or os.path.join(HERE, "terrain_cache")
        dlat = (half_km + 1) / 111.32
        dlon = (half_km + 1) / (111.32 * math.cos(math.radians(lat0)))
        x0, y0 = _tile_xy(lon0 - dlon, lat0 + dlat, zoom)
        x1, y1 = _tile_xy(lon0 + dlon, lat0 - dlat, zoom)
        self.tx0, self.ty0 = int(x0), int(y0)
        nx, ny = int(x1) - self.tx0 + 1, int(y1) - self.ty0 + 1
        mosaic = np.zeros((ny * 256, nx * 256))
        for j in range(ny):
            for i in range(nx):
                mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = \
                    fetch_tile(zoom, self.tx0 + i, self.ty0 + j, cache_dir)
        self.grid = np.maximum(mosaic, 0.0)  # 海面以下當 0
        self.n_tiles = nx * ny

    def __call__(self, lon, lat):
        lon, lat = np.asarray(lon, float), np.asarray(lat, float)
        n = 2 ** self.z
        px = ((lon + 180) / 360 * n - self.tx0) * 256 - 0.5
        lr = np.radians(lat)
        py = ((1 - np.log(np.tan(lr) + 1 / np.cos(lr)) / np.pi) / 2 * n - self.ty0) * 256 - 0.5
        h, w = self.grid.shape
        px, py = np.clip(px, 0, w - 1.001), np.clip(py, 0, h - 1.001)
        i0, j0 = px.astype(int), py.astype(int)
        fx, fy = px - i0, py - j0
        g = self.grid
        return ((1 - fx) * (1 - fy) * g[j0, i0] + fx * (1 - fy) * g[j0, i0 + 1]
                + (1 - fx) * fy * g[j0 + 1, i0] + fx * fy * g[j0 + 1, i0 + 1])

"""Open-Meteo 全球數值模式資料（免金鑰）：地面逐時資料與高空剖面。
供 AERMET（虛擬探空＋地面資料）與備援的邊界層計算使用。資料為模式計算值，不是實際觀測。

為避免單次請求過大而逾時：地面與高空分兩次請求、各自重試，
並把結果暫存在 om_cache/（同一小時內重複使用，不重抓）。"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
LEVELS = [1000, 925, 850, 700, 500]
SURF = ["temperature_2m", "relative_humidity_2m", "surface_pressure", "cloud_cover",
        "shortwave_radiation", "wind_speed_10m", "wind_direction_10m", "boundary_layer_height"]
PROF = ["temperature", "relative_humidity", "wind_speed", "wind_direction", "geopotential_height"]
URL = "https://api.open-meteo.com/v1/forecast"


def _get(lat, lon, variables, past_days, tries=4):
    params = {"latitude": round(lat, 3), "longitude": round(lon, 3),
              "hourly": ",".join(variables), "past_days": past_days, "forecast_days": 1,
              "timezone": "Asia/Taipei", "wind_speed_unit": "ms"}
    last = None
    for i in range(1, tries + 1):
        try:
            r = requests.get(URL, params=params, timeout=(15, 90),
                             headers={"User-Agent": "incinerator-cems-bot/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Open-Meteo 請求失敗（第 %d/%d 次）：%s", i, tries, e)
            time.sleep(5 * i)
    raise RuntimeError(f"Open-Meteo 無法取得資料：{last}")


def fetch(lat: float, lon: float, past_days: int = 1) -> dict:
    """回傳 {"time": [datetime（臺灣時間）…], 各欄位: [數值…], "elevation": 模式地面高度}。
    風速 m/s，氣壓 hPa，高度 m。"""
    key = f"{datetime.now():%Y%m%d%H}_{lat:.3f}_{lon:.3f}_{past_days}"
    cache = os.path.join(HERE, "om_cache", key + ".json")
    if os.path.exists(cache):
        with open(cache) as f:
            h = json.load(f)
    else:
        a = _get(lat, lon, SURF, past_days)
        b = _get(lat, lon, [f"{v}_{p}hPa" for p in LEVELS for v in PROF], past_days)
        h = {**a["hourly"], **b["hourly"], "elevation": a.get("elevation", 0.0)}
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w") as f:
            json.dump(h, f)
    h["time"] = [datetime.fromisoformat(t) for t in h["time"]]
    return h


def index_of(h: dict, when: datetime) -> int:
    """找出 when（臺灣時間，取整點）在資料中的位置。"""
    t = when.replace(minute=0, second=0, microsecond=0)
    return h["time"].index(t)

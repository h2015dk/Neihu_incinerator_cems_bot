"""Open-Meteo 全球數值模式資料（免金鑰）：地面逐時資料與高空剖面。
供 AERMET（虛擬探空＋地面資料）與備援的邊界層計算使用。資料為模式計算值，不是實際觀測。"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]
SURF = ["temperature_2m", "relative_humidity_2m", "surface_pressure", "cloud_cover",
        "shortwave_radiation", "wind_speed_10m", "wind_direction_10m", "boundary_layer_height"]
PROF = ["temperature", "relative_humidity", "wind_speed", "wind_direction", "geopotential_height"]


def fetch(lat: float, lon: float, past_days: int = 2) -> dict:
    """回傳 {"time": [datetime（臺灣時間）…], 各欄位: [數值…], "elevation": 模式地面高度}。
    風速單位 m/s，氣壓 hPa，高度 m。"""
    hourly = SURF + [f"{v}_{p}hPa" for p in LEVELS for v in PROF]
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=60,
                     headers={"User-Agent": "incinerator-cems-bot/1.0"},
                     params={"latitude": lat, "longitude": lon, "hourly": ",".join(hourly),
                             "past_days": past_days, "forecast_days": 1,
                             "timezone": "Asia/Taipei", "wind_speed_unit": "ms"})
    r.raise_for_status()
    js = r.json()
    h = js["hourly"]
    h["time"] = [datetime.fromisoformat(t) for t in h["time"]]
    h["elevation"] = js.get("elevation", 0.0)
    return h


def index_of(h: dict, when: datetime) -> int:
    """找出 when（臺灣時間，取整點）在資料中的位置。"""
    t = when.replace(minute=0, second=0, microsecond=0)
    return h["time"].index(t)

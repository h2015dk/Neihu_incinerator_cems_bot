"""最近測站風向風速：環境部空品測站（AQX_P_432 AQI 即時資料，含風向風速）
或中央氣象署自動氣象站（O-A0001-001，需另申請授權碼）。"""
from __future__ import annotations

import logging
import math

import requests

from cems import fetch_dataset

log = logging.getLogger(__name__)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _f(v):
    try:
        x = float(v)
        return x if x >= 0 else None
    except (TypeError, ValueError):
        return None


def moenv_stations(api_key: str) -> list[dict]:
    out = []
    for r in fetch_dataset("aqx_p_432", api_key):
        lat, lon = _f(r.get("latitude")), _f(r.get("longitude"))
        ws, wd = _f(r.get("wind_speed")), _f(r.get("wind_direc"))
        if None in (lat, lon, ws, wd):
            continue
        out.append({"name": f"環境部{r.get('sitename', '')}站", "lat": lat, "lon": lon,
                    "src_label": "環境部空氣品質監測站", "site": f"{r.get('sitename', '')}站",
                    "ws": ws, "wd": wd, "temp_c": None, "time": r.get("publishtime", ""),
                    "src": "moenv"})
    return out


def cwa_stations(cwa_key: str) -> list[dict]:
    """欄位結構依氣象署 O-A0001-001 現行格式撰寫【待核：氣象署偶有調整欄位】"""
    url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001"
    js = requests.get(url, params={"Authorization": cwa_key, "format": "JSON"}, timeout=60).json()
    out = []
    for s in js.get("records", {}).get("Station", []):
        try:
            coords = s["GeoInfo"]["Coordinates"]
            c = next((c for c in coords if c.get("CoordinateName") == "WGS84"), coords[-1])
            we = s["WeatherElement"]
            ws, wd = _f(we.get("WindSpeed")), _f(we.get("WindDirection"))
            if ws is None or wd is None:
                continue
            out.append({"name": f"氣象署{s.get('StationName', '')}站",
                        "src_label": "中央氣象署自動氣象站", "site": f"{s.get('StationName', '')}站",
                        "lat": float(c["StationLatitude"]), "lon": float(c["StationLongitude"]),
                        "ws": ws, "wd": wd, "temp_c": _f(we.get("AirTemperature")),
                        "time": s.get("ObsTime", {}).get("DateTime", ""), "src": "cwa"})
        except (KeyError, TypeError, ValueError, StopIteration):
            continue
    return out


def get_stations(cfg: dict) -> list[dict]:
    wcfg = cfg.get("wind", {})
    src = wcfg.get("source", "moenv")
    stations: list[dict] = []
    if src in ("moenv", "auto"):
        try:
            stations += moenv_stations(cfg["moenv_api_key"])
        except Exception as e:  # noqa: BLE001
            log.warning("環境部測站資料讀取失敗：%s", e)
    if src in ("cwa", "auto") and cfg.get("cwa_api_key"):
        try:
            stations += cwa_stations(cfg["cwa_api_key"])
        except Exception as e:  # noqa: BLE001
            log.warning("氣象署測站資料讀取失敗：%s", e)
    return stations


def nearest_wind(stations: list[dict], fac: dict, cfg: dict) -> dict | None:
    """fac 可設 wind_station 指定測站；否則用全域 wind.fixed_station；都沒有就找最近。"""
    fixed = fac.get("wind_station") or cfg.get("wind", {}).get("fixed_station")
    cand = [s for s in stations if fixed and fixed in s["name"]] or stations
    if not cand:
        return None
    best = min(cand, key=lambda s: haversine_km(fac["stack_lat"], fac["stack_lon"], s["lat"], s["lon"]))
    return {**best, "dist_km": haversine_km(fac["stack_lat"], fac["stack_lon"], best["lat"], best["lon"])}

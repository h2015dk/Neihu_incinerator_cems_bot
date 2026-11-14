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
    """取最近測站的風向風速。
    - fac.wind_station 或 wind.fixed_station 有填 → 只用指定測站
    - wind.n_nearest > 1 → 取最近 N 站（限 max_km 內）做向量合成"""
    wcfg = cfg.get("wind", {})
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    fixed = fac.get("wind_station") or wcfg.get("fixed_station")
    cand = [s for s in stations if fixed and fixed in s["name"]] or stations
    if not cand:
        return None
    ranked = sorted(({**s, "dist_km": haversine_km(lat0, lon0, s["lat"], s["lon"])} for s in cand),
                    key=lambda s: s["dist_km"])
    n = 1 if fixed else int(wcfg.get("n_nearest", 1))
    sel = [s for s in ranked if s["dist_km"] <= wcfg.get("max_km", 15)][:n] or ranked[:1]
    if len(sel) == 1:
        return sel[0]
    return combine(sel, wcfg.get("weighting", "idw"))


def combine(sel: list[dict], weighting: str = "idw") -> dict:
    """風向以向量平均（考慮風速）合成，避免 350° 與 10° 平均成 180° 的錯誤；
    風速取加權算術平均；一致性 R＝合成向量長度／平均風速（1＝各站風向完全一致）。"""
    ws_ = [1 / max(s["dist_km"], 0.5) ** 2 if weighting == "idw" else 1.0 for s in sel]
    W = sum(ws_)
    u = sum(w * s["ws"] * math.sin(math.radians(s["wd"])) for w, s in zip(ws_, sel)) / W
    v = sum(w * s["ws"] * math.cos(math.radians(s["wd"])) for w, s in zip(ws_, sel)) / W
    ws_mean = sum(w * s["ws"] for w, s in zip(ws_, sel)) / W
    wd = (math.degrees(math.atan2(u, v)) + 360) % 360
    R = math.hypot(u, v) / ws_mean if ws_mean > 0 else 0.0
    temps = [s["temp_c"] for s in sel if s.get("temp_c") is not None]
    srcs = sorted({s.get("src_label", "") for s in sel})
    how = "反距離平方加權" if weighting == "idw" else "等權重"
    return {
        "name": f"{len(sel)} 站合成", "src_label": "、".join(srcs) + f"（{len(sel)} 站{how}合成）",
        "site": "、".join(f"{s.get('site', s['name'])} {s['dist_km']:.1f} km" for s in sel),
        "lat": sel[0]["lat"], "lon": sel[0]["lon"], "dist_km": sel[0]["dist_km"],
        "ws": ws_mean, "wd": wd, "temp_c": sum(temps) / len(temps) if temps else None,
        "time": "、".join(sorted({str(s["time"]) for s in sel})), "consistency": R,
        "members": sel, "weights": [w / W for w in ws_],
    }

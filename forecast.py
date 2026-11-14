"""中央氣象署鄉鎮天氣預報（逐 3 小時）風向。臺北市為 F-D0047-061。
氣象署曾改版欄位結構，新舊格式都處理【待核：上線後以 dry-run 確認抓得到】。"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)
DIRS = ["北", "北北東", "東北", "東北東", "東", "東南東", "東南", "南南東",
        "南", "南南西", "西南", "西南西", "西", "西北西", "西北", "北北西"]


def text_to_deg(text: str | None) -> float | None:
    """「偏北風」「東北風」→ 風從哪個方位來（度）。"""
    if not text:
        return None
    s = str(text).replace("風", "").replace("偏", "").strip()
    return DIRS.index(s) * 22.5 if s in DIRS else None


def _t(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def fetch(cfg: dict, town: str) -> list[dict]:
    key = cfg.get("cwa_api_key")
    fcfg = cfg.get("forecast", {})
    if not key or not fcfg.get("enabled", True) or not town:
        return []
    ds = fcfg.get("dataset", "F-D0047-061")
    try:
        js = requests.get(f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{ds}",
                          params={"Authorization": key, "format": "JSON",
                                  "LocationName": town, "locationName": town},
                          timeout=60).json()
    except Exception as e:  # noqa: BLE001
        log.warning("風向預報讀取失敗：%s", e)
        return []
    recs, out = js.get("records", {}), []
    # 新格式
    for group in recs.get("Locations", []):
        for loc in group.get("Location", []):
            if loc.get("LocationName") != town:
                continue
            for we in loc.get("WeatherElement", []):
                if we.get("ElementName") not in ("風向", "WD"):
                    continue
                for tt in we.get("Time", []):
                    ev = (tt.get("ElementValue") or [{}])[0]
                    txt = ev.get("WindDirection") or ev.get("value")
                    t = _t(tt.get("StartTime") or tt.get("DataTime"))
                    if t and text_to_deg(txt) is not None:
                        out.append({"time": t, "text": txt, "wd": text_to_deg(txt)})
    # 舊格式
    for group in recs.get("locations", []):
        for loc in group.get("location", []):
            if loc.get("locationName") != town:
                continue
            for we in loc.get("weatherElement", []):
                if we.get("elementName") != "WD":
                    continue
                for tt in we.get("time", []):
                    txt = (tt.get("elementValue") or [{}])[0].get("value")
                    t = _t(tt.get("startTime") or tt.get("dataTime"))
                    if t and text_to_deg(txt) is not None:
                        out.append({"time": t, "text": txt, "wd": text_to_deg(txt)})
    if not out:
        log.warning("風向預報解析不到 %s 的資料（欄位格式可能已改版）", town)
    return sorted(out, key=lambda x: x["time"])

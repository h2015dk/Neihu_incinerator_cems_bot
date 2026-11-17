"""檢查 AERMET 需要的即時資料抓不抓得到（第 2 步的前置確認）。

1. 中央氣象署局屬氣象站（O-A0003-001）：附近測站有哪些觀測項目，是否有雲量、日射量
2. 中央氣象署自動氣象站（O-A0001-001）：最近一站的觀測項目
3. 板橋探空（WMO 46692）：NOAA IGRA2 即時檔、美國懷俄明大學探空網站，最新一筆是何時

用法：python aermod_probe.py   （需要環境變數 CWA_API_KEY）"""
from __future__ import annotations

import io
import json
import math
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "incinerator-cems-bot/1.0"}


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def coords(st):
    for c in st.get("GeoInfo", {}).get("Coordinates", []):
        if c.get("CoordinateName") == "WGS84":
            return float(c["StationLatitude"]), float(c["StationLongitude"])
    c = st["GeoInfo"]["Coordinates"][-1]
    return float(c["StationLatitude"]), float(c["StationLongitude"])


def flat(d, prefix=""):
    """把巢狀的觀測項目攤平成 鍵＝值，方便看有哪些欄位。"""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def probe_cwa(ds, key, lat0, lon0, n=4):
    print(f"\n===== 中央氣象署 {ds} =====")
    try:
        js = requests.get(f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{ds}",
                          params={"Authorization": key, "format": "JSON"}, timeout=60).json()
        sts = js.get("records", {}).get("Station", [])
        print(f"測站數：{len(sts)}")
        ranked = sorted(sts, key=lambda s: km(lat0, lon0, *coords(s)))[:n]
        for s in ranked:
            la, lo = coords(s)
            print(f"\n--- {s.get('StationName')}（{s.get('StationId')}），距 {km(lat0, lon0, la, lo):.1f} km，"
                  f"觀測時間 {s.get('ObsTime', {}).get('DateTime')}")
            for k, v in flat(s.get("WeatherElement", {})).items():
                print(f"   {k} = {v}")
    except Exception as e:  # noqa: BLE001
        print("讀取失敗：", e)


def probe_igra():
    print("\n===== 板橋探空：NOAA IGRA2 即時檔（data-y2d）=====")
    base = "https://www.ncei.noaa.gov/data/integrated-global-radiosonde-archive/access/data-y2d/"
    try:
        idx = requests.get(base, timeout=60, headers=UA).text
        names = sorted(set(re.findall(r'TWM00046692[^"<>]*?\.zip', idx)))
        print("找到檔案：", names or "（無）")
        if not names:
            return
        z = zipfile.ZipFile(io.BytesIO(requests.get(base + names[-1], timeout=120, headers=UA).content))
        txt = z.read(z.namelist()[0]).decode("ascii", "replace")
        heads = [ln for ln in txt.splitlines() if ln.startswith("#")]
        print(f"探空筆數：{len(heads)}；最後 3 筆標頭：")
        for h in heads[-3:]:
            print("   " + h[:60])
        last = heads[-1]
        t = datetime(int(last[13:17]), int(last[18:20]), int(last[21:23]), int(last[24:26]),
                     tzinfo=timezone.utc)
        lag = (datetime.now(timezone.utc) - t).total_seconds() / 3600
        print(f"最新一筆：{t:%Y-%m-%d %H}Z（臺灣時間 {(t + timedelta(hours=8)):%m/%d %H:%M}），"
              f"距今約 {lag:.0f} 小時")
    except Exception as e:  # noqa: BLE001
        print("讀取失敗：", e)


def probe_uwyo():
    print("\n===== 板橋探空：美國懷俄明大學 =====")
    now = datetime.now(timezone.utc)
    hh = 12 if now.hour >= 14 else 0
    t = now.replace(hour=hh, minute=0, second=0, microsecond=0)
    if now.hour < 2:
        t -= timedelta(hours=12)
    urls = [
        f"https://weather.uwyo.edu/wsgi/sounding?datetime={t:%Y-%m-%d}%20{t:%H}:00:00"
        f"&id=46692&src=UNKNOWN&type=TEXT:LIST",
        f"https://weather.uwyo.edu/cgi-bin/sounding?region=seasia&TYPE=TEXT%3ALIST"
        f"&YEAR={t:%Y}&MONTH={t:%m}&FROM={t:%d%H}&TO={t:%d%H}&STNM=46692",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=60, headers=UA)
            body = re.sub(r"<[^>]+>", "", r.text)
            lines = [ln for ln in body.splitlines() if ln.strip()]
            print(f"\n{u}\nHTTP {r.status_code}，前 12 行：")
            for ln in lines[:12]:
                print("   " + ln[:100])
        except Exception as e:  # noqa: BLE001
            print(f"{u}\n讀取失敗：{e}")


def main():
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    fac = cfg["facilities"][0]
    key = os.environ.get("CWA_API_KEY") or cfg.get("cwa_api_key")
    if key:
        probe_cwa("O-A0003-001", key, fac["stack_lat"], fac["stack_lon"])
        probe_cwa("O-A0001-001", key, fac["stack_lat"], fac["stack_lon"], n=1)
    else:
        print("沒有 CWA_API_KEY，略過氣象署檢查")
    probe_igra()
    probe_uwyo()


if __name__ == "__main__":
    main()

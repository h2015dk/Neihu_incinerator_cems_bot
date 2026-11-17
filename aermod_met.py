"""AERMOD 氣象輸入：先用 AERMET（做法 A），失敗時改用 Python 計算（做法 B）。

用法（測試，GitHub Actions 上執行）：python aermod_met.py --test
  會抓 Open-Meteo、分別用 A、B 產生當下這一小時的氣象參數，各跑一次 AERMOD 並比較。"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

import aermet_run
import aermod_run
import met_simple
import openmeteo
from plume import solar_elevation

log = logging.getLogger(__name__)


def get_met(lat: float, lon: float, target: datetime, obs: dict | None = None,
            zref: float = 10.0, site: dict | None = None, prefer: str = "aermet") -> dict:
    """回傳 {"method": "AERMET"/"Python 計算", "raw": {...} 或 None, "sfc": SfcHour 或 None,
    "note": 說明}。obs＝{"ws","wd","temp_c"} 為實測風與氣溫（三站合成）。"""
    site = site or {"albedo": 0.18, "bowen": 1.5, "z0": 1.0}
    target = target.replace(minute=0, second=0, microsecond=0)
    h = openmeteo.fetch(lat, lon, past_days=1)
    err = ""
    if prefer == "aermet" and os.path.exists(aermet_run.AERMET_EXE):
        try:
            raw = aermet_run.run(lat, lon, target, obs=obs, zref=zref, site=site, h=h)
            return {"method": "AERMET", "raw": raw, "sfc": None,
                    "note": "AERMET（虛擬探空＋地面資料：Open-Meteo 模式值，風與氣溫為實測）"}
        except Exception as e:  # noqa: BLE001
            err = str(e).splitlines()[0]
            log.warning("AERMET 失敗，改用 Python 計算：%s", err)
    i = openmeteo.index_of(h, target)
    ws = (obs or {}).get("ws", h["wind_speed_10m"][i])
    wd = (obs or {}).get("wd", h["wind_direction_10m"][i])
    tc = (obs or {}).get("temp_c") or h["temperature_2m"][i]
    d, hr = aermet_run.hour_ending(target)
    when = d.replace(hour=0) if hr == 24 else target
    sfc = met_simple.compute(
        when, ws, wd, tc, (h["cloud_cover"][i] or 0) / 100, h["shortwave_radiation"][i] or 0,
        h["boundary_layer_height"][i], solar_elevation(target - timedelta(minutes=30), lat, lon),
        rh=h["relative_humidity_2m"][i] or 70, pres=h["surface_pressure"][i] or 1013,
        zref=zref, **site)
    if hr == 24:
        sfc.hour = 24
    note = "Python 計算邊界層參數（Open-Meteo 模式值，風與氣溫為實測）"
    if err:
        note += f"；AERMET 失敗原因：{err[:80]}"
    return {"method": "Python 計算", "raw": None, "sfc": sfc, "note": note}


def test():
    lat, lon = 25.06304, 121.60340
    target = datetime.now().replace(minute=0, second=0, microsecond=0)
    stk = aermod_run.Stack(q_ng_s=108.2, hs=74.0, ts_k=439.0, vs=13.77, d=1.25)
    grid = aermod_run.Grid(-6000, -6000, 61, 61, 200, 200, discrete=[(-950, -95), (-2520, -1400)])
    for prefer in ("aermet", "python"):
        print(f"\n===== 氣象來源：{prefer} =====")
        try:
            met = get_met(lat, lon, target, zref=10.0, prefer=prefer)
            print("實際使用：", met["method"], "｜", met["note"])
            line = met["raw"]["sfc_line"] if met["raw"] else met["sfc"].line()
            print("SFC：", line)
            res = aermod_run.run(stk, met["sfc"], grid, lat, lon, urban_pop=2600000,
                                 raw=met["raw"])
            import numpy as np
            i = np.unravel_index(np.argmax(res["C"]), res["C"].shape)
            print(f"AERMOD 最大 {res['C'][i]:.1f} fg/m³ @ ({res['X'][i]:.0f}, {res['Y'][i]:.0f}) m；"
                  f"環興公司 {res['discrete'][0]:.1f}、松山國小 {res['discrete'][1]:.1f} fg/m³")
        except Exception as e:  # noqa: BLE001
            print("失敗：", str(e)[:3000])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--test" in sys.argv:
        test()

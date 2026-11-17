"""做法 A：用 EPA AERMET 產生 AERMOD 氣象檔。
- 高空：Open-Meteo 各氣壓層剖面轉成 FSL 探空格式（虛擬探空，每小時一筆，UTC）
- 地面：ONSITE 路徑，逐時風向風速、氣溫、濕度、氣壓、雲量、日射量；
        最近一小時的風與氣溫以氣象署實測取代
AERMET 逐日處理，對流混合層高度要從日出起逐時累積，所以輸入涵蓋前一天到目標小時。

用法（測試）：python aermet_run.py --test   （需先執行 aermod_build.py 產生 bin/aermet）"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

import openmeteo

HERE = os.path.dirname(os.path.abspath(__file__))
AERMET_EXE = os.environ.get("AERMET_EXE", os.path.join(HERE, "bin", "aermet"))
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def dewpoint(t_c: float, rh: float) -> float:
    a, b = 17.62, 243.12
    g = math.log(max(rh, 1) / 100) + a * t_c / (b + t_c)
    return b * g / (a - g)


def hour_ending(t: datetime) -> tuple[datetime, int]:
    """把整點時間轉成 AERMET 的「小時結束」表示：00:00 → 前一天 24 時。"""
    if t.hour == 0:
        return (t - timedelta(days=1)), 24
    return t, t.hour


def write_fsl(path: str, h: dict, lat: float, lon: float, elev: float):
    """每小時一筆虛擬探空（FSL 格式，固定 7 欄寬）。時間轉為 UTC。"""
    out = []
    for i, t in enumerate(h["time"]):
        tu = t - timedelta(hours=8)
        levels = []
        sp = h["surface_pressure"][i]
        t2, rh2 = h["temperature_2m"][i], h["relative_humidity_2m"][i]
        ws10, wd10 = h["wind_speed_10m"][i], h["wind_direction_10m"][i]
        if None in (sp, t2, rh2):
            continue
        levels.append((9, sp, elev, t2, dewpoint(t2, rh2), wd10, ws10))
        for p in openmeteo.LEVELS:
            z = h[f"geopotential_height_{p}hPa"][i]
            tt = h[f"temperature_{p}hPa"][i]
            rr = h[f"relative_humidity_{p}hPa"][i]
            if None in (z, tt, rr) or p >= sp or z <= elev:
                continue  # 低於地面的氣壓層略過
            levels.append((4, p, z, tt, dewpoint(tt, rr), h[f"wind_direction_{p}hPa"][i],
                           h[f"wind_speed_{p}hPa"][i]))
        out.append(f"{254:7d}{tu.hour:7d}{tu.day:7d}      {MON[tu.month - 1]:<4}{tu.year:7d}")
        out.append(f"{1:7d}{99999:7d}{99999:7d}{abs(lat):7.2f}{'N' if lat >= 0 else 'S'}"
                   f"{abs(lon):6.2f}{'E' if lon >= 0 else 'W'}{int(elev):6d}{99999:7d}")
        out.append(f"{2:7d}{99999:7d}{99999:7d}{99999:7d}{len(levels) + 4:7d}{99999:7d}{3:7d}")
        out.append(f"{3:7d}{'':10}{'VSND':<4}{'':14}{99999:7d}{'':4}ms")
        for typ, p, z, tt, td, wd, ws in levels:
            wd_i = 32767 if wd is None else int(round(wd)) % 360
            ws_i = 32767 if ws is None else int(round(ws * 10))   # ms 單位時為 0.1 m/s
            out.append(f"{typ:7d}{int(round(p * 10)):7d}{int(round(z)):7d}"
                       f"{int(round(tt * 10)):7d}{int(round(td * 10)):7d}{wd_i:7d}{ws_i:7d}")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def write_onsite(path: str, h: dict, upto: datetime, obs: dict | None):
    """ONSITE 逐時資料（臺灣時間、小時結束制）。"""
    rows = []
    for i, t in enumerate(h["time"]):
        if t > upto:
            break
        d, hr = hour_ending(t)
        ws, wd = h["wind_speed_10m"][i], h["wind_direction_10m"][i]
        tt, rh = h["temperature_2m"][i], h["relative_humidity_2m"][i]
        if obs and t == upto:  # 目標小時以實測取代
            ws, wd = obs.get("ws", ws), obs.get("wd", wd)
            tt = obs.get("temp_c") if obs.get("temp_c") is not None else tt
        vals = [ws, wd, tt, rh, h["surface_pressure"][i], h["cloud_cover"][i],
                h["shortwave_radiation"][i]]
        miss = [99.0, 999.0, 99.0, 999.0, 9999.0, 99.0, 9999.0]
        vals = [m if v is None else v for v, m in zip(vals, miss)]
        cc = vals[5] if vals[5] == 99.0 else round(vals[5] / 10)   # % → 十分位
        rows.append(f"{d.year % 100:02d} {d.month:02d} {d.day:02d} {hr:02d} "
                    f"{vals[0]:6.2f} {vals[1]:6.1f} {vals[2]:6.1f} {vals[3]:6.1f} "
                    f"{vals[4]:7.1f} {cc:5.1f} {vals[6]:7.1f}")
    with open(path, "w") as f:
        f.write("\n".join(rows) + "\n")


def write_control(path: str, d0: datetime, d1: datetime, lat: float, lon: float, elev: float,
                  zref: float, site: dict):
    ll = f"{abs(lat):.3f}{'N' if lat >= 0 else 'S'} {abs(lon):.3f}{'E' if lon >= 0 else 'W'}"
    xd = f"{d0:%Y/%m/%d} TO {d1:%Y/%m/%d}"
    txt = f"""JOB
   REPORT     aermet.rpt
   MESSAGES   aermet.msg

UPPERAIR
   DATA       ua.fsl  FSL
   EXTRACT    ua_ext.txt
   QAOUT      ua_qa.txt
   XDATES     {xd}
   LOCATION   99999  {ll}  -8

ONSITE
   DATA       os.dat
   QAOUT      os_qa.txt
   XDATES     {xd}
   LOCATION   99999  {ll}  0  {elev:.0f}
   READ       1  OSYR OSMO OSDY OSHR WS01 WD01 TT01 RH01 PRES TSKC INSO
   FORMAT     1  (I2,1X,I2,1X,I2,1X,I2,1X,F6.2,1X,F6.1,1X,F6.1,1X,F6.1,1X,F7.1,1X,F5.1,1X,F7.1)
   THRESHOLD  0.5
   OSHEIGHTS  {zref:.1f}

METPREP
   XDATES     {xd}
   MODEL      AERMOD
   METHOD     WIND_DIR  NORAND
   METHOD     UASELECT  SUNRIS
   OUTPUT     met.sfc
   PROFILE    met.pfl
   FREQ_SECT  ANNUAL  1
   SECTOR     1  0  360
   SITE_CHAR  1  1  {site['albedo']}  {site['bowen']}  {site['z0']}
"""
    with open(path, "w") as f:
        f.write(txt)


def run(lat: float, lon: float, target: datetime, obs: dict | None = None, zref: float = 10.0,
        site: dict | None = None, keep_dir: str | None = None, h: dict | None = None) -> dict:
    """target：目標小時（臺灣時間整點，代表該小時結束）。
    回傳 {"sfc_header", "sfc_line", "pfl_lines", "messages"}；失敗時拋出例外並附上 AERMET 訊息。"""
    if not os.path.exists(AERMET_EXE):
        raise FileNotFoundError(f"找不到 AERMET 執行檔：{AERMET_EXE}")
    site = site or {"albedo": 0.18, "bowen": 1.5, "z0": 1.0}
    h = h or openmeteo.fetch(lat, lon, past_days=2)
    work = keep_dir or tempfile.mkdtemp(prefix="aermet_")
    os.makedirs(work, exist_ok=True)
    elev = float(h.get("elevation") or 0)
    target = target.replace(minute=0, second=0, microsecond=0)
    d1, hr1 = hour_ending(target)
    d0 = d1 - timedelta(days=1)
    write_fsl(os.path.join(work, "ua.fsl"), h, lat, lon, elev)
    write_onsite(os.path.join(work, "os.dat"), h, target, obs)
    write_control(os.path.join(work, "aermet.inp"), d0, d1, lat, lon, elev, zref, site)
    p = subprocess.run([AERMET_EXE, "aermet.inp"], cwd=work, capture_output=True, text=True,
                       timeout=600)
    msg = ""
    for name in ("aermet.msg", "aermet.rpt"):
        fp = os.path.join(work, name)
        if os.path.exists(fp):
            with open(fp, errors="replace") as f:
                msg += f"\n==== {name} ====\n" + f.read()[-6000:]
    sfc_path, pfl_path = os.path.join(work, "met.sfc"), os.path.join(work, "met.pfl")
    if not os.path.exists(sfc_path):
        raise RuntimeError(f"AERMET 沒有產生 met.sfc（代碼 {p.returncode}）\n"
                           f"{p.stdout[-2000:]}\n{p.stderr[-2000:]}{msg}")
    with open(sfc_path) as f:
        lines = f.read().splitlines()
    header, data = lines[0], lines[1:]
    key = (d1.year % 100, d1.month, d1.day, hr1)
    sfc_line = next((ln for ln in data if tuple(int(v) for v in ln.split()[:3]) + (
        int(ln.split()[4]),) == key), None)
    if sfc_line is None:
        raise RuntimeError(f"AERMET 輸出中找不到 {d1:%Y/%m/%d} {hr1} 時\n{msg}")
    vals = sfc_line.split()
    if float(vals[6]) < -9 or float(vals[11]) < -99000 or float(vals[15]) < -9:
        raise RuntimeError(f"AERMET 該小時為缺值：{sfc_line}\n{msg}")
    with open(pfl_path) as f:
        pfl = [ln for ln in f.read().splitlines()
               if ln.split() and tuple(int(v) for v in ln.split()[:4]) == key]
    if not keep_dir:
        shutil.rmtree(work, ignore_errors=True)
    return {"sfc_header": header, "sfc_line": sfc_line, "pfl_lines": pfl, "messages": msg}


def test():
    """在 GitHub Actions 上測試：抓 Open-Meteo，跑 AERMET，印出結果與訊息。"""
    lat, lon = 25.06304, 121.60340
    target = datetime.now().replace(minute=0, second=0, microsecond=0)
    work = os.path.join(HERE, "aermet_test")
    try:
        res = run(lat, lon, target, keep_dir=work)
        print("AERMET 測試成功")
        print("SFC 標頭：", res["sfc_header"])
        print("目標小時：", res["sfc_line"])
        print("PFL：", *res["pfl_lines"], sep="\n  ")
    except Exception as e:  # noqa: BLE001
        print("AERMET 測試失敗：", e)
        for fn in ("aermet.inp", "os.dat", "ua.fsl"):
            fp = os.path.join(work, fn)
            if os.path.exists(fp):
                with open(fp) as f:
                    txt = f.read().splitlines()
                print(f"\n---- {fn}（前 25 行）----", *txt[:25], sep="\n")
        sys.exit(1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()

"""執行 EPA AERMOD 單一小時模擬：寫入 aermod.inp、SFC／PFL 氣象檔，執行並讀回 PLOTFILE。

排放率以 ng/s 輸入（填在 AERMOD 的 g/s 欄位），輸出數值即為 fg/m³：
  AERMOD 濃度[µg/m³] = Q[g/s] × 1e6 × χ/Q；若 Q 填 ng/s 的數值，結果 = 實際 ng/m³ × 1e6 = fg/m³。
這樣可避免極小數值在 PLOTFILE 的固定小數位數中被截成 0。

用法（冒煙測試，確認執行檔可用）：python aermod_run.py --smoke"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
AERMOD_EXE = os.environ.get("AERMOD_EXE", os.path.join(HERE, "bin", "aermod"))


@dataclass
class SfcHour:
    """AERMET 地面檔（SFC）一小時的邊界層參數。"""
    year: int
    month: int
    day: int
    hour: int            # 1～24（AERMET 慣例：該小時結束時刻）
    H: float             # 可感熱通量 W/m²
    ustar: float         # 摩擦速度 m/s
    wstar: float         # 對流速度尺度 m/s（穩定時 -9）
    vptg: float          # 混合層頂上方位溫梯度 K/m
    zic: float           # 對流混合層高度 m（穩定時 -999）
    zim: float           # 機械混合層高度 m
    L: float             # Monin-Obukhov 長度 m
    z0: float            # 地表粗糙度 m
    bowen: float
    albedo: float
    ws: float            # 參考高度風速 m/s
    wd: float            # 風向（從哪來）
    zref: float          # 風速參考高度 m
    temp_k: float        # 氣溫 K
    ztemp: float         # 氣溫量測高度 m

    def line(self) -> str:
        jday = (np.datetime64(f"{self.year:04d}-{self.month:02d}-{self.day:02d}")
                - np.datetime64(f"{self.year:04d}-01-01")).astype(int) + 1
        return (f"{self.year % 100:2d} {self.month:2d} {self.day:2d} {jday:3d} {self.hour:2d} "
                f"{self.H:6.1f} {self.ustar:6.3f} {self.wstar:6.3f} {self.vptg:6.3f} "
                f"{self.zic:5.0f} {self.zim:5.0f} {self.L:8.1f} {self.z0:7.4f} {self.bowen:6.2f} "
                f"{self.albedo:6.2f} {self.ws:7.2f} {self.wd:6.1f} {self.zref:6.1f} "
                f"{self.temp_k:6.1f} {self.ztemp:6.1f}")


@dataclass
class Stack:
    q_ng_s: float        # 排放率 ng I-TEQ/s
    hs: float            # 煙囪高度 m
    ts_k: float          # 排氣溫度 K
    vs: float            # 出口流速 m/s
    d: float             # 出口內徑 m
    base_elev: float = 0.0


@dataclass
class Grid:
    x0: float            # 格點左下角（相對煙囪，m）
    y0: float
    nx: int
    ny: int
    dx: float
    dy: float
    discrete: list = field(default_factory=list)   # 敏感點 [(x, y)]


def write_inputs(work: str, stack: Stack, sfc: SfcHour, grid: Grid, lat: float, lon: float,
                 urban_pop: float | None = None, title: str = "CEMS bot"):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    head = f"{abs(lat):9.3f}{ns}{abs(lon):9.3f}{ew}"
    with open(os.path.join(work, "met.sfc"), "w") as f:
        f.write(f"{head}          UA_ID:    99999  SF_ID:    99999  OS_ID:    99999"
                f"     VERSION: 24142  CEMSBOT\n")
        f.write(sfc.line() + "\n")
    with open(os.path.join(work, "met.pfl"), "w") as f:
        f.write(f"{sfc.year % 100:2d} {sfc.month:2d} {sfc.day:2d} {sfc.hour:2d} "
                f"{sfc.zref:7.1f} 1 {sfc.wd:7.1f} {sfc.ws:7.2f} {sfc.temp_k - 273.15:7.1f} "
                f"{-99.0:7.1f} {-99.0:7.2f}\n")
    urban_co = f"   URBANOPT  {urban_pop:.0f}\n" if urban_pop else ""
    urban_so = "   URBANSRC  STK1\n" if urban_pop else ""
    disc = "".join(f"   DISCCART  {x:.1f} {y:.1f}\n" for x, y in grid.discrete)
    inp = f"""CO STARTING
   TITLEONE  {title[:60]}
   MODELOPT  DFAULT CONC
   AVERTIME  1
   POLLUTID  OTHER
{urban_co}   RUNORNOT  RUN
CO FINISHED

SO STARTING
   LOCATION  STK1 POINT 0.0 0.0 {stack.base_elev:.1f}
   SRCPARAM  STK1 {stack.q_ng_s:.6g} {stack.hs:.2f} {stack.ts_k:.2f} {stack.vs:.2f} {stack.d:.3f}
{urban_so}   SRCGROUP  ALL
SO FINISHED

RE STARTING
   GRIDCART  G1 STA
             G1 XYINC {grid.x0:.1f} {grid.nx} {grid.dx:.1f} {grid.y0:.1f} {grid.ny} {grid.dy:.1f}
             G1 END
{disc}RE FINISHED

ME STARTING
   SURFFILE  met.sfc
   PROFFILE  met.pfl
   SURFDATA  99999 {sfc.year}
   UAIRDATA  99999 {sfc.year}
   PROFBASE  {stack.base_elev:.1f} METERS
ME FINISHED

OU STARTING
   RECTABLE  ALLAVE FIRST
   PLOTFILE  1 ALL FIRST plot.out
OU FINISHED
"""
    with open(os.path.join(work, "aermod.inp"), "w") as f:
        f.write(inp)


def run(stack: Stack, sfc: SfcHour, grid: Grid, lat: float, lon: float,
        urban_pop: float | None = None, keep_dir: str | None = None) -> dict:
    """回傳 {"X","Y","C"（fg/m³，2D 陣列）,"discrete":[濃度…], "log": 尾段訊息}。"""
    if not os.path.exists(AERMOD_EXE):
        raise FileNotFoundError(f"找不到 AERMOD 執行檔：{AERMOD_EXE}")
    work = keep_dir or tempfile.mkdtemp(prefix="aermod_")
    os.makedirs(work, exist_ok=True)
    write_inputs(work, stack, sfc, grid, lat, lon, urban_pop)
    p = subprocess.run([AERMOD_EXE], cwd=work, capture_output=True, text=True, timeout=600)
    out_txt = ""
    if os.path.exists(os.path.join(work, "aermod.out")):
        with open(os.path.join(work, "aermod.out"), errors="replace") as f:
            out_txt = f.read()
    plot = os.path.join(work, "plot.out")
    if p.returncode != 0 or not os.path.exists(plot) or "Finishes Successfully" not in (
            p.stdout + out_txt):
        msgs = [ln for ln in out_txt.splitlines() if " E" in ln[:8] or "ERROR" in ln.upper()]
        raise RuntimeError("AERMOD 執行失敗：\n" + "\n".join(msgs[-15:] or out_txt.splitlines()[-30:]))
    xs, ys, cs = [], [], []
    with open(plot) as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("*"):
                continue
            v = s.replace("D", "E").split()
            try:
                xs.append(float(v[0]))
                ys.append(float(v[1]))
                cs.append(float(v[2]))
            except (ValueError, IndexError):
                continue
    xs, ys, cs = np.array(xs), np.array(ys), np.array(cs)
    n_grid = grid.nx * grid.ny
    if len(xs) < n_grid:
        # 讀不到格點資料：印出輸出檔內容，方便判斷是哪個版本差異造成
        with open(plot, errors="replace") as f:
            head = f.read().splitlines()[:25]
        warn = [ln for ln in out_txt.splitlines()
                if len(ln) > 8 and ln[1:3].isalpha() and ln[4] in "EWI" and ln[5:8].isdigit()]
        raise RuntimeError(
            f"PLOTFILE 只讀到 {len(xs)} 筆，預期 {n_grid} 筆\n"
            "---- plot.out 前 25 行 ----\n" + "\n".join(head) +
            "\n---- aermod.out 錯誤／警告訊息 ----\n" + "\n".join(dict.fromkeys(warn)))
    gx, gy, gc = xs[:n_grid], ys[:n_grid], cs[:n_grid]
    order = np.lexsort((gx, gy))           # 依 y、再依 x 排序成 2D
    X = gx[order].reshape(grid.ny, grid.nx)
    Y = gy[order].reshape(grid.ny, grid.nx)
    C = gc[order].reshape(grid.ny, grid.nx)
    res = {"X": X, "Y": Y, "C": C, "discrete": list(cs[n_grid:]),
           "log": "\n".join(out_txt.splitlines()[-5:])}
    if not keep_dir:
        shutil.rmtree(work, ignore_errors=True)
    return res


def smoke():
    """冒煙測試：用固定的邊界層參數跑一小時，確認執行檔能正常運作。"""
    sfc = SfcHour(2026, 11, 16, 14, H=150.0, ustar=0.45, wstar=1.8, vptg=0.005, zic=900,
                  zim=600, L=-60.0, z0=1.0, bowen=1.5, albedo=0.18, ws=3.0, wd=80.0,
                  zref=10.0, temp_k=295.0, ztemp=2.0)
    stk = Stack(q_ng_s=108.2, hs=74.0, ts_k=439.0, vs=13.77, d=1.25)
    grid = Grid(-5000, -5000, 51, 51, 200, 200, discrete=[(-950, -95)])
    res = run(stk, sfc, grid, 25.063, 121.603, urban_pop=2600000,
              keep_dir=os.path.join(HERE, "aermod_smoke"))
    i = np.unravel_index(np.argmax(res["C"]), res["C"].shape)
    print("AERMOD 冒煙測試成功")
    print(f"  最大 1 小時濃度 {res['C'][i]:.1f} fg/m³，位置 ({res['X'][i]:.0f}, {res['Y'][i]:.0f}) m")
    print(f"  敏感點（西方約 1 km）濃度 {res['discrete'][0]:.1f} fg/m³")
    print(res["log"])


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        smoke()

"""備援做法 B：依 AERMET 的公式，用 Python 直接計算單一小時的邊界層參數（SFC 一行）。
AERMET 執行失敗或資料不足時使用，確保每小時都有 AERMOD 結果。

白天（可感熱通量 > 0）：淨輻射 → 可感熱通量 → 疊代求摩擦速度與 Monin-Obukhov 長度；
  對流混合層高度取數值模式的邊界層高度（AERMET 正式做法是由清晨探空逐時積分）。
夜間：Venkatram 法，溫度尺度 θ* 由雲量估計。機械混合層高度 zim＝2300 u*^1.5（AERMET 公式）。"""
from __future__ import annotations

import math

from aermod_run import SfcHour

K, G, SIGMA, RHO_CP = 0.4, 9.81, 5.67e-8, 1.2 * 1004.0


def _psi_m_unstable(z_over_L: float) -> float:
    mu = (1 - 16 * z_over_L) ** 0.25
    return (2 * math.log((1 + mu) / 2) + math.log((1 + mu * mu) / 2)
            - 2 * math.atan(mu) + math.pi / 2)


def compute(when, ws, wd, temp_c, cloud_frac, sw_rad, blh, sun_alt, rh=70.0, pres=1013.0,
            zref=10.0, z0=1.0, bowen=1.5, albedo=0.18) -> SfcHour:
    T = temp_c + 273.15
    u = max(ws, 0.5)
    n = min(max(cloud_frac, 0.0), 1.0)
    # 淨輻射（Holtslag & van Ulden，AERMET 使用的形式），反照率依太陽仰角修正
    r = albedo + (1 - albedo) * math.exp(-0.1 * max(sun_alt, 0) - 0.5 * (1 - albedo) ** 2)
    rn = ((1 - r) * max(sw_rad, 0) + 5.31e-13 * T ** 6 - SIGMA * T ** 4 + 60 * n) / 1.12
    H = 0.9 * rn / (1 + 1 / bowen) if sun_alt > 0 else -1.0

    if H > 0:  # 對流邊界層
        ustar = K * u / math.log(zref / z0)
        L = -999.0
        for _ in range(30):
            L = -RHO_CP * T * ustar ** 3 / (K * G * H)
            new = K * u / (math.log(zref / z0) - _psi_m_unstable(zref / L)
                           + _psi_m_unstable(z0 / L))
            if abs(new - ustar) < 1e-4:
                ustar = new
                break
            ustar = new
        L = -RHO_CP * T * ustar ** 3 / (K * G * H)
        zic = max(blh or 0, 100.0)
        wstar = (G * H * zic / (RHO_CP * T)) ** (1 / 3)
        zim = 2300 * ustar ** 1.5
        return SfcHour(when.year, when.month, when.day, when.hour or 24, H, ustar, wstar, 0.005,
                       zic, zim, L, z0, bowen, albedo, ws, wd, zref, T, 2.0,
                       rh=rh, pres=pres, ccvr=int(round(n * 10)))

    # 穩定邊界層（Venkatram）
    theta_star = 0.09 * (1 - 0.5 * n * n)
    cdn = K / math.log(zref / z0)
    u0 = math.sqrt(4.7 * zref * G * theta_star / T)
    disc = 1 - (2 * u0 / (math.sqrt(cdn) * u)) ** 2
    ustar = cdn * u / 2 * (1 + math.sqrt(max(disc, 0.0)))
    if disc < 0:  # 低風速：依 AERMET 做法線性降低 θ*
        theta_star *= u / (2 * u0 / math.sqrt(cdn))
    L = T * ustar ** 2 / (K * G * theta_star)
    H = -RHO_CP * ustar * theta_star
    zim = 2300 * ustar ** 1.5
    return SfcHour(when.year, when.month, when.day, when.hour or 24, H, ustar, -9.0, 0.005,
                   -999.0, zim, L, z0, bowen, albedo, ws, wd, zref, T, 2.0,
                   rh=rh, pres=pres, ccvr=int(round(n * 10)))

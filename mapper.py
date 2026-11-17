"""把濃度場畫成 PNG，疊網路地圖底圖（Web Mercator 圖磚）。

圖框可固定：config.yaml 的 map.center 指定位於圖中央的敏感點，map.anchor 指定另一個敏感點
在圖框中的相對位置（x_frac、y_frac，0=左／下緣，1=右／上緣）。兩點即可決定圖框範圍。"""
from __future__ import annotations

import io
import logging
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from PIL import Image  # noqa: E402

from plume import compass, xy_to_lonlat  # noqa: E402

log = logging.getLogger(__name__)
plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC",
                                   "Heiti TC", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

R = 6378137.0
TILE_SOURCES = {
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "© OpenStreetMap contributors"),
    # CARTO 自 2026/8 底起要求 API 金鑰；沒有金鑰的圖磚會被印上「API KEY REQUIRED」浮水印
    "carto_light": ("https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png",
                    "© OpenStreetMap contributors © CARTO"),
}
import os  # noqa: E402
import time  # noqa: E402
# OSM 圖磚政策要求可辨識的 User-Agent；在 GitHub Actions 上自動帶入 repo 網址
_repo = os.environ.get("GITHUB_REPOSITORY", "")
UA = {"User-Agent": f"incinerator-cems-bot/1.0 (+https://github.com/{_repo})" if _repo
      else "incinerator-cems-bot/1.0"}


# 濃度色階：淡黃 → 橘 → 紅 → 深紫（最高一級固定為深紫色）
COLOR_STOPS = ["#ffffb2", "#fecc5c", "#fd8d3c", "#e31a1c", "#99004c"]
TOP_COLOR = "#3f007d"  # 深紫


def level_cmap(n_bands: int, stops=None, top=None):
    stops, top = stops or COLOR_STOPS, top or TOP_COLOR
    if n_bands == 1:
        return ListedColormap([top])
    base = LinearSegmentedColormap.from_list("lv", stops)
    cols = [base(i / max(1, n_bands - 2)) for i in range(n_bands - 1)] + [top]
    return ListedColormap(cols)


# ---------- 座標轉換 ----------
def to_merc(lon, lat):
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    return R * np.radians(lon), R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def from_merc(x, y):
    return np.degrees(x / R), np.degrees(2 * np.arctan(np.exp(y / R)) - np.pi / 2)


# ---------- 底圖 ----------
def _needs_key(source: str) -> bool:
    return source == "carto_light"


def fetch_tile(url: str, key: str = "", tries: int = 3) -> Image.Image:
    """抓單張圖磚。逾時／連線失敗／5xx 會重試；4xx（被封鎖、金鑰錯誤）不重試。"""
    full = f"{url}?key={key}" if key else url
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(full, headers=UA, timeout=20)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        except requests.HTTPError as e:
            if "HTTP 5" not in str(e):
                raise
            last = e
        if i < tries - 1:
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def basemap(ax, x0, x1, y0, y1, source="osm", px_width=1000, fade=0.25) -> str:
    """抓圖磚拼接後貼到 ax（座標為 Web Mercator 公尺）。回傳版權標示文字。"""
    url_t, attrib = TILE_SOURCES[source]
    key = os.environ.get("CARTO_API_KEY", "").strip() if _needs_key(source) else ""
    res = (x1 - x0) / px_width
    z = int(min(18, max(10, math.ceil(math.log2(2 * math.pi * R / 256 / res)))))
    n = 2 ** z
    tile_m = 2 * math.pi * R / n

    def tx(x): return int((x + math.pi * R) // tile_m)
    def ty(y): return int((math.pi * R - y) // tile_m)

    xs, ys = range(tx(x0), tx(x1) + 1), range(ty(y1), ty(y0) + 1)
    canvas = Image.new("RGB", (256 * len(xs), 256 * len(ys)), "white")
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            canvas.paste(fetch_tile(url_t.format(z=z, x=x % n, y=y), key=key),
                         (256 * i, 256 * j))
    img = np.asarray(canvas, float) / 255
    img = img * (1 - fade) + fade  # 底圖調淡，讓濃度色塊看得清楚
    left = xs[0] * tile_m - math.pi * R
    top = math.pi * R - ys[0] * tile_m
    ax.imshow(img, extent=(left, left + len(xs) * tile_m, top - len(ys) * tile_m, top),
              origin="upper", interpolation="bilinear", zorder=0)
    return attrib


# ---------- 圖框 ----------
def fixed_frame(map_cfg: dict, receptors: list[dict]):
    """依 center／anchor 兩個敏感點算出圖框（Mercator）。設定不全時回傳 None。"""
    if not map_cfg or not map_cfg.get("center") or not map_cfg.get("anchor"):
        return None
    byname = {r["name"]: r for r in receptors}
    c, a = byname.get(map_cfg["center"]), byname.get(map_cfg["anchor"].get("name"))
    if not c or not a:
        log.warning("map.center／anchor 名稱找不到對應的 receptors，改用自動範圍")
        return None
    fx, fy = map_cfg["anchor"]["x_frac"], map_cfg["anchor"]["y_frac"]
    cx, cy = to_merc(c["lon"], c["lat"])
    ax_, ay_ = to_merc(a["lon"], a["lat"])
    # anchor 位置 = 0.5 - 距離/(2×半寬) → 半寬 = 距離 / (2×|0.5 - frac|)
    hw = abs(float(cx - ax_)) / (2 * abs(0.5 - fx))
    hh = abs(float(cy - ay_)) / (2 * abs(0.5 - fy))
    # 檢查方向：anchor 在中心左側時 x_frac 必須 < 0.5，反之亦然
    if (ax_ < cx) != (fx < 0.5) or (ay_ < cy) != (fy < 0.5):
        log.warning("anchor 的 x_frac／y_frac 與實際方位相反，圖框仍以距離計算")
    return float(cx) - hw, float(cx) + hw, float(cy) - hh, float(cy) + hh


def auto_frame(points_merc, pad=1500.0):
    xs, ys = zip(*points_merc)
    return min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad


def inside(x, y, fr):
    return fr[0] <= x <= fr[1] and fr[2] <= y <= fr[3]


def edge_marker(ax, x, y, fr, label):
    """點在圖框外時，在框緣畫箭頭指向它。"""
    cx, cy = (fr[0] + fr[1]) / 2, (fr[2] + fr[3]) / 2
    dx, dy = x - cx, y - cy
    s = min((fr[1] - cx) / abs(dx) if dx else 1e9, (fr[3] - cy) / abs(dy) if dy else 1e9) * 0.9
    ex, ey = cx + dx * s, cy + dy * s
    ax.annotate(label, xy=(ex, ey), xytext=(cx + dx * s * 0.78, cy + dy * s * 0.78),
                arrowprops=dict(arrowstyle="-|>", color="black", lw=1.8), fontsize=9,
                ha="center", va="center", zorder=6,
                bbox=dict(fc="white", ec="black", lw=0.6, alpha=0.85))


STAB_DESC = {"A": "極不穩定", "B": "不穩定", "C": "稍不穩定", "D": "中性", "E": "稍穩定", "F": "穩定"}


def _stab(res: dict) -> str:
    """說明框中的大氣穩定度一行（結尾含換行）。"""
    if res.get("model") == "AERMOD":
        zic = f"{res['zic']:.0f} m" if res.get("zic", -999) > 0 else "無（穩定）"
        state = "對流（不穩定）" if res.get("L", 0) < 0 else "穩定"
        return (f"擴散模式：EPA AERMOD｜氣象前處理：{res.get('met_method', '')}\n"
                f"邊界層：{state}，Monin-Obukhov 長度 {res['L']:.0f} m，"
                f"摩擦速度 {res['ustar']:.2f} m/s，對流混合層 {zic}，機械混合層 {res['zim']:.0f} m\n"
                f"氣象資料：Open-Meteo 模式值（雲量、日射、高空剖面），風與氣溫為實測\n"
                f"地形：{res.get('terrain_note', '未納入')}\n")
    cls = res.get("cls")
    if not cls:
        return ""
    alt = res.get("sun_alt")
    when = "" if alt is None else (f"，太陽仰角 {alt:.0f}°" + ("，夜間" if alt <= 0 else ""))
    line = f"大氣穩定度：{cls}（{STAB_DESC.get(cls, '')}{when}）\n"
    if res.get("Z") is not None:
        line += (f"地形：已納入（AWS Terrain Tiles／SRTM），煙囪基地標高 {res['stack_base_m']:.0f} m，"
                 f"水平狀態權重 {res['f_horizontal']:.1f}\n")
    else:
        line += "地形：未納入（平坦地形）\n"
    return line


# ---------- 主繪圖 ----------
def draw(res: dict, fac: dict, wind: dict, levels: list[float], title: str, out_png: str,
         unit_label: str = "fg I-TEQ/m³", receptors: list[dict] | None = None,
         map_cfg: dict | None = None) -> str:
    receptors, map_cfg = receptors or [], map_cfg or {}
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    lon, lat = xy_to_lonlat(res["X"], res["Y"], lat0, lon0)
    MX, MY = to_merc(lon, lat)
    C = res["C"]
    sx, sy = (float(v) for v in to_merc(lon0, lat0))

    fr = fixed_frame(map_cfg, receptors)
    if fr is None:
        pts = [(sx, sy)] + [tuple(float(v) for v in to_merc(r["lon"], r["lat"])) for r in receptors]
        m = C >= min(levels)
        if m.any():
            pts += [(float(MX[m].min()), float(MY[m].min())), (float(MX[m].max()), float(MY[m].max()))]
        fr = auto_frame(pts)

    # 風向測站一定要在圖內：以圖框中心（環興公司）為準等比例放大，維持置中與長寬比
    if wind and map_cfg.get("include_wind_stations", True):
        cx, cy = (fr[0] + fr[1]) / 2, (fr[2] + fr[3]) / 2
        hw, hh = (fr[1] - fr[0]) / 2, (fr[3] - fr[2]) / 2
        pad = map_cfg.get("station_margin_frac", 0.08)  # 測站離框緣至少留 8%
        k = 1.0
        for st in wind.get("members") or [wind]:
            px, py = (float(v) for v in to_merc(st["lon"], st["lat"]))
            k = max(k, abs(px - cx) / (hw * (1 - pad)), abs(py - cy) / (hh * (1 - pad)))
        if k > 1:
            fr = (cx - hw * k, cx + hw * k, cy - hh * k, cy + hh * k)
            log.info("為納入風向測站，圖框放大 %.2f 倍", k)

    w_m, h_m = fr[1] - fr[0], fr[3] - fr[2]
    fig_w = 10.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_w * 0.82 * h_m / w_m + 1.0), dpi=110)
    ax.set_xlim(fr[0], fr[1])
    ax.set_ylim(fr[2], fr[3])
    ax.set_aspect("equal")

    attrib = ""
    src = map_cfg.get("basemap", "osm")
    if src != "none":
        # 指定來源失敗時自動改用另一個來源
        for s in [src] + [k for k in TILE_SOURCES if k != src]:
            if _needs_key(s) and not os.environ.get("CARTO_API_KEY", "").strip():
                log.warning("底圖 %s 需要 CARTO_API_KEY（未設定），略過", s)
                continue
            try:
                attrib = basemap(ax, *fr, source=s, px_width=fig_w * 110,
                                 fade=map_cfg.get("basemap_fade", 0.25))
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                k = os.environ.get("CARTO_API_KEY", "").strip()
                log.warning("底圖 %s 載入失敗：%s", s, msg.replace(k, "***") if k else msg)
        if not attrib:
            log.warning("所有底圖來源都失敗，改為無底圖")
    if not attrib:
        ax.grid(alpha=0.3)

    lv = sorted(levels) + [max(float(C.max()), max(levels)) * 1.01]
    cmap = level_cmap(len(lv) - 1, map_cfg.get("color_stops"), map_cfg.get("top_color"))
    norm = BoundaryNorm(lv, cmap.N)
    cf = ax.contourf(MX, MY, C, levels=lv, cmap=cmap, norm=norm,
                     alpha=map_cfg.get("fill_alpha", 0.6), zorder=2)
    # 最高一級另外疊一層、不透明度較高，確保呈現深紫色
    ax.contourf(MX, MY, C, levels=lv[-2:], colors=[cmap(cmap.N - 1)],
                alpha=map_cfg.get("top_alpha", 0.85), zorder=2.5)
    ax.contour(MX, MY, C, levels=sorted(levels), colors="dimgray", linewidths=0.7, zorder=3)
    Z = res.get("Z")
    if res.get("ZX") is not None and Z is not None:  # 地形網格與濃度網格不同（AERMOD）
        zlon, zlat = xy_to_lonlat(res["ZX"], res["ZY"], lat0, lon0)
        ZMX, ZMY = to_merc(zlon, zlat)
    else:
        ZMX, ZMY = MX, MY
    if Z is not None and map_cfg.get("terrain_contours", True) and float(np.nanmax(Z)) > 50:
        step = map_cfg.get("terrain_contour_step_m", 50)
        zl = np.arange(step, float(np.nanmax(Z)) + step, step)
        cs = ax.contour(ZMX, ZMY, Z, levels=zl, colors="saddlebrown", linewidths=0.5, alpha=0.6,
                        zorder=2.7)
        ax.clabel(cs, levels=zl[1::2] if len(zl) > 4 else zl, fontsize=6, fmt="%d m",
                  inline=True)
    from matplotlib.cm import ScalarMappable
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax, shrink=0.75, pad=0.02)
    cb.set_ticks(sorted(levels))
    cb.set_label(f"地面濃度（{unit_label}，1 小時）")

    # 煙囪
    if inside(sx, sy, fr):
        ax.plot(sx, sy, marker="^", color="black", ms=12, zorder=6, ls="none", label=fac["name"])
    else:
        c0 = receptors[0] if receptors else None
        lab = fac["name"] + "（圖框外）"
        if c0:
            from wind import haversine_km
            dkm = haversine_km(lat0, lon0, c0["lat"], c0["lon"])
            lab = f"{fac['name']}\n距{c0['name']} {dkm:.1f} km"
        edge_marker(ax, sx, sy, fr, lab)

    # 測站（合成風場時畫出每一站）
    if wind:
        first = True
        for st in wind.get("members") or [wind]:
            wx, wy = (float(v) for v in to_merc(st["lon"], st["lat"]))
            if inside(wx, wy, fr):
                ax.plot(wx, wy, marker="o", color="blue", ms=8, zorder=6, ls="none",
                        label="風向測站" if first else None)
                fx = (wx - fr[0]) / (fr[1] - fr[0])
                fy = (wy - fr[2]) / (fr[3] - fr[2])
                right = fx > 0.7                      # 靠右 → 標籤放左邊，避免超出圖框被色條擋住
                low = fy < 0.2                        # 靠下 → 標籤放上方
                ax.annotate(st.get("site", st["name"]), (wx, wy),
                            xytext=(-7 if right else 7, 7 if low else -7),
                            textcoords="offset points", fontsize=8, color="blue", zorder=7,
                            ha="right" if right else "left", va="bottom" if low else "top",
                            bbox=dict(fc="white", ec="none", alpha=0.75, pad=1))
                first = False

    # 敏感點
    for k, rp in enumerate(receptors):
        rx, ry = (float(v) for v in to_merc(rp["lon"], rp["lat"]))
        ax.plot(rx, ry, marker="s", color="green", mec="white", ms=10, zorder=7, ls="none",
                label="敏感點" if k == 0 else None)
        ax.annotate(rp["name"], (rx, ry), xytext=(7, 7), textcoords="offset points", fontsize=10,
                    color="darkgreen", zorder=7,
                    bbox=dict(fc="white", ec="green", lw=0.6, alpha=0.9))

    # 最大著地點
    mlon, mlat = xy_to_lonlat(*res["max_xy"], lat0, lon0)
    mx, my = (float(v) for v in to_merc(mlon, mlat))
    if inside(mx, my, fr):
        ax.plot(mx, my, marker="*", color="magenta", mec="white", ms=16, zorder=7, ls="none",
                label="估算最大著地點")

    # 風向箭頭與風資料說明（右上角，箭頭指向下風）
    if wind:
        to = math.radians((wind["wd"] + 180) % 360)
        ax.annotate("", xy=(0.88 + 0.05 * math.sin(to), 0.92 + 0.05 * math.cos(to)),
                    xytext=(0.88 - 0.05 * math.sin(to), 0.92 - 0.05 * math.cos(to)),
                    xycoords="axes fraction",
                    arrowprops=dict(arrowstyle="-|>", lw=2.5, color="blue"), zorder=8)
        if wind.get("members"):
            sites = "\n".join(f"　{s.get('site', s['name'])}：距煙囪 {s['dist_km']:.1f} km，"
                              f"風從 {s['wd']:.0f}° {s['ws']:.1f} m/s，權重 {w:.0%}"
                              for s, w in zip(wind["members"], wind["weights"]))
            info = (f"合成風：從 {wind['wd']:.0f}°（{compass(wind['wd'])}）來，{wind['ws']:.1f} m/s\n"
                    f"資料來源：{wind.get('src_label', '')}\n"
                    f"測站：\n{sites}\n"
                    f"風向一致性：{wind['consistency']:.2f}（1＝各站完全一致）\n"
                    f"{_stab(res)}"
                    f"觀測時間：{wind['time']}")
        else:
            info = (f"風從 {wind['wd']:.0f}°（{compass(wind['wd'])}）來，{wind['ws']:.1f} m/s\n"
                    f"資料來源：{wind.get('src_label', '')}\n"
                    f"測站：{wind.get('site', wind['name'])}\n"
                    f"測站距焚化廠煙囪：{wind['dist_km']:.1f} km\n"
                    f"{_stab(res)}"
                    f"觀測時間：{wind['time']}")
        wind_info = info

    # 煙流沒進入圖框時提示
    inframe = (MX >= fr[0]) & (MX <= fr[1]) & (MY >= fr[2]) & (MY <= fr[3]) & (C >= min(levels))
    if not inframe.any():
        ax.text(0.5, 0.30, f"本小時估算煙流（≥{min(levels):g} {unit_label}）\n未進入圖框範圍",
                transform=ax.transAxes, ha="center", va="center", fontsize=12, color="dimgray",
                zorder=8, bbox=dict(fc="white", ec="gray", alpha=0.85))

    # 座標軸顯示經緯度
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{from_merc(v, 0)[0]:.3f}°E"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{from_merc(0, v)[1]:.3f}°N"))
    ax.tick_params(labelsize=8)
    ax.set_title(title, fontsize=11)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06), ncol=4, fontsize=8, frameon=False)
    if res.get("model") == "AERMOD":
        note = "AERMOD 即時估算（非法規正式模擬）；排放濃度為假設值"
    else:
        note = "高斯煙流篩選估算（非 AERMOD 正式模擬）；排放濃度為假設值"
        if res.get("model_note"):
            note = res["model_note"] + "\n" + note
    if attrib:
        note += f"　底圖 {attrib}"
    info_txt = (locals().get("wind_info") or "") 
    ax.text(0, -0.12, (info_txt + "\n" if info_txt else "") + note, transform=ax.transAxes,
            fontsize=8, color="navy", va="top", ha="left", linespacing=1.45,
            bbox=dict(fc="white", ec="navy", lw=0.6, boxstyle="round,pad=0.5"))
    fig.savefig(out_png, bbox_inches="tight")
    plt.close(fig)
    return out_png


def make_preview(png: str, out: str, max_px: int = 240) -> str:
    """LINE 預覽圖需較小（1MB 以內）。"""
    img = Image.open(png)
    img.thumbnail((max_px, max_px))
    img.save(out)
    return out

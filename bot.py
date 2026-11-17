"""焚化廠起爐監控機器人：每小時執行一次（cron-job.org → GitHub Actions）。

通報規則（每次起爐事件）：
  1. 起爐後第一次發送（在發送時段內）；若此時環興公司已在下風處，之後不再警告
  2. 第一次發送時不在下風處 → 起爐期間轉為下風時，再發一次警告（僅一次）
  3. 起爐結束時發結束通知；整段起爐都在夜間時，隔天早上補發夜間紀錄
  4. 只在 notify.window_start～window_end 發送，時段外的通知延到下一個發送時段

用法：
  python bot.py                 正式執行
  python bot.py --dry-run       真資料，但不發 LINE、不存狀態
  python bot.py --force         把現在當成起爐，發一則測試訊息給管理者個人（LINE_ADMIN_TO）
  python bot.py --force-group   同上，但直接發到群組（LINE_TO），測群組是否收得到
  python bot.py --demo          跑 tests/ 內所有起爐情境，印出每小時會發什麼（不連網）
  python bot.py --scenario tests/xxx.yaml [--maps]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from datetime import datetime, timedelta

import yaml

import cems
import forecast
import line_push
import mapper
import plume
from plume import compass
import wind as windmod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
HERE = os.path.dirname(os.path.abspath(__file__))

FAC_DEFAULTS = {"stack_height_m": 150, "stack_diameter_m": 2.5, "default_exit_temp_c": 150,
                "default_flow_nm3_per_hr": 100000, "default_exit_velocity_m_s": 15,
                "epb_county": "", "cno": "", "abbr_keywords": []}


def facilities_of(cfg):
    """新格式 facilities: [...]；相容舊格式 facility: {...}。"""
    facs = cfg.get("facilities") or [cfg["facility"]]
    return [{**FAC_DEFAULTS, **f} for f in facs]


def apply_env(cfg):
    """機密資料可由環境變數（GitHub Secrets）覆寫，config.yaml 就不必放金鑰。"""
    env = os.environ.get
    cfg.setdefault("line", {})
    cfg.setdefault("image_host", {})
    for var, d, key in [("MOENV_API_KEY", cfg, "moenv_api_key"),
                        ("CWA_API_KEY", cfg, "cwa_api_key"),
                        ("LINE_CHANNEL_ACCESS_TOKEN", cfg["line"], "channel_access_token"),
                        ("LINE_TO", cfg["line"], "to"),
                        ("LINE_ADMIN_TO", cfg["line"], "admin_to"),
                        ("IMGUR_CLIENT_ID", cfg["image_host"], "imgur_client_id")]:
        if env(var):
            d[key] = env(var)


def build_source(ev_list, fac, cfg):
    """同廠各管道合併成同一點源（等效直徑＝總截面積相同）。"""
    dx = cfg["dioxin"]
    q_total, temps, flows, measured = 0.0, [], [], False
    for ev in ev_list:
        if ev["startup"]:
            conc = dx["startup_conc_ng_iteq_nm3"]
        elif ev["shutdown"]:
            conc = dx.get("shutdown_conc_ng_iteq_nm3", dx["startup_conc_ng_iteq_nm3"])
        elif ev["fault"]:
            conc = dx.get("fault_conc_ng_iteq_nm3", dx["startup_conc_ng_iteq_nm3"])
        else:
            conc = dx["normal_conc_ng_iteq_nm3"]
        f_cems = cems.flow_to_nm3_s(ev["flow"]) if ev.get("polno") != "P" else None
        measured = measured or bool(f_cems)
        f = f_cems or cems.flow_to_nm3_s(ev["flow"]) or fac["default_flow_nm3_per_hr"] / 3600
        q_total += conc * f                    # ng I-TEQ/s
        flows.append(f)
        temps.append(ev["temp_c"] or fac["default_exit_temp_c"])
    ts = sum(temps) / len(temps)
    d_eq = fac["stack_diameter_m"] * math.sqrt(len(ev_list))
    q_act = sum(flows) * (ts + 273.15) / 273.15
    vs = q_act / (math.pi * d_eq ** 2 / 4)
    # 沒有 CEMS 實測流率時，直接採用設定檔的出口流速（例如年均實測值）
    if not measured and fac.get("default_exit_velocity_m_s"):
        vs = fac["default_exit_velocity_m_s"]
    if not (1 <= vs <= 40):
        vs = fac["default_exit_velocity_m_s"]
    return plume.Source(q_g_s=q_total, hs=fac["stack_height_m"], d=d_eq, vs=vs, ts_c=ts), q_total


# ======================== 狀態 ========================
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("events", {})
    st.setdefault("pending", {})
    return st


def save_state(path, st):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, default=str)


# ======================== 判斷工具 ========================
def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_window(now, cfg):
    n = cfg.get("notify", {})
    t = now.hour * 60 + now.minute
    return _hm(n.get("window_start", "07:00")) <= t <= _hm(n.get("window_end", "20:00"))


def receptor_by_name(cfg, name):
    return next((r for r in cfg.get("receptors", []) if r["name"] == name), None)


def downwind(fac, rec, wd, ws, cfg):
    """回傳 True／False，或 None（風速太小無法判斷）。
    判斷方式：敏感點方位落在煙流方向 ±half_angle 內，且距離不超過 max_km。"""
    n = cfg.get("notify", {})
    if ws is not None and ws < cfg["dispersion"]["min_wind_m_s"]:
        return None
    x, y = plume.lonlat_to_xy(rec["lon"], rec["lat"], fac["stack_lat"], fac["stack_lon"])
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    to = (wd + 180) % 360
    diff = abs((bearing - to + 180) % 360 - 180)
    return diff <= n.get("downwind_half_angle_deg", 22.5) and \
        math.hypot(x, y) / 1000 <= n.get("downwind_max_km", 10)


def rec_line(name, status, extra=""):
    if status is None:
        return f"{name}{extra}：本小時風速微弱，無法判斷下風方向"
    return f"{name}{extra}：本小時「{'在' if status else '不在'}」下風方向"


def day_word(t, now):
    d = (now.date() - t.date()).days
    return "今日" if d == 0 else "昨日" if d == 1 else f"{t:%m/%d}"


def forecast_change(fac, rec, cur, fc, now, cfg):
    """找未來 forecast_hours 內，敏感點下風狀態第一次改變的時間。"""
    hrs = cfg.get("notify", {}).get("forecast_hours", 12)
    for f in fc:
        dt = (f["time"] - now).total_seconds() / 3600
        if dt <= 0 or dt > hrs:
            continue
        s = downwind(fac, rec, f["wd"], None, cfg)
        if s != bool(cur):
            return f, s
    return None, None


# ======================== 訊息內容 ========================
FOOT = "※ 本訊息為模式估算，非實測結果"


PHRASE = {
    "startup": {"start": "有起爐作業", "ongoing": "起爐作業持續中", "end": "起爐作業已於{t}結束",
                "follow": "起爐結束時會再通知您", "night": "有起爐作業", "next": "起爐作業"},
    "shutdown": {"start": "有停爐作業", "ongoing": "停爐作業持續中", "end": "停爐作業已於{t}結束",
                 "follow": "停爐結束時會再通知您", "night": "有停爐作業", "next": "停爐作業"},
    "fault": {"start": "防制設備異常（正常運轉中）", "ongoing": "防制設備異常持續中",
              "end": "防制設備已於{t}恢復正常", "follow": "防制設備恢復正常時會再通知您",
              "night": "防制設備異常", "next": "防制設備異常狀態"},
}


def event_types(cfg):
    """(類型, 中文名稱, 運轉狀態碼, 是否發結束通知)"""
    n = cfg["notify"]
    types = [("startup", "起爐", n.get("event_codes", ["B", "C"]), True)]
    if n.get("notify_shutdown", True):
        types.append(("shutdown", "停爐", n.get("shutdown_codes", ["D", "E"]),
                      n.get("shutdown_end_notice", False)))
    if n.get("notify_fault", True):
        types.append(("fault", "防制設備異常", n.get("fault_codes", ["A"]),
                      n.get("fault_end_notice", True)))
    return types


def msg_event(fac, ev, now, statuses, fc, cfg, warning=False, etype="startup", send_end=True,
              extra_note=None):
    ph = PHRASE[etype]
    n = cfg["notify"]
    prim, extras = n["primary_receptor"], n.get("extra_receptors", [])
    start = datetime.fromisoformat(ev["start"])
    last = datetime.fromisoformat(ev["last_hour"])
    dur = int((last - start).total_seconds() // 3600)
    dur_txt = f"已持續 {dur} 小時" if dur > 0 else "剛開始"
    if warning:
        lines = [f"⚠️ {fac['name']}{ph['ongoing']}（{day_word(start, now)} {start:%H:%M} 起，{dur_txt}）"]
    else:
        lines = [f"{fac['name']}{day_word(start, now)} {start:%H:%M} 起{ph['start']}（{dur_txt}）"]
    if extra_note:
        lines.append(extra_note)
    lines.append(rec_line(prim, statuses.get(prim)))
    for e in extras:
        if e in statuses:
            lines.append(rec_line(e, statuses[e], "（補充）"))

    p_rec = receptor_by_name(cfg, prim)
    f, new_s = forecast_change(fac, p_rec, statuses.get(prim), fc, now, cfg) if fc and p_rec else (None, None)
    if f is not None:
        word = "可能吹向" if new_s else "可能不再吹向"
        lines.append(f"預計 {f['time']:%H:%M} 後風向轉{f['text']}，{word}{prim}")
    if statuses.get(prim):
        lines.append("建議：本小時戶外活動可考慮改至室內，辦公室可關窗、空調改內循環")
    elif f is not None and new_s:
        lines.append(f"建議：{f['time']:%H:%M} 後的戶外活動可考慮改至室內")
    if send_end:
        lines.append(ph["follow"])
    lines.append(FOOT)
    return "\n".join(lines)


def msg_end(fac, snap, now, cfg, etype="startup"):
    ph = PHRASE[etype]
    prim = cfg["notify"]["primary_receptor"]
    s, e = datetime.fromisoformat(snap["start"]), datetime.fromisoformat(snap["end"])
    lines = [f"✅ {fac['name']}" + ph["end"].format(t=f"{day_word(e, now)} {e:%H:%M} ")
             + f"（{day_word(s, now)} {s:%H:%M} 起，共 {snap['hours']:g} 小時）"
             + (f"，接著進入{PHRASE[snap['next']]['next']}" if snap.get("next") else ""),
             f"期間{prim}位於下風方向共 {snap['dw_hours']} 小時", FOOT]
    return "\n".join(lines)


def msg_night(fac, snap, now, cfg, etype="startup"):
    ph = PHRASE[etype]
    prim = cfg["notify"]["primary_receptor"]
    s, e = datetime.fromisoformat(snap["start"]), datetime.fromisoformat(snap["end"])
    lines = [f"🌙 夜間紀錄：{fac['name']}於{day_word(s, now)} {s:%H:%M} 至{day_word(e, now)} {e:%H:%M}"
             f" {ph['night']}（共 {snap['hours']:g} 小時），已結束",
             f"期間{prim}位於下風方向共 {snap['dw_hours']} 小時", FOOT]
    return "\n".join(lines)


# ======================== 核心：每座廠每小時一步 ========================
def step(fac, now, obs, wind, fc, st, cfg):
    """obs = {"time": datetime, "startup": bool, "shutdown": bool, "flues": [...]}（無資料時 None）
    回傳要發送的訊息 [(文字, 是否附地圖, 事件類型)]。此函式不做網路傳送，方便情境測試。"""
    n = cfg["notify"]
    receptors = [n["primary_receptor"]] + n.get("extra_receptors", [])
    sts = {} if not wind else {
        r: downwind(fac, receptor_by_name(cfg, r), wind["wd"], wind["ws"], cfg)
        for r in receptors if receptor_by_name(cfg, r)}
    prim_now = sts.get(n["primary_receptor"])
    low = wind and wind.get("consistency", 1) < cfg.get("wind", {}).get("low_consistency", 0.5)

    def note(t):  # 合成風場各站差異大時加註
        return t.replace(FOOT, "※ 各測站風向差異大，下風判斷可信度較低\n" + FOOT) if low else t

    out = []
    for etype, label, _codes, send_end in event_types(cfg):
        key = fac["id"] if etype == "startup" else f"{fac['id']}:{etype}"
        ev = st["events"].setdefault(key, {"active": False})
        pend = st["pending"].setdefault(key, [])

        # 1) 依新的一小時資料更新事件狀態
        if obs and obs["time"].isoformat() != ev.get("last_hour"):
            t = obs["time"]
            if obs.get(etype):
                if not ev["active"]:
                    ev.clear()
                    ev.update(active=True, start=t.isoformat(), hours=0, dw_hours=0,
                              start_sent=False, dw_sent=False)
                ev["hours"] += 1
                ev["dw_hours"] += 1 if prim_now else 0
            elif ev["active"]:
                # 已發過首則 → 結束通知（停爐依設定不發）；整段在夜間 → 早上補夜間紀錄
                kind = ("end" if send_end else None) if ev["start_sent"] else "night"
                if kind:
                    nxt = next((et for et, _lb, _c, _e in event_types(cfg)
                                if et != etype and obs.get(et)), None)
                    dur_h = (t - datetime.fromisoformat(ev["start"])).total_seconds() / 3600
                    pend.append({"kind": kind, "start": ev["start"], "end": t.isoformat(),
                                 "hours": round(dur_h, 1), "dw_hours": ev["dw_hours"], "next": nxt})
                ev.clear()
                ev["active"] = False
            ev["last_hour"] = t.isoformat()

        if not in_window(now, cfg):
            continue

        # 2) 補發已結束事件的通知
        for snap in pend:
            out.append((msg_end(fac, snap, now, cfg, etype) if snap["kind"] == "end"
                        else msg_night(fac, snap, now, cfg, etype), False, etype))
        pend.clear()

        # 3) 進行中的事件
        both_bad = etype in ("startup", "shutdown") and obs and \
            bool(set(obs.get("ops", [])) & set(n.get("fault_with_op_codes", ["B", "D"])))
        xnote = "※ 防制設備同時異常" if both_bad else None
        if ev.get("active"):
            if not ev["start_sent"]:                   # 第一次發送
                out.append((note(msg_event(fac, ev, now, sts, fc, cfg, etype=etype,
                                           send_end=send_end, extra_note=xnote)),
                            n.get("attach_map", True), etype))
                ev["start_sent"] = True
                ev["dw_sent"] = bool(prim_now)         # 第一次就在下風 → 之後不再警告
            elif not ev["dw_sent"] and prim_now:      # 第一次不在下風，之後轉為下風 → 再警告一次
                out.append((note(msg_event(fac, ev, now, sts, fc, cfg, warning=True, etype=etype,
                                           send_end=send_end, extra_note=xnote)),
                            n.get("attach_map", True), etype))
                ev["dw_sent"] = True
    return out


# ======================== 地圖 ========================
def render_map(fac, flues, wind, t_ev, cfg, out_dir, upload=True, when=None):
    """when：判斷日夜（大氣穩定度）用的時間；正式執行用「現在」，情境測試用情境時間。"""
    dcfg, receptors = cfg["dispersion"], cfg.get("receptors", [])
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    rxy = [plume.lonlat_to_xy(r["lon"], r["lat"], lat0, lon0) for r in receptors]
    src, _ = build_source(flues, fac, cfg)
    tfn, tcfg = None, cfg.get("terrain", {})
    if tcfg.get("enabled", False):
        try:
            import terrain as terrainmod
            need = max([math.hypot(x, y) for x, y in rxy] + [0]) / 1000 + 2
            ter = terrainmod.Terrain(lat0, lon0, max(dcfg["grid_half_width_km"], need),
                                     zoom=tcfg.get("zoom", 12))

            def tfn(x, y):
                lo, la = plume.xy_to_lonlat(x, y, lat0, lon0)
                return ter(lo, la)
            log.info("地形：已載入（%d 張圖磚）", ter.n_tiles)
        except Exception as e:  # noqa: BLE001
            tfn = None
            log.warning("地形：載入失敗，本次以平坦地形計算：%s", e)
    res = plume.run(src, wind["ws"], wind["wd"], t_ev.hour, ta_c=wind.get("temp_c") or 25.0,
                    anemo_h=dcfg["anemometer_height_m"], half_km=dcfg["grid_half_width_km"],
                    step=dcfg["grid_step_m"], night_cloudy=dcfg["night_cloudy"],
                    min_ws=dcfg["min_wind_m_s"], receptors_xy=rxy,
                    when=when or datetime.now(), lat=lat0, lon=lon0,
                    terrain_fn=tfn, stack_base_m=fac.get("stack_base_elev_m"),
                    f_horizontal=tcfg.get("f_horizontal"))
    res["C"] = res["C"] * 1e6
    res["max"] *= 1e6
    model_name = "篩選估算（高斯煙流模式）"
    acfg = cfg.get("aermod", {})
    if acfg.get("enabled", False):
        try:
            res = run_aermod(fac, src, wind, when or datetime.now(), rxy, cfg, res)
            model_name = "即時估算（AERMOD）"
        except Exception as e:  # noqa: BLE001
            log.warning("AERMOD 失敗，本次改用高斯煙流模式：%s", str(e)[:300])
            res["model_note"] = f"AERMOD 失敗，改用高斯煙流模式（{str(e).splitlines()[0][:60]}）"
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"plume_{fac['id']}_{t_ev:%Y%m%d_%H%M}.png")
    title = f"{fac['name']} 戴奧辛擴散{model_name}\nCEMS 資料時間 {t_ev:%Y-%m-%d %H:%M}"
    mapper.draw(res, fac, wind, cfg["dispersion"]["contour_levels_fg_teq_m3"], title, png,
                receptors=receptors, map_cfg=cfg.get("map"))
    prev = mapper.make_preview(png, png.replace(".png", "_s.png"))
    if not upload:
        return png, None, None
    img = line_push.upload_image(png, cfg)
    return png, img, (line_push.upload_image(prev, cfg) if img else None)


def run_aermod(fac, src, wind, when, rxy, cfg, gauss_res):
    """以 EPA AERMOD 計算地面濃度（fg I-TEQ/m³）。氣象先用 AERMET，失敗改用 Python 計算。
    回傳與高斯模式相同結構的 dict，供地圖使用。"""
    import numpy as np
    import aermod_met
    import aermod_run
    acfg = cfg.get("aermod", {})
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    target = when.replace(minute=0, second=0, microsecond=0)
    obs = {"ws": wind["ws"], "wd": wind["wd"], "temp_c": wind.get("temp_c")}
    site = acfg.get("site_char", {"albedo": 0.18, "bowen": 1.5, "z0": 1.0})
    met = aermod_met.get_met(lat0, lon0, target, obs=obs, zref=acfg.get("wind_height_m", 10.0),
                             site=site, prefer=acfg.get("met", "aermet"))
    stk = aermod_run.Stack(q_ng_s=src.q_g_s, hs=src.hs, ts_k=src.ts_c + 273.15, vs=src.vs,
                           d=src.d, base_elev=0.0)
    terr, terr_note = None, "未納入（AERMOD 以平坦地形計算）"
    tsrc = acfg.get("terrain_source", "moenv")
    if acfg.get("terrain", True) and tsrc == "moenv":
        try:
            import moenv_terrain
            mt = moenv_terrain.prepare(fac, [(r["lon"], r["lat"]) for r in cfg.get("receptors", [])],
                                       half_km=acfg.get("terrain_grid_half_width_km", 6),
                                       step=acfg.get("terrain_grid_step_m", 100))
            terr = {"rou": mt["rou"], "sou": mt["sou"], "base": mt["base"], "key": mt["key"]}
            g = mt["grid"]
            grid = aermod_run.Grid(g["x0"], g["y0"], g["nx"], g["ny"], g["dx"], g["dy"],
                                   discrete=mt["discrete"])
            terr_note = mt["note"]
            gauss_res = {**gauss_res, "Z": mt["Z"], "X": mt["ZX"], "Y": mt["ZY"]}
            log.info("地形：%s（%d×%d 點）", terr_note, g["nx"], g["ny"])
        except Exception as e:  # noqa: BLE001
            terr = None
            terr_note = "未納入（環境部地形資料讀取失敗，以平坦地形計算）"
            log.warning("環境部地形資料讀取失敗，AERMOD 改以平坦地形計算：%s", str(e)[:300])
    elif acfg.get("terrain", True):
        import aermap_run
        g, disc = aermap_run.grid_for(fac, cfg)
        try:
            terr = aermap_run.run(fac, g, disc, dem_step=acfg.get("dem_step_deg", 0.0005))
            grid = aermod_run.Grid(g["x0"], g["y0"], g["nx"], g["ny"], g["dx"], g["dy"],
                                   discrete=disc)
            terr_note = f"已納入（AERMAP），煙囪基地標高 {terr['base']:.0f} m"
            log.info("AERMAP 地形：%s（快取鍵 %s）", terr_note, terr["key"])
        except Exception as e:  # noqa: BLE001
            terr = None
            terr_note = "未納入（AERMAP 失敗，以平坦地形計算）"
            log.warning("AERMAP 失敗，AERMOD 改以平坦地形計算：%s", str(e)[:300])
    if terr is None:
        half = acfg.get("grid_half_width_km", 8) * 1000
        half = max(half, max([math.hypot(x, y) for x, y in rxy] + [0]) + 1000)
        step = acfg.get("grid_step_m", 100)
        n = int(2 * half / step) + 1
        grid = aermod_run.Grid(-half, -half, n, n, step, step, discrete=rxy)
    out = aermod_run.run(stk, met["sfc"], grid, lat0, lon0,
                         urban_pop=acfg.get("urban_population"), raw=met["raw"], terr=terr)
    C = out["C"]
    i = np.unravel_index(np.argmax(C), C.shape)
    # 從 SFC 取出邊界層參數供地圖說明
    v = (met["raw"]["sfc_line"] if met["raw"] else met["sfc"].line()).split()
    res = {"X": out["X"], "Y": out["Y"], "C": C, "max": float(C[i]),
           "max_xy": (float(out["X"][i]), float(out["Y"][i])),
           "max_dist_m": float(math.hypot(out["X"][i], out["Y"][i])),
           "receptors": [{"conc": c, "downwind_m": r["downwind_m"]}
                         for c, r in zip(out["discrete"], gauss_res["receptors"])],
           "model": "AERMOD", "met_method": met["method"], "met_note": met["note"],
           "H": float(v[5]), "ustar": float(v[6]), "zic": float(v[9]), "zim": float(v[10]),
           "L": float(v[11]), "sun_alt": gauss_res.get("sun_alt"),
           "Z": gauss_res.get("Z"), "ZX": gauss_res.get("X"), "ZY": gauss_res.get("Y"),
           "stack_base_m": terr["base"] if terr else None, "calm": gauss_res.get("calm"),
           "terrain_note": terr_note}
    log.info("AERMOD 完成｜氣象：%s｜L=%s m，zic=%s m｜最大 %.1f fg/m³", met["method"], v[11],
             v[9], res["max"])
    return res


def fake_flue(t, flow_nm3_hr=None, etype="startup"):
    return {"polno": "P", "time": t, "ops": [{"startup": "C", "shutdown": "E", "fault": "A"}[etype]],
            "startup": etype == "startup", "shutdown": etype == "shutdown",
            "fault": etype == "fault", "flow": (float(flow_nm3_hr), "Nm3/hr") if flow_nm3_hr else None,
            "temp_c": None}


# ======================== 正式執行 ========================
def observe(fac, rows, cfg):
    res = cems.evaluate(rows, cfg)
    if not res:
        return None
    t = max(r["time"] for r in res)
    flues = [r for r in res if r["time"] == t]
    obs = {"time": t, "flues": flues, "ops": sorted({o for r in flues for o in r["ops"]})}
    for etype, _label, codes, _end in event_types(cfg):
        obs[etype] = any(set(r["ops"]) & set(codes) for r in flues)
        for r in flues:  # 依通報設定標記各管道狀態（模擬時挑選管道用）
            r[etype] = bool(set(r["ops"]) & set(codes))
    obs.setdefault("startup", False)
    obs.setdefault("shutdown", False)
    return obs


def run_live(cfg, args):
    facs = facilities_of(cfg)
    by_fac = cems.fetch_all_facilities(cfg, facs)
    stations = windmod.get_stations(cfg)
    st_path = os.path.join(HERE, cfg.get("state_file", "state.json"))
    st = load_state(st_path)
    now = datetime.now()
    hb_lines = []
    for fac in facs:
        obs = observe(fac, by_fac.get(fac["name"], []), cfg)
        wind = windmod.nearest_wind(stations, fac, cfg)
        fc = forecast.fetch(cfg, fac.get("forecast_town", ""))
        if obs is None:
            log.error("%s：找不到 CEMS 資料，請確認 cno / abbr_keywords", fac["name"])
            hb_lines.append(f"・{fac['name']}：⚠️ 抓不到 CEMS 資料")
            continue
        age = (now - obs["time"]).total_seconds() / 3600
        ev = st["events"].get(fac["id"], {})
        prim = cfg["notify"]["primary_receptor"]
        dw = downwind(fac, receptor_by_name(cfg, prim), wind["wd"], wind["ws"], cfg) if wind else None
        log.info("%s 狀態｜CEMS 最新 %s（延遲 %.1f 小時）｜運轉代碼 %s｜起爐中：%s｜事件進行中：%s",
                 fac["name"], obs["time"].strftime("%m/%d %H:%M"), age,
                 ",".join(obs["ops"]) or "-", "是" if obs["startup"] else "否",
                 "是" if ev.get("active") else "否")
        if wind:
            for s in wind.get("members", []):
                log.info("%s 測站｜%s 距 %.1f km 風從 %.0f° %.1f m/s（%s）", fac["name"],
                         s.get("site", s["name"]), s["dist_km"], s["wd"], s["ws"], s["time"])
            log.info("%s 風向｜%s %s 風從 %.0f° %.1f m/s%s｜%s：%s",
                     fac["name"], wind.get("src_label", ""), wind.get("site", wind["name"]),
                     wind["wd"], wind["ws"],
                     f"｜一致性 {wind['consistency']:.2f}" if "consistency" in wind else "",
                     prim, {True: "在下風", False: "不在下風", None: "風速微弱無法判斷"}[dw])
        else:
            log.warning("%s：抓不到測站風向", fac["name"])
        log.info("%s 預報｜%s", fac["name"], f"取得 {len(fc)} 筆" if fc else "未取得（沒有 CWA_API_KEY 或解析失敗）")
        hb_lines.append(f"・{fac['name']}：CEMS 最新 {obs['time']:%m/%d %H:%M}"
                        f"（運轉代碼 {','.join(obs['ops']) or '-'}）"
                        + (f"｜⚠️資料延遲 {age:.0f} 小時" if age > cfg["alert"]["max_data_age_hours"] else ""))
        if args.force:  # 測試：把現在當成起爐，直接發一則
            st_tmp = {"events": {}, "pending": {}}
            obs_f = {**obs, "startup": True, "shutdown": False}
            msgs = step(fac, now.replace(hour=12), obs_f, wind, fc, st_tmp, cfg)
            msgs = [("🧪 測試訊息（非真實起爐）\n" + m, a, e) for m, a, e in msgs if e == "startup"]
        else:
            msgs = step(fac, now, obs, wind, fc, st, cfg)
        for text, attach, etype in msgs:
            img = prev = None
            if attach and wind:
                if args.force:  # 測試：固定用設定檔的測試流率（未設定則用預設流率）
                    flues = [fake_flue(obs["time"], fac.get("force_flow_nm3_per_hr"))]
                else:
                    flues = [f for f in obs["flues"] if f.get(etype)] or \
                        [fake_flue(obs["time"], etype=etype)]
                _, img, prev = render_map(fac, flues, wind, obs["time"], cfg,
                                          os.path.join(HERE, "out"), upload=not args.dry_run)
            line_push.push(cfg, text, img, prev, dry_run=args.dry_run,
                           admin=args.force and not args.to_group)

    # 每日運作確認（當天第一次進入發送時段時）
    log.info("發送時段：%s｜今日運作確認：%s", "是" if in_window(now, cfg) else "否",
             "已發送" if st.get("heartbeat_date") == now.date().isoformat() else "未發送")
    if cfg["notify"].get("heartbeat") and in_window(now, cfg) and not args.force \
            and st.get("heartbeat_date") != now.date().isoformat():
        line_push.push(cfg, "🤖 監控機器人運作正常\n" + "\n".join(hb_lines), dry_run=args.dry_run,
                       admin=True)
        if not args.dry_run:
            st["heartbeat_date"] = now.date().isoformat()
    if not args.dry_run and not args.force:
        save_state(st_path, st)


# ======================== 情境測試 ========================
def run_scenario(path, cfg, make_maps=False):
    with open(path, encoding="utf-8") as f:
        sc = yaml.safe_load(f)
    facs = {f["id"]: f for f in facilities_of(cfg)}
    fac = facs[sc["facility"]]
    t0 = datetime.strptime(sc["start"], "%Y-%m-%d %H:%M")
    hours = sc["hours"]
    st = {"events": {}, "pending": {}}
    print(f"\n==== 情境：{sc.get('title', path)}（{fac['name']}）====")
    for i, h in enumerate(hours):
        t = t0 + timedelta(hours=i)
        now = t + timedelta(minutes=20)
        flags = {et: bool(set(h["codes"]) & set(cs)) for et, _l, cs, _e in event_types(cfg)}
        startup = flags.get("startup", False)
        etype_now = next((e for e in ("startup", "shutdown", "fault") if flags.get(e)), "startup")
        obs = {"time": t, "flues": [fake_flue(t, etype=etype_now)], "ops": list(h["codes"]),
               **{e: flags.get(e, False) for e in ("startup", "shutdown", "fault")}}
        wind = {"name": "情境測站", "src_label": "情境測試（假設值）", "site": "情境測站",
                "lat": fac["stack_lat"] + 0.01, "lon": fac["stack_lon"] + 0.015,
                "ws": h["ws"], "wd": h["wd"], "temp_c": 22, "time": f"{t:%m/%d %H:%M}",
                "dist_km": 1.8}
        # 預報：直接拿情境後面時段的風向當作「完美預報」，每 3 小時一筆
        fc = [{"time": t0 + timedelta(hours=j), "wd": hours[j]["wd"],
               "text": compass(hours[j]["wd"]) + "風"}
              for j in range(i + 1, len(hours)) if (j % 3 == 0)]
        msgs = step(fac, now, obs, wind, fc, st, cfg)
        dw = downwind(fac, receptor_by_name(cfg, cfg["notify"]["primary_receptor"]),
                      h["wd"], h["ws"], cfg)
        tag = ("起爐" if startup else "停爐" if flags.get("shutdown")
               else "防制異常" if flags.get("fault") else "正常") + f"({','.join(h['codes'])})"
        print(f"[{now:%m/%d %H:%M}] CEMS {t:%H:%M} {tag}｜風從 {h['wd']}° {h['ws']} m/s｜"
              f"{cfg['notify']['primary_receptor']}{'在' if dw else '不在' if dw is False else '?'}下風"
              f"｜{'發送時段' if in_window(now, cfg) else '靜默時段'}｜發送 {len(msgs)} 則")
        for text, attach, etype in msgs:
            print("   ┌──── LINE 訊息" + ("（附地圖）" if attach else ""))
            for ln in text.split("\n"):
                print("   │ " + ln)
            print("   └────")
            if attach and make_maps:
                png, _, _ = render_map(fac, [fake_flue(t, etype=etype)], wind, t, cfg,
                                       os.path.join(HERE, "out", "scenario"), upload=False,
                                       when=now)
                print(f"   地圖：{png}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="真資料，但不發 LINE、不存狀態")
    ap.add_argument("--force", action="store_true", help="把現在當成起爐，發一則測試訊息給管理者個人")
    ap.add_argument("--force-group", action="store_true", help="同 --force，但直接發到群組（LINE_TO）")
    ap.add_argument("--demo", action="store_true", help="跑 tests/ 內所有情境（不連網、不發 LINE）")
    ap.add_argument("--scenario", help="跑指定情境檔")
    ap.add_argument("--diagnose", action="store_true", help="印出 CEMS 資料集實際內容，找正確的管制編號")
    ap.add_argument("--maps", action="store_true", help="情境測試時也產生地圖")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    apply_env(cfg)
    args.to_group = args.force_group
    args.force = args.force or args.force_group
    for i, f in enumerate(cfg.get("facilities", [])):
        f.setdefault("id", f"f{i}")

    if args.diagnose:
        cems.diagnose(cfg, facilities_of(cfg))
        stations = windmod.get_stations(cfg)
        for fac in facilities_of(cfg):
            print(f"\n===== 距 {fac['name']} 最近的測站（有風向風速資料者）=====")
            near = sorted(stations, key=lambda s: windmod.haversine_km(
                fac["stack_lat"], fac["stack_lon"], s["lat"], s["lon"]))[:6]
            for s in near:
                d = windmod.haversine_km(fac["stack_lat"], fac["stack_lon"], s["lat"], s["lon"])
                print(f"   {s['name']:<14} 距 {d:5.2f} km｜座標 {s['lat']:.4f}, {s['lon']:.4f}"
                      f"｜目前風從 {s['wd']:.0f}° {s['ws']:.1f} m/s（{s['time']}）")
            print("   （程式自動採用第一個；要指定可在 config.yaml 該廠的 wind_station 填站名，如 \"汐止\"）")
        return
    if args.demo or args.scenario:
        import glob
        paths = [args.scenario] if args.scenario else sorted(glob.glob(os.path.join(HERE, "tests", "*.yaml")))
        for p in paths:
            run_scenario(p, cfg, make_maps=args.maps)
        return
    run_live(cfg, args)


if __name__ == "__main__":
    main()

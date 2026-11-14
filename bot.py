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
    q_total, temps, flows = 0.0, [], []
    for ev in ev_list:
        if ev["startup"]:
            conc = dx["startup_conc_ng_iteq_nm3"]
        elif ev["shutdown"]:
            conc = dx.get("shutdown_conc_ng_iteq_nm3", dx["startup_conc_ng_iteq_nm3"])
        elif ev["fault"]:
            conc = dx.get("fault_conc_ng_iteq_nm3", dx["startup_conc_ng_iteq_nm3"])
        else:
            conc = dx["normal_conc_ng_iteq_nm3"]
        f = cems.flow_to_nm3_s(ev["flow"]) or fac["default_flow_nm3_per_hr"] / 3600
        q_total += conc * f                    # ng I-TEQ/s
        flows.append(f)
        temps.append(ev["temp_c"] or fac["default_exit_temp_c"])
    ts = sum(temps) / len(temps)
    d_eq = fac["stack_diameter_m"] * math.sqrt(len(ev_list))
    q_act = sum(flows) * (ts + 273.15) / 273.15
    vs = q_act / (math.pi * d_eq ** 2 / 4)
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


def msg_event(fac, ev, now, statuses, fc, cfg, warning=False):
    n = cfg["notify"]
    prim, extras = n["primary_receptor"], n.get("extra_receptors", [])
    start = datetime.fromisoformat(ev["start"])
    last = datetime.fromisoformat(ev["last_hour"])
    dur = int((last - start).total_seconds() // 3600)
    dur_txt = f"已持續 {dur} 小時" if dur > 0 else "剛開始"
    if warning:
        lines = [f"⚠️ {fac['name']}起爐作業持續中（{day_word(start, now)} {start:%H:%M} 起，{dur_txt}）"]
    else:
        lines = [f"{fac['name']}{day_word(start, now)} {start:%H:%M} 起有起爐作業（{dur_txt}）"]
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
    lines += ["起爐結束時會再通知您", FOOT]
    return "\n".join(lines)


def msg_end(fac, snap, now, cfg):
    prim = cfg["notify"]["primary_receptor"]
    s, e = datetime.fromisoformat(snap["start"]), datetime.fromisoformat(snap["end"])
    lines = [f"✅ {fac['name']}起爐作業已於{day_word(e, now)} {e:%H:%M} 結束"
             f"（{day_word(s, now)} {s:%H:%M} 起，共 {snap['hours']} 小時）",
             f"期間{prim}位於下風方向共 {snap['dw_hours']} 小時", FOOT]
    return "\n".join(lines)


def msg_night(fac, snap, now, cfg):
    prim = cfg["notify"]["primary_receptor"]
    s, e = datetime.fromisoformat(snap["start"]), datetime.fromisoformat(snap["end"])
    lines = [f"🌙 夜間紀錄：{fac['name']}於{day_word(s, now)} {s:%H:%M} 至{day_word(e, now)} {e:%H:%M}"
             f" 有起爐作業（共 {snap['hours']} 小時），已結束",
             f"期間{prim}位於下風方向共 {snap['dw_hours']} 小時", FOOT]
    return "\n".join(lines)


# ======================== 核心：每座廠每小時一步 ========================
def step(fac, now, obs, wind, fc, st, cfg):
    """obs = {"time": datetime, "startup": bool, "flues": [...]}（無資料時 None）
    回傳要發送的訊息 [(文字, 是否附地圖)]。此函式不做任何網路傳送，方便情境測試。"""
    fid = fac["id"]
    ev = st["events"].setdefault(fid, {"active": False})
    pend = st["pending"].setdefault(fid, [])
    out = []
    n = cfg["notify"]
    receptors = [n["primary_receptor"]] + n.get("extra_receptors", [])

    def statuses_now():
        if not wind:
            return {}
        return {r: downwind(fac, receptor_by_name(cfg, r), wind["wd"], wind["ws"], cfg)
                for r in receptors if receptor_by_name(cfg, r)}

    sts = statuses_now()
    prim_now = sts.get(n["primary_receptor"])

    # 1) 依新的一小時資料更新事件狀態
    if obs and obs["time"].isoformat() != ev.get("last_hour"):
        t = obs["time"]
        if obs["startup"]:
            if not ev["active"]:
                ev.clear()
                ev.update(active=True, start=t.isoformat(), hours=0, dw_hours=0,
                          start_sent=False, dw_sent=False)
            ev["hours"] += 1
            ev["dw_hours"] += 1 if prim_now else 0
        elif ev["active"]:
            pend.append({"kind": "end" if ev["start_sent"] else "night",
                         "start": ev["start"], "end": t.isoformat(),
                         "hours": ev["hours"], "dw_hours": ev["dw_hours"]})
            ev.clear()
            ev["active"] = False
        ev["last_hour"] = t.isoformat()

    if not in_window(now, cfg):
        return out

    # 2) 補發已結束事件的通知
    for snap in pend:
        out.append((msg_end(fac, snap, now, cfg) if snap["kind"] == "end"
                    else msg_night(fac, snap, now, cfg), False))
    pend.clear()

    # 3) 進行中的事件
    if ev.get("active"):
        if not ev["start_sent"]:                       # 第一次發送
            out.append((msg_event(fac, ev, now, sts, fc, cfg), n.get("attach_map", True)))
            ev["start_sent"] = True
            ev["dw_sent"] = bool(prim_now)             # 第一次就在下風 → 之後不再警告
        elif not ev["dw_sent"] and prim_now:          # 第一次不在下風，之後轉為下風 → 再警告一次
            out.append((msg_event(fac, ev, now, sts, fc, cfg, warning=True),
                        n.get("attach_map", True)))
            ev["dw_sent"] = True
    return out


# ======================== 地圖 ========================
def render_map(fac, flues, wind, t_ev, cfg, out_dir, upload=True):
    dcfg, receptors = cfg["dispersion"], cfg.get("receptors", [])
    lat0, lon0 = fac["stack_lat"], fac["stack_lon"]
    rxy = [plume.lonlat_to_xy(r["lon"], r["lat"], lat0, lon0) for r in receptors]
    src, _ = build_source(flues, fac, cfg)
    res = plume.run(src, wind["ws"], wind["wd"], t_ev.hour, ta_c=wind.get("temp_c") or 25.0,
                    anemo_h=dcfg["anemometer_height_m"], half_km=dcfg["grid_half_width_km"],
                    step=dcfg["grid_step_m"], night_cloudy=dcfg["night_cloudy"],
                    min_ws=dcfg["min_wind_m_s"], receptors_xy=rxy)
    res["C"] = res["C"] * 1e6
    res["max"] *= 1e6
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"plume_{fac['id']}_{t_ev:%Y%m%d_%H%M}.png")
    title = f"{fac['name']} 戴奧辛擴散篩選估算\nCEMS 資料時間 {t_ev:%Y-%m-%d %H:%M}"
    mapper.draw(res, fac, wind, cfg["dispersion"]["contour_levels_fg_teq_m3"], title, png,
                receptors=receptors, map_cfg=cfg.get("map"))
    prev = mapper.make_preview(png, png.replace(".png", "_s.png"))
    if not upload:
        return png, None, None
    img = line_push.upload_image(png, cfg)
    return png, img, (line_push.upload_image(prev, cfg) if img else None)


def fake_flue(t):
    return {"polno": "P", "time": t, "ops": ["C"], "startup": True, "shutdown": False,
            "fault": False, "flow": None, "temp_c": None}


# ======================== 正式執行 ========================
def observe(fac, rows, cfg):
    res = cems.evaluate(rows, cfg)
    if not res:
        return None
    t = max(r["time"] for r in res)
    codes = set(cfg["notify"].get("event_codes", ["B", "C"]))
    flues = [r for r in res if r["time"] == t]
    return {"time": t, "startup": any(set(r["ops"]) & codes for r in flues),
            "flues": flues, "ops": sorted({o for r in flues for o in r["ops"]})}


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
        hb_lines.append(f"・{fac['name']}：CEMS 最新 {obs['time']:%m/%d %H:%M}"
                        f"（運轉代碼 {','.join(obs['ops']) or '-'}）"
                        + (f"｜⚠️資料延遲 {age:.0f} 小時" if age > cfg["alert"]["max_data_age_hours"] else ""))
        if args.force:  # 測試：把現在當成起爐，直接發一則
            st_tmp = {"events": {}, "pending": {}}
            obs_f = {**obs, "startup": True}
            msgs = step(fac, now.replace(hour=12), obs_f, wind, fc, st_tmp, cfg)
            msgs = [("🧪 測試訊息（非真實起爐）\n" + m, a) for m, a in msgs]
        else:
            msgs = step(fac, now, obs, wind, fc, st, cfg)
        for text, attach in msgs:
            img = prev = None
            if attach and wind:
                flues = [f for f in obs["flues"] if f["startup"]] or [fake_flue(obs["time"])]
                _, img, prev = render_map(fac, flues, wind, obs["time"], cfg,
                                          os.path.join(HERE, "out"), upload=not args.dry_run)
            line_push.push(cfg, text, img, prev, dry_run=args.dry_run,
                           admin=args.force and not args.to_group)

    # 每日運作確認（當天第一次進入發送時段時）
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
    codes = set(cfg["notify"].get("event_codes", ["B", "C"]))
    hours = sc["hours"]
    st = {"events": {}, "pending": {}}
    print(f"\n==== 情境：{sc.get('title', path)}（{fac['name']}）====")
    for i, h in enumerate(hours):
        t = t0 + timedelta(hours=i)
        now = t + timedelta(minutes=20)
        startup = bool(set(h["codes"]) & codes)
        obs = {"time": t, "startup": startup, "flues": [fake_flue(t)]}
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
        tag = "起爐" if startup else "正常"
        print(f"[{now:%m/%d %H:%M}] CEMS {t:%H:%M} {tag}｜風從 {h['wd']}° {h['ws']} m/s｜"
              f"{cfg['notify']['primary_receptor']}{'在' if dw else '不在' if dw is False else '?'}下風"
              f"｜{'發送時段' if in_window(now, cfg) else '靜默時段'}｜發送 {len(msgs)} 則")
        for text, attach in msgs:
            print("   ┌──── LINE 訊息" + ("（附地圖）" if attach else ""))
            for ln in text.split("\n"):
                print("   │ " + ln)
            print("   └────")
            if attach and make_maps:
                png, _, _ = render_map(fac, [fake_flue(t)], wind, t, cfg,
                                       os.path.join(HERE, "out", "scenario"), upload=False)
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

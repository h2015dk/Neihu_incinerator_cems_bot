"""CEMS 資料擷取與判讀（環境部開放資料 AQX_P_187：1 小時紀錄值）"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

API = "https://data.moenv.gov.tw/api/v2"
log = logging.getLogger(__name__)


def fetch_dataset(dataset: str, api_key: str, filters: str | None = None,
                  page: int = 1000, max_rows: int = 50000) -> list[dict]:
    """抓整個資料集（自動分頁）。回傳欄位名一律轉小寫。
    平臺曾調整回傳格式，可能是 {"records":[...]} 或直接 [...]，兩種都處理。"""
    params = {"api_key": api_key, "format": "json", "limit": page, "offset": 0}
    if filters:
        params["filters"] = filters
    rows: list[dict] = []
    while len(rows) < max_rows:
        r = requests.get(f"{API}/{dataset}", params=params, timeout=60)
        r.raise_for_status()
        js = r.json()
        recs = js.get("records", []) if isinstance(js, dict) else js
        if not recs:
            break
        rows.extend({k.lower(): v for k, v in rec.items()} for rec in recs)
        if len(recs) < page:
            break
        params["offset"] += page
    return rows


def fetch_all_facilities(cfg: dict, facilities: list[dict]) -> dict[str, list[dict]]:
    """依序嘗試：①管制編號篩選 ②縣市篩選 ③抓全表。每一步都確認真的比對到該廠才採用，
    避免伺服器端篩選語法失效（被忽略）時，只抓到別家資料就誤判為「沒有資料」。"""
    key = cfg["moenv_api_key"]
    out: dict[str, list[dict]] = {}
    pending = list(facilities)

    def take(rows, label):
        nonlocal pending
        still = []
        for f in pending:
            m = [r for r in rows if _match(r, f)]
            if m:
                out[f["name"]] = m
                log.info("%s：以%s取得 %d 筆", f["name"], label, len(m))
            else:
                still.append(f)
        pending = still

    for f in [f for f in pending if f.get("cno")]:
        try:
            take(fetch_dataset("aqx_p_187", key, filters=f"cno,EQ,{f['cno']}", max_rows=20000),
                 "管制編號篩選")
        except Exception as e:  # noqa: BLE001
            log.warning("管制編號篩選失敗：%s", e)
    for county in sorted({f.get("epb_county") for f in pending if f.get("epb_county")}):
        try:
            take(fetch_dataset("aqx_p_187", key, filters=f"epb_county,EQ,{county}", max_rows=50000),
                 "縣市篩選")
        except Exception as e:  # noqa: BLE001
            log.warning("縣市篩選失敗：%s", e)
    if pending:
        take(fetch_dataset("aqx_p_187", key, max_rows=400000), "全表")
    for f in pending:
        out[f["name"]] = []
    return out


def diagnose(cfg: dict, facilities: list[dict]) -> None:
    """印出資料集實際內容，協助找出正確的管制編號與公司簡稱。"""
    key = cfg["moenv_api_key"]
    for f in facilities:
        print(f"\n===== 診斷：{f['name']}（設定 cno={f.get('cno')!r}，關鍵字={f.get('abbr_keywords')}）=====")
        if f.get("cno"):
            try:
                rows = fetch_dataset("aqx_p_187", key, filters=f"cno,EQ,{f['cno']}", max_rows=5000)
                cnos = {r.get("cno") for r in rows}
                print(f"① 管制編號篩選：回傳 {len(rows)} 筆，含管制編號 {len(cnos)} 種"
                      + ("（篩選似乎沒有作用）" if len(cnos) > 1 else ""))
                _summary(rows[:2000], f)
            except Exception as e:  # noqa: BLE001
                print("① 管制編號篩選錯誤：", e)
        county = f.get("epb_county")
        try:
            rows = fetch_dataset("aqx_p_187", key, filters=f"epb_county,EQ,{county}", max_rows=50000)
            print(f"② 縣市篩選（{county}）：回傳 {len(rows)} 筆")
            _summary(rows, f)
        except Exception as e:  # noqa: BLE001
            print("② 縣市篩選錯誤：", e)


def _summary(rows, f):
    if not rows:
        print("   （沒有資料）")
        return
    times = sorted(str(r.get("m_time", "")) for r in rows)
    print(f"   監測時間範圍：{times[0]} ～ {times[-1]}")
    print(f"   欄位：{', '.join(rows[0].keys())}")
    seen = {}
    for r in rows:
        seen.setdefault((r.get("cno"), r.get("abbr"), r.get("epb_county")), set()).add(r.get("polno"))
    kws = [k for k in f.get("abbr_keywords", [])] + ["內湖", "焚化"]
    hit = {k: v for k, v in seen.items() if any(w in str(k[1]) for w in kws)}
    show = hit or dict(list(seen.items())[:30])
    print("   " + ("名稱含關鍵字／焚化的設施：" if hit else "找不到含關鍵字的設施，列出前 30 家："))
    for (cno, abbr, cty), pols in show.items():
        print(f"   - cno={cno}  abbr={abbr}  縣市={cty}  煙囪={sorted(map(str, pols))}")


def _match(r: dict, fac: dict) -> bool:
    if fac.get("cno"):
        return str(r.get("cno", "")).strip() == fac["cno"]
    abbr = str(r.get("abbr", ""))
    county_ok = not fac.get("epb_county") or fac["epb_county"] in str(r.get("epb_county", ""))
    return county_ok and any(k in abbr for k in fac.get("abbr_keywords", []))


def parse_code2(code: str | None) -> dict:
    """114/1/2 修正後的資料辨識碼：運轉狀態(1碼英文)+監測設施(1碼)+數據狀態(2碼數字)，如 'NA10'。
    舊制只有 2 碼數字。兩種都接受。（組合方式依辨識碼說明頁表格推定【待核】）"""
    c = (code or "").strip().upper()
    if len(c) >= 4 and c[0].isalpha():
        return {"op": c[0], "mon": c[1], "data": c[2:4], "raw": c}
    return {"op": None, "mon": None, "data": c[-2:] if c else "", "raw": c}


OP_DESC = {
    "N": "正常運轉／防制設施正常", "A": "正常運轉／防制設施異常",
    "B": "起爐／防制設施異常", "C": "起爐／防制設施正常",
    "D": "停爐／防制設施異常", "E": "停爐／防制設施正常",
    "H": "暫停運轉", "F": "暫停運轉", "I": "歲修", "G": "歲修", "J": "停工", "P": "停工",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_time(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def evaluate(rows: list[dict], cfg: dict) -> list[dict]:
    """依煙囪(polno)彙整最新一小時，判斷起停爐、防制設施異常、CO 超標。"""
    a = cfg["alert"]
    latest: dict[str, dict] = {}
    for r in rows:
        t = _parse_time(r.get("m_time", ""))
        if t is None:
            continue
        p = str(r.get("polno", "?"))
        latest.setdefault(p, {"time": t, "rows": []})
        if t > latest[p]["time"]:
            latest[p] = {"time": t, "rows": []}
        if t == latest[p]["time"]:
            latest[p]["rows"].append(r)

    results = []
    for polno, d in sorted(latest.items()):
        ops, co, flow, temp, o2 = set(), None, None, None, None
        for r in d["rows"]:
            pc = parse_code2(r.get("code2"))
            if pc["op"]:
                ops.add(pc["op"])
            desc = str(r.get("itemdesc", ""))
            val = _num(r.get("m_value"))
            if "一氧化碳" in desc or desc.upper().strip() == "CO":
                co = {"value": val, "std": _num(r.get("std")), "unit": r.get("unit", ""),
                      "code": pc, "code2desc": r.get("code2desc", "")}
            elif "流率" in desc or "流量" in desc:
                flow = (val, str(r.get("unit", "")))
            elif "溫度" in desc:
                temp = val
            elif "氧" in desc:
                o2 = val

        reasons = []
        startup = ops & set(a["startup_codes"])
        shutdown = ops & set(a["shutdown_codes"])
        fault = ops & set(a.get("control_fault_codes", []))
        if startup:
            reasons.append("起爐：" + "、".join(OP_DESC.get(o, o) for o in sorted(startup)))
        if shutdown:
            reasons.append("停爐：" + "、".join(OP_DESC.get(o, o) for o in sorted(shutdown)))
        if fault:
            reasons.append("防制設施異常：" + "、".join(OP_DESC.get(o, o) for o in sorted(fault)))

        co_exceed = False
        if co and co["value"] is not None:
            if co["code"]["data"] == "11":
                co_exceed = True
            elif co["std"] and co["value"] > co["std"]:
                co_exceed = True
            warn = a.get("co_warn_ppm")
            if warn and co["value"] > warn and not co_exceed:
                reasons.append(f"CO 預警：{co['value']:g} {co['unit']}（自訂門檻 {warn}）")
        if co_exceed:
            reasons.append(f"CO 超標：{co['value']:g} {co['unit']}（標準 {co['std']}）")

        results.append({
            "polno": polno, "time": d["time"], "ops": sorted(ops), "reasons": reasons,
            "startup": bool(startup), "shutdown": bool(shutdown), "fault": bool(fault),
            "co": co, "co_exceed": co_exceed, "flow": flow, "temp_c": temp, "o2": o2,
            "abbr": d["rows"][0].get("abbr", "") if d["rows"] else "",
        })
    return results


def flow_to_nm3_s(flow: tuple | None) -> float | None:
    if not flow or flow[0] is None:
        return None
    v, unit = flow
    u = unit.lower().replace("³", "3")
    if "/hr" in u or "/h" in u:
        return v / 3600
    if "/min" in u:
        return v / 60
    if "/s" in u:
        return v
    return None  # 單位不明就不用

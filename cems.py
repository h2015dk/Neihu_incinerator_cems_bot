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
    """全部設施共用一次抓取；回傳 {設施名稱: 該設施的資料列}。"""
    key = cfg["moenv_api_key"]
    rows: list[dict] = []
    if all(f.get("cno") for f in facilities):
        try:  # 每廠都有管制編號 → 伺服器端篩選，資料量小
            for f in facilities:
                rows += fetch_dataset("aqx_p_187", key, filters=f"cno,EQ,{f['cno']}")
        except Exception as e:  # noqa: BLE001
            log.warning("伺服器端篩選失敗，改抓全表：%s", e)
            rows = []
    if not rows:
        rows = fetch_dataset("aqx_p_187", key)
    return {f["name"]: [r for r in rows if _match(r, f)] for f in facilities}


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

"""CEMS 資料擷取與判讀（環境部開放資料 AQX_P_187：1 小時紀錄值）"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

API = "https://data.moenv.gov.tw/api/v2"
log = logging.getLogger(__name__)


class ServerBusy(Exception):
    """環境部 API 暫時無法正常回應（逾時、連線失敗、5xx/429、回應不是 JSON）。"""


def _get_json(url: str, params: dict, tries: int = 3, timeout=(10, 40)):
    """單次請求最多重試 tries 次（退避 3、6 秒）。逾時、連線失敗、5xx/429、
    以及回應不是合法 JSON（空內容或錯誤頁）都視為伺服器問題；用盡次數後丟出 ServerBusy。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            t0 = time.monotonic()
            r = requests.get(url, params=params, timeout=timeout)
            log.info("API 回應 HTTP %s，耗時 %.1f 秒", r.status_code, time.monotonic() - t0)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()          # 其他 4xx（如金鑰錯誤）不重試，直接丟出
            return r.json()
        except (requests.Timeout, requests.ConnectionError, ValueError) as e:
            last = e
        except requests.HTTPError as e:
            if "HTTP 429" not in str(e) and "HTTP 5" not in str(e):
                raise
            last = e
        log.warning("請求失敗（第 %d/%d 次）：%s", i + 1, tries, last)
        if i < tries - 1:
            time.sleep(3 * (i + 1))
    raise ServerBusy(str(last))


def fetch_dataset(dataset: str, api_key: str, filters: str | None = None,
                  page: int = 1000, max_rows: int = 50000) -> list[dict]:
    """抓整個資料集（自動分頁）。回傳欄位名一律轉小寫。
    平臺曾調整回傳格式，可能是 {"records":[...]} 或直接 [...]，兩種都處理。"""
    params = {"api_key": api_key, "format": "json", "limit": page, "offset": 0}
    if filters:
        params["filters"] = filters
    rows: list[dict] = []
    while len(rows) < max_rows:
        js = _get_json(f"{API}/{dataset}", params)
        recs = js.get("records", []) if isinstance(js, dict) else js
        if not recs:
            break
        rows.extend({k.lower(): v for k, v in rec.items()} for rec in recs)
        if len(recs) < page:
            break
        params["offset"] += page
    return rows


def _ds(cfg: dict) -> str:
    """CEMS 資料集：aqx_p_187＝1 小時紀錄值（預設）、aqx_p_186＝15 分鐘紀錄值、aqx_p_185＝6 分鐘紀錄值"""
    return str(cfg.get("cems_dataset", "aqx_p_187")).lower()


def fetch_all_facilities(cfg: dict, facilities: list[dict]) -> dict[str, list[dict]]:
    """依序嘗試：①管制編號篩選 ②縣市篩選 ③抓全表。每一步都確認真的比對到該廠才採用，
    避免伺服器端篩選語法失效（被忽略）時，只抓到別家資料就誤判為「沒有資料」。
    重要：只有「查詢成功但比對不到」才往下退；若是逾時／連線失敗（伺服器問題），
    退到更大的查詢只會更慢，直接放棄這一輪，等下一次排程再試。"""
    key = cfg["moenv_api_key"]
    out: dict[str, list[dict]] = {}
    pending = list(facilities)
    server_down = False

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
            # API 回傳順序為新資料在前，只需最新一小時，1500 筆綽綽有餘。
            rows = fetch_dataset(_ds(cfg), key, filters=f"cno,EQ,{f['cno']}",
                                 page=500, max_rows=1500)
            ts = [t for t in (_parse_time(r.get("m_time")) for r in rows) if t]
            if ts and (datetime.now() - max(ts)).total_seconds() > 12 * 3600:
                # 順序可能不是新到舊，擴大抓取，但設上限避免拖垮整個 job
                log.info("%s：前 %d 筆最新只到 %s，擴大抓取", f["name"], len(rows), max(ts))
                rows = fetch_dataset(_ds(cfg), key, filters=f"cno,EQ,{f['cno']}",
                                     page=1000, max_rows=10000)
            take(rows, "管制編號篩選")
        except ServerBusy as e:
            log.error("%s：環境部 API 無法正常回應，本輪放棄（不改用更大的查詢）：%s", f["name"], e)
            server_down = True
        except Exception as e:  # noqa: BLE001
            log.warning("管制編號篩選失敗：%s", e)

    if server_down:
        for f in pending:
            out[f["name"]] = []
        return out

    for county in sorted({f.get("epb_county") for f in pending if f.get("epb_county")}):
        try:
            take(fetch_dataset(_ds(cfg), key, filters=f"epb_county,EQ,{county}", max_rows=20000),
                 "縣市篩選")
        except ServerBusy as e:
            log.error("縣市篩選：環境部 API 無法正常回應，本輪放棄：%s", e)
            for f in pending:
                out[f["name"]] = []
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("縣市篩選失敗：%s", e)
    if pending:
        try:
            take(fetch_dataset(_ds(cfg), key, max_rows=100000), "全表")
        except ServerBusy as e:
            log.error("全表抓取：環境部 API 無法正常回應，本輪放棄：%s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("全表抓取失敗：%s", e)
    for f in pending:
        out[f["name"]] = []
    return out


def diagnose(cfg: dict, facilities: list[dict]) -> None:
    """印出資料集實際內容，協助找出正確的管制編號與公司簡稱；並比較三個 CEMS 資料集的延遲。"""
    key = cfg["moenv_api_key"]
    print(f"目前使用的 CEMS 資料集：{_ds(cfg)}")
    for f in [f for f in facilities if f.get("cno")]:
        print(f"\n===== 延遲比較：{f['name']}（cno={f['cno']}）=====")
        for ds, name in [("aqx_p_187", "1 小時紀錄值"), ("aqx_p_186", "15 分鐘紀錄值"),
                         ("aqx_p_185", "6 分鐘紀錄值")]:
            try:
                rows = fetch_dataset(ds, key, filters=f"cno,EQ,{f['cno']}", max_rows=3000)
                ts = [t for t in (_parse_time(r.get("m_time")) for r in rows) if t]
                if ts:
                    lag = (datetime.now() - max(ts)).total_seconds() / 3600
                    print(f"   {ds}（{name}）：最新 {max(ts):%m/%d %H:%M}，延遲約 {lag:.1f} 小時，"
                          f"取得 {len(rows)} 筆")
                else:
                    print(f"   {ds}（{name}）：沒有資料")
            except Exception as e:  # noqa: BLE001
                print(f"   {ds}（{name}）：錯誤 {e}")
    for f in facilities:
        print(f"\n===== 診斷：{f['name']}（設定 cno={f.get('cno')!r}，關鍵字={f.get('abbr_keywords')}）=====")
        if f.get("cno"):
            try:
                rows = fetch_dataset(_ds(cfg), key, filters=f"cno,EQ,{f['cno']}", max_rows=5000)
                cnos = {r.get("cno") for r in rows}
                print(f"① 管制編號篩選：回傳 {len(rows)} 筆，含管制編號 {len(cnos)} 種"
                      + ("（篩選似乎沒有作用）" if len(cnos) > 1 else ""))
                _summary(rows[:2000], f)
            except Exception as e:  # noqa: BLE001
                print("① 管制編號篩選錯誤：", e)
        county = f.get("epb_county")
        try:
            rows = fetch_dataset(_ds(cfg), key, filters=f"epb_county,EQ,{county}", max_rows=50000)
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
    """支援 2026-11-14T08:00:00（API 實際格式）、2026-11-14 08:00 等寫法。"""
    txt = str(s).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(txt, fmt)
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
            elif "氧氣" in desc:
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

# 內湖焚化廠 CEMS 監控機器人

每小時讀取環境部開放資料的焚化廠連續自動監測（CEMS）數據，偵測**起爐、停爐、防制設備異常**，以及**環興公司位於下風且 CO 偏高**的情況，結合即時風向與 EPA AERMOD 擴散模式，估算戴奧辛地面濃度分布，並透過 LINE 群組發送通知與模擬圖。

> ⚠️ **使用限制**
> 本系統的擴散結果為「AERMOD 即時估算」，**不是**依空氣品質模式評估技術規範所做的正式模擬。CEMS 不量測戴奧辛，模擬使用的排放濃度為**假設值**；氣象資料部分來自全球數值模式。結果僅供內部掌握煙流可能影響範圍，請勿當成實測結果引用或對外發布。

---

## 目錄

- [功能概覽](#功能概覽)
- [通報規則](#通報規則)
- [運作流程](#運作流程)
- [資料來源](#資料來源)
- [檔案結構](#檔案結構)
- [安裝與部署](#安裝與部署)
- [執行模式](#執行模式)
- [主要設定](#主要設定)
- [模擬方法說明](#模擬方法說明)
- [常見問題](#常見問題)

---

## 功能概覽

| 功能 | 說明 |
|---|---|
| CEMS 監控 | 每小時讀取內湖焚化廠 3 根管道（P001～P003）的 15 分鐘紀錄值，依資料辨識碼判斷運轉狀態 |
| 事件通報 | 起爐、停爐、防制設備異常時發送 LINE 通知，附戴奧辛擴散模擬圖 |
| 下風判斷 | 以最近 3 個測站合成風向，判斷環興公司（主要敏感點）與松山國小（補充）是否位於下風 |
| 風向預報 | 依中央氣象署鄉鎮預報，提醒未來幾小時是否可能轉為下風 |
| 擴散模擬 | EPA AERMOD＋AERMET，地形使用環境部空品模式支援中心 AERMOD 地形資料；失敗時自動改用高斯煙流模式 |
| 下風且 CO 偏高通知 | 環興公司在下風且任一管道 CO 超過 50 ppm 時，以正常運轉濃度模擬並通知（每天一則） |
| 運作確認 | 每天第一次進入發送時段時，發一則「機器人運作正常」給管理者 |

---

## 通報規則

所有通知只在 **07:00～20:00**（臺灣時間）發送；時段外發生的事件，延到下一個發送時段補發。

### 事件通報

| 事件 | 運轉狀態碼 | 首則（附圖） | 轉為下風時再警告 | 結束通知 | 模擬濃度（ng I-TEQ/Nm³） |
|---|---|---|---|---|---|
| 起爐 | B、C | ✅ | ✅（僅一次） | ✅ | 10 |
| 停爐 | D、E | ✅ | ✅（僅一次） | ❌ | 10【待核】 |
| 防制設備異常（正常運轉中） | A | ✅ | ✅（僅一次） | ✅「已恢復正常」 | 1 |

- 首則發送時若環興公司已在下風，該次事件不再發下風警告。
- 整段事件都在夜間時，隔天 07:20 補發一則「夜間紀錄」。
- 起爐或停爐時若防制設備同時異常（B、D 碼），訊息加註「※ 防制設備同時異常」。
- 起爐後直接停爐時，起爐結束通知會加註「接著進入停爐作業」。

### 下風且 CO 偏高通知（每天最多一則）

| 狀況 | 處理方式 |
|---|---|
| 環興公司在下風，且任一管道 CO > 50 ppm | CO 超過 50 ppm 的管道有幾根，就用幾根、每根以 0.1 ng I-TEQ/Nm³ 模擬並發送 |
| 起爐進行中 | 不做此判斷，由起爐事件通報處理（起爐結束後當天仍可觸發） |
| 停爐、防制設備異常進行中 | 改用事件濃度；若本次已發出附圖的事件通報，即視為當天的此項通知 |

### 下風判定

敏感點相對煙囪的方位落在煙流方向（風向＋180°）左右各 **22.5°** 內，且距離 10 km 以內，即判定為下風。風速低於 0.5 m/s 時顯示「風速微弱，無法判斷」，不觸發警告。

---

## 運作流程

```
cron-job.org（每小時第 20 分）
        │  呼叫 GitHub API
        ▼
GitHub Actions：cems-bot.yml
        │
        ├─ 環境部開放資料 AQX_P_186 ──► CEMS 運轉狀態、CO、排放流率
        ├─ 環境部空品測站＋氣象署自動站 ──► 最近 3 站合成風向風速
        ├─ 氣象署鄉鎮預報 ──► 未來風向
        │
        ├─ 判斷：起爐／停爐／防制設備異常／下風且 CO 偏高
        │
        ├─ 需要附圖時：
        │     Open-Meteo（雲量、日射、高空剖面）＋實測風與氣溫
        │        └─► AERMET（失敗改 Python 計算）─► AERMOD ─► 模擬圖
        │
        └─ LINE Messaging API ──► 群組
```

---

## 資料來源

| 資料 | 來源 | 備註 |
|---|---|---|
| CEMS 監測數據 | 環境部環境資料開放平臺 AQX_P_186（15 分鐘紀錄值） | 延遲約 1 小時；也可改用 AQX_P_187（1 小時紀錄值，延遲約 3 小時） |
| 地面風向風速 | 環境部空品測站（AQX_P_432）、中央氣象署自動氣象站（O-A0001-001） | 取最近 3 站，反距離平方加權向量合成 |
| 風向預報 | 中央氣象署鄉鎮預報 F-D0047-061（臺北市逐 3 小時） | 需 CWA_API_KEY |
| 雲量、日射量、高空剖面 | Open-Meteo 全球數值模式（免金鑰） | 模式值，非觀測；用於 AERMET 虛擬探空 |
| 地形 | 環境部空氣品質模式支援中心「AERMOD 地形資料」（2022.12.30 版，TWD97 TM2，100 m） | 已含山丘高度尺度，存於 `terrain_moenv/` |
| 底圖 | OpenStreetMap／CARTO | 地圖下方附版權標示 |

---

## 檔案結構

```
├── bot.py                 主程式：資料整合、事件判斷、通報、繪圖流程
├── cems.py                CEMS 資料擷取與資料辨識碼判讀
├── wind.py                測站風向風速、多站合成
├── forecast.py            氣象署鄉鎮風向預報
├── line_push.py           LINE 推播、模擬圖上傳
├── mapper.py              模擬圖繪製（底圖、色階、說明框）
├── plume.py               高斯煙流模式（AERMOD 失敗時的備援）、太陽仰角
├── terrain.py             AWS 地形圖磚（高斯模式用）
│
├── aermod_build.py        下載並編譯 EPA AERMOD／AERMET／AERMAP
├── aermod_run.py          產生 AERMOD 輸入檔、執行、讀取結果
├── aermet_run.py          虛擬探空＋地面資料 → AERMET → SFC／PFL
├── met_simple.py          備援：以 Python 計算邊界層參數
├── aermod_met.py          氣象輸入整合（先 AERMET，失敗改 Python）
├── openmeteo.py           Open-Meteo 資料擷取與快取
├── moenv_terrain.py       讀取環境部 AERMOD 地形資料
├── aermap_run.py          （備用）自行執行 AERMAP
├── aermod_probe.py        （工具）檢查 AERMET 所需資料是否可取得
│
├── terrain_moenv/         環境部地形資料（4 個 .TER 檔）
├── tests/                 情境測試檔（--demo 使用）
├── config.yaml            設定檔（不含金鑰）
├── requirements.txt
└── .github/workflows/
    ├── cems-bot.yml       每小時監控（由 cron-job.org 觸發）
    └── aermod-setup.yml   編譯 AERMOD 等程式並測試（手動執行）
```

---

## 安裝與部署

### 1. 設定 GitHub Secrets

repo → Settings → Secrets and variables → Actions → New repository secret

| 名稱 | 內容 | 必要性 |
|---|---|---|
| `MOENV_API_KEY` | 環境部環境資料開放平臺 API Key | 必要 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API channel access token | 必要 |
| `LINE_TO` | 通報對象：群組 groupId（C 開頭） | 必要 |
| `LINE_ADMIN_TO` | 管理者個人 userId（U 開頭）；運作確認與測試訊息只發這裡 | 建議 |
| `CWA_API_KEY` | 中央氣象署開放資料授權碼（測站風向、風向預報） | 建議 |
| `CARTO_API_KEY` | CARTO 底圖金鑰 | 選用 |
| `IMGUR_CLIENT_ID` | 圖床改用 Imgur 時使用 | 選用 |

> 金鑰一律放在 Secrets，**不要**寫進 `config.yaml`（本 repo 為公開 repo）。

### 2. 設定 Actions 權限

Settings → Actions → General → Workflow permissions 選 **Read and write permissions**（模擬圖會上傳到本 repo 的 `plume-images` 分支，供 LINE 讀取）。

### 3. 編譯 AERMOD 等程式（只需一次）

Actions → **aermod-setup** → Run workflow。會從 EPA 網站下載原始碼編譯，並把執行檔存入快取供每小時監控使用。EPA 發布新版時可再執行一次。

### 4. 設定 cron-job.org

| 欄位 | 設定 |
|---|---|
| URL | `https://api.github.com/repos/<帳號>/<repo>/actions/workflows/cems-bot.yml/dispatches` |
| Schedule | 每小時第 20 分（crontab `20 * * * *`），時區 Asia/Taipei |
| Method | POST |
| Headers | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <Fine-grained token>`<br>`X-GitHub-Api-Version: 2022-11-28`<br>`Content-Type: application/json` |
| Body | `{"ref":"main"}` |

Fine-grained token 只需對本 repo 開 **Actions：Read and write** 權限；請記下到期日，到期前更換。

### 5. 測試

依序執行 `diagnose` → `dry-run` → `force-group` → `daily-test`（見下節）。

---

## 執行模式

Actions → beitou-cems-bot → Run workflow → 選擇模式：

| 模式 | 用途 | 是否發 LINE | 是否影響當天紀錄 |
|---|---|---|---|
| `normal` | 正式執行（cron-job.org 觸發的就是這個） | 依規則 | 是 |
| `dry-run` | 用真實資料檢查判斷，log 印出狀態與預計訊息 | 否 | 否 |
| `demo` | 跑 `tests/` 內的起爐、停爐、防制異常情境，並產生地圖 | 否 | 否 |
| `force` | 把現在當成起爐，發一則附圖測試訊息給管理者 | 是（管理者） | 否 |
| `force-group` | 同上，直接發到群組 | 是（群組） | 否 |
| `daily-test` | 立即發一則「下風且 CO 偏高通知」測試（略過下風與 CO 條件） | 是（管理者） | 否 |
| `diagnose` | 列出 CEMS 資料內容、三個資料集的延遲比較、最近 6 個測站 | 否 | 否 |

> `LINE_ADMIN_TO` 未設定時，「發給管理者」的訊息會改發到群組。

產生的模擬圖會存成 Artifact（保留 7 天），可在該次執行頁面最下方下載。

---

## 主要設定

`config.yaml` 重點欄位（完整說明見檔案內註解）：

| 區段 | 欄位 | 目前值 | 說明 |
|---|---|---|---|
| `cems_dataset` | | `aqx_p_186` | CEMS 資料集（15 分鐘紀錄值） |
| `facilities` | `cno` | `A4200130` | 內湖焚化廠管制編號 |
| | `stack_height_m` | 74 | 煙囪高度 |
| | `stack_diameter_m` | 1.25 | 單一管道出口內徑 |
| | `default_flow_nm3_per_hr` | 38,968.2 | CEMS 無流率時使用（2025 年均值） |
| | `default_exit_temp_c` | 165.9 | 排氣溫度（2025 年均值） |
| | `default_exit_velocity_m_s` | 13.77 | 出口流速（2025 年均值） |
| `receptors` | | 環興公司、松山國小 | 敏感點 |
| `notify` | `window_start`／`window_end` | 07:00／20:00 | 發送時段 |
| | `primary_receptor` | 環興公司 | 下風警告依據 |
| | `daily_downwind.co_threshold_ppm` | 50 | 下風且 CO 偏高通知的 CO 門檻 |
| `dioxin` | `startup_conc_ng_iteq_nm3` | 10 | 起爐假設濃度 |
| | `shutdown_conc_ng_iteq_nm3` | 10 | 停爐假設濃度【待核】 |
| | `fault_conc_ng_iteq_nm3` | 1 | 防制設備異常假設濃度 |
| | `normal_conc_ng_iteq_nm3` | 0.1 | 正常運轉假設濃度 |
| `wind` | `n_nearest` | 3 | 合成風向使用的測站數 |
| `aermod` | `enabled` | true | 使用 AERMOD（false 則只用高斯模式） |
| | `urban_population` | 2,600,000 | AERMOD 都市選項人口【待核】 |
| | `terrain_source` | `moenv` | 地形來源（環境部資料） |
| `map` | `center`／`anchor` | 環興公司／松山國小 | 圖框置中與定位 |

---

## 模擬方法說明

| 項目 | 做法 |
|---|---|
| 擴散模式 | EPA AERMOD（法規預設選項、都市選項），單一小時、受體間距 100 m、範圍煙囪周圍 ±6 km |
| 氣象前處理 | EPA AERMET：Open-Meteo 1000～500 hPa 剖面組成虛擬探空，地面資料以 ONSITE 格式輸入；目標小時的風與氣溫以實測（三站合成）取代。AERMET 失敗時改用 Python 依 AERMET 公式計算 |
| 地形 | 環境部空品模式支援中心 AERMOD 地形資料（地面高程與山丘高度尺度） |
| 排放 | 假設戴奧辛濃度 × 管道排氣流率（優先使用 CEMS 實測流率）；多根管道合併為等效煙囪 |
| 污染物型態 | 以氣狀物、不計沉降處理（空氣濃度屬保守估計）；不適用於沉降量或食物鏈暴露評估 |
| 未納入 | 建物下洗（BPIP-PRIME）、地形對風場的影響、實測探空 |
| 濃度單位 | fg I-TEQ/m³（1 小時平均） |

---

## 常見問題

| 症狀 | 可能原因與處理 |
|---|---|
| log 出現「找不到 CEMS 資料」 | 檢查 `MOENV_API_KEY` 是否設定；用 `diagnose` 確認管制編號 |
| 環境部 API 連線逾時 | 伺服器忙碌，程式會自動重試；偶發可忽略 |
| 模擬圖標題為「高斯煙流模式」 | AERMOD 執行失敗，log 會有原因；可重跑 aermod-setup |
| 說明框「地形：未納入」 | `terrain_moenv/` 缺少需要的 .TER 檔，log 會列出缺少的檔名 |
| 訊息有文字、沒有圖 | Actions 權限未設為 Read and write，或 repo 非公開 |
| cron-job.org 回應 401／403 | Fine-grained token 過期或權限不足 |
| LINE 回應 400 | `LINE_TO` 不是有效的 groupId／userId |

詳細的 GitHub 與 LINE 群組設定步驟，請參考專案內的部署說明文件。

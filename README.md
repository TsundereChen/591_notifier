# 591 Telegram 租屋通知機器人

爬取 591 租屋搜尋結果，將新物件傳送給指定的 Telegram 使用者，並以 SQLite
避免重複通知。縣市、行政區、物件類型、租金、AI 評估與 cron 排程都能透過按鈕設定。

## 功能

- 每次讀取前 5 頁，每個縣市最多處理 150 筆結果。
- 每筆通知會從物件詳情頁載入照片，並以 Telegram 相簿傳送最多 10 張。
- 可選擇使用 OpenCode Go AI，依完整詳情與照片評估物件，並將推薦結果與理由附在通知中。
- 每個縣市使用獨立 SQLite 資料表。
- Telegram 接受通知或 AI 過濾後才保存完整物件資料。
- 每次排程爬取完成後，Telegram 會回報各縣市的處理、重試、已匹配、新推送、AI 過濾與失敗筆數。
- 使用 `TELEGRAM_ALLOWED_USER_ID` 限制單一擁有者與私人聊天室。
- 支援自訂五欄式 cron、Docker 與 amd64/arm64 映像。

## 快速開始

先透過 [BotFather](https://t.me/BotFather) 建立 bot，再執行：

```sh
docker run -d \
  --name 591-notifier \
  --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN="你的 bot token" \
  -e TELEGRAM_ALLOWED_USER_ID="你的 Telegram user ID" \
  -e OPENCODE_GO_API_KEY="你的 OpenCode Go API key" \
  -v 591-notifier-data:/data \
  ghcr.io/tsunderechen/591_notifier:latest
```

指定使用者在私人聊天室傳送 `/start` 後即可用按鈕設定。YAML 與 SQLite 會保存
在 `/data`；同一份資料目錄只允許一個 bot 執行個體使用。

## YAML 設定

容器首次啟動會從 `config.yaml.example` 建立設定。本機使用時可手動複製：

```sh
cp config.yaml.example config.yaml
```

```yaml
database: listings.db
schedule: "*/15 * * * *"
timezone: Asia/Taipei

telegram:
  owner_user_id:
  chat_id:

ai:
  enabled: false
  filter: true
  model: kimi-k3
  criteria:
  max_images: 6

crawl:
  - region: 新北市
    sections: [土城區, 中和區]
    kinds: [整層住家, 獨立套房]
    price: {min: 10000, max: 30000}
```

`sections`、`kinds` 與 `price` 可留空。同一縣市只能設定一次；相對的資料庫路徑
以 YAML 所在目錄為基準。

## AI 物件評估

在 OpenCode Zen 訂閱 Go 後，將 API key 設為 `OPENCODE_GO_API_KEY`，再從 Telegram
選單的「🤖 AI 評估」啟用功能。預設模型為 `kimi-k3`，可在選單或 YAML 的
`ai.model` 改為其他 OpenCode Go 模型。

AI 會使用物件詳情頁的文字欄位、描述、設備、租住說明和最多 `ai.max_images`
張照片，回傳推薦與 0 到 10 分評分。`ai.criteria` 可填入個人偏好，例如「預算
兩萬內、重視採光、步行十分鐘內到捷運」。

- `ai.filter: true`：AI 不推薦的物件不會通知，且會保存為已處理，避免下次重複評估。
- `ai.filter: false`：所有新物件都會通知，並在訊息內附上 AI 評語。
- AI 或詳情頁暫時失敗時採取 fail-open 行為：仍通知物件，避免遺漏可能合適的房源。

## 通知去重

傳送前會先建立投遞保留紀錄；Telegram 接受訊息後，再以 SQLite 交易保存物件
與 message ID。若傳送失敗或結果因中斷而無法確認，下一次爬蟲會自動重送。
待重試物件會連同通知內容保存，即使已離開最新 150 筆搜尋結果仍會重試。
由於 Telegram 可能已接受訊息但程式來不及儲存結果，自動重送可能造成重複通知。
可在下次爬蟲前使用 `/pending` 查看嘗試次數與最後錯誤，並將已收到的物件標記為已送達。舊資料庫中
無法證明已通知的資料也會自動重送。

可用指令：`/start`、`/menu`、`/crawl`、`/pending`。

## 日誌與診斷

容器日誌會為每次爬蟲建立 `run_id`，並記錄每一頁的來源筆數、已接受的唯一
筆數、跨頁重複、無效資料、解析略過與是否不足 30 筆。這可直接解釋為何某縣市
少於 150 筆。通知、重試、Telegram 限流、SQLite 寫入及未處理的 bot 例外也會帶有
縣市、物件 ID 與嘗試次數；bot token 會被遮蔽。

預設應用程式日誌為 `INFO`，但 `httpx`/`httpcore` 的成功請求會維持在 `WARNING`，
避免輪詢訊息淹沒診斷資料。需要額外 HTTP 細節時可設定：

```sh
-e LOG_LEVEL=DEBUG -e HTTP_LOG_LEVEL=INFO
```

## 本機開發

```sh
python -m pip install -r requirements-dev.txt
pre-commit install
pytest
```

```sh
# Run the bot from source.
PYTHONPATH=src python -m rent591_notifier

# Run Black against all tracked files.
pre-commit run --all-files

# Run live 591 integration tests.
pytest -m integration
```

正式程式位於 `src/rent591_notifier/`。`requirements.txt` 用於容器，
`requirements-dev.txt` 另外包含測試與 pre-commit 工具。

## CI 與映像

GitHub Actions 會執行 Black、Python 3.13/3.14 測試與容器 smoke test；全部通過
後才發布 `linux/amd64`、`linux/arm64` 映像至
`ghcr.io/tsunderechen/591_notifier`。591 實站測試僅供本機需要時手動執行，不會在
GitHub Actions 中執行。

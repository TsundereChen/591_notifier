# 591 Telegram 租屋通知機器人

本服務會爬取 591 租屋物件，並將尚未通知過的結果傳送給一位 Telegram
使用者。所有爬蟲條件都能透過 Telegram 按鈕設定並儲存至 YAML；只有成功
送達的物件才會記錄在 SQLite 資料庫中。

## 通知與去重機制

每次排程或手動執行時，服務會依序：

1. 固定檢查搜尋結果第一頁，每個縣市最多處理 30 筆不重複的結果。
2. 傳送每筆物件前，先查詢該縣市專屬的 SQLite 資料表。
3. 若物件 ID 已存在，直接略過，不再傳送。
4. 將尚未看過的物件傳送至 Telegram。
5. Telegram 確認訊息送達後，才將物件寫入資料庫。

若 Telegram 傳送失敗，該物件不會寫入資料庫，下一次執行時仍可重試。
各縣市使用獨立資料表，例如 `listings_新北市` 與 `listings_台北市`。

## 設定 Telegram

1. 透過 [BotFather](https://t.me/BotFather) 建立機器人並複製權杖（token）。
2. 啟動容器時設定 `TELEGRAM_BOT_TOKEN`。
3. 開啟機器人對話並傳送 `/start`；第一位允許的使用者會成為擁有者。
4. 使用按鈕啟用縣市，並設定行政區、物件類型、租金範圍及執行排程。

建議設定 `TELEGRAM_ALLOWED_USER_ID` 為你的 Telegram 數字使用者 ID，避免
全新的機器人被其他人搶先綁定。機器人權杖只會從環境變數讀取，不會寫入
YAML。

可用指令：

- `/start`：綁定全新的機器人並開啟選單。
- `/menu`：開啟互動式設定選單。
- `/crawl`：立即執行一次爬蟲。

排程選單提供常用執行頻率，也可輸入自訂的五欄式 cron 表達式，例如
`*/10 * * * *`。cron 會使用設定檔中的時區，預設為 `Asia/Taipei`。

## YAML 設定

Telegram 中的修改會以原子操作寫回 `config.yaml`。設定內容等同於：

```yaml
database: listings.db
schedule: "*/15 * * * *"
timezone: Asia/Taipei

telegram:
  owner_user_id:
  chat_id:

crawl:
  - region: 新北市
    sections: [土城區, 中和區]
    kinds: [整層住家, 獨立套房]
    price:
      min: 10000
      max: 30000
```

`sections`、`kinds` 或 `price` 留空代表不套用該項篩選。機器人固定只讀取
第一頁，並自行限制每個縣市最多 30 筆，因此不提供 `pages` 設定。

## 使用 Docker 執行

在本機建置映像：

```sh
docker build -t 591-notifier .
```

使用 `/data` 持久化磁碟區啟動：

```sh
docker run -d \
  --name 591-notifier \
  --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN="123456:請替換成實際權杖" \
  -e TELEGRAM_ALLOWED_USER_ID="123456789" \
  -v 591-notifier-data:/data \
  591-notifier
```

容器內的預設環境變數：

- `CONFIG_PATH=/data/config.yaml`
- `DATABASE_PATH=/data/listings.db`
- `CONFIG_TEMPLATE_PATH=/app/config.yaml`

第一次啟動時，系統會將映像內附的設定範本複製到執行期設定路徑。修改後的
YAML 與 SQLite 資料庫都會保存在 `/data`，更換容器後仍可繼續使用。

## GitHub Container Registry

[`.github/workflows/test.yml`](.github/workflows/test.yml) 會先執行所有單元測試。
分支或標籤推送時，只有測試全部通過才會執行映像建置，並將多平台映像發布至：

```text
ghcr.io/擁有者/儲存庫名稱
```

同一份映像清單包含 `linux/amd64` 與 `linux/arm64`。提取要求（pull request）
只執行測試，不會發布映像。

## 本機開發

```sh
python -m pip install -r requirements-dev.txt
pytest
```

預設不執行會連線至 591 的整合測試；若要執行，請使用：

```sh
pytest -m integration
```

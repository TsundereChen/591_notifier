# 591 Telegram 租屋通知機器人

本服務會爬取 591 租屋物件，並將尚未通知過的結果傳送給一位 Telegram
使用者。所有爬蟲條件都能透過 Telegram 按鈕設定並儲存至 YAML；完整物件
資料只有在 Telegram 接受訊息後才會記錄到該縣市的 SQLite 資料表。

## 通知與去重機制

每次排程或手動執行時，服務會依序：

1. 固定檢查搜尋結果第一頁，每個縣市最多處理 30 筆不重複的結果。
2. 傳送每筆物件前，先查詢該縣市專屬的 SQLite 資料表。
3. 若物件 ID 已存在，直接略過，不再傳送。
4. 在 `delivery_attempts` 建立只含縣市與物件 ID 的傳送保留紀錄。
5. 將尚未看過的物件傳送至 Telegram。
6. Telegram 接受訊息後，以同一個 SQLite 交易寫入完整物件與 message ID。

Telegram 與 SQLite 無法共用同一個交易。若程序在傳送期間中斷，服務會將該
物件標示為「結果不明」並停止自動重送，以避免重複訊息。請使用 `/pending`
檢查聊天記錄後，按下「已收到，不再傳送」或「未收到，允許重試」。各縣市
使用獨立資料表，例如 `listings_新北市` 與 `listings_台北市`。

舊版資料庫中沒有通知證明的既有物件會標示為 `unknown`，不會被直接當作已
通知，也不會冒險自動重送。它們再次出現在搜尋頁面時會出現在 `/pending`，
由擁有者確認已收到或允許重試。

## 設定 Telegram

1. 透過 [BotFather](https://t.me/BotFather) 建立機器人並複製權杖（token）。
2. 啟動容器時同時設定 `TELEGRAM_BOT_TOKEN` 與
   `TELEGRAM_ALLOWED_USER_ID`。兩者缺一時服務會拒絕啟動。
3. 由指定使用者開啟與機器人的私聊並傳送 `/start` 完成綁定。
4. 使用按鈕啟用縣市，並設定行政區、物件類型、租金範圍及執行排程。

機器人只接受指定使用者在已綁定私聊中的操作，不支援群組操作，也不會因為
擁有者在其他聊天室使用指令而改變通知目的地。機器人權杖只會從環境變數
讀取，不會寫入 YAML。

可用指令：

- `/start`：綁定全新的機器人並開啟選單。
- `/menu`：開啟互動式設定選單。
- `/crawl`：立即執行一次爬蟲。
- `/pending`：處理傳送結果不明、為避免重複而暫停的物件。

排程選單提供常用執行頻率，也可輸入自訂的五欄式 cron 表達式，例如
`*/10 * * * *`。cron 會使用設定檔中的時區，預設為 `Asia/Taipei`。

## YAML 設定

Telegram 中的修改會以原子操作寫回 `config.yaml`。請先從範本建立本機設定：

```sh
cp config.yaml.example config.yaml
```

範本內容如下：

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
第一頁，並自行限制每個縣市最多 30 筆，因此不提供 `pages` 設定。同一縣市
只能出現一次；請把它的行政區、類型與租金條件合併在同一筆設定中。相對的
`database` 路徑會以 `config.yaml` 所在目錄為基準。

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
- `CONFIG_TEMPLATE_PATH=/app/config.yaml.example`

第一次啟動時，系統會將映像內附的設定範本複製到執行期設定路徑。修改後的
YAML 與 SQLite 資料庫都會保存在 `/data`，更換容器後仍可繼續使用。
同一份 `/data` 只允許一個 bot 容器使用；第二個執行個體會因單副本鎖而拒絕
啟動，避免 YAML、SQLite 與 Telegram long polling 互相競爭。

## GitHub Container Registry

[`.github/workflows/test.yml`](.github/workflows/test.yml) 會使用 Python 3.13 與
3.14 執行單元測試，再進行 amd64 容器 smoke test。分支或標籤推送時，只有
上述檢查全部通過才會建置並將多平台映像發布至：

```text
ghcr.io/擁有者/儲存庫名稱
```

同一份映像清單包含 `linux/amd64` 與 `linux/arm64`。提取要求（pull request）
只執行測試，不會發布映像。每日排程與手動 workflow 另會執行連線至 591 的
即時整合測試。GitHub Actions 與 Python Docker 基底映像均鎖定不可變 SHA／
digest，Python 套件也鎖定確切版本。

## 本機開發

```sh
python -m pip install -r requirements-dev.txt
pytest
```

正式程式採用 `src` package 佈局：

```text
src/rent591_notifier/
├── __main__.py       # `python -m rent591_notifier` 執行入口
├── bot.py            # Telegram 指令、按鈕與排程
├── config_store.py   # YAML 設定與單副本鎖
├── crawler.py        # 591 HTTP 請求與頁面解析
├── database.py       # SQLite schema、遷移與通知帳本
└── notifier.py       # 爬取與通知流程協調
```

若要直接從原始碼啟動 bot，可使用：

```sh
PYTHONPATH=src python -m rent591_notifier
```

預設不執行會連線至 591 的整合測試；若要執行，請使用：

```sh
pytest -m integration
```

`requirements.txt` 只包含容器執行期套件；`requirements-dev.txt` 引用前者並
額外加入測試工具，因此兩份檔案分別服務正式映像與本機／CI 開發環境。

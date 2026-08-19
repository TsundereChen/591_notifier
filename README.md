# 591 Telegram 租屋通知機器人

定期搜尋 [591 租屋](https://rent.591.com.tw/) 結果，將符合條件且尚未通知的
物件傳送到 Telegram。縣市、行政區、物件類型、租金範圍、執行排程與可選的 AI
評估，都可以直接在 Telegram 選單設定。

## 功能

- 每個縣市每次讀取搜尋結果前 5 頁，最多取得 150 筆目前結果。
- 通知會包含物件主要資訊與照片，最多傳送 10 張照片。
- 支援多個縣市，每個縣市可設定不同的行政區、物件類型與租金範圍。
- 支援常用排程與自訂五欄式 cron 表達式。
- 可選擇使用 OpenCode Go 或 OpenCode Zen 評估物件，附上推薦分數與理由，或只通知 AI 推薦的物件。
- 只允許指定使用者在私人聊天中操作，避免通知發送到錯誤的聊天室。
- 提供 Docker 映像，支援 `linux/amd64` 與 `linux/arm64`。

## 快速開始

先透過 [BotFather](https://t.me/BotFather) 建立 Telegram bot，然後啟動容器：

```sh
docker run -d \
  --name 591-notifier \
  --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN="你的 bot token" \
  -e TELEGRAM_ALLOWED_USER_ID="你的 Telegram user ID" \
  -v 591-notifier-data:/data \
  ghcr.io/tsunderechen/591_notifier:latest
```

接著由指定使用者開啟與 bot 的私人聊天，傳送 `/start`：

1. `/start` 會綁定通知聊天室並開啟設定選單。
2. 在「縣市與篩選條件」中啟用至少一個縣市。
3. 視需要設定行政區、物件類型、租金與排程。
4. 使用「立即執行爬蟲」或等待下一次排程。

設定檔與 SQLite 資料庫會保存在 `/data`，刪除並重新建立容器也不會遺失設定。
同一個 `/data` 資料卷只應由一個 bot 執行個體使用。

## Telegram 指令

| 指令 | 用途 |
| --- | --- |
| `/start` | 綁定聊天室並開啟主選單 |
| `/menu` | 開啟設定選單 |
| `/crawl` | 立即執行一次爬蟲 |
| `/pending` | 查看需要人工確認的通知 |

機器人只接受 `TELEGRAM_ALLOWED_USER_ID` 指定的使用者，且只在已綁定的私人聊天中
回應；群組聊天不會執行操作。

## 設定檔

大多數設定都能從 Telegram 選單完成。若要在本機直接編輯 YAML，可先建立設定檔：

```sh
cp config.yaml.example config.yaml
```

基本設定如下：

```yaml
database: listings.db
schedule: "*/15 * * * *"
timezone: Asia/Taipei

crawl:
  - region: 新北市
    sections: [土城區, 中和區]
    kinds: [整層住家, 獨立套房]
    price: {min: 10000, max: 30000}
```

`sections`、`kinds` 與 `price` 可省略或留空，代表不限該項條件。同一縣市只能設定
一次；若要套用多個條件，請合併在同一筆縣市設定中。相對的 `database` 路徑會以
YAML 檔案所在目錄為基準。

容器第一次啟動時會從 `config.yaml.example` 建立 `/data/config.yaml`。之後從
Telegram 修改的設定也會保存到該檔案。

## AI 評估（選用）

AI 預設停用。啟用後會讀取物件詳情與照片，再根據預設或自訂條件產生推薦結果。
可在 Telegram 的「AI 評估」選單設定提供者、模型、評估標準，以及要不要過濾不推薦
的物件。

- **OpenCode Go**：需要設定 `OPENCODE_GO_API_KEY`。
- **OpenCode Zen**：免費模型可以不設定 API key；付費模型需要 `OPENCODE_ZEN_API_KEY`。

例如，啟用 Go 時將下列選項加入上方的 `docker run` 命令：

```sh
-e OPENCODE_GO_API_KEY="你的 OpenCode Go API key"
```

AI 評估失敗時不會因此遺漏物件，該物件仍會照常通知。

## 本機開發

```sh
python -m pip install -r requirements-dev.txt
pre-commit install
pytest
```

從原始碼啟動 bot：

```sh
PYTHONPATH=src python -m rent591_notifier
```

需要連線 591 實站時，另外執行整合測試：

```sh
pytest -m integration
```

正式程式位於 `src/rent591_notifier/`。CI 會在 Python 3.13 與 3.14 執行測試，並
確認 Docker 映像可以正常匯入與建置。

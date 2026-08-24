# 591 Telegram 租屋通知機器人

定期搜尋 [591 租屋](https://rent.591.com.tw/) 結果，將符合條件且尚未通知的
物件傳送到 Telegram。縣市、行政區、物件類型、租金範圍、排除關鍵字、執行排程與可選的 AI
評估，都可以直接在 Telegram 選單設定。

## 功能

- 每個縣市每次讀取搜尋結果前 5 頁，最多取得 150 筆目前結果。
- 通知會包含物件主要資訊與照片，最多傳送 10 張照片。
- 支援多個縣市，每個縣市可設定不同的行政區、物件類型與租金範圍。
- 每個縣市可設定排除關鍵字；以不分大小寫的部分文字比對物件列表標題、標籤與基本資訊，命中任一關鍵字時會標記為已篩選並在後續執行重新比對，移除排除條件後仍符合搜尋條件的物件會恢復通知。
- 支援常用排程與自訂五欄式 cron 表達式。
- 可使用支援 OpenAI Chat Completions 格式與 Bearer API key 認證的 API 評估物件，支援多模型重試與故障轉移，並可附上推薦理由或只通知 AI 推薦的物件。
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
4. 視需要設定「排除關鍵字」，以逗號或換行分隔。
5. 使用「立即執行爬蟲」或等待下一次排程。

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
    exclude_keywords: [頂樓加蓋, 雅房]
```

`sections`、`kinds`、`price` 與 `exclude_keywords` 可省略或留空，代表不限該項條件。同一縣市只能設定
一次；若要套用多個條件，請合併在同一筆縣市設定中。相對的 `database` 路徑會以
YAML 檔案所在目錄為基準。

容器第一次啟動時會從 `config.yaml.example` 建立 `/data/config.yaml`。之後從
Telegram 修改的設定也會保存到該檔案。

## AI 評估（選用）

AI 預設停用。啟用後會讀取物件詳情與照片，再根據預設或自訂條件產生推薦結果。
可在 Telegram 的「AI 評估」選單設定 API 端點、API 金鑰、模型清單、評估標準，以及要不要
過濾不推薦的物件。API 端點、API 金鑰與至少一個模型都設定後，AI 評估才會執行；缺少任一項時，
即使 AI 狀態為啟用，也會略過 AI 評估並照常通知物件，不會套用 AI 過濾。

- `api_endpoint`：API 基礎 URL（例如 `https://api.openai.com/v1`）或完整的
  `/chat/completions` URL。不可包含登入資訊、查詢字串或 fragment。建議使用 HTTPS；HTTP
  會以明文傳送 API key、物件文字與照片。
- `api_key`：非空的 Bearer API key，只從此設定讀取並保存於 `config.yaml`；不支援 AI API key
  環境變數或無金鑰 API。請保護資料卷與 Telegram 對話，直接編輯 YAML 時應以引號包住金鑰。
- `models`：依嘗試順序排列的模型 ID 清單，沒有預設值。每次爬蟲中，模型失敗時會重試目前物件；
  累積失敗五次後，該模型在本次爬蟲中停用並改試下一個模型。所有模型都停用後，剩餘物件會略過
  AI 並照常通知。新的排程或手動爬蟲會重新計算失敗次數。

若要分析照片，模型應支援 image input；不支援圖片的模型若回傳 HTTP 400，程式會先退回只傳送
文字，所有請求形式都失敗才會計入該模型的一次失敗。

例如，直接在設定檔填入：

```yaml
ai:
  enabled: true
  filter: true
  api_endpoint: "https://api.openai.com/v1"
  api_key: "sk-..."
  models:
    - "gpt-4o-mini"
    - "openai/gpt-4o"
```

AI 評估失敗時不會因此遺漏物件，該物件仍會照常通知。

從舊版 OpenCode Go／Zen 設定升級時，`provider` 會自動轉換成對應的 `api_endpoint`。
程式不再讀取 `OPENCODE_GO_API_KEY` 或 `OPENCODE_ZEN_API_KEY`；若原本透過環境變數提供金鑰，
請改由 Telegram 的「AI 評估」選單輸入，或寫入 `/data/config.yaml` 的 `ai.api_key`。舊版單一
`model` 設定會自動轉換成 `models` 清單。

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

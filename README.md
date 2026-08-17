# 591 Telegram 租屋通知機器人

爬取 591 租屋搜尋結果，將新物件傳送給指定的 Telegram 使用者，並以 SQLite
避免重複通知。縣市、行政區、物件類型、租金與 cron 排程都能透過按鈕設定。

## 功能

- 每次讀取第一頁，每個縣市最多處理 30 筆結果。
- 每筆通知會從物件詳情頁載入照片，並以 Telegram 相簿傳送最多 10 張。
- 每個縣市使用獨立 SQLite 資料表。
- 只有 Telegram 接受通知後才保存完整物件資料。
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

crawl:
  - region: 新北市
    sections: [土城區, 中和區]
    kinds: [整層住家, 獨立套房]
    price: {min: 10000, max: 30000}
```

`sections`、`kinds` 與 `price` 可留空。同一縣市只能設定一次；相對的資料庫路徑
以 YAML 所在目錄為基準。

## 通知去重

傳送前會先建立投遞保留紀錄；Telegram 接受訊息後，再以 SQLite 交易保存物件
與 message ID。若傳送結果因中斷而無法確認，物件不會自動重送。請使用
`/pending` 查看聊天記錄後，確認已收到或允許重試。舊資料庫中無法證明已通知
的資料也採用相同處理方式。

可用指令：`/start`、`/menu`、`/crawl`、`/pending`。

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

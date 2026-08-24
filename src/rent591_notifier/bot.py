"""Interactive Telegram bot for configuring and running the 591 notifier."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .ai import AIModelError, evaluate_listing
from .config_store import ConfigStore
from .crawler import (
    KINDS,
    REGIONS,
    SECTIONS,
    _resolve_region,
    _validate_price_range,
    crawl_rent_details,
)
from .database import (
    ambiguous_deliveries,
    init_db,
    resolve_ambiguous_delivery,
    resolve_database_path,
)
from .keyword_filter import normalize_keywords
from .notifier import crawl_and_notify

LOGGER = logging.getLogger(__name__)
CRAWL_JOB_NAME = "scheduled-crawl"
MAX_MODEL_FAILURES_PER_CRAWL = 5
AI_TIMEOUT_PER_LISTING = 5 * 60
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
REDACTED = "[REDACTED]"
TELEGRAM_BOT_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\d{5,16}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)

SCHEDULE_PRESETS = {
    "5m": ("每 5 分鐘", "*/5 * * * *"),
    "15m": ("每 15 分鐘", "*/15 * * * *"),
    "30m": ("每 30 分鐘", "*/30 * * * *"),
    "1h": ("每小時", "0 * * * *"),
    "6h": ("每 6 小時", "0 */6 * * *"),
    "8am": ("每天 08:00", "0 8 * * *"),
}

PRICE_PRESETS = {
    "all": ("不限租金", None, None),
    "u10": ("NT$10,000 以下", None, 10000),
    "10-20": ("NT$10,000～20,000", 10000, 20000),
    "20-30": ("NT$20,000～30,000", 20000, 30000),
    "30-40": ("NT$30,000～40,000", 30000, 40000),
    "o40": ("NT$40,000 以上", 40000, None),
}
PRICE_STEP = 5000
DEFAULT_PRICE_MAX = 50000
MAX_IMAGES_PER_LISTING = 10


def _redact_sensitive_text(text, sensitive_values=()):
    for value in sensitive_values:
        if value:
            text = text.replace(str(value), REDACTED)
    return TELEGRAM_BOT_TOKEN_PATTERN.sub(REDACTED, text)


class _RedactingFormatter(logging.Formatter):
    """Redact secrets after rendering the complete log record and traceback."""

    def __init__(self, *args, sensitive_values=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.sensitive_values = tuple(sensitive_values)

    def format(self, record):
        sensitive_values = self.sensitive_values + tuple(
            getattr(record, "sensitive_values", ())
        )
        return _redact_sensitive_text(super().format(record), sensitive_values)


def _configure_logging(token=None):
    handler = logging.StreamHandler()
    handler.setFormatter(_RedactingFormatter(LOG_FORMAT, sensitive_values=(token,)))
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True
    )
    http_log_level = os.getenv("HTTP_LOG_LEVEL", "WARNING").upper()
    try:
        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(http_log_level)
    except ValueError:
        for logger_name in ("httpx", "httpcore"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        LOGGER.warning(
            "Invalid HTTP_LOG_LEVEL=%r; defaulting third-party HTTP logs to WARNING",
            http_log_level,
        )
        http_log_level = "WARNING"
    LOGGER.info(
        "Logging configured application_level=%s third_party_http_level=%s",
        os.getenv("LOG_LEVEL", "INFO").upper(),
        http_log_level,
    )


def _store(application):
    return application.bot_data["config_store"]


def _job_for_region(data, region_id):
    return ConfigStore.find_job(data, region_id)


def _selected_ids(values, mapping):
    values = values or []
    return {key for key, name in mapping.items() if key in values or name in values}


def _allowed_user_id(value=None):
    raw = value if value is not None else os.getenv("TELEGRAM_ALLOWED_USER_ID")
    if raw is None or str(raw).strip() == "":
        raise RuntimeError("必須設定 TELEGRAM_ALLOWED_USER_ID，機器人拒絕開放式認領")
    try:
        allowed_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID 必須是整數") from exc
    if allowed_id <= 0:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID 必須是正整數")
    return allowed_id


async def _authorized(update, application, claim=False):
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    if chat.type != ChatType.PRIVATE:
        return False
    if user.id != application.bot_data["allowed_user_id"]:
        return False

    store = _store(application)
    data = store.load()
    owner_id = data["telegram"].get("owner_user_id")
    if owner_id is None and claim:
        store.bind_owner(user.id, chat.id)
        return True
    if owner_id != user.id:
        return False
    if data["telegram"].get("chat_id") is None and claim:
        store.bind_owner(user.id, chat.id)
        return True
    return data["telegram"].get("chat_id") == chat.id


async def _reject(update):
    if update.callback_query:
        await update.callback_query.answer(
            "僅允許指定擁有者在與機器人的私聊中操作。", show_alert=True
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "僅允許指定擁有者在與機器人的私聊中操作。"
        )


async def _edit(query, text, keyboard):
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


def _home_view(data):
    text = (
        "591 租屋通知機器人\n\n"
        f"{_config_summary(data)}\n\n"
        "成功傳送或被篩選的物件會寫入資料庫。每次執行時，"
        "每個縣市最多檢查 5 頁、150 筆結果。"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏙 縣市與篩選條件", callback_data="regions")],
            [InlineKeyboardButton("🤖 AI 評估", callback_data="ai")],
            [InlineKeyboardButton("🕒 執行排程", callback_data="schedule")],
            [InlineKeyboardButton("▶️ 立即執行爬蟲", callback_data="run")],
            [InlineKeyboardButton("⚠️ 結果不明通知", callback_data="pending")],
            [InlineKeyboardButton("📋 顯示目前設定", callback_data="show")],
        ]
    )
    return text, keyboard


def _regions_view(data):
    enabled = {_resolve_region(job["region"]) for job in data["crawl"]}
    buttons = []
    row = []
    for region_id, name in REGIONS.items():
        mark = "✅" if region_id in enabled else "▫️"
        row.append(
            InlineKeyboardButton(f"{mark} {name}", callback_data=f"region:{region_id}")
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ 主選單", callback_data="home")])
    return "請選擇要啟用或編輯篩選條件的縣市。", InlineKeyboardMarkup(buttons)


def _region_view(data, region_id):
    name = REGIONS[region_id]
    job = _job_for_region(data, region_id)
    if job is None:
        text = f"{name}\n\n狀態：未啟用"
        buttons = [
            [InlineKeyboardButton("啟用", callback_data=f"region_toggle:{region_id}")],
            [InlineKeyboardButton("⬅️ 縣市列表", callback_data="regions")],
        ]
        return text, InlineKeyboardMarkup(buttons)

    sections = job.get("sections") or []
    kinds = job.get("kinds") or []
    price = job.get("price") or {}
    exclude_keywords = job.get("exclude_keywords") or []
    section_text = "、".join(map(str, sections)) if sections else "全部"
    kind_text = "、".join(map(str, kinds)) if kinds else "全部"
    price_text = _format_price(price.get("min"), price.get("max"))
    keyword_text = "、".join(map(str, exclude_keywords)) if exclude_keywords else "無"
    text = (
        f"{name}\n\n狀態：已啟用\n行政區：{section_text}\n"
        f"物件類型：{kind_text}\n租金：{price_text}\n"
        f"排除關鍵字：{keyword_text}"
    )
    buttons = [
        [InlineKeyboardButton("📍 行政區", callback_data=f"sections:{region_id}")],
        [InlineKeyboardButton("🏠 物件類型", callback_data=f"kinds:{region_id}")],
        [InlineKeyboardButton("💰 租金範圍", callback_data=f"price:{region_id}")],
        [InlineKeyboardButton("🚫 排除關鍵字", callback_data=f"keywords:{region_id}")],
        [InlineKeyboardButton("停用", callback_data=f"region_toggle:{region_id}")],
        [InlineKeyboardButton("⬅️ 縣市列表", callback_data="regions")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _sections_view(data, region_id):
    job = _job_for_region(data, region_id) or {}
    selected = _selected_ids(job.get("sections"), SECTIONS[region_id])
    rows = []
    row = []
    for section_id, name in SECTIONS[region_id].items():
        mark = "✅" if section_id in selected else "▫️"
        row.append(
            InlineKeyboardButton(
                f"{mark} {name}",
                callback_data=f"section_toggle:{region_id}:{section_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "全部行政區", callback_data=f"sections_clear:{region_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ 縣市設定", callback_data=f"region:{region_id}")],
        ]
    )
    note = (
        "未勾選時代表全部行政區。"
        if not selected
        else f"已選擇 {len(selected)} 個行政區。"
    )
    return f"{REGIONS[region_id]}行政區\n\n{note}", InlineKeyboardMarkup(rows)


def _kinds_view(data, region_id):
    job = _job_for_region(data, region_id) or {}
    selected = _selected_ids(job.get("kinds"), KINDS)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if kind_id in selected else '▫️'} {name}",
                callback_data=f"kind_toggle:{region_id}:{kind_id}",
            )
        ]
        for kind_id, name in KINDS.items()
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "全部類型", callback_data=f"kinds_clear:{region_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ 縣市設定", callback_data=f"region:{region_id}")],
        ]
    )
    note = (
        "未勾選時代表全部類型。" if not selected else f"已選擇 {len(selected)} 種類型。"
    )
    return f"{REGIONS[region_id]}物件類型\n\n{note}", InlineKeyboardMarkup(rows)


def _keywords_view(data, region_id):
    job = _job_for_region(data, region_id) or {}
    keywords = job.get("exclude_keywords") or []
    keyword_text = "、".join(map(str, keywords)) if keywords else "未設定"
    text = (
        f"{REGIONS[region_id]}排除關鍵字\n\n"
        f"目前設定：{keyword_text}\n"
        "物件標題、標籤與列表基本資訊只要命中任一關鍵字，就不會通知。"
    )
    rows = [
        [
            InlineKeyboardButton(
                "✏️ 編輯關鍵字", callback_data=f"keywords_edit:{region_id}"
            )
        ],
    ]
    if keywords:
        rows.append(
            [
                InlineKeyboardButton(
                    "清除關鍵字", callback_data=f"keywords_clear:{region_id}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("⬅️ 縣市設定", callback_data=f"region:{region_id}")]
    )
    return text, InlineKeyboardMarkup(rows)


def _ai_view(data):
    ai = data.get("ai") or {}
    enabled = bool(ai.get("enabled"))
    filter_mode = bool(ai.get("filter", True))
    api_endpoint = ai.get("api_endpoint") or "未設定"
    models = ai.get("models") or []
    models_text = "、".join(models) or "未設定"
    criteria = (ai.get("criteria") or "").strip() or "（使用預設標準）"
    key_status = "已設定" if ai.get("api_key") else "未設定"
    missing = [
        label
        for label, value in (
            ("API 端點", ai.get("api_endpoint")),
            ("API 金鑰", ai.get("api_key")),
            ("模型", models),
        )
        if not value
    ]
    status = (
        f"啟用（設定不完整：{'、'.join(missing)}）"
        if enabled and missing
        else "啟用" if enabled else "停用"
    )
    text = (
        "AI 物件評估\n\n"
        f"狀態：{status}\n"
        f"模式：{'過濾不推薦的物件' if filter_mode else '僅在通知中標註評語'}\n"
        f"API 端點：{api_endpoint}\n"
        f"模型：{models_text}\n"
        f"API 金鑰：{key_status}\n"
        f"評估標準：{criteria}"
    )
    buttons = [
        [
            InlineKeyboardButton(
                "停用 AI 評估" if enabled else "啟用 AI 評估",
                callback_data="ai_toggle",
            )
        ],
        [InlineKeyboardButton("切換模式（過濾／標註）", callback_data="ai_mode")],
        [InlineKeyboardButton("✏️ API 端點", callback_data="ai_api_endpoint")],
        [InlineKeyboardButton("✏️ 評估標準", callback_data="ai_criteria")],
        [InlineKeyboardButton("✏️ 模型清單", callback_data="ai_models")],
        [InlineKeyboardButton("✏️ API 金鑰", callback_data="ai_api_key")],
        [InlineKeyboardButton("⬅️ 主選單", callback_data="home")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def _format_price(price_min, price_max):
    if price_min is None and price_max is None:
        return "不限"
    if price_min is None:
        return f"NT${price_max:,} 以下"
    if price_max is None:
        return f"NT${price_min:,} 以上"
    return f"NT${price_min:,}～{price_max:,}"


def _price_bound_label(name, value):
    return f"{name}：{'不限' if value is None else f'{value:,}'}"


def _adjust_price_bound(price_min, price_max, bound, direction):
    if direction not in {-1, 1}:
        raise ValueError("price adjustment direction must be -1 or 1")
    if bound == "min":
        if price_min is None and direction < 0:
            return price_min, price_max
        adjusted = (price_min or 0) + direction * PRICE_STEP
        adjusted = None if adjusted <= 0 else adjusted
        if adjusted is not None and price_max is not None:
            adjusted = min(adjusted, price_max)
        return adjusted, price_max
    if bound == "max":
        baseline = price_max
        if baseline is None:
            baseline = max(DEFAULT_PRICE_MAX, price_min or 0)
        adjusted = max(0, baseline + direction * PRICE_STEP)
        if price_min is not None:
            adjusted = max(adjusted, price_min)
        return price_min, adjusted
    raise ValueError(f"unknown price bound: {bound!r}")


def _price_view(data, region_id):
    job = _job_for_region(data, region_id) or {}
    price = job.get("price") or {}
    price_min = price.get("min")
    price_max = price.get("max")
    rows = [
        [InlineKeyboardButton(label, callback_data=f"price_set:{region_id}:{key}")]
        for key, (label, _, _) in PRICE_PRESETS.items()
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "−5,000", callback_data=f"price_adjust:{region_id}:min:-1"
                ),
                InlineKeyboardButton(
                    _price_bound_label("最低", price_min),
                    callback_data=f"price_clear:{region_id}:min",
                ),
                InlineKeyboardButton(
                    "+5,000", callback_data=f"price_adjust:{region_id}:min:1"
                ),
            ],
            [
                InlineKeyboardButton(
                    "−5,000", callback_data=f"price_adjust:{region_id}:max:-1"
                ),
                InlineKeyboardButton(
                    _price_bound_label("最高", price_max),
                    callback_data=f"price_clear:{region_id}:max",
                ),
                InlineKeyboardButton(
                    "+5,000", callback_data=f"price_adjust:{region_id}:max:1"
                ),
            ],
            [
                InlineKeyboardButton(
                    "自訂範圍", callback_data=f"price_custom:{region_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ 縣市設定", callback_data=f"region:{region_id}")],
        ]
    )
    return (
        (
            f"{REGIONS[region_id]}租金範圍\n\n目前設定："
            f"{_format_price(price_min, price_max)}\n"
            "點選中間的最低／最高租金可取消該端限制。"
        ),
        InlineKeyboardMarkup(rows),
    )


def _schedule_view(data):
    rows = [
        [InlineKeyboardButton(label, callback_data=f"schedule_set:{key}")]
        for key, (label, _) in SCHEDULE_PRESETS.items()
    ]
    rows.extend(
        [
            [InlineKeyboardButton("自訂 cron 表達式", callback_data="schedule_custom")],
            [InlineKeyboardButton("⬅️ 主選單", callback_data="home")],
        ]
    )
    return (
        f"爬蟲執行排程\n\n目前設定：{data['schedule']}\n時區：{data['timezone']}",
        InlineKeyboardMarkup(rows),
    )


def _format_schedule(expression):
    for label, preset in SCHEDULE_PRESETS.values():
        if expression == preset:
            return label
    match = re.fullmatch(r"\*/(\d+) \* \* \* \*", expression)
    if match:
        return f"每 {int(match.group(1))} 分鐘"
    match = re.fullmatch(r"(\d+) (\d+) \* \* \*", expression)
    if match:
        minute, hour = map(int, match.groups())
        if 0 <= minute <= 59 and 0 <= hour <= 23:
            return f"每天 {hour:02d}:{minute:02d}"
    return f"自訂排程（{expression}）"


def _config_summary(data):
    ai = data.get("ai") or {}
    if ai.get("enabled"):
        mode = "過濾模式" if ai.get("filter", True) else "標註模式"
        models = "、".join(ai.get("models") or []) or "未設定模型"
        ai_text = f"啟用（{mode}，{models}）"
    else:
        ai_text = "停用"
    lines = [
        "目前設定",
        "",
        "排程：",
        f"  執行頻率：{_format_schedule(data['schedule'])}",
        f"  時區：{data['timezone']}",
        f"AI 評估：{ai_text}",
        "已啟用縣市：",
    ]
    if not data["crawl"]:
        lines.append("  無")
        return "\n".join(lines)
    for job in data["crawl"]:
        price = job.get("price") or {}
        lines.extend(
            [
                f"  - {job['region']}",
                f"    行政區：{'、'.join(map(str, job.get('sections') or [])) or '全部'}",
                f"    物件類型：{'、'.join(map(str, job.get('kinds') or [])) or '全部'}",
                f"    租金範圍：{_format_price(price.get('min'), price.get('max'))}",
                f"    排除關鍵字：{'、'.join(map(str, job.get('exclude_keywords') or [])) or '無'}",
            ]
        )
    return "\n".join(lines)


def _cron_trigger(data):
    try:
        timezone = ZoneInfo(data["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"無法辨識時區：{data['timezone']!r}") from exc
    try:
        return CronTrigger.from_crontab(data["schedule"], timezone=timezone)
    except ValueError as exc:
        raise ValueError(f"cron 表達式格式錯誤：{data['schedule']!r}") from exc


def reschedule(application):
    data = _store(application).load()
    trigger = _cron_trigger(data)
    for job in application.job_queue.get_jobs_by_name(CRAWL_JOB_NAME):
        job.schedule_removal()
    application.job_queue.run_custom(
        scheduled_crawl,
        job_kwargs={"trigger": trigger, "coalesce": True, "max_instances": 1},
        name=CRAWL_JOB_NAME,
    )
    LOGGER.info(
        "Crawl schedule configured expression=%s timezone=%s",
        data["schedule"],
        data["timezone"],
    )


def _listing_message(region, listing):
    details = " · ".join(
        filter(
            None,
            [
                listing.get("kind"),
                listing.get("layout"),
                listing.get("area"),
                listing.get("floor"),
            ],
        )
    )
    lines = [
        f"🏠 {listing.get('title', '新租屋物件')}",
        f"💰 {listing.get('price', '')}",
        f"📍 {region} {listing.get('location', '')}",
    ]
    if details:
        lines.append(f"🏢 {details}")
    verdict = listing.get("ai")
    if isinstance(verdict, dict):
        if verdict.get("unavailable"):
            lines.append("🤖 AI 評估：⚠️ 暫時無法評估")
        else:
            good = bool(verdict.get("good"))
            score = verdict.get("score")
            score_text = f"（{score}/10）" if isinstance(score, int) else ""
            lines.append(
                f"🤖 AI 評估：{'✅ 推薦' if good else '⚠️ 不推薦'}{score_text}"
            )
            reason = str(verdict.get("reason") or "").strip()
            if reason:
                lines.append(f"💭 {reason}")
    if listing.get("url"):
        lines.extend(["", listing["url"]])
    return "\n".join(lines)


def _usable_image_urls(values):
    """Return unique HTTP image URLs within Telegram's album limit."""
    images = []
    for value in values:
        if (
            isinstance(value, str)
            and value.startswith(("https://", "http://"))
            and value not in images
        ):
            images.append(value)
            if len(images) == MAX_IMAGES_PER_LISTING:
                break
    return images


async def _listing_images(listing):
    """Load the listing's detail-page album, falling back to its thumbnail."""
    preloaded = _usable_image_urls(listing.get("images") or [])
    if preloaded:
        return preloaded
    fallback = _usable_image_urls([listing.get("image")])
    url = listing.get("url")
    if not url:
        return fallback

    try:
        payload = await asyncio.to_thread(crawl_rent_details, url, delay=0)
        details = json.loads(payload)
        result = details.get("listings")
        if (
            not isinstance(result, list)
            or not result
            or not isinstance(result[0], dict)
        ):
            raise ValueError("detail crawler returned an invalid payload")
        images = result[0].get("images")
        if not isinstance(images, list):
            return fallback
        return _usable_image_urls(images) or fallback
    except Exception:
        LOGGER.warning(
            "Could not load detail-page images; using thumbnail listing_id=%s",
            listing.get("id", "unknown"),
            exc_info=True,
        )
        return fallback


async def _send_listing(bot, chat_id, text, images, *, listing_id="unknown"):
    """Send one listing as a photo, album, or text-only fallback."""
    if len(images) > 1:
        media = [
            InputMediaPhoto(media=image, caption=text if index == 0 else None)
            for index, image in enumerate(images[:MAX_IMAGES_PER_LISTING])
        ]
        try:
            messages = await bot.send_media_group(chat_id=chat_id, media=media)
            return messages[0]
        except BadRequest as exc:
            LOGGER.warning(
                "Telegram rejected listing album; falling back listing_id=%s error=%s",
                listing_id,
                exc,
            )
    if images:
        try:
            return await bot.send_photo(chat_id=chat_id, photo=images[0], caption=text)
        except BadRequest as exc:
            LOGGER.warning(
                "Telegram rejected listing photo; falling back to text listing_id=%s "
                "error=%s",
                listing_id,
                exc,
            )
    return await bot.send_message(
        chat_id=chat_id, text=text, disable_web_page_preview=False
    )


def _pending_rows(store):
    data = store.load()
    db_path = resolve_database_path(store.path, data["database"])
    conn = init_db(db_path)
    try:
        return ambiguous_deliveries(conn, limit=5)
    finally:
        conn.close()


async def _pending_view(store):
    pending = await asyncio.to_thread(_pending_rows, store)
    if not pending:
        return (
            "目前沒有結果不明的通知。",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 主選單", callback_data="home")]]
            ),
        )
    rows = []
    lines = [
        "結果不明通知",
        "",
        "這些物件推送失敗或結果無法確認，下次爬蟲會自動重試。",
        "若聊天記錄中已收到，可標記為已送達以避免重複：",
    ]
    for item in pending:
        region_id = item["region_id"]
        listing_id = item["listing_id"]
        detail = f"• {item['region']} #{listing_id}（已嘗試 {item.get('attempt_count', 0)} 次）"
        if item.get("last_error"):
            detail += f"\n  {str(item['last_error'])[:160]}"
        lines.extend(["", detail])
        rows.append(
            [
                InlineKeyboardButton(
                    "已收到，不再傳送",
                    callback_data=f"delivery:sent:{region_id}:{listing_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ 主選單", callback_data="home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _resolve_pending(store, region_id, listing_id, delivered):
    data = store.load()
    db_path = resolve_database_path(store.path, data["database"])
    conn = init_db(db_path, [region_id])
    try:
        return resolve_ambiguous_delivery(
            conn,
            region_id,
            listing_id,
            delivered=delivered,
            now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    finally:
        conn.close()


async def _run_crawler(application, status_chat_id=None):
    data = _store(application).load()
    chat_id = data["telegram"].get("chat_id")
    if not chat_id:
        LOGGER.warning("略過爬蟲：尚未有 Telegram 對話綁定此機器人")
        return None
    if not data["crawl"]:
        LOGGER.warning("Skipping crawl because no regions are enabled")
        if status_chat_id:
            await application.bot.send_message(status_chat_id, "尚未啟用任何縣市。")
        return None

    evaluate = None
    ai_config = data.get("ai") or {}
    if ai_config.get("enabled"):
        missing = [
            label
            for label, value in (
                ("API endpoint", ai_config.get("api_endpoint")),
                ("API key", ai_config.get("api_key")),
                ("models", ai_config.get("models")),
            )
            if not value
        ]
        if missing:
            LOGGER.warning(
                "AI evaluation is enabled but required settings are missing: %s; "
                "continuing without AI",
                ", ".join(missing),
            )
        else:
            filter_rejected = ai_config.get("filter", True)
            models = ai_config["models"]
            model_failures = dict.fromkeys(models, 0)
            models_exhausted = False

            async def evaluate(region, listing, crawl_filters=None):
                nonlocal models_exhausted
                if models_exhausted:
                    listing["ai"] = {"unavailable": True}
                    return True
                deadline = asyncio.get_running_loop().time() + AI_TIMEOUT_PER_LISTING
                for model in models:
                    if model_failures[model] >= MAX_MODEL_FAILURES_PER_CRAWL:
                        continue
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        listing["ai"] = {"unavailable": True}
                        return True
                    try:
                        verdict, images = await asyncio.wait_for(
                            asyncio.to_thread(
                                evaluate_listing,
                                ai_config,
                                region,
                                listing,
                                model=model,
                                crawl_filters=crawl_filters,
                            ),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        model_failures[model] += 1
                        LOGGER.warning(
                            "AI listing evaluation exceeded the per-listing timeout; "
                            "delivering without AI verdict region=%s listing_id=%s "
                            "model=%s timeout_s=%s failures=%s/%s",
                            region,
                            listing.get("id", "unknown"),
                            model,
                            AI_TIMEOUT_PER_LISTING,
                            model_failures[model],
                            MAX_MODEL_FAILURES_PER_CRAWL,
                        )
                        listing["ai"] = {"unavailable": True}
                        return True
                    except AIModelError:
                        model_failures[model] += 1
                        LOGGER.warning(
                            "AI model evaluation failed; trying next model if available "
                            "region=%s listing_id=%s model=%s failures=%s/%s",
                            region,
                            listing.get("id", "unknown"),
                            model,
                            model_failures[model],
                            MAX_MODEL_FAILURES_PER_CRAWL,
                            exc_info=True,
                            extra={"sensitive_values": (ai_config.get("api_key"),)},
                        )
                        continue
                    except Exception:
                        LOGGER.exception(
                            "AI evaluation failed; delivering listing without AI verdict "
                            "region=%s listing_id=%s model=%s",
                            region,
                            listing.get("id", "unknown"),
                            model,
                            extra={"sensitive_values": (ai_config.get("api_key"),)},
                        )
                        listing["ai"] = {"unavailable": True}
                        return True
                    listing["ai"] = verdict
                    if images:
                        listing["images"] = images
                    return bool(verdict["good"]) or not filter_rejected
                models_exhausted = all(
                    failures >= MAX_MODEL_FAILURES_PER_CRAWL
                    for failures in model_failures.values()
                )
                if models_exhausted:
                    LOGGER.error(
                        "All AI models reached the per-crawl failure limit; "
                        "continuing without AI"
                    )
                listing["ai"] = {"unavailable": True}
                return True

    async def notify(region, listing):
        text = _listing_message(region, listing)
        images = await _listing_images(listing)
        listing["images"] = images
        for attempt in range(3):
            try:
                message = await _send_listing(
                    application.bot,
                    chat_id,
                    text,
                    images,
                    listing_id=listing.get("id", "unknown"),
                )
                return {"chat_id": chat_id, "message_id": message.message_id}
            except RetryAfter as exc:
                if attempt == 2:
                    LOGGER.error(
                        "Telegram rate limit exhausted region=%s listing_id=%s "
                        "attempt=%s retry_after_s=%s",
                        region,
                        listing.get("id", "unknown"),
                        attempt + 1,
                        exc.retry_after,
                    )
                    raise
                LOGGER.warning(
                    "Telegram rate limit encountered region=%s listing_id=%s "
                    "attempt=%s retry_after_s=%s",
                    region,
                    listing.get("id", "unknown"),
                    attempt + 1,
                    exc.retry_after,
                )
                await asyncio.sleep(float(exc.retry_after) + 0.25)

    try:
        LOGGER.info(
            "Starting crawl trigger=%s enabled_regions=%s",
            "manual" if status_chat_id else "scheduled",
            len(data["crawl"]),
        )
        crawl_kwargs = {"evaluate": evaluate} if evaluate is not None else {}
        summary = await crawl_and_notify(
            _store(application).path, notify, **crawl_kwargs
        )
    except Exception:
        LOGGER.exception(
            "Crawl execution failed trigger=%s",
            "manual" if status_chat_id else "scheduled",
        )
        error_chat_id = status_chat_id or chat_id
        try:
            await application.bot.send_message(
                error_chat_id, "爬蟲執行失敗，請查看容器日誌。"
            )
        except Exception:
            LOGGER.exception("Could not send crawl failure report to Telegram")
        return None
    LOGGER.info(
        "Crawl execution succeeded trigger=%s fetched=%s notified=%s failed=%s",
        "manual" if status_chat_id else "scheduled",
        summary.get("fetched", "unknown"),
        summary.get("notified", "unknown"),
        summary.get("failed", "unknown"),
    )
    return summary


def enqueue_crawl(application, status_chat_id=None):
    """Atomically start one background crawl from the event-loop thread."""
    current = application.bot_data.get("crawl_task")
    if current is not None and not current.done():
        LOGGER.info("Crawl request ignored because another crawl is active")
        return None
    task = application.create_task(
        _run_crawler(application, status_chat_id), name="591-crawl"
    )
    application.bot_data["crawl_task"] = task
    LOGGER.info(
        "Crawl task enqueued trigger=%s", "manual" if status_chat_id else "scheduled"
    )

    def clear(completed):
        if application.bot_data.get("crawl_task") is completed:
            application.bot_data["crawl_task"] = None

    task.add_done_callback(clear)
    return task


async def scheduled_crawl(context: ContextTypes.DEFAULT_TYPE):
    LOGGER.info("Scheduled crawl triggered")
    enqueue_crawl(context.application)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application, claim=True):
        await _reject(update)
        return
    text, keyboard = _home_view(_store(context.application).load())
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    text, keyboard = _home_view(_store(context.application).load())
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def ai_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    text, keyboard = _ai_view(_store(context.application).load())
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def crawl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    task = enqueue_crawl(context.application, update.effective_chat.id)
    await update.effective_message.reply_text(
        "正在背景執行爬蟲……" if task else "爬蟲正在執行中，請稍候。"
    )


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    text, keyboard = await _pending_view(_store(context.application))
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    query = update.callback_query
    await query.answer()
    action = query.data
    store = _store(context.application)
    context.user_data.pop("awaiting", None)

    if action == "home":
        view = _home_view(store.load())
    elif action == "regions":
        view = _regions_view(store.load())
    elif action.startswith("region:"):
        view = _region_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("region_toggle:"):
        region_id = int(action.split(":")[1])
        store.toggle_region(region_id)
        view = _region_view(store.load(), region_id)
    elif action.startswith("sections:"):
        view = _sections_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("section_toggle:"):
        _, region, section = action.split(":")
        store.toggle_section(int(region), int(section))
        view = _sections_view(store.load(), int(region))
    elif action.startswith("sections_clear:"):
        region_id = int(action.split(":")[1])
        store.clear_sections(region_id)
        view = _sections_view(store.load(), region_id)
    elif action.startswith("kinds:"):
        view = _kinds_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("kind_toggle:"):
        _, region, kind = action.split(":")
        store.toggle_kind(int(region), int(kind))
        view = _kinds_view(store.load(), int(region))
    elif action.startswith("kinds_clear:"):
        region_id = int(action.split(":")[1])
        store.clear_kinds(region_id)
        view = _kinds_view(store.load(), region_id)
    elif action.startswith("keywords:"):
        view = _keywords_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("keywords_edit:"):
        region_id = int(action.split(":")[1])
        context.user_data["awaiting"] = ("keywords", region_id)
        await query.message.reply_text(
            "請輸入要排除的關鍵字，以逗號或換行分隔；任一關鍵字命中物件列表資訊就不會通知。"
            "輸入 - 清除全部關鍵字。"
        )
        return
    elif action.startswith("keywords_clear:"):
        region_id = int(action.split(":")[1])
        store.set_exclude_keywords(region_id, None)
        view = _keywords_view(store.load(), region_id)
    elif action.startswith("price:"):
        view = _price_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("price_set:"):
        _, region, preset = action.split(":")
        _, price_min, price_max = PRICE_PRESETS[preset]
        store.set_price(int(region), price_min, price_max)
        view = _price_view(store.load(), int(region))
    elif action.startswith("price_adjust:"):
        _, region, bound, direction = action.split(":")
        region_id = int(region)
        price = (_job_for_region(store.load(), region_id) or {}).get("price") or {}
        price_min, price_max = _adjust_price_bound(
            price.get("min"), price.get("max"), bound, int(direction)
        )
        store.set_price(region_id, price_min, price_max)
        view = _price_view(store.load(), region_id)
    elif action.startswith("price_clear:"):
        _, region, bound = action.split(":")
        region_id = int(region)
        price = (_job_for_region(store.load(), region_id) or {}).get("price") or {}
        price_min = None if bound == "min" else price.get("min")
        price_max = None if bound == "max" else price.get("max")
        store.set_price(region_id, price_min, price_max)
        view = _price_view(store.load(), region_id)
    elif action.startswith("price_custom:"):
        region_id = int(action.split(":")[1])
        context.user_data["awaiting"] = ("price", region_id)
        await query.message.reply_text(
            "請輸入最低與最高月租，兩者之間以空格分隔。若不限制其中一端，"
            "請輸入 -，例如：10000 30000 或 40000 -"
        )
        return
    elif action == "ai":
        view = _ai_view(store.load())
    elif action == "ai_toggle":
        store.set_ai_enabled(not store.load()["ai"]["enabled"])
        view = _ai_view(store.load())
    elif action == "ai_mode":
        store.set_ai_filter(not store.load()["ai"]["filter"])
        view = _ai_view(store.load())
    elif action == "ai_criteria":
        context.user_data["awaiting"] = ("ai_criteria", None)
        await query.message.reply_text(
            "請輸入 AI 評估標準，例如：預算兩萬內、重視採光、近捷運。"
            "輸入 - 可恢復預設標準。"
        )
        return
    elif action == "ai_models":
        context.user_data["awaiting"] = ("ai_models", None)
        await query.message.reply_text(
            "請輸入 API 接受的模型 ID，以逗號或換行分隔並依序嘗試。"
            "每個模型在單次爬蟲失敗五次後會停用。輸入 - 清除。"
        )
        return
    elif action == "ai_api_endpoint":
        context.user_data["awaiting"] = ("ai_api_endpoint", None)
        await query.message.reply_text(
            "請輸入 OpenAI Chat Completions API 基礎 URL，例如："
            "https://api.openai.com/v1；也可輸入完整的 /chat/completions URL。"
            "輸入 - 清除。"
        )
        return
    elif action == "ai_api_key":
        context.user_data["awaiting"] = ("ai_api_key", None)
        await query.message.reply_text("請輸入 API 金鑰，或輸入 - 清除。")
        return
    elif action == "schedule":
        view = _schedule_view(store.load())
    elif action.startswith("schedule_set:"):
        preset = action.split(":")[1]
        store.set_schedule(SCHEDULE_PRESETS[preset][1])
        reschedule(context.application)
        view = _schedule_view(store.load())
    elif action == "schedule_custom":
        context.user_data["awaiting"] = ("schedule", None)
        await query.message.reply_text("請輸入五欄式 cron 表達式，例如：*/10 * * * *")
        return
    elif action == "show":
        view = (
            _config_summary(store.load()),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ 主選單", callback_data="home")]]
            ),
        )
    elif action == "pending":
        view = await _pending_view(store)
    elif action.startswith("delivery:"):
        _, decision, region, listing_id = action.split(":", 3)
        if decision not in {"sent", "retry"}:
            return
        await asyncio.to_thread(
            _resolve_pending,
            store,
            int(region),
            listing_id,
            decision == "sent",
        )
        view = await _pending_view(store)
    elif action == "run":
        task = enqueue_crawl(context.application, query.message.chat_id)
        await query.message.reply_text(
            "正在背景執行爬蟲……" if task else "爬蟲正在執行中，請稍候。"
        )
        view = _home_view(store.load())
    else:
        return
    await _edit(query, *view)


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _authorized(update, context.application):
        await _reject(update)
        return
    awaiting = context.user_data.pop("awaiting", None)
    if not awaiting:
        await update.effective_message.reply_text("請使用 /menu 設定爬蟲。")
        return
    kind, region_id = awaiting
    store = _store(context.application)
    value = update.effective_message.text.strip()
    if kind == "schedule":
        try:
            data = store.load()
            data["schedule"] = value
            _cron_trigger(data)
        except ValueError as exc:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text(str(exc))
            return
        store.set_schedule(value)
        reschedule(context.application)
        await update.effective_message.reply_text("排程已更新。請使用 /menu 繼續設定。")
        return

    if kind == "ai_criteria":
        try:
            store.set_ai_criteria(None if value == "-" else value)
        except ValueError:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text(
                "評估標準不可為空，且最多 2,000 個字元。"
            )
            return
        await update.effective_message.reply_text(
            "AI 評估標準已更新。請使用 /ai 繼續設定。"
        )
        return

    if kind == "ai_models":
        models = (
            []
            if value == "-"
            else [
                model.strip() for model in re.split(r"[,，\n]+", value) if model.strip()
            ]
        )
        try:
            if not models and value != "-":
                raise ValueError
            store.set_ai_models(models)
        except ValueError:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text(
                "模型清單無效；請輸入不重複的模型 ID，並以逗號或換行分隔。"
            )
            return
        await update.effective_message.reply_text(
            "AI 模型清單已更新。請使用 /ai 繼續設定。"
        )
        return

    if kind == "ai_api_endpoint":
        try:
            store.set_ai_api_endpoint(None if value == "-" else value)
        except ValueError:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text(
                "API 端點必須是完整的 HTTP(S) URL，且不可包含登入資訊、"
                "查詢字串或 fragment。"
            )
            return
        await update.effective_message.reply_text(
            "AI API 端點已更新。請使用 /ai 繼續設定。"
        )
        return

    if kind == "ai_api_key":
        try:
            store.set_ai_api_key(None if value == "-" else value)
        except ValueError:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text("API 金鑰格式無效。")
            return
        await update.effective_message.reply_text(
            "AI API 金鑰已更新。請使用 /ai 繼續設定。"
        )
        return

    if kind == "keywords":
        try:
            if value == "-":
                keywords = None
            else:
                keywords = normalize_keywords(
                    [
                        part.strip()
                        for part in re.split(r"[,，、;；\n]+", value)
                        if part.strip()
                    ]
                )
                if not keywords:
                    raise ValueError("請至少輸入一個關鍵字，或輸入 - 清除。")
            store.set_exclude_keywords(region_id, keywords)
        except ValueError as exc:
            context.user_data["awaiting"] = awaiting
            await update.effective_message.reply_text(str(exc))
            return
        await update.effective_message.reply_text(
            "排除關鍵字已更新。請使用 /menu 繼續設定。"
        )
        return

    match = re.fullmatch(r"\s*(\d+|-)\s+(\d+|-)\s*", value)
    if not match:
        context.user_data["awaiting"] = awaiting
        await update.effective_message.reply_text("請輸入兩個數值，例如：10000 30000")
        return
    price_min = None if match.group(1) == "-" else int(match.group(1))
    price_max = None if match.group(2) == "-" else int(match.group(2))
    try:
        _validate_price_range(price_min, price_max)
    except ValueError:
        context.user_data["awaiting"] = awaiting
        await update.effective_message.reply_text(
            "租金範圍無效，最低租金不可高於最高租金。"
        )
        return
    store.set_price(region_id, price_min, price_max)
    await update.effective_message.reply_text("租金範圍已更新。請使用 /menu 繼續設定。")


async def post_init(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "綁定並開啟機器人"),
            BotCommand("menu", "開啟設定選單"),
            BotCommand("ai", "設定 AI 物件評估"),
            BotCommand("crawl", "立即執行爬蟲"),
            BotCommand("pending", "處理結果不明通知"),
        ]
    )
    reschedule(application)
    LOGGER.info("Telegram bot initialized")


async def post_shutdown(application: Application):
    instance_lock = application.bot_data.get("instance_lock")
    if instance_lock is not None:
        instance_lock.close()
    LOGGER.info("Telegram bot shut down")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log unhandled Telegram handler failures without serializing update data."""
    LOGGER.error(
        "Unhandled Telegram handler error update_type=%s",
        type(update).__name__ if update is not None else "none",
        exc_info=context.error,
    )


def build_application(token, config_path, template_path=None, allowed_user_id=None):
    allowed_user_id = _allowed_user_id(allowed_user_id)
    store = ConfigStore(config_path, template_path=template_path)
    data = store.load()
    owner_id = data["telegram"].get("owner_user_id")
    if owner_id is not None and owner_id != allowed_user_id:
        raise RuntimeError(
            "config.yaml 的 owner_user_id 與 TELEGRAM_ALLOWED_USER_ID 不一致"
        )
    _cron_trigger(data)
    instance_lock = store.acquire_instance_lock()
    try:
        application = (
            ApplicationBuilder()
            .token(token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
    except Exception:
        instance_lock.close()
        raise
    application.bot_data["config_store"] = store
    application.bot_data["allowed_user_id"] = allowed_user_id
    application.bot_data["instance_lock"] = instance_lock
    application.bot_data["crawl_task"] = None
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("ai", ai_menu))
    application.add_handler(CommandHandler("crawl", crawl_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    application.add_error_handler(error_handler)
    return application


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    _configure_logging(token)
    if not token:
        raise RuntimeError("必須設定 TELEGRAM_BOT_TOKEN")
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    template_path = os.getenv("CONFIG_TEMPLATE_PATH")
    try:
        application = build_application(token, config_path, template_path)
    except Exception:
        LOGGER.exception(
            "Telegram bot initialization failed config_path=%s", config_path
        )
        raise
    LOGGER.info("Starting Telegram polling config_path=%s", config_path)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram bot stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()

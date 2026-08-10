"""Interactive Telegram bot for configuring and running the 591 notifier."""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from .config_store import ConfigStore
from .crawler import KINDS, REGIONS, SECTIONS, _resolve_region, _validate_price_range
from .database import (
    ambiguous_deliveries,
    init_db,
    resolve_ambiguous_delivery,
    resolve_database_path,
)
from .notifier import crawl_and_notify

LOGGER = logging.getLogger(__name__)
CRAWL_JOB_NAME = "scheduled-crawl"

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
    active = len(data["crawl"])
    text = (
        "591 租屋通知機器人\n\n"
        f"已啟用縣市：{active}\n"
        f"排程：{data['schedule']}\n"
        f"時區：{data['timezone']}\n\n"
        "只有成功傳送通知的物件才會寫入資料庫。每次執行時，"
        "每個縣市最多檢查 30 筆結果。"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏙 縣市與篩選條件", callback_data="regions")],
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
    section_text = "、".join(map(str, sections)) if sections else "全部"
    kind_text = "、".join(map(str, kinds)) if kinds else "全部"
    price_text = _format_price(price.get("min"), price.get("max"))
    text = (
        f"{name}\n\n狀態：已啟用\n行政區：{section_text}\n"
        f"物件類型：{kind_text}\n租金：{price_text}"
    )
    buttons = [
        [InlineKeyboardButton("📍 行政區", callback_data=f"sections:{region_id}")],
        [InlineKeyboardButton("🏠 物件類型", callback_data=f"kinds:{region_id}")],
        [InlineKeyboardButton("💰 租金範圍", callback_data=f"price:{region_id}")],
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


def _format_price(price_min, price_max):
    if price_min is None and price_max is None:
        return "不限"
    if price_min is None:
        return f"NT${price_max:,} 以下"
    if price_max is None:
        return f"NT${price_min:,} 以上"
    return f"NT${price_min:,}～{price_max:,}"


def _price_view(data, region_id):
    job = _job_for_region(data, region_id) or {}
    price = job.get("price") or {}
    rows = [
        [InlineKeyboardButton(label, callback_data=f"price_set:{region_id}:{key}")]
        for key, (label, _, _) in PRICE_PRESETS.items()
    ]
    rows.extend(
        [
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
            f"{_format_price(price.get('min'), price.get('max'))}"
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


def _config_summary(data):
    lines = [
        "目前設定",
        "",
        f"排程：{data['schedule']}",
        f"時區：{data['timezone']}",
    ]
    for job in data["crawl"]:
        price = job.get("price") or {}
        lines.extend(
            [
                "",
                f"• {job['region']}",
                f"  行政區：{'、'.join(map(str, job.get('sections') or [])) or '全部'}",
                f"  物件類型：{'、'.join(map(str, job.get('kinds') or [])) or '全部'}",
                f"  租金：{_format_price(price.get('min'), price.get('max'))}",
            ]
        )
    if not data["crawl"]:
        lines.extend(["", "尚未啟用任何縣市。"])
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
    if listing.get("url"):
        lines.extend(["", listing["url"]])
    return "\n".join(lines)


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
        "這些物件可能已由 Telegram 接受，但資料庫未完成確認。",
        "請先查看聊天記錄，再選擇處理方式：",
    ]
    for item in pending:
        region_id = item["region_id"]
        listing_id = item["listing_id"]
        lines.extend(["", f"• {item['region']} #{listing_id}"])
        rows.append(
            [
                InlineKeyboardButton(
                    "已收到，不再傳送",
                    callback_data=f"delivery:sent:{region_id}:{listing_id}",
                ),
                InlineKeyboardButton(
                    "未收到，允許重試",
                    callback_data=f"delivery:retry:{region_id}:{listing_id}",
                ),
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
        if status_chat_id:
            await application.bot.send_message(status_chat_id, "尚未啟用任何縣市。")
        return None

    async def notify(region, listing):
        text = _listing_message(region, listing)
        for attempt in range(3):
            try:
                message = await application.bot.send_message(
                    chat_id=chat_id, text=text, disable_web_page_preview=False
                )
                return {"chat_id": chat_id, "message_id": message.message_id}
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                await asyncio.sleep(float(exc.retry_after) + 0.25)

    try:
        summary = await crawl_and_notify(_store(application).path, notify)
    except Exception:
        LOGGER.exception("爬蟲執行失敗")
        error_chat_id = status_chat_id or chat_id
        await application.bot.send_message(
            error_chat_id, "爬蟲執行失敗，請查看容器日誌。"
        )
        return None
    if status_chat_id:
        await application.bot.send_message(
            status_chat_id,
            "爬蟲執行完成："
            f"已通知 {summary['notified']} 筆、已傳送過 {summary['skipped']} 筆、"
            f"失敗 {summary['failed']} 筆、結果不明 {summary['ambiguous']} 筆、"
            f"解析失敗 {summary['parse_failed']} 筆。",
        )
    return summary


def enqueue_crawl(application, status_chat_id=None):
    """Atomically start one background crawl from the event-loop thread."""
    current = application.bot_data.get("crawl_task")
    if current is not None and not current.done():
        return None
    task = application.create_task(
        _run_crawler(application, status_chat_id), name="591-crawl"
    )
    application.bot_data["crawl_task"] = task

    def clear(completed):
        if application.bot_data.get("crawl_task") is completed:
            application.bot_data["crawl_task"] = None

    task.add_done_callback(clear)
    return task


async def scheduled_crawl(context: ContextTypes.DEFAULT_TYPE):
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
    elif action.startswith("price:"):
        view = _price_view(store.load(), int(action.split(":")[1]))
    elif action.startswith("price_set:"):
        _, region, preset = action.split(":")
        _, price_min, price_max = PRICE_PRESETS[preset]
        store.set_price(int(region), price_min, price_max)
        view = _price_view(store.load(), int(region))
    elif action.startswith("price_custom:"):
        region_id = int(action.split(":")[1])
        context.user_data["awaiting"] = ("price", region_id)
        await query.message.reply_text(
            "請輸入最低與最高月租，兩者之間以空格分隔。若不限制其中一端，"
            "請輸入 -，例如：10000 30000 或 40000 -"
        )
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
            BotCommand("crawl", "立即執行爬蟲"),
            BotCommand("pending", "處理結果不明通知"),
        ]
    )
    reschedule(application)


async def post_shutdown(application: Application):
    instance_lock = application.bot_data.get("instance_lock")
    if instance_lock is not None:
        instance_lock.close()


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
    application.add_handler(CommandHandler("crawl", crawl_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    return application


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("必須設定 TELEGRAM_BOT_TOKEN")
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    template_path = os.getenv("CONFIG_TEMPLATE_PATH")
    application = build_application(token, config_path, template_path)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

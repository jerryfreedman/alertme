import os
import json
import logging
import requests
import pytz
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALPHA_VANTAGE_KEY = os.environ["ALPHA_VANTAGE_KEY"]
SETTINGS_FILE = "settings.json"
EST = pytz.timezone("America/New_York")

ITEM_ALIASES = {
    "BTC": "BTC",
    "BITCOIN": "BTC",
    "OIL": "OIL",
    "CRUDE": "OIL",
    "CRUDE OIL": "OIL",
    "QQQ": "QQQ",
}

ITEM_LABELS = {
    "BTC": "Bitcoin (BTC)",
    "OIL": "Crude Oil (WTI)",
    "QQQ": "QQQ ETF",
}

EMOJIS = {
    "BTC": "₿",
    "OIL": "🛢️",
    "QQQ": "📈",
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {"chat_ids": [], "tracked_items": ["BTC", "OIL", "QQQ"], "alert_times": ["09:00", "19:00"]}


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def get_btc_price():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        price = data["bitcoin"]["usd"]
        change = data["bitcoin"]["usd_24h_change"]
        arrow = "▲" if change >= 0 else "▼"
        return f"₿ BTC: ${price:,.0f} ({arrow}{abs(change):.2f}% today)"
    except Exception as e:
        logger.error(f"BTC fetch error: {e}")
        return "₿ BTC: data unavailable right now"


def get_alpha_vantage_quote(symbol):
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=GLOBAL_QUOTE&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}"
        )
        resp = requests.get(url, timeout=10)
        data = resp.json()
        q = data["Global Quote"]
        price = float(q["05. price"])
        change_pct = float(q["10. change percent"].replace("%", ""))
        arrow = "▲" if change_pct >= 0 else "▼"
        return price, change_pct, arrow
    except Exception as e:
        logger.error(f"Alpha Vantage {symbol} error: {e}")
        return None, None, None


def get_oil_price():
    # WTI crude oil via Alpha Vantage commodity endpoint
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=WTI&interval=daily&apikey={ALPHA_VANTAGE_KEY}"
        )
        resp = requests.get(url, timeout=10)
        data = resp.json()
        values = data.get("data", [])
        if len(values) < 2:
            return "🛢️ Crude Oil: data unavailable right now"
        today_val = float(values[0]["value"])
        prev_val = float(values[1]["value"])
        change_pct = ((today_val - prev_val) / prev_val) * 100
        arrow = "▲" if change_pct >= 0 else "▼"
        return f"🛢️ WTI Oil: ${today_val:.2f}/bbl ({arrow}{abs(change_pct):.2f}% today)"
    except Exception as e:
        logger.error(f"Oil fetch error: {e}")
        return "🛢️ Crude Oil: data unavailable right now"


def get_qqq_price():
    price, change_pct, arrow = get_alpha_vantage_quote("QQQ")
    if price is None:
        return "📈 QQQ: data unavailable right now"
    return f"📈 QQQ: ${price:,.2f} ({arrow}{abs(change_pct):.2f}% today)"


def get_custom_item_price(symbol):
    price, change_pct, arrow = get_alpha_vantage_quote(symbol)
    if price is None:
        return f"📊 {symbol}: data unavailable right now"
    return f"📊 {symbol}: ${price:,.2f} ({arrow}{abs(change_pct):.2f}% today)"


FETCHERS = {
    "BTC": get_btc_price,
    "OIL": get_oil_price,
    "QQQ": get_qqq_price,
}


def build_alert_message(tracked_items):
    now = datetime.now(EST)
    greeting = "Good morning" if now.hour < 12 else "Good evening"
    lines = [f"*{greeting}! Here's your market briefing* 🌟\n"]
    for item in tracked_items:
        fetcher = FETCHERS.get(item, lambda: get_custom_item_price(item))
        lines.append(fetcher())
    lines.append(f"\n_Prices as of {now.strftime('%b %d, %Y %I:%M %p')} EST_")
    lines.append("_Stay sharp out there! 🚀_")
    return "\n".join(lines)


def format_settings_summary(settings):
    items = settings.get("tracked_items", [])
    times = settings.get("alert_times", [])
    item_list = ", ".join(items) if items else "none"
    time_list = ", ".join(f"{t} EST" for t in times) if times else "none"
    return (
        f"\n\n*Current settings:*\n"
        f"📊 Tracked: {item_list}\n"
        f"🕐 Alert times: {time_list}"
    )


async def send_alert(app, chat_ids, tracked_items):
    msg = build_alert_message(tracked_items)
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send to {chat_id}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = load_settings()
    chat_id = update.effective_chat.id
    if chat_id not in settings["chat_ids"]:
        settings["chat_ids"].append(chat_id)
        save_settings(settings)
    summary = format_settings_summary(settings)
    await update.message.reply_text(
        "👋 *AlertMe is online!*\n\n"
        "I'll ping you with market data on schedule. You can boss me around:\n\n"
        "• `add AAPL` — track a new ticker\n"
        "• `remove QQQ` — stop tracking something\n"
        "• `add time 08:00` — add an alert time (EST)\n"
        "• `remove time 19:00` — remove an alert time\n"
        "• `status` — see current settings\n"
        "• `now` — get prices right now\n"
        + summary,
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = load_settings()
    chat_id = update.effective_chat.id
    if chat_id not in settings["chat_ids"]:
        settings["chat_ids"].append(chat_id)
        save_settings(settings)

    text = update.message.text.strip().lower()

    if text == "status":
        summary = format_settings_summary(settings)
        await update.message.reply_text("Here's what I've got:" + summary, parse_mode="Markdown")
        return

    if text == "now":
        msg = build_alert_message(settings["tracked_items"])
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if text.startswith("add time "):
        time_str = text.replace("add time ", "").strip()
        await cmd_add_time(update, settings, time_str)
        return

    if text.startswith("remove time ") or text.startswith("delete time "):
        time_str = text.split(" ", 2)[-1].strip()
        await cmd_remove_time(update, settings, time_str)
        return

    if text.startswith("add "):
        symbol = text.replace("add ", "").strip().upper()
        symbol = ITEM_ALIASES.get(symbol, symbol)
        await cmd_add_item(update, settings, symbol)
        return

    if text.startswith("remove ") or text.startswith("delete "):
        symbol = text.split(" ", 1)[-1].strip().upper()
        symbol = ITEM_ALIASES.get(symbol, symbol)
        await cmd_remove_item(update, settings, symbol)
        return

    await update.message.reply_text(
        "Hmm, I didn't catch that 🤔\n\n"
        "Try:\n"
        "• `add TSLA` — add a ticker\n"
        "• `remove BTC` — remove a ticker\n"
        "• `add time 08:00` — add alert time (EST, 24h format)\n"
        "• `remove time 09:00` — remove alert time\n"
        "• `status` — show settings\n"
        "• `now` — get prices right now",
        parse_mode="Markdown",
    )


async def cmd_add_item(update, settings, symbol):
    if symbol in settings["tracked_items"]:
        await update.message.reply_text(
            f"✅ {symbol} is already on your watchlist!" + format_settings_summary(settings),
            parse_mode="Markdown",
        )
        return
    settings["tracked_items"].append(symbol)
    save_settings(settings)
    await update.message.reply_text(
        f"✨ Added *{symbol}* to your watchlist!" + format_settings_summary(settings),
        parse_mode="Markdown",
    )


async def cmd_remove_item(update, settings, symbol):
    if symbol not in settings["tracked_items"]:
        await update.message.reply_text(
            f"🤷 {symbol} isn't on your watchlist." + format_settings_summary(settings),
            parse_mode="Markdown",
        )
        return
    settings["tracked_items"].remove(symbol)
    save_settings(settings)
    await update.message.reply_text(
        f"🗑️ Removed *{symbol}* from your watchlist." + format_settings_summary(settings),
        parse_mode="Markdown",
    )


async def cmd_add_time(update, settings, time_str):
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please use 24h format like `08:30` or `19:00`.", parse_mode="Markdown"
        )
        return
    if time_str in settings["alert_times"]:
        await update.message.reply_text(
            f"✅ {time_str} EST is already scheduled!" + format_settings_summary(settings),
            parse_mode="Markdown",
        )
        return
    settings["alert_times"].append(time_str)
    settings["alert_times"].sort()
    save_settings(settings)
    await update.message.reply_text(
        f"⏰ Added alert at *{time_str} EST*!" + format_settings_summary(settings),
        parse_mode="Markdown",
    )


async def cmd_remove_time(update, settings, time_str):
    if time_str not in settings["alert_times"]:
        await update.message.reply_text(
            f"🤷 {time_str} isn't in your schedule." + format_settings_summary(settings),
            parse_mode="Markdown",
        )
        return
    settings["alert_times"].remove(time_str)
    save_settings(settings)
    await update.message.reply_text(
        f"🗑️ Removed *{time_str} EST* from schedule." + format_settings_summary(settings),
        parse_mode="Markdown",
    )


def reschedule_jobs(scheduler, app):
    for job in scheduler.get_jobs():
        job.remove()
    settings = load_settings()
    for time_str in settings.get("alert_times", []):
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(
            send_alert,
            "cron",
            hour=hour,
            minute=minute,
            timezone=EST,
            args=[app, settings["chat_ids"], settings["tracked_items"]],
            id=f"alert_{time_str}",
            replace_existing=True,
        )
        logger.info(f"Scheduled alert at {time_str} EST")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def post_init(application):
        settings = load_settings()
        for time_str in settings.get("alert_times", []):
            hour, minute = map(int, time_str.split(":"))
            scheduler.add_job(
                send_alert,
                "cron",
                hour=hour,
                minute=minute,
                timezone=EST,
                args=[application, settings["chat_ids"], settings["tracked_items"]],
                id=f"alert_{time_str}",
                replace_existing=True,
            )
            logger.info(f"Scheduled alert at {time_str} EST")
        scheduler.start()

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

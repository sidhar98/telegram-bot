import json
import requests
import time
import os
import asyncio
import datetime
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────
#   CONFIG  — only change BOT_TOKEN
# ─────────────────────────────────────────
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")   # <-- Replace with your BotFather token

PROTECTION_INTERVAL = 240            # 60 seconds

VOUCHER_VALUES = {
    "SVH": 4000,
    "SV3": 5000,
    "SVC": 1000,
    "SVD": 2000,
    "SVA": 500,
    "SVG": 500,
    "SVI": 500,
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#   IN-MEMORY PROTECTION STATE
#   One protection session per chat_id
# ─────────────────────────────────────────
# { chat_id: { "codes": [...], "task": asyncio.Task, "cycle": int, "status_msg_id": int } }
protection_sessions: dict = {}


# ─────────────────────────────────────────
#   COOKIE LOADER
# ─────────────────────────────────────────
def load_cookies() -> str:
    path = "cookies.json"
    if not os.path.exists(path):
        logger.error("cookies.json not found!")
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.read().strip())
        if isinstance(data, list):
            return "; ".join(
                f"{item['name']}={item['value']}"
                for item in data
                if isinstance(item, dict) and "name" in item and "value" in item
            )
        elif isinstance(data, dict):
            return "; ".join(f"{k}={v}" for k, v in data.items())
    except Exception as e:
        logger.error(f"Error parsing cookies.json: {e}")
    return ""


COOKIE_STRING = load_cookies()


def get_headers() -> dict:
    return {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://www.sheinindia.in",
        "pragma": "no-cache",
        "referer": "https://www.sheinindia.in/cart",
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "x-tenant-id": "SHEIN",
        "cookie": COOKIE_STRING,
    }


# ─────────────────────────────────────────
#   VOUCHER HELPERS
# ─────────────────────────────────────────
def get_voucher_value(code: str):
    prefix = code[:3].upper()
    return VOUCHER_VALUES.get(prefix, None)


def check_voucher(code: str):
    url = "https://www.sheinindia.in/api/cart/apply-voucher"
    payload = {"voucherId": code, "device": {"client_type": "mobile_web"}}
    try:
        resp = requests.post(url, json=payload, headers=get_headers(), timeout=60)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, None
    except Exception as e:
        logger.warning(f"Request error for {code}: {e}")
        return None, None


def reset_voucher(code: str):
    url = "https://www.sheinindia.in/api/cart/reset-voucher"
    payload = {"voucherId": code, "device": {"client_type": "mobile_web"}}
    try:
        requests.post(url, json=payload, headers=get_headers(), timeout=30)
    except Exception:
        pass


def is_valid(response_data) -> bool:
    if not response_data:
        return False
    if "errorMessage" in response_data:
        errors = response_data.get("errorMessage", {}).get("errors", [])
        for err in errors:
            if err.get("type") == "VoucherOperationError":
                if "not applicable" in err.get("message", "").lower():
                    return False
        return False
    return True


def run_cycle_sync(codes: list) -> dict:
    """Run one full protection cycle (blocking — called via asyncio.to_thread)."""
    start_time = time.time()
    results = []
    for code in codes:
        _, data = check_voucher(code)
        valid = is_valid(data)
        val = get_voucher_value(code) if valid else None
        results.append({"code": code, "valid": valid, "value": val})
        reset_voucher(code)
        time.sleep(2)
    total_time = time.time() - start_time
    valid_codes = [r for r in results if r["valid"]]
    return {
        "results": results,
        "valid_codes": valid_codes,
        "total_time": total_time,
        "avg_speed": len(codes) / total_time if total_time > 0 else 0,
    }


# ─────────────────────────────────────────
#   MESSAGE BUILDERS
# ─────────────────────────────────────────
def build_check_message(cycle_data: dict, cycle_num: int = None) -> str:
    results = cycle_data["results"]
    valid_count = len(cycle_data["valid_codes"])
    total_time = cycle_data["total_time"]
    avg_speed = cycle_data["avg_speed"]

    result_lines = []
    for r in results:
        if r["valid"]:
            val_str = f"₹{r['value']}" if r["value"] else "₹???"
            result_lines.append(f"🟢 `{r['code']}` — *{val_str}*")
        else:
            result_lines.append(f"🔴 `{r['code']}`")

    header = (
        f"🔄 *CYCLE #{cycle_num} COMPLETED*"
        if cycle_num
        else "🏁 *CHECK COMPLETED SUCCESSFULLY*"
    )

    return (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *RESULTS:*\n"
        + "\n".join(result_lines)
        + "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Total Time: `{total_time:.2f}s`\n"
        f"🚀 Avg Speed: `{avg_speed:.1f} req/s`\n"
        f"✅ Valid Coupons: `{valid_count}`"
    )


def build_protection_status(chat_id: int, cycle_num: int, next_scan_str: str) -> str:
    session = protection_sessions.get(chat_id, {})
    codes = session.get("codes", [])
    return (
        "🛡️ *PROTECTION MODE ACTIVE*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Protecting: `{len(codes)}` code(s)\n"
        f"🔄 Cycle: `#{cycle_num}`\n"
        f"⏰ Next scan: `{next_scan_str}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "_Press Stop Protection to cancel._"
    )


def stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop Protection", callback_data="stop_protection")]
    ])


# ─────────────────────────────────────────
#   PROTECTION BACKGROUND LOOP
# ─────────────────────────────────────────
async def protection_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = protection_sessions[chat_id]
    codes = session["codes"]
    cycle_num = 1

    try:
        while True:
            # Run cycle in thread so bot stays responsive
            cycle_data = await asyncio.to_thread(run_cycle_sync, codes)
            session["cycle"] = cycle_num

            # Post cycle result
            result_msg = build_check_message(cycle_data, cycle_num)
            await context.bot.send_message(
                chat_id=chat_id,
                text=result_msg,
                parse_mode="Markdown",
            )

            # Alert if any code went dead
            dead = [r for r in cycle_data["results"] if not r["valid"]]
            if dead:
                dead_list = "\n".join(f"💀 `{r['code']}`" for r in dead)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ *ALERT — Dead Code(s) Detected!*\n{dead_list}",
                    parse_mode="Markdown",
                )

            # Countdown — sleep 8 mins, update status every 60s
            next_scan_dt = datetime.datetime.now() + datetime.timedelta(seconds=PROTECTION_INTERVAL)
            next_scan_str = next_scan_dt.strftime("%H:%M:%S")
            status_text = build_protection_status(chat_id, cycle_num, next_scan_str)

            # Update the pinned status message
            try:
                status_msg_id = session.get("status_msg_id")
                if status_msg_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text=status_text,
                        parse_mode="Markdown",
                        reply_markup=stop_keyboard(),
                    )
                else:
                    sent = await context.bot.send_message(
                        chat_id=chat_id,
                        text=status_text,
                        parse_mode="Markdown",
                        reply_markup=stop_keyboard(),
                    )
                    session["status_msg_id"] = sent.message_id
            except Exception:
                pass

            # Sleep with live countdown updates every 10s
            remaining = PROTECTION_INTERVAL
            while remaining > 0:
                sleep_chunk = min(10, remaining)
                await asyncio.sleep(sleep_chunk)
                remaining -= sleep_chunk

                if remaining > 0:
                    ndt = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
                    ns = ndt.strftime("%H:%M:%S")
                    st = build_protection_status(chat_id, cycle_num, ns)
                    try:
                        smid = session.get("status_msg_id")
                        if smid:
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=smid,
                                text=st,
                                parse_mode="Markdown",
                                reply_markup=stop_keyboard(),
                            )
                    except Exception:
                        pass

            cycle_num += 1

    except asyncio.CancelledError:
        logger.info(f"Protection loop cancelled for chat_id={chat_id}")


# ─────────────────────────────────────────
#   HELPERS
# ─────────────────────────────────────────
async def _stop_protection(chat_id, context, message, edit=False):
    session = protection_sessions.get(chat_id)

    if not session or session.get("task") is None or session["task"].done():
        text = "ℹ️ No active protection session to stop."
        if edit:
            try:
                await message.edit_text(text)
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=text)
        else:
            await message.reply_text(text)
        return

    session["task"].cancel()
    protection_sessions.pop(chat_id, None)

    text = "🛑 *Protection mode stopped.*\nYour codes are no longer being monitored."
    if edit:
        try:
            await message.edit_text(text, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    else:
        await message.reply_text(text, parse_mode="Markdown")


async def _start_protection(chat_id, codes, context, reply_to_message=None, edit_message=None):
    """Shared logic to kick off a protection session."""
    # Cancel existing session
    if chat_id in protection_sessions:
        old = protection_sessions[chat_id].get("task")
        if old and not old.done():
            old.cancel()

    init_text = (
        f"🛡️ *Protection Mode Started!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Codes: `{len(codes)}`\n"
        f"⏱ Interval: every *60 seconds*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Running first scan now..."
    )

    if edit_message:
        await edit_message.edit_text(init_text, parse_mode="Markdown")
        sent = edit_message
    elif reply_to_message:
        sent = await reply_to_message.reply_text(init_text, parse_mode="Markdown")
    else:
        sent = await context.bot.send_message(chat_id=chat_id, text=init_text, parse_mode="Markdown")

    protection_sessions[chat_id] = {
        "codes": codes,
        "task": None,
        "cycle": 0,
        "status_msg_id": sent.message_id,
    }

    task = asyncio.create_task(protection_loop(chat_id, context))
    protection_sessions[chat_id]["task"] = task


# ─────────────────────────────────────────
#   COMMAND HANDLERS
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🛍 *SHEIN Voucher Checker + Protector*\n\n"
        "Send voucher code(s), one per line — I'll check them instantly.\n\n"
        "*Commands:*\n"
        "`/check CODE1 CODE2` — check code(s)\n"
        "`/protect CODE1 CODE2` — start protection every 60 secs\n"
        "`/stopprotect` — stop protection\n"
        "`/status` — view protection status\n"
        "`/help` — full help"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 *How to use:*\n\n"
        "• Send code(s) one per line → instant check\n"
        "• `/check CODE1 CODE2 ...` — check manually\n"
        "• `/protect CODE1 CODE2 ...` — auto-check every 60 secs\n"
        "• `/stopprotect` — cancel protection\n"
        "• `/status` — current protection info\n\n"
        "After a check, tap *🛡️ Protect valid code(s)* to protect them instantly.\n\n"
        "*Voucher values:*\n"
        "`SVH`→₹4000 | `SV3`→₹5000 | `SVC`→₹1000\n"
        "`SVD`→₹2000 | `SVA`→₹500 | `SVG`→₹500 | `SVI`→₹500\n\n"
        "🛡️ Protection sends you cycle results + alerts if a code dies."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/check CODE1 CODE2 ...`", parse_mode="Markdown"
        )
        return
    await _process_codes(update, context, list(context.args))


async def protect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/protect CODE1 CODE2 ...`", parse_mode="Markdown"
        )
        return
    codes = [c.upper().strip() for c in context.args if c.strip()]
    await _start_protection(chat_id, codes, context, reply_to_message=update.message)


async def stopprotect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _stop_protection(chat_id, context, update.message)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = protection_sessions.get(chat_id)

    if not session or session.get("task") is None or session["task"].done():
        await update.message.reply_text(
            "ℹ️ No active protection session.\n"
            "Use `/protect CODE1 CODE2 ...` to start one.",
            parse_mode="Markdown",
        )
        return

    codes = session["codes"]
    cycle = session.get("cycle", 0)
    status = (
        "🛡️ *PROTECTION STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Protecting `{len(codes)}` code(s)\n"
        f"🔄 Last completed cycle: `#{cycle}`\n"
        "✅ Status: *Active*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(f"• `{c}`" for c in codes)
    )
    await update.message.reply_text(
        status, parse_mode="Markdown", reply_markup=stop_keyboard()
    )


# ─────────────────────────────────────────
#   PLAIN MESSAGE HANDLER
# ─────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    codes = [line.strip().upper() for line in text.splitlines() if line.strip()]
    if not codes:
        return
    await _process_codes(update, context, codes)


async def _process_codes(update: Update, context: ContextTypes.DEFAULT_TYPE, codes: list):
    if not COOKIE_STRING:
        await update.message.reply_text(
            "⚠️ Bot not configured: `cookies.json` missing or empty.",
            parse_mode="Markdown",
        )
        return

    status_msg = await update.message.reply_text(
        f"⏳ Checking `{len(codes)}` code(s)...", parse_mode="Markdown"
    )

    cycle_data = await asyncio.to_thread(run_cycle_sync, codes)
    reply = build_check_message(cycle_data)

    # Offer to protect valid codes with a button
    valid_codes = cycle_data["valid_codes"]
    markup = None
    if valid_codes:
        code_args = " ".join(r["code"] for r in valid_codes)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🛡️ Protect {len(valid_codes)} valid code(s)",
                callback_data=f"protect:{code_args}"
            )]
        ])

    await status_msg.edit_text(reply, parse_mode="Markdown", reply_markup=markup)


# ─────────────────────────────────────────
#   CALLBACK QUERY HANDLER
# ─────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "stop_protection":
        await _stop_protection(chat_id, context, query.message, edit=True)

    elif query.data.startswith("protect:"):
        codes_str = query.data[len("protect:"):]
        codes = [c.upper().strip() for c in codes_str.split() if c.strip()]
        await _start_protection(chat_id, codes, context, edit_message=query.message)


# ─────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────
def main():
    if not COOKIE_STRING:
        print("⚠️  WARNING: cookies.json not found or empty. Checks will fail.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("protect", protect_cmd))
    app.add_handler(CommandHandler("stopprotect", stopprotect_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":

    main()


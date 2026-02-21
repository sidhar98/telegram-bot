"""
SHEIN Voucher Checker + Protector — Telegram Bot

SETUP:
  1. Put cookies.json in the SAME folder as this script
  2. Set BOT_TOKEN below (get from @BotFather on Telegram)
  3. pip install python-telegram-bot requests
  4. python shein_bot.py
"""

import json
import requests
import os
import datetime
import asyncio
import logging
import sys
from collections import defaultdict

from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ──────────────────────────────────────────────────────
#  CONFIGURATION — only edit this section
# ──────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")   # from @BotFather on Telegram

# Known voucher value prefixes — add more if needed
VOUCHER_VALUES = {
    "SVH": 4000,
    "SV3": 5000,
    "SVC": 1000,
    "SVD": 2000,
    "SVA": 500,
    "SVG": 500,
}

# Wait between full scan cycles (3 minutes)
INTERVAL_SECONDS = 180

# Delay between each individual code check
# 1.5s = best balance of speed vs rate-limit safety
# 50 codes ≈ 75s | 60 codes ≈ 90s | full cycle ≈ 4.5 min
CODE_CHECK_DELAY = 1.5

# ──────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────
#  COOKIES — auto-loaded from cookies.json at startup
# ──────────────────────────────────────────────────────

def load_cookies() -> str:
    path = "cookies.json"
    if not os.path.exists(path):
        print(f"❌ cookies.json not found at: {path}")
        print("   Place cookies.json in the same folder as shein_bot.py and restart.")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return "; ".join(
                f"{i['name']}={i['value']}" for i in data
                if isinstance(i, dict) and "name" in i and "value" in i
            )
        if isinstance(data, dict):
            return "; ".join(f"{k}={v}" for k, v in data.items())
    except Exception as e:
        print(f"❌ Error reading cookies.json: {e}")
        sys.exit(1)
    print("❌ cookies.json format not recognised.")
    sys.exit(1)


COOKIE_STRING = load_cookies()

# ──────────────────────────────────────────────────────
#  PER-USER STATE
# ──────────────────────────────────────────────────────

state: dict = defaultdict(lambda: {
    "vouchers": {},       # { "CODE": {"paused": bool} }
    "protect_task": None,
    "checking": False,
    "stop_requested": False,
})



def S(uid: int) -> dict:
    return state[uid]

# ──────────────────────────────────────────────────────
#  SHEIN API  (exact logic from original shein.py)
# ──────────────────────────────────────────────────────

def _headers() -> dict:
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


def _check_voucher(code: str):
    """Returns (status_code, json_data) or (None, None) on network error."""
    try:
        r = requests.post(
            "https://www.sheinindia.in/api/cart/apply-voucher",
            json={"voucherId": code, "device": {"client_type": "mobile_web"}},
            headers=_headers(),
            timeout=60,
        )
        try:
            return r.status_code, r.json()
        except json.JSONDecodeError:
            return r.status_code, None
    except Exception as e:
        log.warning("Network error %s: %s", code, e)
        return None, None


def _reset_voucher(code: str):
    """Removes code from cart after checking — keeps cart clean."""
    try:
        requests.post(
            "https://www.sheinindia.in/api/cart/reset-voucher",
            json={"voucherId": code, "device": {"client_type": "mobile_web"}},
            headers=_headers(),
            timeout=30,
        )
    except Exception:
        pass


def _is_valid(data) -> bool:
    """
    Exact logic from original shein.py:
      - data is None / empty       → False (network/parse failure)
      - 'errorMessage' key present → False (voucher dead/used/invalid)
      - no 'errorMessage' key      → True  (voucher alive and valid)
    """
    if not data:
        return False
    if "errorMessage" in data:
        return False
    return True


def _voucher_value(code: str) -> str:
    """
    Returns rupee value string for a code.
    Checks all known prefixes against the code (not just first 3 chars)
    because some codes have longer prefixes like SVI, SVIB, etc.
    Falls back to 'Unknown' if no prefix matches.
    """
    upper = code.upper()
    for prefix, value in VOUCHER_VALUES.items():
        if upper.startswith(prefix):
            return str(value)
    return "Unknown"


# ──────────────────────────────────────────────────────
#  SAFE SEND  (retries on flood / network errors)
# ──────────────────────────────────────────────────────

async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return
        except TelegramError as e:
            if "flood" in str(e).lower():
                await asyncio.sleep(5 * (attempt + 1))
            else:
                log.error("Send error attempt %d: %s", attempt + 1, e)
                await asyncio.sleep(2)
    log.error("Failed to send to %s after 3 attempts", chat_id)


# ──────────────────────────────────────────────────────
#  CORE SCAN ENGINE
# ──────────────────────────────────────────────────────

async def run_scan(bot: Bot, chat_id: int, codes: list, progress_msg_id: int = None) -> tuple:
    """
    Scans codes, updates progress bar live, returns (alive, dead, errors).
      alive  = API confirmed valid
      dead   = API confirmed dead/used
      errors = network failure — kept safe, never removed
    """
    alive, dead, errors = [], [], []
    total = len(codes)
    last_text = [""]
    loop = asyncio.get_event_loop()

    async def update_progress(done: int, current: str):
        if total == 0 or not progress_msg_id:
            return
        pct = int(done / total * 100)
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        text = (
            f"⚙️ *Scanning... {pct}%*\n"
            f"`[{bar}]`\n"
            f"Checked: `{done}/{total}`\n"
            f"✅ Alive: `{len(alive)}`  ❌ Dead: `{len(dead)}`\n"
            f"Current: `{current}`"
        )
        if text == last_text[0]:
            return
        last_text[0] = text
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass

    for idx, code in enumerate(codes, 1):
        await update_progress(idx - 1, code)
        status, data = await loop.run_in_executor(None, _check_voucher, code)

        if status is None and data is None:
            errors.append(code)          # network error — keep safe
        elif _is_valid(data):
            alive.append(code)
        else:
            dead.append(code)

        await loop.run_in_executor(None, _reset_voucher, code)
        await asyncio.sleep(CODE_CHECK_DELAY)

    await update_progress(total, "✅ Done")
    return alive, dead, errors


# ──────────────────────────────────────────────────────
#  PROTECTION LOOP
# ──────────────────────────────────────────────────────

async def protection_loop(bot: Bot, chat_id: int, uid: int):
    s = S(uid)
    s["stop_requested"] = False
    cycle = 1
    log.info("Protection started uid=%s", uid)

    try:
        while not s["stop_requested"]:
            codes = [c for c, v in list(s["vouchers"].items()) if not v.get("paused")]

            if not codes:
                await safe_send(
                    bot, chat_id,
                    "⚠️ No active codes to protect.\nAdd codes with /add or resume with /resume."
                )
                # Wait then retry
                for _ in range(INTERVAL_SECONDS // 5):
                    if s["stop_requested"]:
                        break
                    await asyncio.sleep(5)
                continue

            prog = await bot.send_message(
                chat_id=chat_id,
                text=f"🔄 *Cycle #{cycle}* — scanning {len(codes)} codes...",
                parse_mode=ParseMode.MARKDOWN,
            )

            alive, dead, errors = await run_scan(bot, chat_id, codes, progress_msg_id=prog.message_id)

            # Build result summary — codes are NEVER auto-removed, only you can remove them
            lines = [f"🔁 *Cycle #{cycle} done*\n"]
            if alive:
                lines.append(f"✅ *ALIVE ({len(alive)}):*")
                for c in alive:
                    lines.append(f"  • `{c}` — ₹{_voucher_value(c)}")
            if dead:
                lines.append(f"\n❌ *DEAD/USED ({len(dead)}):*")
                for c in dead:
                    lines.append(f"  • `{c}` — still rescanning next cycle")
            if errors:
                lines.append(f"\n⚠️ *Network error — retrying next cycle ({len(errors)}):*")
                for c in errors:
                    lines.append(f"  • `{c}`")

            next_time = datetime.datetime.now() + datetime.timedelta(seconds=INTERVAL_SECONDS)
            lines.append(f"\n⏰ Next scan: `{next_time.strftime('%H:%M:%S')}`")
            lines.append(f"📦 Remaining: `{len(s['vouchers'])}` codes")

            await safe_send(bot, chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
            cycle += 1

            # Wait in small chunks so stop responds quickly
            for _ in range(INTERVAL_SECONDS // 5):
                if s["stop_requested"]:
                    break
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        pass  # silently exit — stop message is sent by cmd_stop
    except Exception as e:
        log.exception("Protection crashed uid=%s: %s", uid, e)
        await safe_send(
            bot, chat_id,
            f"❌ Protection crashed:\n`{e}`\n\nRestart with /run",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s["protect_task"] = None


# ──────────────────────────────────────────────────────
#  HELPER — extract codes from command message
# ──────────────────────────────────────────────────────

def _parse_codes(ctx, message_text: str) -> list:
    args_part = " ".join(ctx.args) if ctx.args else ""
    lines = message_text.split("\n")
    extra = " ".join(lines[1:]) if len(lines) > 1 else ""
    combined = (args_part + " " + extra).replace(",", " ")
    return [c.strip().upper() for c in combined.split() if len(c.strip()) > 4]


# ──────────────────────────────────────────────────────
#  COMMANDS
# ──────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *SHEIN Voucher Bot*\n\n"
        "*Commands:*\n"
        "`/add <codes>` — add codes to protection list\n"
        "`/protect <code>` — protect a single code instantly\n"
        "`/check <codes>` — instant one-time check\n"
        "`/run` — start full protection loop\n"
        "`/stop` — stop protection loop\n"
        "`/pause <code>` — skip a code during scans\n"
        "`/resume <code>` — re-enable a paused code\n"
        "`/list` — show all protected codes\n"
        "`/clear` — remove all codes\n"
        "`/status` — show bot status\n\n"
        "⚡ *Speed:* 50 codes ≈ 75s · 60 codes ≈ 90s\n"
        "🔁 *Full cycle:* scan + 3 min wait ≈ every 4.5 min",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    codes = _parse_codes(ctx, update.message.text or "")
    if not codes:
        await update.message.reply_text(
            "Usage: `/add CODE1 CODE2 CODE3`\nAlso works with one code per line.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    added, skipped = [], []
    for code in codes:
        if code not in s["vouchers"]:
            s["vouchers"][code] = {"paused": False}
            added.append(code)
        else:
            skipped.append(code)

    msg = f"✅ Added *{len(added)}* codes"
    if skipped:
        msg += f", skipped *{len(skipped)}* duplicates"
    msg += f"\n📦 Total protected: *{len(s['vouchers'])}*"
    if added:
        msg += "\n\n*Added:*\n" + "\n".join(f"  • `{c}`" for c in added)

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_protect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /protect CODE1 CODE2 ... — adds one or more codes and starts protection.
    If protection is already running, codes are added to the existing loop.
    """
    uid = update.effective_user.id
    s = S(uid)

    codes = _parse_codes(ctx, update.message.text or "")
    if not codes:
        await update.message.reply_text(
            "Usage: `/protect CODE1 CODE2 ...`\nExample: `/protect SVC1234ABCD SVH5678EFGH`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    added, skipped = [], []
    for code in codes:
        if code not in s["vouchers"]:
            s["vouchers"][code] = {"paused": False}
            added.append(code)
        else:
            skipped.append(code)

    # If protection loop already running, just confirm the add
    if s["protect_task"] and not s["protect_task"].done():
        msg = f"✅ Added *{len(added)}* code(s) to the active protection loop"
        if skipped:
            msg += f", skipped *{len(skipped)}* already protected"
        msg += f"\n📦 Total codes now: *{len(s['vouchers'])}*"
        if added:
            msg += "\n\n*Added:*\n" + "\n".join(f"  • `{c}`" for c in added)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # Start protection loop
    s["protect_task"] = asyncio.create_task(
        protection_loop(ctx.bot, update.effective_chat.id, uid)
    )

    active = sum(1 for v in s["vouchers"].values() if not v.get("paused"))
    est_min = round(active * CODE_CHECK_DELAY / 60, 1)
    cycle_min = round(est_min + INTERVAL_SECONDS / 60, 1)

    msg = f"🛡️ *Protection started!*\n"
    msg += f"📦 Protecting *{active}* code(s)\n"
    msg += f"⚡ Scan time: ~*{est_min} min*\n"
    msg += f"🔁 Full cycle every ~*{cycle_min} min*\n\n"
    if added:
        msg += "*Codes added:*\n" + "\n".join(f"  • `{c}`" for c in added) + "\n\n"
    msg += "Use /stop to stop."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    if s["checking"]:
        await update.message.reply_text("⚠️ A check is already running, please wait.")
        return

    codes = _parse_codes(ctx, update.message.text or "")
    if not codes:
        await update.message.reply_text(
            "Usage: `/check CODE1 CODE2 CODE3`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    s["checking"] = True
    prog = await update.message.reply_text(f"🔍 Checking {len(codes)} voucher(s)... Please wait.")

    try:
        alive, dead, errors = await run_scan(
            ctx.bot, update.effective_chat.id, codes,
            progress_msg_id=prog.message_id,
        )
    finally:
        s["checking"] = False

    lines = ["📊 *INSTANT CHECK RESULTS*\n"]

    if alive:
        lines.append(f"✅ *WORKING ({len(alive)}):*")
        for c in alive:
            lines.append(f"  `{c}` — ₹{_voucher_value(c)}")
    else:
        lines.append("✅ WORKING (0): None")

    if dead:
        lines.append(f"\n❌ *DEAD/USED ({len(dead)}):*")
        for c in dead:
            lines.append(f"  `{c}`")
    else:
        lines.append("\n❌ DEAD/USED (0): None")

    if errors:
        lines.append(f"\n⚠️ *Network error — try again ({len(errors)}):*")
        for c in errors:
            lines.append(f"  `{c}`")

    result_text = "\n".join(lines)

    if alive:
        # Build one button per alive code to protect it individually
        keyboard = []
        for c in alive:
            keyboard.append([
                InlineKeyboardButton(
                    f"🔒 Protect {c}",
                    callback_data=f"protect_one:{uid}:{c}"
                )
            ])
        # Also offer a "protect all" button if more than one alive
        if len(alive) > 1:
            all_codes = ",".join(alive)
            keyboard.insert(0, [
                InlineKeyboardButton(
                    f"🔒 Protect All {len(alive)} Working Codes",
                    callback_data=f"protect_all:{uid}:{all_codes}"
                )
            ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=result_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )
    else:
        await safe_send(ctx.bot, update.effective_chat.id, result_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    if not s["vouchers"]:
        await update.message.reply_text("⚠️ No codes to protect. Use /add first.")
        return

    if s["protect_task"] and not s["protect_task"].done():
        await update.message.reply_text("🛡️ Protection is already running! Use /stop first.")
        return

    s["protect_task"] = asyncio.create_task(
        protection_loop(ctx.bot, update.effective_chat.id, uid)
    )

    active = sum(1 for v in s["vouchers"].values() if not v.get("paused"))
    est_min = round(active * CODE_CHECK_DELAY / 60, 1)
    cycle_min = round(est_min + INTERVAL_SECONDS / 60, 1)

    await update.message.reply_text(
        f"🚀 *Protection started!*\n"
        f"📦 Protecting *{active}* codes\n"
        f"⚡ Scan time: ~*{est_min} min*\n"
        f"🔁 Full cycle every ~*{cycle_min} min*\n\n"
        f"Use /stop to stop.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    task = s.get("protect_task")
    if task and not task.done():
        s["stop_requested"] = True   # signal loop to exit cleanly
        task.cancel()
        s["protect_task"] = None
        await update.message.reply_text("🛑 Protection stopped.")
    else:
        await update.message.reply_text("ℹ️ Protection is not currently running.")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    if not ctx.args:
        await update.message.reply_text("Usage: `/pause CODE`", parse_mode=ParseMode.MARKDOWN)
        return

    code = ctx.args[0].upper()
    if code not in s["vouchers"]:
        await update.message.reply_text(
            f"❌ `{code}` is not in the protection list.", parse_mode=ParseMode.MARKDOWN
        )
        return

    s["vouchers"][code]["paused"] = True
    await update.message.reply_text(
        f"⏸ `{code}` paused — will be skipped during scans.", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    if not ctx.args:
        await update.message.reply_text("Usage: `/resume CODE`", parse_mode=ParseMode.MARKDOWN)
        return

    code = ctx.args[0].upper()
    if code not in s["vouchers"]:
        s["vouchers"][code] = {"paused": False}
        await update.message.reply_text(
            f"✅ `{code}` added and now protected.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        s["vouchers"][code]["paused"] = False
        await update.message.reply_text(f"▶️ `{code}` resumed.", parse_mode=ParseMode.MARKDOWN)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    if not s["vouchers"]:
        await update.message.reply_text("📭 No codes in the list. Use /add to add some.")
        return

    lines = ["📋 *Protected Codes:*\n"]
    for code, meta in s["vouchers"].items():
        # Show paused vs protected — NOT "active" which confused with "working"
        icon = "⏸" if meta.get("paused") else "🔒"
        status = "paused" if meta.get("paused") else "protected"
        val = _voucher_value(code)
        lines.append(f"  {icon} `{code}` ₹{val} — {status}")

    running = s.get("protect_task") and not s["protect_task"].done()
    lines.append(f"\n🛡️ Protection: {'🟢 RUNNING' if running else '🔴 STOPPED'}")
    lines.append(f"📦 Total: {len(s['vouchers'])} codes")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)
    count = len(s["vouchers"])
    s["vouchers"].clear()
    await update.message.reply_text(f"🗑️ Cleared all {count} codes.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = S(uid)

    running = s.get("protect_task") and not s["protect_task"].done()
    total = len(s["vouchers"])
    active = sum(1 for v in s["vouchers"].values() if not v.get("paused"))

    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"🍪 Cookies: ✅ Loaded from cookies.json\n"
        f"📦 Total codes: *{total}*\n"
        f"🔒 Protected: *{active}*\n"
        f"⏸ Paused: *{total - active}*\n"
        f"🛡️ Protection: {'🟢 RUNNING' if running else '🔴 STOPPED'}\n"
        f"⏱ Interval: *{INTERVAL_SECONDS // 60} min* between cycles",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_plain_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    If someone sends a raw message (not a command), treat it as codes to check instantly.
    Filters out very short text so normal messages don't get checked by accident.
    """
    text = (update.message.text or "").strip()
    if not text:
        return

    # Extract potential codes (words longer than 4 chars, no spaces = likely a code)
    potential = [w.upper() for w in text.replace(",", " ").split() if len(w) > 4]
    if not potential:
        return

    uid = update.effective_user.id
    s = S(uid)

    if s["checking"]:
        await update.message.reply_text("⚠️ A check is already running, please wait.")
        return

    s["checking"] = True
    prog = await update.message.reply_text(f"🔍 Checking {len(potential)} voucher(s)... Please wait.")

    try:
        alive, dead, errors = await run_scan(
            ctx.bot, update.effective_chat.id, potential,
            progress_msg_id=prog.message_id,
        )
    finally:
        s["checking"] = False

    lines = ["📊 *INSTANT CHECK RESULTS*\n"]

    if alive:
        lines.append(f"✅ *WORKING ({len(alive)}):*")
        for c in alive:
            lines.append(f"  `{c}` — ₹{_voucher_value(c)}")
    else:
        lines.append("✅ WORKING (0): None")

    if dead:
        lines.append(f"\n❌ *DEAD/USED ({len(dead)}):*")
        for c in dead:
            lines.append(f"  `{c}`")
    else:
        lines.append("\n❌ DEAD/USED (0): None")

    if errors:
        lines.append(f"\n⚠️ *Network error — try again ({len(errors)}):*")
        for c in errors:
            lines.append(f"  `{c}`")

    result_text = "\n".join(lines)

    if alive:
        keyboard = []
        for c in alive:
            keyboard.append([
                InlineKeyboardButton(
                    f"🔒 Protect {c}",
                    callback_data=f"protect_one:{uid}:{c}"
                )
            ])
        if len(alive) > 1:
            all_codes = ",".join(alive)
            keyboard.insert(0, [
                InlineKeyboardButton(
                    f"🔒 Protect All {len(alive)} Working Codes",
                    callback_data=f"protect_all:{uid}:{all_codes}"
                )
            ])
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=result_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await safe_send(ctx.bot, update.effective_chat.id, result_text, parse_mode=ParseMode.MARKDOWN)


# ──────────────────────────────────────────────────────
#  CALLBACK — inline button handler for protect buttons
# ──────────────────────────────────────────────────────

async def callback_protect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the inline 🔒 Protect button taps from /check results."""
    query = update.callback_query
    await query.answer()  # remove the loading spinner

    data = query.data  # e.g. "protect_one:12345:SVC1234" or "protect_all:12345:SVC1,SVH2"
    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    action, uid_str, codes_str = parts
    uid = int(uid_str)
    s = S(uid)

    codes_to_add = [c.strip().upper() for c in codes_str.split(",") if c.strip()]

    added, skipped = [], []
    for code in codes_to_add:
        if code not in s["vouchers"]:
            s["vouchers"][code] = {"paused": False}
            added.append(code)
        else:
            skipped.append(code)

    # Start protection loop if not already running
    loop_started = False
    if s["protect_task"] is None or s["protect_task"].done():
        s["protect_task"] = asyncio.create_task(
            protection_loop(ctx.bot, query.message.chat_id, uid)
        )
        loop_started = True

    if len(codes_to_add) == 1:
        code = codes_to_add[0]
        if skipped:
            msg = f"🔒 `{code}` is already being protected."
        else:
            msg = f"🛡️ `{code}` added to protection!"
            if loop_started:
                msg += f"\n🚀 Protection loop started."
    else:
        msg = f"🛡️ *{len(added)}* code(s) added to protection!"
        if skipped:
            msg += f" ({len(skipped)} already protected)"
        if loop_started:
            msg += f"\n🚀 Protection loop started."

    msg += f"\n📦 Total protected: *{len(s['vouchers'])}*"

    # Edit the original message to remove the buttons (avoid double-tapping)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await ctx.bot.send_message(
        chat_id=query.message.chat_id,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
    )

# ──────────────────────────────────────────────────────
#  ERROR HANDLER
# ──────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ Something went wrong:\n`{ctx.error}`\n\nBot is still running.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set your BOT_TOKEN at the top of the script.")
        sys.exit(1)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("protect", cmd_protect))
    app.add_handler(CommandHandler("check",   cmd_check))
    app.add_handler(CommandHandler("run",     cmd_run))
    app.add_handler(CommandHandler("stop",    cmd_stop))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CommandHandler("clear",   cmd_clear))
    app.add_handler(CommandHandler("status",  cmd_status))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_protect, pattern=r"^protect_(one|all):"))

    # Handle plain text messages as instant voucher checks
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_message))

    app.add_error_handler(error_handler)

    print("====================================================")
    print("🛡️  SHEIN Voucher Bot is running...")
    print("🍪  Cookies loaded from cookies.json")
    print("====================================================")

    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()

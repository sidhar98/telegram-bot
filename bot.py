import json
import requests
import re
import os
import time
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
# ===================== CONFIGURATION =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # <-- Replace with your Telegram bot token

MAX_WORKERS = 8          # Number of vouchers to check at once
THREAD_DELAY = 0.3       # Delay between starting threads
RETRY_DELAY = 0.5        # Delay before retrying a failed request
CHECK_INTERVAL = 60     # Seconds between cycles (auto-protect loop — 4 minutes)

# Thread locks
proxy_lock = threading.Lock()

# Per-user protected codes: { user_id (int) -> [code, ...] }
protected_codes: dict[int, list[str]] = {}
protect_lock = threading.Lock()

# Scheduled chats tracker
_scheduled_chats: set = set()
_scheduled_lock = threading.Lock()

# ===================== TIMESTAMP HELPER =====================
def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ===================== STARTUP LOGGER =====================
def _startup_log(msg: str) -> None:
    """Single entry-point for essential startup messages printed to console."""
    print(msg)

# ===================== PROXY MANAGER =====================
class ProxyRotator:
    def __init__(self, proxy_file="proxies.txt"):
        self.proxies = []
        self.index = 0
        self.load_proxies(proxy_file)

    def load_proxies(self, filename):
        """Load proxies from file, each line format: username:password@host:port"""
        if not os.path.exists(filename):
            return  # no proxy file — continue without proxies
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '@' not in line:
                    continue
                try:
                    creds, hostport = line.split('@', 1)
                    if ':' in creds:
                        username, password = creds.split(':', 1)
                    else:
                        username = creds
                        password = ""
                    if ':' in hostport:
                        host, port = hostport.split(':', 1)
                    else:
                        host = hostport
                        port = "80"
                    proxy_url = f"http://{username}:{password}@{host}:{port}"
                    self.proxies.append({
                        "http": proxy_url,
                        "https": proxy_url
                    })
                except Exception:
                    pass  # skip malformed proxy lines silently
        _startup_log(f"Loaded {len(self.proxies)} proxies.")

    def get_next_proxy(self):
        """Return the next proxy dict for a new cycle, rotating round-robin."""
        with proxy_lock:
            if not self.proxies:
                return None
            proxy = self.proxies[self.index]
            self.index = (self.index + 1) % len(self.proxies)
            return proxy

# ===================== SHEIN LOGIC =====================
def load_cookies():
    if not os.path.exists("cookies.json"):
        return None
    try:
        with open("cookies.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return "; ".join(f"{c['name']}={c['value']}" for c in data if "name" in c and "value" in c)
        return None
    except Exception:
        return None

# ===================== LIVE PROGRESS STATE =====================
class ScanState:
    """
    Shared mutable state for one scanning cycle.
    Worker threads write into this; the async progress-editor reads from it.
    All fields accessed from threads are protected by a lock.
    """
    def __init__(self, total: int):
        self.lock = threading.Lock()
        self.total = total
        self.checked = 0
        self.current_code = ""
        self.valid = 0
        self.invalid = 0
        self.errors = 0
        self.result_lines: list[str] = []   # ordered result strings for final message
        self.done = False                    # set True when all threads finish

    def record(self, code: str, status: str, line: str):
        with self.lock:
            self.checked += 1
            self.current_code = code
            if status == "valid":
                self.valid += 1
            elif status == "invalid":
                self.invalid += 1
            else:
                self.errors += 1
            self.result_lines.append(line)

    def snapshot(self):
        with self.lock:
            return (
                self.checked,
                self.total,
                self.valid,
                self.invalid,
                self.errors,
                self.current_code,
                self.done,
                list(self.result_lines),
            )

# ===================== MESSAGE BUILDERS =====================
def build_progress_bar(checked: int, total: int, width: int = 19) -> str:
    pct = checked / total if total else 0
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"

def build_progress_message(checked, total, valid, invalid, errors, current_code) -> str:
    pct = int(checked / total * 100) if total else 0
    bar = build_progress_bar(checked, total)
    return (
        f"⚙️ Scanning... {pct}%\n"
        f"{bar}\n"
        f"Checked: {checked}/{total}\n"
        f"✅ Valid: {valid}  ❌ Invalid: {invalid}  ⚠️ Errors: {errors}\n"
        f"Current: {current_code}"
    )

def build_final_message(result_lines: list[str], valid, invalid, errors, total) -> str:
    results_block = "\n".join(result_lines) if result_lines else "(no results)"
    summary = (
        "============================\n"
        "✅ CYCLE COMPLETE — SUMMARY\n"
        f"🟢 Valid   : {valid}\n"
        f"🔴 Invalid : {invalid}\n"
        f"⚠️ Errors  : {errors}\n"
        f"📦 Total   : {total}"
    )
    return f"{results_block}\n\n{summary}"

# ===================== VOUCHER TASK (LOGIC UNCHANGED) =====================
def check_voucher_task(code, cookie_string, session, proxy, state: ScanState):
    """
    Check a voucher with exactly one retry on technical errors.
    Records result into ScanState — no direct Telegram calls from this thread.
    All voucher / API / retry / proxy logic is identical to the original script.
    """
    url = "https://www.sheinindia.in/api/cart/apply-voucher"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-tenant-id": "SHEIN",
        "cookie": cookie_string
    }
    payload = {"voucherId": code, "device": {"client_type": "web"}}

    for attempt in range(2):  # attempt 0 = first try, 1 = retry
        try:
            r = session.post(url, json=payload, headers=headers, timeout=10, proxies=proxy)
            response = r.json()

            # Business response – no retry needed
            if "errorMessage" not in response:
                line = f"🟢 Working: {code}"
                state.record(code, "valid", line)
                return
            else:
                error_msg = response.get("errorMessage", "Unknown error")
                already_redeemed_keywords = ["already redeemed", "already been redeemed"]
                if any(kw in str(error_msg).lower() for kw in already_redeemed_keywords):
                    line = f"🟡 Already Redeemed: {code} — {error_msg}"
                else:
                    line = f"🔴 Not Applicable: {code} — {error_msg}"
                state.record(code, "invalid", line)
                return

        except Exception as e:
            if attempt == 0:
                # First failure – wait and retry once
                time.sleep(RETRY_DELAY)
                continue
            else:
                # Second failure – record error
                line = f"⚠️ Error: {code} — {type(e).__name__}: {e}"
                state.record(code, "error", line)
                return

# ===================== HELPERS =====================
def get_codes_from_text(text: str) -> list[str]:
    """Extract unique SV... voucher codes from any text."""
    codes = re.findall(r'\bSV\w+', text)
    return list(dict.fromkeys(codes))

# ===================== CYCLE RUNNER =====================
async def run_cycle(codes: list[str], chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Run a full checking cycle.
      1. Send ONE progress message.
      2. Edit it live every 1.5 s while worker threads run.
      3. Replace it with the complete results + summary when done.
    No extra messages are sent during scanning.
    """
    bot = context.bot

    cookie_string = load_cookies()
    if not cookie_string:
        await bot.send_message(chat_id=chat_id, text="❌ No valid cookies found. Please add cookies.json.")
        return

    proxy_rotator = ProxyRotator()
    proxy = proxy_rotator.get_next_proxy()
    session = requests.Session()
    state = ScanState(total=len(codes))

    # ── Send the single progress message ──────────────────────────────────────
    init_text = build_progress_message(0, len(codes), 0, 0, 0, "—")
    sent = await bot.send_message(chat_id=chat_id, text=init_text)
    msg_id = sent.message_id

    # ── Launch thread pool without blocking the event loop ────────────────────
    loop = asyncio.get_event_loop()

    def run_threads():
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for code in codes:
                executor.submit(check_voucher_task, code, cookie_string, session, proxy, state)
                time.sleep(THREAD_DELAY)
        with state.lock:
            state.done = True

    thread_future = loop.run_in_executor(None, run_threads)

    # ── Live-edit loop ─────────────────────────────────────────────────────────
    last_text = init_text
    while True:
        await asyncio.sleep(1.5)          # stay well within Telegram's 30 edits/min limit
        checked, total, valid, invalid, errors, current_code, done, _ = state.snapshot()
        new_text = build_progress_message(checked, total, valid, invalid, errors, current_code or "—")
        if new_text != last_text:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=new_text)
                last_text = new_text
            except Exception:
                pass  # edit may fail if message content is identical; safe to ignore
        if done:
            break

    # Wait for thread pool to flush completely
    await thread_future

    # ── Replace progress message with final results + summary ─────────────────
    checked, total, valid, invalid, errors, _, _, result_lines = state.snapshot()
    final_text = build_final_message(result_lines, valid, invalid, errors, total)

    MAX_TG = 4096
    if len(final_text) <= MAX_TG:
        # Everything fits in one edit
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=final_text)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=final_text)
    else:
        # Too long: put summary in the progress-message slot, chunk results below
        summary_only = build_final_message([], valid, invalid, errors, total)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=summary_only)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=summary_only)

        # Send result lines chunked into ≤4096-char messages
        chunk = ""
        for line in result_lines:
            candidate = (chunk + "\n" + line).strip()
            if len(candidate) > MAX_TG:
                await bot.send_message(chat_id=chat_id, text=chunk)
                await asyncio.sleep(0.5)
                chunk = line
            else:
                chunk = candidate
        if chunk:
            await bot.send_message(chat_id=chat_id, text=chunk)

# ===================== TELEGRAM HANDLERS =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the SHEIN Voucher Checker Bot!\n\n"
        "Send me voucher codes (starting with SV) and I'll check them live.\n"
        "Use /protect CODE to add codes to auto-check every 4 minutes (your list only)."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain messages containing voucher codes."""
    text = update.message.text or ""
    codes = get_codes_from_text(text)

    if not codes:
        await update.message.reply_text("⚠️ No voucher codes found. Send codes starting with SV...")
        return

    await update.message.reply_text(f"🚀 {len(codes)} code(s) received. Starting cycle...")
    await run_cycle(codes, update.effective_chat.id, context)

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/protect CODE — adds a voucher code to THIS user's private protection list."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /protect SVI5GTC0C1PES9F")
        return

    user_id  = update.effective_user.id
    chat_id  = update.effective_chat.id

    added = []
    with protect_lock:
        user_list = protected_codes.setdefault(user_id, [])
        for arg in args:
            for code in get_codes_from_text(arg):
                if code not in user_list:
                    user_list.append(code)
                    added.append(code)

    if added:
        await update.message.reply_text(
            "🛡 Code(s) added to your protection list:\n" + "\n".join(added)
        )
        # Schedule a per-user auto-check job (once per user, keyed by user_id)
        with _scheduled_lock:
            if user_id not in _scheduled_chats:
                _scheduled_chats.add(user_id)
                context.job_queue.run_repeating(
                    protected_codes_job,
                    interval=CHECK_INTERVAL,
                    first=CHECK_INTERVAL,
                    chat_id=chat_id,          # where to send results
                    user_id=user_id,          # whose codes to check
                    name=f"protect_{user_id}",
                )
                await update.message.reply_text(
                    f"⏱ Auto-check scheduled every {CHECK_INTERVAL}s for your codes."
                )
    else:
        await update.message.reply_text("⚠️ No new valid SV... codes found in your arguments.")

# ===================== PROTECTED CODES AUTO-CHECKER =====================
async def protected_codes_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Periodically checks the protected codes belonging to the specific user
    who scheduled this job.  Each user's job is completely independent.
    """
    user_id = context.job.user_id   # set when job was registered
    chat_id = context.job.chat_id

    with protect_lock:
        codes = list(protected_codes.get(user_id, []))

    if not codes:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔁 Auto-checking {len(codes)} of your protected code(s)...",
    )
    await run_cycle(codes, chat_id, context)

# ===================== MAIN =====================
async def post_init(application: Application):
    pass  # reserved for future startup tasks

def main():
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("protect", protect_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started successfully.")
    print("Loaded cookies and proxies (if available).")
    print("Auto-check system running. Waiting for commands...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()






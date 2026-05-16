"""
Telegram Media Downloader Bot — Single File Version
Supports: YouTube Video, YouTube Audio, YouTube Music, Instagram Reels
Just add your BOT_TOKEN below and run: python bot.py
"""

import asyncio
import logging
import re
import uuid
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Callable, Coroutine
import time

import yt_dlp
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.error import TelegramError

# ══════════════════════════════════════════════════════════════════
#  ✏️  CONFIGURE HERE
# ══════════════════════════════════════════════════════════════════

BOT_TOKEN       = ""   
ADMIN_IDS       = []                    
MAX_FILESIZE_MB = 50
DOWNLOAD_DIR    = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("bot")

# ══════════════════════════════════════════════════════════════════
#  PLATFORM DETECTION
# ══════════════════════════════════════════════════════════════════

class Platform(Enum):
    YOUTUBE       = auto()
    YOUTUBE_MUSIC = auto()
    INSTAGRAM     = auto()
    UNKNOWN       = auto()

_YT_RE  = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch\?v=|shorts/|embed/|v/|live/)?[\w\-]{11}", re.I)
_YTM_RE = re.compile(r"(https?://)?(music\.youtube\.com)/", re.I)
_IG_RE  = re.compile(r"(https?://)?(www\.)?instagram\.com/(p|reel|tv)/[\w\-]+", re.I)

def detect_platform(url: str) -> Platform:
    if _YTM_RE.search(url):   return Platform.YOUTUBE_MUSIC
    if _YT_RE.search(url):    return Platform.YOUTUBE
    if _IG_RE.search(url):    return Platform.INSTAGRAM
    return Platform.UNKNOWN

def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://\S+", text)

# ══════════════════════════════════════════════════════════════════
#  RATE LIMITER
# ══════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, max_req=5, window=60):
        self.max_req = max_req
        self.window  = window
        self._data: dict[int, deque] = defaultdict(deque)

    def is_allowed(self, uid: int) -> bool:
        now = time.monotonic()
        q   = self._data[uid]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_req:
            return False
        q.append(now)
        return True

    def wait(self, uid: int) -> int:
        q = self._data[uid]
        return max(0, int(self.window - (time.monotonic() - q[0])) + 1) if q else 0

limiter = RateLimiter()

# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════

def human_size(b: int) -> str:
    for u in ("B","KB","MB","GB"):
        if abs(b) < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def progress_bar(done: int, total: int, w: int = 10) -> str:
    if total <= 0: return "░"*w + " ??%"
    pct = done / total
    return "▓"*int(w*pct) + "░"*(w-int(w*pct)) + f" {pct*100:.0f}%"

def truncate(s: str, n: int = 80) -> str:
    return s if len(s) <= n else s[:n-1] + "…"

async def delete_file(path: Optional[Path], delay: float = 0) -> None:
    if not path: return
    if delay: await asyncio.sleep(delay)
    try: Path(path).unlink(missing_ok=True)
    except OSError: pass

async def download_thumb(url: str, dest: Path) -> Optional[Path]:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, follow_redirects=True)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════
#  DOWNLOAD RESULT
# ══════════════════════════════════════════════════════════════════

@dataclass
class Result:
    success:        bool
    file_path:      Optional[Path] = None
    title:          str = "Unknown"
    uploader:       str = "Unknown"
    duration_secs:  int = 0
    thumbnail_path: Optional[Path] = None
    filesize:       int = 0
    error:          Optional[str]  = None

# ══════════════════════════════════════════════════════════════════
#  YT-DLP PROGRESS HOOK
# ══════════════════════════════════════════════════════════════════

def make_hook(on_progress, loop) -> Callable:
    last = [-1]
    def hook(d):
        if d.get("status") == "downloading":
            done  = d.get("downloaded_bytes", 0) or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            eta   = d.get("eta") or 0
            pct   = int(done/total*100) if total else 0
            if pct - last[0] < 5: return
            last[0] = pct
            bar  = progress_bar(done, total)
            spd  = f"{human_size(int(speed))}/s" if speed else "—"
            text = f"📥 *Downloading…*\n`{bar}`\n💾 {human_size(done)} / {human_size(total)}\n⚡ {spd}  •  ⏱ {eta}s"
            asyncio.run_coroutine_threadsafe(on_progress(text), loop)
        elif d.get("status") == "finished":
            asyncio.run_coroutine_threadsafe(on_progress("⚙️ *Post-processing…*"), loop)
    return hook

# ══════════════════════════════════════════════════════════════════
#  DOWNLOADERS
# ══════════════════════════════════════════════════════════════════

_COMMON = {
    "quiet": True, "no_warnings": True, "noprogress": True,
    "noplaylist": True, "retries": 3, "fragment_retries": 3,
    "concurrent_fragment_downloads": 4,
}

async def _run(url: str, opts: dict, uid: str, mode: str) -> Result:
    def _blocking():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        ext   = "mp3" if mode == "audio" else "mp4"
        files = list(DOWNLOAD_DIR.glob(f"{mode}_{uid}.*"))
        pref  = [f for f in files if f.suffix == f".{ext}"]
        found = (pref or files)[0] if files else None
        return info, found

    try:
        loop = asyncio.get_event_loop()
        info, fp = await loop.run_in_executor(None, _blocking)
        if not fp or not fp.exists():
            return Result(success=False, error="File not found after download.")
        size = fp.stat().st_size
        if size > MAX_FILESIZE_MB * 1024 * 1024:
            fp.unlink(missing_ok=True)
            return Result(success=False, error=f"❌ Too large ({human_size(size)}). Limit: {MAX_FILESIZE_MB} MB.")
        title    = info.get("title", "Unknown")
        uploader = info.get("uploader") or info.get("channel") or "Unknown"
        duration = int(info.get("duration") or 0)
        thumb_url = info.get("thumbnail")
        thumb_path = None
        if thumb_url and mode == "video":
            thumb_path = await download_thumb(thumb_url, DOWNLOAD_DIR / f"thumb_{uid}.jpg")
        return Result(True, fp, title, uploader, duration, thumb_path, size)
    except yt_dlp.utils.DownloadError as e:
        return Result(success=False, error=str(e))
    except Exception as e:
        logger.exception("Download error")
        return Result(success=False, error=str(e))


async def dl_video(url: str, on_progress=None) -> Result:
    uid  = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    hooks = [make_hook(on_progress, loop)] if on_progress else []
    opts = {
        **_COMMON,
        "outtmpl":  str(DOWNLOAD_DIR / f"video_{uid}.%(ext)s"),
        "format":   f"bestvideo[ext=mp4][filesize<{MAX_FILESIZE_MB}M]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "progress_hooks": hooks,
    }
    return await _run(url, opts, uid, "video")


async def dl_audio(url: str, on_progress=None) -> Result:
    uid  = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    hooks = [make_hook(on_progress, loop)] if on_progress else []
    opts = {
        **_COMMON,
        "outtmpl":  str(DOWNLOAD_DIR / f"audio_{uid}.%(ext)s"),
        "format":   "bestaudio/best",
        "progress_hooks": hooks,
        "writethumbnail": True,
        "embedthumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata",     "add_metadata": True},
            {"key": "EmbedThumbnail",     "already_have_thumbnail": False},
        ],
    }
    return await _run(url, opts, uid, "audio")


async def dl_instagram(url: str, on_progress=None) -> Result:
    uid  = uuid.uuid4().hex[:8]
    loop = asyncio.get_event_loop()
    hooks = [make_hook(on_progress, loop)] if on_progress else []
    opts = {
        **_COMMON,
        "outtmpl":  str(DOWNLOAD_DIR / f"video_{uid}.%(ext)s"),
        "format":   "best[ext=mp4]/best",
        "progress_hooks": hooks,
    }
    result = await _run(url, opts, uid, "video")
    if not result.success and ("login" in (result.error or "").lower() or "private" in (result.error or "").lower()):
        result.error = "❌ This post is private or requires login. Only public posts are supported."
    return result

# ══════════════════════════════════════════════════════════════════
#  CAPTION BUILDER
# ══════════════════════════════════════════════════════════════════

def build_caption(r: Result, mode: str) -> str:
    icon = "🎵" if mode == "audio" else "🎬"
    lines = [f"{icon} *{truncate(r.title)}*", f"👤 {truncate(r.uploader, 50)}"]
    if r.duration_secs:
        m, s = divmod(r.duration_secs, 60)
        lines.append(f"⏱ {m}:{s:02d}")
    if r.filesize:
        lines.append(f"💾 {human_size(r.filesize)}")
    lines.append("\n📥 _Downloaded by this bot_")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════
#  CORE SEND LOGIC
# ══════════════════════════════════════════════════════════════════

async def execute_download(chat_id, user_id, url, mode, status_msg, on_progress, context):
    if mode == "yt_video":
        result = await dl_video(url, on_progress)
    elif mode in ("yt_audio", "yt_music"):
        result = await dl_audio(url, on_progress)
    elif mode == "instagram":
        result = await dl_instagram(url, on_progress)
    else:
        await status_msg.edit_text("❓ Unknown mode.", parse_mode=ParseMode.MARKDOWN)
        return

    if not result.success:
        await status_msg.edit_text(f"❌ *Failed*\n\n{result.error}", parse_mode=ParseMode.MARKDOWN)
        return

    await status_msg.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

    caption = build_caption(result, mode)
    is_audio = mode in ("yt_audio", "yt_music")

    try:
        if is_audio:
            with open(result.file_path, "rb") as f:
                await context.bot.send_audio(
                    chat_id, audio=f, caption=caption, parse_mode=ParseMode.MARKDOWN,
                    title=result.title, performer=result.uploader, duration=result.duration_secs,
                )
        else:
            thumb_bytes = open(result.thumbnail_path, "rb") if result.thumbnail_path and result.thumbnail_path.exists() else None
            with open(result.file_path, "rb") as f:
                try:
                    await context.bot.send_video(
                        chat_id, video=f, caption=caption, parse_mode=ParseMode.MARKDOWN,
                        duration=result.duration_secs, supports_streaming=True, thumbnail=thumb_bytes,
                    )
                finally:
                    if thumb_bytes: thumb_bytes.close()
    except TelegramError as e:
        await status_msg.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        await delete_file(result.file_path)
        await delete_file(result.thumbnail_path)
        try: await status_msg.delete()
        except TelegramError: pass

# ══════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════

HELP_TEXT = (
    "📖 *Media Downloader Bot*\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🔗 *Supported Links:*\n\n"
    "📺 *YouTube*\n"
    "  `youtube.com/watch?v=...`\n"
    "  `youtu.be/...`\n"
    "  `youtube.com/shorts/...`\n\n"
    "🎵 *YouTube Music*\n"
    "  `music.youtube.com/...`\n\n"
    "📸 *Instagram*\n"
    "  `instagram.com/reel/...`\n"
    "  `instagram.com/p/...`\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "📌 *Limits:* 50 MB · 10 min · 5 req/min\n"
)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Help", callback_data="help"),
        InlineKeyboardButton("ℹ️ About", callback_data="about"),
    ]])
    await update.message.reply_text(
        f"👋 *Hey {user.first_name}!*\n\n"
        "I can download media from:\n\n"
        "📺 YouTube — Video or MP3\n"
        "🎵 YouTube Music — MP3\n"
        "📸 Instagram — Reels & Posts\n\n"
        "Just send me a link!",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""

    # Rate limit
    if user.id not in ADMIN_IDS and not limiter.is_allowed(user.id):
        wait = limiter.wait(user.id)
        await update.message.reply_text(f"⏳ Slow down! Wait *{wait}s* before next request.", parse_mode=ParseMode.MARKDOWN)
        return

    urls = extract_urls(text)
    if not urls:
        await update.message.reply_text("🔗 Send a valid YouTube or Instagram link.\nType /help for examples.")
        return

    url      = urls[0]
    platform = detect_platform(url)

    if platform == Platform.UNKNOWN:
        await update.message.reply_text("❓ *Unsupported link.*\n\nSupported: YouTube, YouTube Music, Instagram.", parse_mode=ParseMode.MARKDOWN)
        return

    # Instagram → auto download
    if platform == Platform.INSTAGRAM:
        status = await update.message.reply_text("⏳ *Processing Instagram link…*", parse_mode=ParseMode.MARKDOWN)
        async def prog(t): 
            try: await status.edit_text(t, parse_mode=ParseMode.MARKDOWN)
            except: pass
        await execute_download(update.effective_chat.id, user.id, url, "instagram", status, prog, context)
        return

    # YouTube Music → auto download MP3
    if platform == Platform.YOUTUBE_MUSIC:
        status = await update.message.reply_text("⏳ *Processing YouTube Music link…*", parse_mode=ParseMode.MARKDOWN)
        async def prog(t):
            try: await status.edit_text(t, parse_mode=ParseMode.MARKDOWN)
            except: pass
        await execute_download(update.effective_chat.id, user.id, url, "yt_music", status, prog, context)
        return

    # YouTube → ask format
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video (MP4)", callback_data=f"dl|yt_video|{url}"),
            InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"dl|yt_audio|{url}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await update.message.reply_text(
        f"📺 *YouTube link detected!*\n\n`{truncate(url, 60)}`\n\nChoose format:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True,
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data or ""

    if data == "cancel":
        await query.edit_message_text("❌ *Cancelled.*", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "help":
        await query.edit_message_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start")]]))
        return

    if data == "about":
        await query.edit_message_text(
            "🤖 *About*\n\nBuilt with:\n• `python-telegram-bot`\n• `yt-dlp`\n• `ffmpeg`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start")]]))
        return

    if data == "start":
        user = update.effective_user
        await query.edit_message_text(
            f"👋 *Hey {user.first_name}!* Send me a link to get started 🚀",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📖 Help", callback_data="help"),
                InlineKeyboardButton("ℹ️ About", callback_data="about"),
            ]]))
        return

    if data.startswith("dl|"):
        _, mode, url = data.split("|", 2)
        status = await query.edit_message_text("⏳ *Starting download…*", parse_mode=ParseMode.MARKDOWN)
        user   = update.effective_user

        async def prog(t):
            try: await status.edit_text(t, parse_mode=ParseMode.MARKDOWN)
            except: pass

        await execute_download(query.message.chat_id, user.id, url, mode, status, prog, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
        except: pass

# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "👋 Welcome"),
        BotCommand("help",  "📖 Help"),
    ])
    me = await app.bot.get_me()
    logger.info("✅ Bot running: @%s", me.username)

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set your BOT_TOKEN in the script first!")
        return

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help",  help_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    logger.info("Starting bot... (Ctrl+C to stop)")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()

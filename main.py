import os
import re
import hashlib
import logging
import urllib.parse
import asyncio
import math
import time
from contextlib import asynccontextmanager

import aiohttp

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pyrogram import Client
from pyrogram.errors import FloodWait
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── ENV CONFIG ────────────────────────────────────────

BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
API_ID           = int(os.getenv("API_ID", "0"))
API_HASH         = os.getenv("API_HASH", "")
STORAGE_CHANNEL  = int(os.getenv("STORAGE_CHANNEL", "0"))
SECRET_KEY       = os.getenv("SECRET_KEY", "mysecretkey123")
BASE_URL         = os.getenv("BASE_URL", "http://localhost:8000")
PORT             = int(os.getenv("PORT", 8000))
ALLOWED_USERS    = os.getenv("ALLOWED_USERS", "")
FIREBASE_URL     = os.getenv("FIREBASE_URL", "")  # e.g. https://animeverse-9eada-default-rtdb.firebaseio.com
SERVER_NAME      = os.getenv("SERVER_NAME", "Player")  # Firebase mein server field ki value

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pyro: Client = None

# ── IN-MEMORY USER SETUP STORE ────────────────────────
# Format: { user_id: {"slug": "attack-on-titan", "season": "S01"} }
user_setup: dict = {}


# ── HELPERS ───────────────────────────────────────────

def is_allowed(user_id):
    if not ALLOWED_USERS.strip():
        return True
    return str(user_id) in [u.strip() for u in ALLOWED_USERS.split(",")]


def generate_code(msg_id, filename):
    raw = f"{SECRET_KEY}:{msg_id}:{filename}"
    return hashlib.md5(raw.encode()).hexdigest()[:24]


def make_stream_link(msg_id, filename):
    safe = urllib.parse.quote(filename)
    code = generate_code(msg_id, filename)
    return f"{BASE_URL}/dl/{msg_id}/{safe}?code={code}"


def verify_code(msg_id, filename, code):
    return generate_code(msg_id, filename) == code


def extract_episode(text: str):
    """
    Caption ya filename se sirf episode NUMBER nikalta hai.

    Priority order:
      1. EPISODE/EP keyword ke baad number  → "EPISODE - 07", "EP04", "Ep.7", "ep_01"
      2. E + number (standalone)            → "E09", "E40"
      3. Standalone 1-2 digit number        → "07", "11", "40"
         (season number S1/S2 pehle hata deta hai false match avoid karne ke liye)

    Returns int ya None.
    """
    if not text:
        return None

    t = text.upper()
    ep_num = None

    # Priority 1: EPISODE / EP keyword ke baad number
    ep_match = re.search(r'\bEP(?:ISODE)?\s*[-:→►\s]*\s*(\d{1,3})\b', t)
    if ep_match:
        ep_num = int(ep_match.group(1))

    # Priority 2: E + number (E09, E40)
    if ep_num is None:
        e_match = re.search(r'\bE(\d{1,3})\b', t)
        if e_match:
            ep_num = int(e_match.group(1))

    # Priority 3: standalone 1-2 digit number
    # (S1/S2 jaise season numbers hata ke false match avoid karo)
    if ep_num is None:
        cleaned = re.sub(r'\bS\d{1,2}\b', '', t)
        nums = re.findall(r'\b(\d{1,2})\b', cleaned)
        if nums:
            ep_num = int(nums[0])

    return ep_num


def extract_quality(text: str):
    """Caption/filename se quality nikalta hai: 480p / 720p / 1080p. Returns str ya None."""
    if not text:
        return None
    q_match = re.search(r'\b(1080[Pp]|720[Pp]|480[Pp])\b', text)
    if q_match:
        val = q_match.group(1).lower()   # "1080p", "720p", "480p"
        return val
    return None


def get_extension(filename: str, fallback: str = "mp4") -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return fallback


async def save_to_firebase(slug: str, season: str, ep_num: int, stream_link: str) -> bool:
    """
    Firebase REST API se save karta hai — koi key.json nahi chahiye.
    Path: Animes/{slug}/{season}/E{ep_num} -> { link, server, time }
    Firebase Realtime DB REST: PUT {db_url}/path.json?auth={secret}  (ya bina auth agar rules open hain)
    """
    try:
        ep_key  = f"E{ep_num}"
        db_url  = FIREBASE_URL.rstrip("/")
        url     = f"{db_url}/Animes/{slug}/{season}/{ep_key}.json"
        payload = {
            "link"  : stream_link,
            "server": SERVER_NAME,
            "time"  : int(time.time()),
        }
        async with aiohttp.ClientSession() as session:
            async with session.put(url, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Firebase saved: Animes/{slug}/{season}/{ep_key}")
                    return True
                else:
                    text = await resp.text()
                    logger.error(f"Firebase error {resp.status}: {text}")
                    return False
    except Exception as e:
        logger.error(f"Firebase save error: {e}")
        return False


# ── BOT HANDLERS ──────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text(
        "👋 *Video Storage Bot*\n\n"
        "📌 *Setup karo:*\n`/setup <anime-slug> <season>`\n"
        "_Example: /setup attack-on-titan 1_\n\n"
        "Phir video forward karo — caption mein episode number hona chahiye "
        "jaise `Episode 7`, `Ep 01`, `EP-12` etc.\n\n"
        "Bot automatically filename banayega! 🚀",
        parse_mode="Markdown",
    )


async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setup <anime-slug> <season>
    Example: /setup mushoku-tensei S02
    """
    if not is_allowed(update.effective_user.id):
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:* `/setup <anime-slug> <season-number>`\n\n"
            "*Examples:*\n"
            "`/setup attack-on-titan 1`\n"
            "`/setup mushoku-tensei 2`\n"
            "`/setup one-piece 7`",
            parse_mode="Markdown",
        )
        return

    slug = args[0].lower().strip()
    raw_season = args[1].strip()
    # Sirf number diya (1,2,7) → S1,S2,S7 | S1/S01 diya → as-is uppercase
    if raw_season.isdigit():
        season = f"S{raw_season}"
    else:
        season = raw_season.upper()

    user_setup[update.effective_user.id] = {"slug": slug, "season": season}
    logger.info(f"User {update.effective_user.id} setup: slug={slug}, season={season}")

    await update.message.reply_text(
        f"✅ *Setup Saved!*\n\n"
        f"🎌 *Anime Slug:* `{slug}`\n"
        f"📺 *Season:* `{season}`\n\n"
        f"Ab video forward karo aur caption mein episode number likhna na bhoolo!\n"
        f"_Supported: Episode 7 / Ep 01 / EP-12 / E07_",
        parse_mode="Markdown",
    )


async def clear_setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clearsetup — user ka setup clear karta hai"""
    if not is_allowed(update.effective_user.id):
        return
    user_setup.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑️ Setup clear ho gaya. `/setup` se naya set karo.", parse_mode="Markdown")


async def current_setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mysetup — current setup dikhata hai"""
    if not is_allowed(update.effective_user.id):
        return
    setup = user_setup.get(update.effective_user.id)
    if not setup:
        await update.message.reply_text("⚠️ Koi setup nahi hai. `/setup` se set karo.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"📋 *Current Setup:*\n\n"
        f"🎌 *Anime:* `{setup['slug']}`\n"
        f"📺 *Season:* `{setup['season']}`",
        parse_mode="Markdown",
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    msg = update.message
    uid = update.effective_user.id
    file_obj = None
    raw_name = ""

    # ── Media type detect karo ──
    if msg.video:
        file_obj = msg.video
        raw_name = msg.video.file_name or ""
    elif msg.document:
        file_obj = msg.document
        raw_name = msg.document.file_name or ""
    elif msg.audio:
        file_obj = msg.audio
        raw_name = msg.audio.file_name or ""
    elif msg.video_note:
        file_obj = msg.video_note
        raw_name = ""
    else:
        await msg.reply_text("❌ Sirf video, document, ya audio files bhejein.")
        return

    # ── Episode number nikalo: caption pehle, phir filename fallback ──
    caption_text = msg.caption or ""
    ep_num = extract_episode(caption_text)          # Step 1: caption se try karo
    if ep_num is None and raw_name:                 # Step 2: caption mein nahi mila -> filename try
        ep_num = extract_episode(raw_name)

    # ── Filename banao ──
    setup = user_setup.get(uid)

    if setup:
        # Setup hai — episode number zaroori hai
        if ep_num is None:
            await msg.reply_text(
                "⚠️ *Episode number nahi mila!*\n\n"
                "Caption mein episode number likhna zaroori hai.\n"
                "*Supported formats:*\n"
                "`Episode 7` | `Ep 01` | `EP-12` | `E07`\n\n"
                "_Video forward karte waqt caption mein likho._",
                parse_mode="Markdown",
            )
            return

        ep_str = str(ep_num)                    # 1→"1", 7→"7", 12→"12"
        ext = get_extension(raw_name, fallback="mp4" if (msg.video or msg.video_note) else "mkv")
        filename = f"{setup['slug']}-{setup['season']}-E{ep_str}.{ext}"

    else:
        # No setup — original naming fallback
        if msg.video:
            filename = raw_name or f"video_{file_obj.file_unique_id}.mp4"
        elif msg.document:
            filename = raw_name or f"doc_{file_obj.file_unique_id}"
        elif msg.audio:
            filename = raw_name or f"audio_{file_obj.file_unique_id}.mp3"
        else:
            filename = f"videonote_{file_obj.file_unique_id}.mp4"

    # ── Processing ──
    processing = await msg.reply_text("⏳ Processing...")

    try:
        forwarded = await context.bot.copy_message(
            chat_id=STORAGE_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        storage_msg_id = forwarded.message_id
        stream_link = make_stream_link(storage_msg_id, filename)
        file_size_mb = round(file_obj.file_size / (1024 * 1024), 2) if file_obj.file_size else "?"

        # ── Firebase mein save karo (sirf setup mode mein) ──
        fb_saved = False
        if setup and ep_num:
            fb_saved = await save_to_firebase(setup["slug"], setup["season"], ep_num, stream_link)

        # Episode info line (setup mode mein hi dikhao)
        ep_line = f"🎬 *Episode:* `E{ep_num}`\n" if setup and ep_num else ""
        fb_line = f"🔥 *Firebase:* `Animes/{setup['slug']}/{setup['season']}/E{ep_num}`\n" if fb_saved else ""

        await processing.delete()
        await msg.reply_text(
            f"✅ *File Saved Successfully!*\n\n"
            f"📁 *File:* `{filename}`\n"
            f"{ep_line}"
            f"{fb_line}"
            f"📦 *Size:* {file_size_mb} MB\n"
            f"🆔 *Storage ID:* `{storage_msg_id}`\n\n"
            f"🔗 *Streaming Link:*\n`{stream_link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶️ Stream / Download", url=stream_link)]]
            ),
        )
    except Exception as e:
        logger.error(f"handle_media error: {e}")
        await processing.edit_text(f"❌ Error: {e}")


async def get_link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/getlink <message_id> <filename>`", parse_mode="Markdown")
        return
    try:
        msg_id = int(args[0])
        filename = " ".join(args[1:])
        link = make_stream_link(msg_id, filename)
        await update.message.reply_text(
            f"🔗 *Link:*\n`{link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Open", url=link)]]),
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid message ID.")


# ── FASTAPI ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pyro
    pyro = Client(
        "stream_session",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )
    await pyro.start()
    logger.info("Pyrogram client started.")
    yield
    await pyro.stop()


web_app = FastAPI(title="TG Stream Server", lifespan=lifespan)


@web_app.get("/")
async def index():
    return HTMLResponse("""
    <html><body style='font-family:sans-serif;text-align:center;padding:80px;background:#0f0f0f;color:#fff'>
    <h1>🎬 TG Stream Server</h1><p style='color:#aaa'>Online ✅</p>
    </body></html>
    """)



@web_app.get("/watch/{msg_id}/{filename:path}")
async def watch_file(msg_id: int, filename: str, code: str):
    """HTML5 player page — preload=auto se fast buffering"""
    decoded = urllib.parse.unquote(filename)
    if not verify_code(msg_id, decoded, code):
        raise HTTPException(status_code=403, detail="Invalid or expired link.")

    stream_url = f"/dl/{msg_id}/{urllib.parse.quote(decoded)}?code={code}"
    safe_title = decoded.replace('"', '&quot;')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box }}
  body {{ background:#000; display:flex; flex-direction:column; align-items:center;
          justify-content:center; min-height:100vh; font-family:sans-serif; color:#fff }}
  video {{ width:100%; max-width:960px; max-height:90vh; background:#000 }}
  .title {{ margin-top:12px; font-size:14px; color:#aaa; max-width:960px;
             white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding:0 12px }}
</style>
</head>
<body>
<video
  src="{stream_url}"
  controls
  autoplay
  preload="auto"
  playsinline
  controlslist="nodownload"
></video>
<div class="title">{safe_title}</div>
</body>
</html>"""
    return HTMLResponse(html)


@web_app.get("/dl/{msg_id}/{filename:path}")
async def stream_file(msg_id: int, filename: str, code: str, request: Request):
    decoded = urllib.parse.unquote(filename)

    if not verify_code(msg_id, decoded, code):
        raise HTTPException(status_code=403, detail="Invalid or expired link.")

    try:
        message = await pyro.get_messages(STORAGE_CHANNEL, msg_id)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        message = await pyro.get_messages(STORAGE_CHANNEL, msg_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Error: {e}")

    if not message or message.empty:
        raise HTTPException(status_code=404, detail="Message not found.")

    media = message.video or message.document or message.audio or message.video_note
    if not media:
        raise HTTPException(status_code=404, detail="No media in message.")

    file_size = media.file_size
    mime_type = getattr(media, "mime_type", "application/octet-stream")
    CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB — faster initial load

    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1

    if range_header:
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0]) if parts[0] else 0
        end   = int(parts[1]) if parts[1] else file_size - 1

    content_length   = end - start + 1
    offset           = start // CHUNK_SIZE
    first_chunk_cut  = start % CHUNK_SIZE
    limit            = math.ceil(content_length / CHUNK_SIZE)

    safe_filename = urllib.parse.quote(decoded)

    response_headers = {
        "Content-Type":        mime_type,
        "Accept-Ranges":       "bytes",
        "Content-Disposition": f"inline; filename*=UTF-8''{safe_filename}",
        "Content-Length":      str(content_length),
        "Cache-Control":       "no-store",
    }
    if range_header:
        response_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    async def generator():
        bytes_sent  = 0
        chunk_index = 0
        try:
            async for chunk in pyro.stream_media(message, offset=offset, limit=limit):
                if chunk_index == 0:
                    chunk = chunk[first_chunk_cut:]
                remaining = content_length - bytes_sent
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                yield chunk
                bytes_sent  += len(chunk)
                chunk_index += 1
                if bytes_sent >= content_length:
                    break
        except Exception as e:
            logger.error(f"Stream error msg_id={msg_id}: {e}")

    status_code = 206 if range_header else 200
    logger.info(f"Streaming msg_id={msg_id} | {decoded} | bytes {start}-{end}/{file_size}")
    return StreamingResponse(generator(), status_code=status_code, headers=response_headers)


# ── MAIN ──────────────────────────────────────────────

async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("setup",       setup_cmd))
    app.add_handler(CommandHandler("mysetup",     current_setup_cmd))
    app.add_handler(CommandHandler("clearsetup",  clear_setup_cmd))
    app.add_handler(CommandHandler("getlink",     get_link_cmd))

    # Media handler
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VIDEO_NOTE,
        handle_media,
    ))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot started polling.")
    return app


async def run_server():
    config = uvicorn.Config(web_app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    bot_app = await run_bot()
    try:
        await asyncio.gather(run_server())
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

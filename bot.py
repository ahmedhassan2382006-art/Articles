# -*- coding: utf-8 -*-
import os
import sys
import re
import json
import base64
import time
import math
import shutil
import logging
import asyncio
import tempfile
import subprocess
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Any, List

import requests
import feedparser
from bs4 import BeautifulSoup
import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==================== إعداد السجلات ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== قراءة التوكنات بذكاء ====================
SECRET_RAW = (
    os.environ.get("BOT_SECRET")
    or os.environ.get("3bot")
    or os.environ.get("BOT_TOKEN")
    or ""
).strip()

BOT_TOKEN = None
GITHUB_TOKEN = None

for sep in ["\\", "/", "\n", "|"]:
    if sep in SECRET_RAW:
        parts = SECRET_RAW.split(sep, 1)
        BOT_TOKEN = parts[0].strip()
        GITHUB_TOKEN = parts[1].strip()
        break

if not BOT_TOKEN:
    BOT_TOKEN = SECRET_RAW

if not BOT_TOKEN:
    logger.error("❌ لم يتم العثور على توكن البوت في السيكريت!")
    sys.exit(1)

REPO_OWNER = "ahmedhassan2382006-art"
REPO_NAME = "Articles"
CONFIG_FILE_PATH = "config.json"
GITHUB_API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{CONFIG_FILE_PATH}"

MAX_TELEGRAM_FILE_SIZE = 49 * 1024 * 1024

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
}

# ==================== مزامنة GitHub API بدون تجميد البوت ====================
GITHUB_FILE_SHA = None

def _sync_get_config():
    global GITHUB_FILE_SHA
    default_cfg = {
        "check_interval_minutes": 10,
        "text_mode": "full",
        "video_mode": "file",
        "photo_mode": "file",
        "registered_chats": [],
        "sites": []
    }

    if not GITHUB_TOKEN:
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default_cfg

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-RSS-Bot"
    }
    try:
        res = requests.get(GITHUB_API_URL, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            GITHUB_FILE_SHA = data.get("sha")
            raw_content = base64.b64decode(data.get("content", "")).decode("utf-8")
            return json.loads(raw_content)
    except Exception as e:
        logger.error(f"خطأ قراءة جيت هب: {e}")

    return default_cfg

def _sync_save_config(new_config: dict):
    global GITHUB_FILE_SHA
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if not GITHUB_TOKEN:
        return True

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-RSS-Bot"
    }
    try:
        content_bytes = json.dumps(new_config, ensure_ascii=False, indent=2).encode("utf-8")
        b64_content = base64.b64encode(content_bytes).decode("utf-8")
        payload = {
            "message": "Update config.json [Bot Auto Sync]",
            "content": b64_content,
            "sha": GITHUB_FILE_SHA
        }
        res = requests.put(GITHUB_API_URL, headers=headers, json=payload, timeout=8)
        if res.status_code in (200, 201):
            GITHUB_FILE_SHA = res.json().get("content", {}).get("sha", GITHUB_FILE_SHA)
            return True
    except Exception as e:
        logger.error(f"خطأ حفظ جيت هب: {e}")
    return False

CONFIG = _sync_get_config()

async def save_config_async():
    await asyncio.to_thread(_sync_save_config, CONFIG)

# ==================== فحص الـ RSS المطور وتخطي الحظر ====================
def parse_feed_robust(url: str):
    url = url.strip()
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code == 200 and resp.content:
            feed = feedparser.parse(resp.content)
            if feed.entries or len(feed.feed) > 0:
                return feed, url
    except Exception:
        pass

    try:
        feed = feedparser.parse(url)
        if feed.entries or len(feed.feed) > 0:
            return feed, url
    except Exception:
        pass

    if not any(url.endswith(x) for x in ("/feed", "/feed/", "/rss", ".xml")):
        for suffix in ["/feed", "/feed/", "/rss"]:
            test_url = url.rstrip("/") + suffix
            try:
                resp = requests.get(test_url, headers=BROWSER_HEADERS, timeout=7)
                if resp.status_code == 200:
                    feed = feedparser.parse(resp.content)
                    if feed.entries:
                        return feed, test_url
            except Exception:
                continue

    return None, url

def parse_date(entry):
    for f in ('published', 'updated', 'pubDate'):
        raw = entry.get(f)
        if raw and isinstance(raw, str):
            try:
                dt = parsedate_to_datetime(raw)
                return dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass
    return "تاريخ غير محدد"

def extract_article_text(entry, link: str) -> str:
    full_text = ""
    if 'content' in entry and len(entry.content) > 0:
        full_text = entry.content[0].get('value', '')
    elif 'summary_detail' in entry and entry.summary_detail.get('value'):
        full_text = entry.summary_detail.get('value', '')
    elif 'summary' in entry:
        full_text = entry.get('summary', '')

    if full_text:
        return BeautifulSoup(full_text, "html.parser").get_text(separator="\n", strip=True)

    if link:
        try:
            r = requests.get(link, timeout=7, headers=BROWSER_HEADERS)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, "html.parser")
                for s in soup(["script", "style", "nav", "header", "footer"]):
                    s.decompose()
                main_tag = soup.find("article") or soup.find("main") or soup.body
                if main_tag:
                    return main_tag.get_text(separator="\n", strip=True)[:2500]
        except Exception:
            pass
    return "نص الموضوع متاح عبر الرابط المرفق."

# ==================== تنزيل الفيديوهات وتقطيعها ====================
YTDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "visionos", "default"]
        }
    }
}

def download_video_stream(url: str, out_folder: str) -> str:
    out_tmpl = os.path.join(out_folder, "%(id)s.%(ext)s")
    opts = dict(YTDL_BASE_OPTS)
    opts.update({
        "outtmpl": out_tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

def split_video_ffmpeg(filepath: str, max_chunk_bytes: int = MAX_TELEGRAM_FILE_SIZE) -> List[str]:
    filesize = os.path.getsize(filepath)
    if filesize <= max_chunk_bytes:
        return [filepath]

    cmd_dur = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", filepath
    ]
    try:
        res = subprocess.run(cmd_dur, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration = float(res.stdout.strip())
    except Exception:
        duration = 600.0

    num_parts = math.ceil(filesize / (max_chunk_bytes * 0.92))
    part_duration = duration / num_parts

    parts = []
    base_dir = os.path.dirname(filepath)
    name, ext = os.path.splitext(os.path.basename(filepath))

    for i in range(num_parts):
        start_time = i * part_duration
        out_part = os.path.join(base_dir, f"{name}_part_{i+1:02d}{ext}")
        cmd_split = [
            "ffmpeg", "-y", "-ss", str(start_time), "-i", filepath,
            "-t", str(part_duration), "-c", "copy", "-avoid_negative_ts", "1",
            out_part
        ]
        subprocess.run(cmd_split, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(out_part) and os.path.getsize(out_part) > 0:
            parts.append(out_part)

    return parts if parts else [filepath]

# ==================== لوحة المفاتيح ====================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة موقع", callback_data="add_site"), InlineKeyboardButton("🗑️ حذف موقع", callback_data="del_site")],
        [InlineKeyboardButton("📋 عرض المواقع", callback_data="list_sites"), InlineKeyboardButton("⚙️ الإعدادات المتقدمة", callback_data="settings")],
        [InlineKeyboardButton("🔄 فحص فوري لكافة المصادر", callback_data="force_check")]
    ])

def get_settings_keyboard():
    s = CONFIG
    t_labels = {"full": "📄 النص كاملاً", "summary": "📝 ملخص فقط", "title": "📌 العنوان فقط"}
    v_labels = {"file": "🎬 إرسال كفيديو مجزأ", "link": "🔗 إرسال كرابط فقط"}
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"عرض المنشورات: {t_labels.get(s.get('text_mode'), 'كامل')}", callback_data="toggle_text")],
        [InlineKeyboardButton(f"الفيديوهات: {v_labels.get(s.get('video_mode'), 'ملف')}", callback_data="toggle_video")],
        [InlineKeyboardButton(f"⏱️ مدة الفحص: كل {s.get('check_interval_minutes', 10)} دقيقة", callback_data="change_interval")],
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")]
    ])

# ==================== الأوامر ومعالجة الرسائل ====================
async def register_chat_if_needed(chat_id):
    if chat_id not in CONFIG.get("registered_chats", []):
        CONFIG.setdefault("registered_chats", []).append(chat_id)
        await save_config_async()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await register_chat_if_needed(chat_id)

    name = update.effective_user.first_name or "يا بطل"
    await update.message.reply_text(
        f"👋 **أهلاً بك يا {name} في بوت المتابعة الذكي!**\n"
        f"المستودع: `{REPO_OWNER}/{REPO_NAME}`\n"
        f"تم تفعيل هذا الشات بنجاح.\n\n"
        f"💡 لإضافة أي موقع في المجموعة أو الخاص، اكتب:\n"
        f"`/add رابط_الموقع` أو ابدأ الرابط بـ `/`\n\n"
        f"👇 **لوحة التحكم السريعة:**",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await register_chat_if_needed(chat_id)

    raw_text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+)', raw_text)
    if not match:
        await update.message.reply_text("❌ يرجى كتابة الرابط بجوار الأمر، مثال:\n`/add https://example.com/feed`", parse_mode="Markdown")
        return
    await process_and_add_url(match.group(1), update)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "main_menu":
        await query.edit_message_text("👇 **لوحة التحكم الرئيسية:**", reply_markup=get_main_keyboard(), parse_mode="Markdown")

    elif data == "list_sites":
        sites = CONFIG.get("sites", [])
        if not sites:
            await query.edit_message_text("📭 لا توجد أي مواقع مضافة حالياً.", reply_markup=get_main_keyboard())
            return
        text = "📋 **قائمة المواقع والروابط المتابعة:**\n\n"
        for idx, s in enumerate(sites, 1):
            text += f"{idx}. 📌 **{s.get('name')}**\n🔗 `{s.get('url')}`\n\n"
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

    elif data == "add_site":
        context.user_data["waiting_for_url"] = True
        await query.message.reply_text("🔗 **أرسل الآن الرابط (سواء بدأ بـ / أو بدون):**")

    elif data == "del_site":
        sites = CONFIG.get("sites", [])
        if not sites:
            await query.edit_message_text("📭 لا توجد مواقع لحذفها.", reply_markup=get_main_keyboard())
            return
        buttons = []
        for s in sites:
            buttons.append([InlineKeyboardButton(f"❌ {s.get('name')}", callback_data=f"del_{s.get('id')}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
        await query.edit_message_text("🗑️ **اختر الموقع المراد حذفه نهائياً:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("del_"):
        site_id = data[4:]
        CONFIG["sites"] = [s for s in CONFIG.get("sites", []) if str(s.get("id")) != site_id]
        await save_config_async()
        await query.edit_message_text("✅ **تم حذف الموقع بنجاح!**", reply_markup=get_main_keyboard())

    elif data == "settings":
        await query.edit_message_text("⚙️ **إعدادات البوت والتحكم في المحتوى:**", reply_markup=get_settings_keyboard())

    elif data == "toggle_text":
        modes = ["full", "summary", "title"]
        cur = CONFIG.get("text_mode", "full")
        CONFIG["text_mode"] = modes[(modes.index(cur) + 1) % len(modes)]
        await save_config_async()
        await query.edit_message_reply_markup(reply_markup=get_settings_keyboard())

    elif data == "toggle_video":
        cur = CONFIG.get("video_mode", "file")
        CONFIG["video_mode"] = "link" if cur == "file" else "file"
        await save_config_async()
        await query.edit_message_reply_markup(reply_markup=get_settings_keyboard())

    elif data == "change_interval":
        intervals = [5, 10, 15, 30, 60]
        cur = CONFIG.get("check_interval_minutes", 10)
        idx = intervals.index(cur) if cur in intervals else 0
        CONFIG["check_interval_minutes"] = intervals[(idx + 1) % len(intervals)]
        await save_config_async()
        await query.edit_message_reply_markup(reply_markup=get_settings_keyboard())

    elif data == "force_check":
        await query.message.reply_text("⏳ **سيتم الفحص الآن...**")
        await process_feeds_cycle(context.application, announce=False)
        await query.message.reply_text("✅ **اكتمل فحص جميع المواقع بنجاح.**")

async def process_and_add_url(raw_url: str, update: Update):
    clean_url = raw_url.strip()
    status_msg = await update.message.reply_text("🔍 **جاري فحص الرابط سريعاً وتخطي الحجب...**")

    feed, final_url = await asyncio.to_thread(parse_feed_robust, clean_url)

    if not feed or (not feed.entries and len(feed.feed) == 0):
        await status_msg.edit_text("❌ **تعذر قراءة الخلاصات من هذا الرابط.** تأكد أن الرابط يعمل ومتاح.")
        return

    site_name = feed.feed.get("title") or "مصدر تقني"
    last_id = getattr(feed.entries[0], 'id', getattr(feed.entries[0], 'link', '')) if feed.entries else ""

    new_entry = {
        "id": int(time.time()),
        "name": site_name,
        "url": final_url,
        "last_id": last_id
    }
    CONFIG.setdefault("sites", []).append(new_entry)
    await save_config_async()

    await status_msg.edit_text(
        f"✅ **تمت إضافة المصدر بنجاح!**\n📌 **الاسم:** {site_name}\n🔗 `{final_url}`",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """التقاط الروابط حتى لو بدأت بـ / في المجموعات أو الخاص"""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    await register_chat_if_needed(chat_id)

    # التحقق إن كان النص يحتوي على رابط ويب
    match = re.search(r'(https?://[^\s]+)', text)
    if match:
        # إما أنه ضغط زر الإضافة، أو بدأ الرسالة بـ /
        if context.user_data.get("waiting_for_url") or text.startswith("/"):
            context.user_data["waiting_for_url"] = False
            await process_and_add_url(match.group(1), update)
    elif context.user_data.get("waiting_for_url"):
        context.user_data["waiting_for_url"] = False
        cleaned = text.lstrip("/").strip()
        if not cleaned.startswith("http"):
            cleaned = "https://" + cleaned
        await process_and_add_url(cleaned, update)

# ==================== دورة فحص المواقع بالتوالي الدقيق ====================
async def process_feeds_cycle(app, announce=True):
    sites = CONFIG.get("sites", [])
    chats = CONFIG.get("registered_chats", [])
    if not sites or not chats:
        return

    # إرسال إشعار بدء الفحص
    if announce:
        for cid in chats:
            try:
                await app.bot.send_message(cid, "⏳ **سيتم الفحص الدوري الآن...**", parse_mode="Markdown")
            except Exception:
                pass

    text_mode = CONFIG.get("text_mode", "full")
    video_mode = CONFIG.get("video_mode", "file")

    # فحص موقع موقع على التوالي وبدون تداخل
    for idx, site in enumerate(sites, 1):
        try:
            feed, _ = await asyncio.to_thread(parse_feed_robust, site["url"])
            if not feed or not feed.entries:
                continue

            new_items = []
            for entry in feed.entries:
                e_id = getattr(entry, 'id', getattr(entry, 'link', ''))
                if e_id == site.get("last_id"):
                    break
                new_items.append(entry)

            if new_items:
                for entry in reversed(new_items):
                    title = entry.get("title", "بدون عنوان").strip()
                    link = entry.get("link", "").strip()
                    pub_date = parse_date(entry)

                    if text_mode == "title":
                        msg_body = f"🔔 **منشور جديد من {site['name']}**\n\n📌 {title}\n🕒 {pub_date}\n🔗 {link}"
                    elif text_mode == "summary":
                        raw_summary = entry.get('summary', '') or entry.get('description', '')
                        clean_summary = BeautifulSoup(raw_summary, "html.parser").get_text(separator=" ", strip=True)[:350]
                        msg_body = f"🔔 **منشور جديد من {site['name']}**\n\n📌 {title}\n🕒 {pub_date}\n\n📝 **الملخص:**\n{clean_summary}...\n\n🔗 {link}"
                    else:
                        body = await asyncio.to_thread(extract_article_text, entry, link)
                        msg_body = (
                            f"🔔 **{site['name']}**\n\n"
                            f"📌 **العنوان:** {title}\n\n"
                            f"📝 **نص الموضوع:**\n{body[:3500]}\n\n"
                            f"🕒 **الوقت:** {pub_date}\n"
                            f"🔗 **رابط صفحة الويب:** {link}"
                        )

                    is_video_post = ("youtube.com" in link or "youtu.be" in link)
                    download_done = False

                    if is_video_post and video_mode == "file":
                        temp_dir = tempfile.mkdtemp()
                        try:
                            downloaded_file = await asyncio.to_thread(download_video_stream, link, temp_dir)
                            if downloaded_file and os.path.exists(downloaded_file):
                                parts = await asyncio.to_thread(split_video_ffmpeg, downloaded_file)
                                total_parts = len(parts)

                                for c_id in chats:
                                    for p_idx, part_path in enumerate(parts, 1):
                                        caption = f"🎬 {title[:60]}"
                                        if total_parts > 1:
                                            caption += f" (الجزء {p_idx} من {total_parts})"
                                        with open(part_path, "rb") as vf:
                                            await app.bot.send_video(
                                                chat_id=c_id,
                                                video=vf,
                                                caption=caption,
                                                supports_streaming=True
                                            )
                                download_done = True
                        except Exception as ve:
                            logger.error(f"خطأ تحميل الفيديو: {ve}")
                        finally:
                            shutil.rmtree(temp_dir, ignore_errors=True)

                    if not download_done:
                        for c_id in chats:
                            try:
                                await app.bot.send_message(chat_id=c_id, text=msg_body)
                            except Exception as me:
                                logger.error(f"فشل إرسال المقال إلى {c_id}: {me}")

                    site["last_id"] = getattr(feed.entries[0], 'id', getattr(feed.entries[0], 'link', ''))
                    await save_config_async()

            # انتظار بسيط ومريح بين كل موقع والآخر لضمان عدم حظر تيليجرام
            await asyncio.sleep(0.5)

        except Exception as se:
            logger.error(f"خطأ فحص {site.get('name')}: {se}")

    # إشعار انتهاء الفحص
    if announce:
        for cid in chats:
            try:
                await app.bot.send_message(cid, "✅ **اكتمل فحص جميع المواقع بنجاح.**", parse_mode="Markdown")
            except Exception:
                pass

# ==================== الفحص الدوري الخلفي ====================
async def background_loop(app):
    await asyncio.sleep(15)
    while True:
        try:
            await process_feeds_cycle(app, announce=True)
        except Exception as e:
            logger.error(f"خطأ الفحص الدوري: {e}")
        interval_secs = max(60, CONFIG.get("check_interval_minutes", 10) * 60)
        await asyncio.sleep(interval_secs)

async def on_post_init(app):
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم بدء حلقة الفحص المتتالية بنجاح.")

# ==================== تشغيل التطبيق ====================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(16)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # التقاط أي رسالة تبدأ بـ / أو روابط
    app.add_handler(MessageHandler(filters.ALL, handle_any_message))

    print("🚀 تم تشغيل البوت بنجاح! البوت الآن فائق السرعة وجاهز...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
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
from datetime import datetime, timezone
import calendar
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
from telegram.error import BadRequest
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

# ==================== قراءة التوكنات ====================
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
START_TIME = time.time()
MAX_SERVER_LIFETIME = (5 * 3600) - 300 

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}

scraping_session = requests.Session()
scraping_session.headers.update(BROWSER_HEADERS)

trans_session = requests.Session()
trans_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9"
})

CONFIG_LOCK = asyncio.Lock()
GITHUB_FILE_SHA = None

# اللغات المدعومة للترجمة
SUPPORTED_LANGS = {
    'ar': '🇸🇦 العربية',
    'en': '🇬🇧 الإنجليزية',
    'fr': '🇫🇷 الفرنسية',
    'es': '🇪🇸 الإسبانية',
    'de': '🇩🇪 الألمانية',
    'tr': '🇹🇷 التركية',
    'none': '🚫 إيقاف الترجمة'
}

# ==================== خوارزميات الترجمة مثل الأداة ====================
def parse_google_response(data):
    if isinstance(data, dict):
        if "sentences" in data:
            return "".join([s.get("trans", "") for s in data["sentences"] if isinstance(s, dict)])
        if "translation" in data:
            return data["translation"]
    elif isinstance(data, list):
        result = []
        if len(data) > 0 and isinstance(data[0], list):
            for item in data[0]:
                if isinstance(item, list) and len(item) > 0 and isinstance(item[0], str):
                    result.append(item[0])
                elif isinstance(item, str):
                    result.append(item)
            if result:
                return "".join(result)
        elif len(data) > 0 and isinstance(data[0], str):
            return "".join(data)
    elif isinstance(data, str):
        return data
    return None

def translate_google(text, target_lang):
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8", "Accept": "*/*"}
    try:
        url = "https://clients5.google.com/translate_a/t"
        params = {"client": "dict-chrome-ex", "sl": "auto", "tl": target_lang}
        res = trans_session.post(url, params=params, data={"q": text}, headers=headers, timeout=8)
        if res.status_code == 200:
            parsed = parse_google_response(res.json())
            if parsed and parsed.strip():
                return parsed.strip()
    except Exception:
        pass

    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t"}
        res = trans_session.post(url, params=params, data={"q": text}, headers=headers, timeout=8)
        if res.status_code == 200:
            parsed = parse_google_response(res.json())
            if parsed and parsed.strip():
                return parsed.strip()
    except Exception:
        pass

    raise RuntimeError("تعذر الاتصال بخوادم ترجمة جوجل.")

def translate_mymemory(text, target_lang):
    url = "https://api.mymemory.translated.net/get"
    params = {"q": text[:450], "langpair": f"en|{target_lang}"}
    res = trans_session.get(url, params=params, timeout=8)
    if res.status_code == 200:
        val = res.json().get("responseData", {}).get("translatedText", "")
        if val and "MYMEMORY WARNING" not in val and "INVALID TARGET" not in val:
            return val
    raise RuntimeError("فشل خادم مايميموري.")

def split_into_chunks(text, max_len=700):
    if not text:
        return []
    paragraphs = text.split("\n")
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        p = para.strip()
        if not p:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            continue

        if len(p) > max_len:
            sentences = re.split(r'(?<=[.?!؟؛\n])\s+', p)
            for s in sentences:
                s = s.strip()
                if not s:
                    continue
                if len(s) > max_len:
                    for i in range(0, len(s), max_len):
                        sub = s[i:i+max_len].strip()
                        if sub:
                            chunks.append(sub)
                elif len(current_chunk) + len(s) + 1 <= max_len:
                    current_chunk += (" " if current_chunk else "") + s
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = s
        elif len(current_chunk) + len(p) + 1 <= max_len:
            current_chunk += ("\n\n" if current_chunk else "") + p
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = p

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks

def _execute_translation(chunk, target_lang, engine):
    if not chunk or not chunk.strip():
        return chunk
    engines = [engine]
    if "google" not in engines:
        engines.append("google")
    if "mymemory" not in engines:
        engines.append("mymemory")

    for eng in engines:
        try:
            if eng == "google":
                return translate_google(chunk, target_lang)
            elif eng == "mymemory":
                return translate_mymemory(chunk, target_lang)
        except Exception:
            continue
    return chunk

def translate_text_pipeline(text, target_lang, engine="google"):
    if not text or target_lang == 'none':
        return text, False

    chunks = split_into_chunks(text, max_len=700)
    if not chunks:
        return text, False

    translated_blocks = []
    any_success = False

    for chunk in chunks:
        trans_chunk = _execute_translation(chunk, target_lang, engine)
        if trans_chunk != chunk:
            any_success = True
        translated_blocks.append(trans_chunk)
        time.sleep(0.08)

    return "\n\n".join(translated_blocks), any_success

def is_english_content(title: str, text: str = "") -> bool:
    """كشف المنشورات الإنجليزية تلقائياً"""
    sample = f"{title} {text[:300]}"
    eng_chars = len(re.findall(r'[a-zA-Z]', sample))
    ar_chars = len(re.findall(r'[\u0600-\u06FF]', sample))
    return eng_chars > ar_chars and eng_chars >= 6

# ==================== مزامنة GitHub API وحفظ الإعدادات ====================
def _sync_get_config():
    global GITHUB_FILE_SHA
    default_cfg = {
        "check_interval_minutes": 10,
        "last_check_timestamp": 0,
        "trans_engine": "google",
        "target_lang": "ar",
        "video_mode": "file",
        "active_chat_id": None,
        "sites": []
    }

    if not GITHUB_TOKEN:
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    for k, v in default_cfg.items():
                        cfg.setdefault(k, v)
                    return cfg
            except Exception:
                pass
        return default_cfg

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-RSS-Bot"
    }
    try:
        res = requests.get(GITHUB_API_URL, headers=headers, timeout=12)
        if res.status_code == 200:
            data = res.json()
            GITHUB_FILE_SHA = data.get("sha")
            raw_content = base64.b64decode(data.get("content", "")).decode("utf-8")
            cfg = json.loads(raw_content)
            for k, v in default_cfg.items():
                cfg.setdefault(k, v)
            return cfg
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

    for attempt in range(2):
        try:
            if not GITHUB_FILE_SHA:
                res_check = requests.get(GITHUB_API_URL, headers=headers, timeout=8)
                if res_check.status_code == 200:
                    GITHUB_FILE_SHA = res_check.json().get("sha")

            content_bytes = json.dumps(new_config, ensure_ascii=False, indent=2).encode("utf-8")
            b64_content = base64.b64encode(content_bytes).decode("utf-8")
            payload = {
                "message": "Update config.json [Bot Auto Sync]",
                "content": b64_content,
                "sha": GITHUB_FILE_SHA
            }
            res = requests.put(GITHUB_API_URL, headers=headers, json=payload, timeout=10)
            if res.status_code in (200, 201):
                GITHUB_FILE_SHA = res.json().get("content", {}).get("sha", GITHUB_FILE_SHA)
                return True
            elif res.status_code == 409:
                GITHUB_FILE_SHA = None
        except Exception as e:
            logger.error(f"خطأ حفظ جيت هب: {e}")
            GITHUB_FILE_SHA = None
    return False

CONFIG = _sync_get_config()

async def save_config_async():
    async with CONFIG_LOCK:
        await asyncio.to_thread(_sync_save_config, CONFIG)

async def safe_edit_text(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            try:
                await query.message.reply_text(text=text, reply_markup=reply_markup, disable_web_page_preview=True)
            except Exception:
                pass
    except Exception as ex:
        logger.error(f"خطأ تعديل الرسالة: {ex}")

# ==================== استخراج الخلاصات والمقالات ====================
def parse_feed_robust(url: str):
    url = url.strip()
    try:
        resp = scraping_session.get(url, timeout=10, allow_redirects=True)
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
                resp = scraping_session.get(test_url, timeout=7)
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
    return "تاريخ النشر غير محدد"

def get_entry_id(entry):
    return str(getattr(entry, 'id', getattr(entry, 'link', '')) or getattr(entry, 'title', '')).strip()

def extract_full_article(url: str, entry=None) -> str:
    """استخراج المقال النصي كاملاً من المصدر بنفس طريقة الأداة"""
    if url:
        try:
            resp = scraping_session.get(url, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "iframe"]):
                    tag.decompose()

                container = (
                    soup.find("article") or
                    soup.find("main") or
                    soup.find("div", {"role": "main"}) or
                    soup.find(class_=re.compile(r"(article-content|post-content|entry-content|story-body|article__body|content__body|post-body)", re.I)) or
                    soup.body
                )

                if container:
                    parts = []
                    for elem in container.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
                        text = elem.get_text(separator=" ", strip=True)
                        if not text or len(text) < 15:
                            continue
                        if elem.name in ["h1", "h2", "h3", "h4"]:
                            parts.append(f"\n【 {text} 】\n")
                        elif elem.name == "li":
                            parts.append(f"• {text}")
                        elif elem.name == "blockquote":
                            parts.append(f"« {text} »")
                        else:
                            parts.append(text)

                    result = "\n\n".join(parts)
                    if len(result.strip()) > 100:
                        return result.strip()
        except Exception:
            pass

    # إذا تعذر السحب المباشر، يتم الاعتماد على محتوى الـ RSS
    if entry:
        if 'content' in entry and len(entry.content) > 0:
            full_text = entry.content[0].get('value', '')
        elif 'summary_detail' in entry and entry.summary_detail.get('value'):
            full_text = entry.summary_detail.get('value', '')
        elif 'summary' in entry:
            full_text = entry.get('summary', '')
        elif 'description' in entry:
            full_text = entry.get('description', '')
        else:
            full_text = ""

        if full_text:
            return BeautifulSoup(full_text, "html.parser").get_text(separator="\n", strip=True)

    return "المحتوى الكامل متاح عبر الرابط المرفق للمقال."

# ==================== معالجة الفيديوهات ====================
YTDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
}

def download_video_stream(url: str, out_folder: str) -> str:
    out_tmpl = os.path.join(out_folder, "%(id)s.%(ext)s")
    opts = dict(YTDL_BASE_OPTS)
    opts.update({
        "outtmpl": out_tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
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

# ==================== لوحات المفاتيح والأزرار ====================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة موقع", callback_data="add_site"), InlineKeyboardButton("🗑️ حذف موقع", callback_data="del_site")],
        [InlineKeyboardButton("📋 عرض المواقع", callback_data="list_sites"), InlineKeyboardButton("⚙️ الإعدادات المتقدمة", callback_data="settings")],
        [InlineKeyboardButton("🔄 فحص فوري الآن", callback_data="force_check")]
    ])

def get_settings_keyboard():
    s = CONFIG
    engine_label = "جوجل 🚀" if s.get("trans_engine") == "google" else "مايميموري 🧠"
    target_code = s.get("target_lang", "ar")
    lang_label = SUPPORTED_LANGS.get(target_code, "العربية 🇸🇦")
    v_labels = {"file": "إرسال كفيديو مجزأ", "link": "إرسال كرابط"}
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 لغة الترجمة: {lang_label}", callback_data="toggle_target_lang")],
        [InlineKeyboardButton(f"⚙️ محرك الترجمة: {engine_label}", callback_data="toggle_engine")],
        [InlineKeyboardButton(f"الفيديوهات: {v_labels.get(s.get('video_mode'), 'ملف')}", callback_data="toggle_video")],
        [InlineKeyboardButton(f"⏱️ مدة الفحص: كل {s.get('check_interval_minutes', 10)} دقيقة", callback_data="change_interval")],
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")]
    ])

# ==================== إدارة المحادثات والأوامر ====================
async def update_active_chat(chat):
    if chat and chat.id:
        if CONFIG.get("active_chat_id") != chat.id:
            CONFIG["active_chat_id"] = chat.id
            await save_config_async()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_active_chat(update.effective_chat)
    name = update.effective_user.first_name or "يا بطل"
    await update.message.reply_text(
        f"مرحباً بك يا {name} في بوت المتابعة والترجمة التلقائية!\n\n"
        f"✅ تم تعيين هذه المحادثة كوجهة أساسية.\n"
        f"لإضافة موقع أرسل: `/add رابط_الموقع` أو اختر من اللوحة أدناه:",
        reply_markup=get_main_keyboard()
    )

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_active_chat(update.effective_chat)
    raw_text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+)', raw_text)
    if not match:
        await update.message.reply_text("يرجى كتابة الرابط بجوار الأمر، مثال:\n/add https://example.com/feed")
        return
    await process_and_add_url(match.group(1), update)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    await update_active_chat(query.message.chat)

    if data == "main_menu":
        await safe_edit_text(query, "لوحة التحكم الرئيسية:", get_main_keyboard())

    elif data == "list_sites":
        sites = CONFIG.get("sites", [])
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="main_menu")]])
        if not sites:
            await safe_edit_text(query, "لا توجد أي مواقع مضافة حالياً في القائمة.", back_kb)
            return

        text_lines = ["📋 قائمة المواقع المتابعة حالياً:\n"]
        for idx, s in enumerate(sites, 1):
            text_lines.append(f"{idx}. {s.get('name')}\nالرابط: {s.get('url')}\n")
        full_text = "\n".join(text_lines)

        if len(full_text) > 3800:
            full_text = full_text[:3800] + "\n...(تم اختصار باقي القائمة)"

        await safe_edit_text(query, full_text, back_kb)

    elif data == "add_site":
        context.user_data["waiting_for_url"] = True
        await query.message.reply_text("أرسل الآن رابط الموقع أو الخلاصة:")

    elif data == "del_site":
        sites = CONFIG.get("sites", [])
        if not sites:
            await safe_edit_text(query, "لا توجد مواقع لحذفها.", get_main_keyboard())
            return
        buttons = []
        for s in sites:
            buttons.append([InlineKeyboardButton(f"❌ {s.get('name')[:28]}", callback_data=f"del_{s.get('id')}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
        await safe_edit_text(query, "اختر الموقع الذي تريد حذفه:", InlineKeyboardMarkup(buttons))

    elif data.startswith("del_"):
        site_id = data[4:]
        CONFIG["sites"] = [s for s in CONFIG.get("sites", []) if str(s.get("id")) != site_id]
        await save_config_async()
        await safe_edit_text(query, "✅ تم حذف الموقع بنجاح!", get_main_keyboard())

    elif data == "settings":
        await safe_edit_text(query, "⚙️ إعدادات البوت والترجمة:", get_settings_keyboard())

    elif data == "toggle_engine":
        cur = CONFIG.get("trans_engine", "google")
        CONFIG["trans_engine"] = "mymemory" if cur == "google" else "google"
        await save_config_async()
        await safe_edit_text(query, "⚙️ إعدادات البوت والترجمة:", get_settings_keyboard())

    elif data == "toggle_target_lang":
        langs = list(SUPPORTED_LANGS.keys())
        cur = CONFIG.get("target_lang", "ar")
        idx = langs.index(cur) if cur in langs else 0
        CONFIG["target_lang"] = langs[(idx + 1) % len(langs)]
        await save_config_async()
        await safe_edit_text(query, "⚙️ إعدادات البوت والترجمة:", get_settings_keyboard())

    elif data == "toggle_video":
        cur = CONFIG.get("video_mode", "file")
        CONFIG["video_mode"] = "link" if cur == "file" else "file"
        await save_config_async()
        await safe_edit_text(query, "⚙️ إعدادات البوت والترجمة:", get_settings_keyboard())

    elif data == "change_interval":
        intervals = [5, 10, 15, 30, 60]
        cur = CONFIG.get("check_interval_minutes", 10)
        idx = intervals.index(cur) if cur in intervals else 0
        CONFIG["check_interval_minutes"] = intervals[(idx + 1) % len(intervals)]
        await save_config_async()
        await safe_edit_text(query, "⚙️ إعدادات البوت والترجمة:", get_settings_keyboard())

    elif data == "force_check":
        status_msg = await query.message.reply_text("🔄 جاري الفحص الفوري الآن...")
        new_found = await process_feeds_cycle(context.application, target_chat_override=query.message.chat_id)
        if new_found > 0:
            await status_msg.edit_text(f"✅ اكتمل الفحص وتم إرسال {new_found} مقال/منشور جديد.")
        else:
            await status_msg.edit_text("✅ تم الفحص، لا توجد أي مقالات جديدة.")

async def process_and_add_url(raw_url: str, update: Update):
    clean_url = raw_url.strip()
    status_msg = await update.message.reply_text("🔍 جاري فحص الرابط وجلب أحدث مقال...")

    feed, final_url = await asyncio.to_thread(parse_feed_robust, clean_url)

    if not feed or (not feed.entries and len(feed.feed) == 0):
        await status_msg.edit_text("❌ تعذر قراءة الرابط كخلاصة RSS صالحة. تأكد من صحة الرابط.")
        return

    site_name = feed.feed.get("title") or "مصدر تقني"
    last_id = get_entry_id(feed.entries[0]) if feed.entries else ""

    new_entry = {
        "id": int(time.time()),
        "name": site_name,
        "url": final_url,
        "last_id": last_id
    }
    CONFIG.setdefault("sites", []).append(new_entry)
    await save_config_async()

    await status_msg.edit_text(
        f"✅ تمت إضافة الموقع وحفظ آخر مقال كمرجع بنجاح!\nالاسم: {site_name}\nالرابط: {final_url}",
        reply_markup=get_main_keyboard()
    )

async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    await update_active_chat(update.effective_chat)

    match = re.search(r'(https?://[^\s]+)', text)
    if match:
        if context.user_data.get("waiting_for_url") or text.startswith("/"):
            context.user_data["waiting_for_url"] = False
            await process_and_add_url(match.group(1), update)
    elif context.user_data.get("waiting_for_url"):
        context.user_data["waiting_for_url"] = False
        cleaned = text.lstrip("/").strip()
        if not cleaned.startswith("http"):
            cleaned = "https://" + cleaned
        await process_and_add_url(cleaned, update)

# ==================== دورة فحص المواقع والترجمة الذكية ====================
async def process_feeds_cycle(app, target_chat_override=None) -> int:
    sites = CONFIG.get("sites", [])
    if not sites:
        return 0

    target_chat = target_chat_override or CONFIG.get("active_chat_id")
    if not target_chat:
        return 0

    engine = CONFIG.get("trans_engine", "google")
    target_lang = CONFIG.get("target_lang", "ar")
    video_mode = CONFIG.get("video_mode", "file")
    total_new_items = 0
    config_modified = False

    for site in sites:
        try:
            feed, _ = await asyncio.to_thread(parse_feed_robust, site["url"])
            if not feed or not feed.entries:
                continue

            latest_entry = feed.entries[0]
            latest_id = get_entry_id(latest_entry)

            # إذا كان أول فحص: نحفظ آخر مقال كمرجع فقط ولا نرسله
            if not site.get("last_id"):
                site["last_id"] = latest_id
                config_modified = True
                continue

            # في الفحوصات التالية: جلب المقالات الأحدث من المحفوظة
            new_items = []
            for entry in feed.entries:
                e_id = get_entry_id(entry)
                if e_id == site.get("last_id"):
                    break
                new_items.append(entry)

            if new_items:
                total_new_items += len(new_items)
                # الإرسال بترتيب النشر (من الأقدم إلى الأحدث)
                for entry in reversed(new_items):
                    raw_title = entry.get("title", "بدون عنوان").strip()
                    link = entry.get("link", "").strip()
                    pub_date = parse_date(entry)

                    # استخراج المقال كاملاً بنفس طريقة الأداة
                    full_text = await asyncio.to_thread(extract_full_article, link, entry)

                    # فحص ما إذا كان المنشور باللغة الإنجليزية للترجمة
                    need_trans = (target_lang != 'none') and is_english_content(raw_title, full_text)

                    if need_trans:
                        # ترجمة العنوان لوحده
                        final_title, title_ok = await asyncio.to_thread(
                            translate_text_pipeline, raw_title, target_lang, engine
                        )
                        # ترجمة نص المقال لوحده
                        final_content, body_ok = await asyncio.to_thread(
                            translate_text_pipeline, full_text, target_lang, engine
                        )
                        is_translated = (title_ok or body_ok)
                    else:
                        final_title = raw_title
                        final_content = full_text
                        is_translated = False

                    trans_tag = " (مترجم)" if is_translated else ""

                    # تنسيق المقال كما في الأداة تماماً
                    # العنوان ثم نص المقال كاملاً ثم الوقت والتاريخ ثم الرابط
                    header_part = f"العنوان{trans_tag}: {final_title}\n\nنص المقال كاملاً كما كُتب في الموقع{trans_tag}:\n"
                    footer_part = f"\n\nالوقت والتاريخ: {pub_date}\nرابط صفحة الويب: {link}"

                    # ضمان عدم تجاوز الحد الأقصى لحجم رسالة تيليجرام (4096 حرف)
                    available_content_len = 4000 - len(header_part) - len(footer_part)
                    if len(final_content) > available_content_len:
                        final_content = final_content[:max(100, available_content_len)] + "\n...(تم اختصار باقي المقال لطوله)"

                    msg_body = f"{header_part}{final_content}{footer_part}"

                    # معالجة الفيديوهات إن وجدت
                    is_video_post = ("youtube.com" in link or "youtu.be" in link)
                    download_done = False

                    if is_video_post and video_mode == "file":
                        temp_dir = tempfile.mkdtemp()
                        try:
                            downloaded_file = await asyncio.to_thread(download_video_stream, link, temp_dir)
                            if downloaded_file and os.path.exists(downloaded_file):
                                parts = await asyncio.to_thread(split_video_ffmpeg, downloaded_file)
                                total_parts = len(parts)

                                for p_idx, part_path in enumerate(parts, 1):
                                    caption = f"🎬 {final_title[:60]}"
                                    if total_parts > 1:
                                        caption += f" (الجزء {p_idx} من {total_parts})"
                                    with open(part_path, "rb") as vf:
                                        await app.bot.send_video(
                                            chat_id=target_chat,
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
                        try:
                            await app.bot.send_message(chat_id=target_chat, text=msg_body, disable_web_page_preview=False)
                        except Exception as me:
                            logger.error(f"فشل إرسال الرسالة: {me}")

                    await asyncio.sleep(0.5)

                site["last_id"] = latest_id
                config_modified = True

            await asyncio.sleep(0.2)

        except Exception as se:
            logger.error(f"خطأ فحص موقع {site.get('name')}: {se}")

    if config_modified:
        await save_config_async()

    return total_new_items

# ==================== الفحص الدوري ====================
async def background_loop(app):
    await asyncio.sleep(5)
    while True:
        try:
            if time.time() - START_TIME >= MAX_SERVER_LIFETIME:
                logger.info("⏳ انتهت فترة الـ 5 ساعات، جاري الإغلاق النظيف لإفساح المجال للجدول التالي...")
                os._exit(0)

            interval_seconds = max(60, CONFIG.get("check_interval_minutes", 10) * 60)
            last_check = CONFIG.get("last_check_timestamp", 0)
            now = time.time()
            elapsed = now - last_check

            if elapsed >= interval_seconds:
                CONFIG["last_check_timestamp"] = now
                await save_config_async()
                await process_feeds_cycle(app)

            await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"خطأ في حلقة الفحص: {e}")
            await asyncio.sleep(10)

async def on_post_init(app):
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم بدء الجدولة الدقيقة والترجمة بنجاح.")

# ==================== التشغيل ====================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(8)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_message))

    print("🚀 تم تشغيل البوت بنجاح مع محركات الترجمة والتنسيق المطور.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
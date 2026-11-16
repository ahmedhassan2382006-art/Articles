import os
import re
import sys
import json
import base64
import time
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple, List

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest, RetryAfter, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ==================== إعداد السجلات ====================
logging.basicConfig(
    format="%(asctime)s - [BOT2] - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot2")

# ==================== إعدادات GitHub وقراءة التوكنات ====================
SECRET_RAW = (
    os.environ.get("BOT_SECRET")
    or os.environ.get("3bot")
    or os.environ.get("BOT_TOKEN")
    or ""
).strip()

GITHUB_TOKEN = None
for sep in ["\\", "/", "\n", "|"]:
    if sep in SECRET_RAW:
        parts = SECRET_RAW.split(sep, 1)
        GITHUB_TOKEN = parts[1].strip()
        break

if not GITHUB_TOKEN:
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip() or None

REPO_OWNER = "ahmedhassan2382006-art"
REPO_NAME = "Articles"
TOKEN_FILE_PATH = "t.txt"
STATE_FILE_PATH = "s.json"
LOCAL_STATE_PATH = "s.json"

GITHUB_TOKEN_FILE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{TOKEN_FILE_PATH}"
GITHUB_STATE_FILE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{STATE_FILE_PATH}"

START_TIME = time.time()
# 4 ساعات و53 دقيقة: يغلق قبل وصول موعد تشغيل البوت القديم عند الساعة 5:00
MAX_SERVER_LIFETIME = (5 * 3600) - 420
GITHUB_SYNC_INTERVAL = 9 * 60  # حفظ محتوى الملف المحلي على GitHub كل 9 دقائق

STATE_LOCK = asyncio.Lock()
GITHUB_STATE_SHA: Optional[str] = None
STATE_DIRTY = False
LAST_GITHUB_SYNC_TS = 0.0

SEEN_MEDIA_GROUPS: Dict[str, float] = {}


def _github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Vote-Tracker-Bot2",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def load_bot2_token() -> Optional[str]:
    """جلب توكن البوت الثاني من ملف t.txt على GitHub أو محلياً"""
    if GITHUB_TOKEN:
        try:
            res = requests.get(GITHUB_TOKEN_FILE_URL, headers=_github_headers(), timeout=12)
            if res.status_code == 200:
                raw_b64 = res.json().get("content", "")
                content = base64.b64decode(raw_b64).decode("utf-8-sig")
                for line in content.splitlines():
                    tok = line.strip()
                    if tok and ":" in tok and not tok.startswith("#"):
                        return tok
            else:
                logger.warning(f"⚠️ تعذر قراءة t.txt من GitHub (كود: {res.status_code})")
        except Exception as e:
            logger.error(f"خطأ أثناء جلب t.txt من GitHub: {e}")

    if os.path.exists(TOKEN_FILE_PATH):
        try:
            with open(TOKEN_FILE_PATH, "r", encoding="utf-8-sig") as f:
                for line in f:
                    tok = line.strip()
                    if tok and ":" in tok and not tok.startswith("#"):
                        return tok
        except Exception as e:
            logger.error(f"خطأ قراءة {TOKEN_FILE_PATH}: {e}")

    return None


BOT2_TOKEN = load_bot2_token()
if not BOT2_TOKEN:
    logger.error("❌ لم يتم العثور على توكن البوت الثاني في t.txt!")
    sys.exit(1)


# ==================== إدارة الملف المحلي s.json والمزامنة كل 9 دقائق ====================
def _default_state() -> Dict[str, Any]:
    return {
        "owner_id": None,
        "is_running": False,
        "default_channel_id": None,
        "default_group_id": None,
        "known_groups": {},
        "channels": {},
        "group_post_map": {},
    }


def _normalize_state(cfg: Any) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("owner_id", None)
    cfg.setdefault("is_running", False)
    cfg.setdefault("default_channel_id", None)
    cfg.setdefault("default_group_id", None)
    cfg.setdefault("known_groups", {})
    cfg.setdefault("channels", {})
    cfg.setdefault("group_post_map", {})
    return cfg


def write_local_state_sync(state_obj: Dict[str, Any]) -> bool:
    """كتابة آمنة فورية إلى ملف s.json على الجهاز"""
    try:
        tmp_path = f"{LOCAL_STATE_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state_obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LOCAL_STATE_PATH)
        return True
    except Exception as e:
        logger.error(f"خطأ أثناء حفظ {LOCAL_STATE_PATH} محلياً: {e}")
        return False


def _sync_push_local_file_to_github() -> bool:
    """قراءة محتوى s.json من الجهاز ورفعه إلى GitHub مع [skip ci] لمنع إعادة تشغيل أي بوت"""
    global GITHUB_STATE_SHA
    if not GITHUB_TOKEN:
        return False
    if not os.path.exists(LOCAL_STATE_PATH):
        return False

    try:
        with open(LOCAL_STATE_PATH, "rb") as f:
            content_bytes = f.read()
    except Exception as e:
        logger.error(f"تعذر قراءة الملف المحلي {LOCAL_STATE_PATH}: {e}")
        return False

    for _ in range(3):
        try:
            if not GITHUB_STATE_SHA:
                res_check = requests.get(GITHUB_STATE_FILE_URL, headers=_github_headers(), timeout=10)
                if res_check.status_code == 200:
                    GITHUB_STATE_SHA = res_check.json().get("sha")

            b64_content = base64.b64encode(content_bytes).decode("utf-8")
            payload: Dict[str, Any] = {
                "message": "Update s.json [skip ci]",
                "content": b64_content,
            }
            if GITHUB_STATE_SHA:
                payload["sha"] = GITHUB_STATE_SHA

            res = requests.put(GITHUB_STATE_FILE_URL, headers=_github_headers(), json=payload, timeout=15)
            if res.status_code in (200, 201):
                GITHUB_STATE_SHA = res.json().get("content", {}).get("sha", GITHUB_STATE_SHA)
                logger.info("☁️ تم حفظ محتوى s.json المحلي إلى GitHub بنجاح.")
                return True
            elif res.status_code == 409:
                GITHUB_STATE_SHA = None
                time.sleep(0.4)
            else:
                logger.warning(f"⚠️ رد غير متوقع من GitHub عند حفظ s.json: {res.status_code} - {res.text[:160]}")
                return False
        except Exception as e:
            logger.error(f"خطأ رفع s.json إلى GitHub: {e}")
            GITHUB_STATE_SHA = None
            time.sleep(0.5)
    return False


def _init_load_state() -> Dict[str, Any]:
    """جلب s.json من GitHub عند التشغيل وحفظه محلياً على الجهاز"""
    global GITHUB_STATE_SHA
    default_cfg = _default_state()

    local_cfg = None
    if os.path.exists(LOCAL_STATE_PATH):
        try:
            with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
                local_cfg = _normalize_state(json.load(f))
        except Exception:
            local_cfg = None

    if GITHUB_TOKEN:
        try:
            res = requests.get(GITHUB_STATE_FILE_URL, headers=_github_headers(), timeout=12)
            if res.status_code == 200:
                data = res.json()
                GITHUB_STATE_SHA = data.get("sha")
                raw_content = base64.b64decode(data.get("content", "")).decode("utf-8")
                parsed = _normalize_state(json.loads(raw_content) if raw_content.strip() else default_cfg)
                write_local_state_sync(parsed)
                return parsed
            elif res.status_code == 404:
                cfg_to_save = local_cfg or default_cfg
                write_local_state_sync(cfg_to_save)
                _sync_push_local_file_to_github()
                return cfg_to_save
        except Exception as e:
            logger.error(f"خطأ جلب s.json من GitHub: {e}")

    cfg = local_cfg or default_cfg
    write_local_state_sync(cfg)
    return cfg


STATE: Dict[str, Any] = _init_load_state()


async def save_local_now():
    """حفظ التعديلات في ملف s.json المحلي فوراً"""
    global STATE_DIRTY
    async with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        ok = await asyncio.to_thread(write_local_state_sync, snapshot)
        if ok:
            STATE_DIRTY = True


async def sync_local_to_github_now() -> bool:
    """مزامنة ملف s.json المحلي إلى GitHub"""
    global STATE_DIRTY, LAST_GITHUB_SYNC_TS
    async with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        await asyncio.to_thread(write_local_state_sync, snapshot)
        ok = await asyncio.to_thread(_sync_push_local_file_to_github)
        if ok:
            STATE_DIRTY = False
            LAST_GITHUB_SYNC_TS = time.time()
        return ok


# ==================== تصميم أزرار التصويت (نمط الاستفتاء) ====================
def make_styled_button(text: str, callback_data: str, style: str, use_style: bool = True) -> InlineKeyboardButton:
    if not use_style:
        return InlineKeyboardButton(text=text, callback_data=callback_data)
    try:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)
    except TypeError:
        return InlineKeyboardButton(
            text=text,
            callback_data=callback_data,
            api_kwargs={"style": style},
        )


def build_vote_markup(
    channel_id: int | str,
    msg_id: int | str,
    likes: int = 0,
    dislikes: int = 0,
    use_style: bool = True,
) -> InlineKeyboardMarkup:
    total = likes + dislikes
    if total > 0:
        like_pct = round((likes / total) * 100)
        dislike_pct = 100 - like_pct
        like_text = f"👍 إعجاب   •   {likes}  ({like_pct}%)"
        dislike_text = f"👎 عدم إعجاب   •   {dislikes}  ({dislike_pct}%)"
        info_text = f"👥 المصوتون: {total}  •  تحقق من تصويتك"
    else:
        like_text = "☐  👍 إعجاب   •   0"
        dislike_text = "☐  👎 عدم إعجاب   •   0"
        info_text = "🗳️ اضغط للتصويت (صوت واحد لكل مستخدم)"

    keyboard = [
        [make_styled_button(like_text, f"v:l:{channel_id}:{msg_id}", style="success", use_style=use_style)],
        [make_styled_button(dislike_text, f"v:d:{channel_id}:{msg_id}", style="danger", use_style=use_style)],
        [make_styled_button(info_text, f"v:i:{channel_id}:{msg_id}", style="primary", use_style=use_style)],
    ]
    return InlineKeyboardMarkup(keyboard)


def has_vote_markup(reply_markup: Optional[InlineKeyboardMarkup]) -> bool:
    if not reply_markup or not getattr(reply_markup, "inline_keyboard", None):
        return False
    for row in reply_markup.inline_keyboard:
        for btn in row:
            cb = getattr(btn, "callback_data", "") or ""
            if cb.startswith("v:l:") or cb.startswith("v:d:"):
                return True
    return False


async def safe_edit_reply_markup(
    bot,
    chat_id: int,
    message_id: int,
    channel_id: int | str,
    channel_msg_id: int | str,
    likes: int,
    dislikes: int,
    silent_not_editable: bool = False,
) -> str:
    """
    يرجع الحالة:
    'ok' | 'not_modified' | 'not_found' | 'not_editable' | 'forbidden' | 'error'
    """
    for use_style in (True, False):
        markup = build_vote_markup(channel_id, channel_msg_id, likes, dislikes, use_style=use_style)
        for _ in range(2):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                )
                return "ok"
            except RetryAfter as ra:
                await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            except BadRequest as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return "not_modified"
                if use_style:
                    # التراجع التلقائي لأزرار قياسية إذا لم يدعم العميل/التطبيق style
                    break
                if "message to edit not found" in err or "message_id_invalid" in err:
                    return "not_found"
                if "message can't be edited" in err or "message cannot be edited" in err:
                    return "not_editable"
                if not silent_not_editable:
                    logger.warning(f"⚠️ تعذر تعديل أزرار ({chat_id}, {message_id}): {e}")
                return "error"
            except Forbidden as e:
                if not silent_not_editable:
                    logger.warning(f"⛔ لا توجد صلاحية تعديل في ({chat_id}): {e}")
                return "forbidden"
            except TelegramError as e:
                if not silent_not_editable:
                    logger.warning(f"⚠️ خطأ تيليجرام ({chat_id}, {message_id}): {e}")
                return "error"
    return "error"


# ==================== إدارة القنوات والمجموعات ====================
def get_or_init_channel(channel_id: int, title: str = "") -> Dict[str, Any]:
    ch_key = str(channel_id)
    channels = STATE.setdefault("channels", {})
    if ch_key not in channels:
        channels[ch_key] = {
            "title": title or f"قناة ({channel_id})",
            "linked_group_id": None,
            "linked_group_title": None,
            "can_edit_messages": True,
            "last_known_msg_id": 0,
            "posts": {},
        }
    else:
        if title:
            channels[ch_key]["title"] = title
        channels[ch_key].setdefault("can_edit_messages", True)
        channels[ch_key].setdefault("last_known_msg_id", 0)
        channels[ch_key].setdefault("posts", {})
    return channels[ch_key]


def get_or_init_group(group_id: int, title: str = "", linked_channel_id: Optional[int] = None) -> Dict[str, Any]:
    grp_key = str(group_id)
    groups = STATE.setdefault("known_groups", {})
    if grp_key not in groups:
        groups[grp_key] = {
            "title": title or f"مجموعة ({group_id})",
            "linked_channel_id": linked_channel_id,
        }
    else:
        if title:
            groups[grp_key]["title"] = title
        if linked_channel_id is not None:
            groups[grp_key]["linked_channel_id"] = linked_channel_id
    return groups[grp_key]


def remove_channel_data(channel_id: int | str):
    ch_key = str(channel_id)
    STATE.get("channels", {}).pop(ch_key, None)

    if STATE.get("default_channel_id") == int(channel_id):
        STATE["default_channel_id"] = None
        STATE["is_running"] = False

    gmap = STATE.get("group_post_map", {})
    for k in [k for k, v in gmap.items() if str(v).startswith(f"{ch_key}:")]:
        gmap.pop(k, None)


def remove_group_data(group_id: int | str):
    grp_int = int(group_id)
    STATE.get("known_groups", {}).pop(str(grp_int), None)

    if STATE.get("default_group_id") == grp_int:
        STATE["default_group_id"] = None

    for ch_data in STATE.get("channels", {}).values():
        if ch_data.get("linked_group_id") == grp_int:
            ch_data["linked_group_id"] = None
            ch_data["linked_group_title"] = None
        for post_data in ch_data.get("posts", {}).values():
            if post_data.get("group_id") == grp_int:
                post_data["group_id"] = None
                post_data["group_msg_id"] = None

    gmap = STATE.get("group_post_map", {})
    prefix = f"{grp_int}:"
    for k in [k for k in gmap.keys() if k.startswith(prefix)]:
        gmap.pop(k, None)


async def inspect_chat_membership(bot, chat_id: int) -> Tuple[bool, bool, bool, str, str, Optional[int], Optional[int]]:
    """
    يرجع: (is_active, is_admin, can_edit_messages, chat_type, title, linked_chat_id, pinned_msg_id)
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        status = getattr(member, "status", "")
        is_admin = status in ("administrator", "creator")
        is_active = status in ("administrator", "creator", "member", "restricted")
        if not is_active:
            return False, False, False, "", "", None, None

        if status == "creator":
            can_edit = True
        elif status == "administrator":
            can_edit = bool(getattr(member, "can_edit_messages", False))
        else:
            can_edit = False

        chat = await bot.get_chat(chat_id)
        chat_type = getattr(chat, "type", "")
        title = getattr(chat, "title", "") or str(chat_id)
        linked_id = getattr(chat, "linked_chat_id", None)
        pinned = getattr(chat, "pinned_message", None)
        pinned_id = getattr(pinned, "message_id", None) if pinned else None
        return True, is_admin, can_edit, chat_type, title, linked_id, pinned_id
    except (BadRequest, Forbidden, TelegramError):
        return False, False, False, "", "", None, None


async def sync_single_chat(bot, chat_id: int) -> Tuple[bool, str]:
    """فحص وتحديث قناة أو مجموعة وإضافتها للذاكرة المحلية"""
    active, is_admin, can_edit, ctype, title, linked_id, pinned_id = await inspect_chat_membership(bot, chat_id)
    if not active:
        remove_channel_data(chat_id)
        remove_group_data(chat_id)
        return False, "البوت غير موجود في هذه الدردشة."

    if ctype == "channel":
        if not is_admin:
            remove_channel_data(chat_id)
            return False, f"البوت في القناة '{title}' ليس مشرفاً!"
        ch_data = get_or_init_channel(chat_id, title)
        ch_data["can_edit_messages"] = can_edit
        if pinned_id and pinned_id > int(ch_data.get("last_known_msg_id", 0) or 0):
            ch_data["last_known_msg_id"] = pinned_id

        if linked_id:
            g_active, _, _, gtype, gtitle, _, _ = await inspect_chat_membership(bot, linked_id)
            if g_active and gtype in ("group", "supergroup"):
                ch_data["linked_group_id"] = linked_id
                ch_data["linked_group_title"] = gtitle
                get_or_init_group(linked_id, gtitle, linked_channel_id=chat_id)
            else:
                ch_data["linked_group_id"] = None
                ch_data["linked_group_title"] = None
        else:
            ch_data["linked_group_id"] = None
            ch_data["linked_group_title"] = None

        return True, f"تم تحديث القناة: {title}"

    if ctype in ("group", "supergroup"):
        get_or_init_group(chat_id, title, linked_channel_id=linked_id)
        if linked_id:
            c_active, c_admin, c_edit, _, ctitle, _, cpinned = await inspect_chat_membership(bot, linked_id)
            if c_active and c_admin:
                ch_data = get_or_init_channel(linked_id, ctitle)
                ch_data["can_edit_messages"] = c_edit
                ch_data["linked_group_id"] = chat_id
                ch_data["linked_group_title"] = title
                if cpinned and cpinned > int(ch_data.get("last_known_msg_id", 0) or 0):
                    ch_data["last_known_msg_id"] = cpinned
        return True, f"تم تحديث المجموعة: {title}"

    return False, "نوع الدردشة غير مدعوم."


async def verify_all_registered_chats(bot):
    """التحقق من كافة القنوات والمجموعات المسجلة وتنظيف ما خرج منه البوت"""
    ch_ids = [int(k) for k in list(STATE.get("channels", {}).keys())]
    grp_ids = [int(k) for k in list(STATE.get("known_groups", {}).keys())]

    for cid in ch_ids:
        await sync_single_chat(bot, cid)
    for gid in grp_ids:
        await sync_single_chat(bot, gid)

    await save_local_now()


# ==================== تتبع آخر 10 منشورات وإضافة الأزرار ====================
def is_service_message(msg) -> bool:
    service_attrs = (
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "pinned_message",
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
    )
    return any(getattr(msg, attr, None) is not None for attr in service_attrs)


def get_or_init_post(ch_data: Dict[str, Any], msg_id: int) -> Dict[str, Any]:
    posts = ch_data.setdefault("posts", {})
    m_key = str(msg_id)
    if m_key not in posts:
        posts[m_key] = {
            "likes": 0,
            "dislikes": 0,
            "voters": {},
            "group_id": None,
            "group_msg_id": None,
        }
    return posts[m_key]


async def ensure_post_has_buttons(
    bot,
    channel_id: int,
    channel_msg_id: int,
    force_edit: bool = False,
) -> str:
    """إضافة أو تحديث أزرار التصويت على منشور في القناة"""
    ch_data = get_or_init_channel(channel_id)
    posts = ch_data.setdefault("posts", {})
    m_key = str(channel_msg_id)
    already_tracked = m_key in posts

    if already_tracked and not force_edit:
        return "already"

    post_data = get_or_init_post(ch_data, channel_msg_id)
    status = await safe_edit_reply_markup(
        bot=bot,
        chat_id=channel_id,
        message_id=channel_msg_id,
        channel_id=channel_id,
        channel_msg_id=channel_msg_id,
        likes=post_data.get("likes", 0),
        dislikes=post_data.get("dislikes", 0),
    )

    if status in ("ok", "not_modified"):
        if int(ch_data.get("last_known_msg_id", 0) or 0) < channel_msg_id:
            ch_data["last_known_msg_id"] = channel_msg_id

        if post_data.get("group_id") and post_data.get("group_msg_id"):
            await safe_edit_reply_markup(
                bot=bot,
                chat_id=int(post_data["group_id"]),
                message_id=int(post_data["group_msg_id"]),
                channel_id=channel_id,
                channel_msg_id=channel_msg_id,
                likes=post_data.get("likes", 0),
                dislikes=post_data.get("dislikes", 0),
                silent_not_editable=True,
            )
        return status

    if not already_tracked:
        posts.pop(m_key, None)
    return status


async def scan_last_10_posts(bot, channel_id: int, force_recheck_tracked: bool = False) -> Tuple[int, int]:
    """
    1) يكتشف للأمام أي منشورات وصلت أثناء تبديل جلسة الـ 5 ساعات.
    2) يفحص آخر 10 منشورات فقط ويضيف الأزرار لمن ينقصه.
    يرجع: (عدد ما تم إضافته، آخر رقم منشور معروف)
    """
    ch_data = get_or_init_channel(channel_id)
    if not ch_data.get("can_edit_messages", True):
        logger.warning(f"⛔ القناة {channel_id} لا تمنح البوت صلاحية تعديل رسائل الآخرين.")
        return 0, int(ch_data.get("last_known_msg_id", 0) or 0)

    last_id = int(ch_data.get("last_known_msg_id", 0) or 0)
    added_count = 0

    if last_id <= 0:
        return 0, 0

    # 1) فحص للأمام لالتقاط المنشورات المرسلة أثناء توقف/تبديل الجلسة
    consecutive_not_found = 0
    for probe_id in range(last_id + 1, last_id + 26):
        res = await ensure_post_has_buttons(bot, channel_id, probe_id, force_edit=True)
        if res == "ok":
            added_count += 1
            last_id = probe_id
            consecutive_not_found = 0
        elif res in ("already", "not_modified", "not_editable"):
            last_id = probe_id
            consecutive_not_found = 0
        elif res == "forbidden":
            break
        elif res == "not_found":
            consecutive_not_found += 1
            if consecutive_not_found >= 5:
                break
        await asyncio.sleep(0.2)

    ch_data["last_known_msg_id"] = last_id

    # 2) فحص آخر 10 منشورات فقط
    start_id = max(1, last_id - 9)
    posts = ch_data.get("posts", {})
    for mid in range(start_id, last_id + 1):
        if str(mid) in posts and not force_recheck_tracked:
            continue
        res = await ensure_post_has_buttons(
            bot,
            channel_id,
            mid,
            force_edit=(force_recheck_tracked or str(mid) not in posts),
        )
        if res == "ok":
            added_count += 1
        elif res == "forbidden":
            break
        await asyncio.sleep(0.2)

    await save_local_now()
    return added_count, last_id


# ==================== استخراج بيانات المنشور المحوّل / الروابط ====================
def extract_origin_from_forwarded_post(msg) -> Tuple[Optional[int], Optional[int], str]:
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None)
        origin_msg_id = getattr(origin, "message_id", None)
        if origin_chat and getattr(origin_chat, "type", "") == "channel":
            return origin_chat.id, origin_msg_id, getattr(origin_chat, "title", "") or ""
        sender_chat = getattr(origin, "sender_chat", None)
        if sender_chat:
            return sender_chat.id, origin_msg_id, getattr(sender_chat, "title", "") or ""

    fwd_chat = getattr(msg, "forward_from_chat", None)
    fwd_msg_id = getattr(msg, "forward_from_message_id", None)
    if fwd_chat:
        return fwd_chat.id, fwd_msg_id, getattr(fwd_chat, "title", "") or ""

    return None, None, ""


def parse_chat_or_link(text: str) -> Tuple[Optional[int | str], Optional[int]]:
    text = text.strip()
    # رابط خاص: https://t.me/c/123456789/45
    m_priv = re.search(r"t\.me/c/(\d+)/(\d+)", text)
    if m_priv:
        return int(f"-100{m_priv.group(1)}"), int(m_priv.group(2))

    # رابط عام: https://t.me/channel_name/45
    m_pub_post = re.search(r"t\.me/([A-Za-z0-9_]{4,})/(\d+)", text)
    if m_pub_post:
        return f"@{m_pub_post.group(1)}", int(m_pub_post.group(2))

    # رابط قناة/مجموعة: https://t.me/channel_name
    m_pub = re.search(r"t\.me/([A-Za-z0-9_]{4,})$", text)
    if m_pub:
        return f"@{m_pub.group(1)}", None

    if text.startswith("@") and len(text) >= 5:
        return text.split()[0], None

    if re.fullmatch(r"-100\d{5,}", text) or re.fullmatch(r"-\d{5,}", text):
        return int(text), None

    return None, None


# ==================== لوحة التحكم الخاصة بالمالك ====================
def is_owner(user_id: int) -> bool:
    owner = STATE.get("owner_id")
    return owner is not None and int(owner) == int(user_id)


def build_control_panel_text() -> str:
    is_running = bool(STATE.get("is_running", False))
    def_ch_id = STATE.get("default_channel_id")
    def_grp_id = STATE.get("default_group_id")

    channels = STATE.get("channels", {})
    groups = STATE.get("known_groups", {})

    status_line = "🟢 يعمل الآن (مفعّل)" if is_running else "⏸ متوقف (بانتظار أمر البدء)"

    if def_ch_id and str(def_ch_id) in channels:
        ch_info = channels[str(def_ch_id)]
        ch_line = f"{ch_info.get('title', def_ch_id)} (<code>{def_ch_id}</code>)"
        if not ch_info.get("can_edit_messages", True):
            ch_line += "\n⚠️ <b>تحذير:</b> فعّل صلاحية <i>تعديل رسائل الآخرين</i> للبوت في هذه القناة!"
        last_mid = int(ch_info.get("last_known_msg_id", 0) or 0)
        last_mid_line = str(last_mid) if last_mid > 0 else "غير معروف بعد (أعد توجيه آخر منشور للبوت)"
    else:
        ch_line = "❌ لم تُحدد بعد"
        last_mid_line = "—"

    if def_grp_id and str(def_grp_id) in groups:
        grp_info = groups[str(def_grp_id)]
        grp_line = f"{grp_info.get('title', def_grp_id)} (<code>{def_grp_id}</code>)"
    else:
        grp_line = "بدون مجموعة افتراضية"

    sync_age = (
        f"منذ {int((time.time() - LAST_GITHUB_SYNC_TS) // 60)} دقيقة"
        if LAST_GITHUB_SYNC_TS > 0
        else "لم تتم بعد"
    )

    return (
        "🎛️ <b>لوحة تحكم بوت التصويت والتتبع</b>\n\n"
        f"• <b>الحالة:</b> {status_line}\n"
        f"• <b>القناة الافتراضية:</b> {ch_line}\n"
        f"• <b>المجموعة الافتراضية:</b> {grp_line}\n"
        f"• <b>آخر رقم منشور معروف:</b> <code>{last_mid_line}</code>\n"
        f"• <b>القنوات المكتشفة:</b> {len(channels)} | <b>المجموعات المكتشفة:</b> {len(groups)}\n"
        f"• <b>آخر مزامنة لـ s.json على GitHub:</b> {sync_age} (كل 9 دقائق تلقائياً)\n\n"
        "💡 <i>يمكنك أيضاً كتابة «ابدأ» أو «إيقاف»، أو إعادة توجيه أي منشور من قناتك هنا للتعرف عليها وعلى آخر رقم منشور فوراً.</i>"
    )


def build_control_panel_markup() -> InlineKeyboardMarkup:
    is_running = bool(STATE.get("is_running", False))
    toggle_btn = (
        InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data="p:toggle")
        if is_running
        else InlineKeyboardButton("▶️ ابدأ العمل", callback_data="p:toggle")
    )
    keyboard = [
        [toggle_btn],
        [
            InlineKeyboardButton("📢 اختيار القناة الافتراضية", callback_data="p:chs"),
            InlineKeyboardButton("💬 اختيار المجموعة الافتراضية", callback_data="p:grps"),
        ],
        [
            InlineKeyboardButton("🔍 فحص آخر 10 منشورات", callback_data="p:scan10"),
            InlineKeyboardButton("🔄 تحديث القنوات والمجموعات", callback_data="p:refresh"),
        ],
        [
            InlineKeyboardButton("💾 حفظ s.json على GitHub الآن", callback_data="p:sync"),
            InlineKeyboardButton("❓ تعليمات الإعداد", callback_data="p:help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_channels_list_markup() -> InlineKeyboardMarkup:
    def_ch = STATE.get("default_channel_id")
    rows: List[List[InlineKeyboardButton]] = []
    for ch_key, ch_data in STATE.get("channels", {}).items():
        mark = "✅ " if str(def_ch) == ch_key else "📢 "
        warn = " ⚠️" if not ch_data.get("can_edit_messages", True) else ""
        title = (ch_data.get("title") or ch_key)[:30]
        rows.append([InlineKeyboardButton(f"{mark}{title}{warn}", callback_data=f"p:sch:{ch_key}")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


def build_groups_list_markup() -> InlineKeyboardMarkup:
    def_grp = STATE.get("default_group_id")
    rows: List[List[InlineKeyboardButton]] = []
    for grp_key, grp_data in STATE.get("known_groups", {}).items():
        mark = "✅ " if str(def_grp) == grp_key else "💬 "
        title = (grp_data.get("title") or grp_key)[:32]
        rows.append([InlineKeyboardButton(f"{mark}{title}", callback_data=f"p:sgrp:{grp_key}")])
    none_mark = "✅ " if def_grp is None else ""
    rows.append([InlineKeyboardButton(f"{none_mark}🚫 بدون مجموعة افتراضية", callback_data="p:sgrp:none")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


async def send_or_edit_panel(target_msg, edit: bool = False):
    text = build_control_panel_text()
    markup = build_control_panel_markup()
    if edit:
        try:
            await target_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest:
            pass
    await target_msg.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private":
        return

    user_id = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = user_id
        await save_local_now()
        await sync_local_to_github_now()
    elif not is_owner(user_id):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.")
        return

    await verify_all_registered_chats(context.bot)
    await send_or_edit_panel(msg, edit=False)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private":
        return

    user_id = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = user_id
        await save_local_now()
    elif not is_owner(user_id):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.")
        return

    # 1) إذا أعاد المالك توجيه رسالة من قناة أو مجموعة للتعرف عليها
    fwd_chat_id, fwd_msg_id, _ = extract_origin_from_forwarded_post(msg)
    if fwd_chat_id:
        ok, info = await sync_single_chat(context.bot, fwd_chat_id)
        if ok and fwd_msg_id and str(fwd_chat_id) in STATE.get("channels", {}):
            ch_data = STATE["channels"][str(fwd_chat_id)]
            if fwd_msg_id > int(ch_data.get("last_known_msg_id", 0) or 0):
                ch_data["last_known_msg_id"] = fwd_msg_id
            info += f"\n🔢 تم تسجيل رقم المنشور: {fwd_msg_id}"
        await save_local_now()
        await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        if ok and STATE.get("is_running") and STATE.get("default_channel_id") == fwd_chat_id:
            added, _ = await scan_last_10_posts(context.bot, fwd_chat_id)
            if added:
                await msg.reply_text(f"🗳️ تم إضافة أزرار التصويت لـ {added} منشورات من آخر 10 منشورات.")
        await send_or_edit_panel(msg, edit=False)
        return

    text = (msg.text or "").strip()
    if not text:
        return

    lower_text = text.lower()
    if lower_text in ("ابدأ", "ابدا", "تشغيل", "start", "/run"):
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await msg.reply_text("⚠️ حدد **القناة الافتراضية** أولاً من القائمة ثم اضغط «ابدأ العمل».", parse_mode="HTML")
            await send_or_edit_panel(msg, edit=False)
            return
        STATE["is_running"] = True
        await save_local_now()
        await sync_local_to_github_now()
        added, last_id = await scan_last_10_posts(context.bot, int(def_ch))
        note = (
            f"▶️ تم بدء العمل على القناة الافتراضية! تم فحص آخر 10 منشورات وإضافة الأزرار لـ ({added}) منشور."
            if last_id > 0
            else "▶️ تم بدء العمل! أرسل منشوراً في القناة أو أعد توجيه آخر منشور هنا ليفحص البوت آخر 10 منشورات فوراً."
        )
        await msg.reply_text(note)
        await send_or_edit_panel(msg, edit=False)
        return

    if lower_text in ("ايقاف", "إيقاف", "توقف", "قف", "stop", "/stop"):
        STATE["is_running"] = False
        await save_local_now()
        await sync_local_to_github_now()
        await msg.reply_text("⏸ تم إيقاف البوت مؤقتاً وحفظ الحالة.")
        await send_or_edit_panel(msg, edit=False)
        return

    # 2) إذا أرسل المالك رابط منشور أو @username أو معرّف قناة/مجموعة
    chat_ref, link_msg_id = parse_chat_or_link(text)
    if chat_ref is not None:
        try:
            chat_obj = await context.bot.get_chat(chat_ref)
            ok, info = await sync_single_chat(context.bot, chat_obj.id)
            if ok and link_msg_id and str(chat_obj.id) in STATE.get("channels", {}):
                ch_data = STATE["channels"][str(chat_obj.id)]
                if link_msg_id > int(ch_data.get("last_known_msg_id", 0) or 0):
                    ch_data["last_known_msg_id"] = link_msg_id
                info += f"\n🔢 تم تسجيل رقم المنشور: {link_msg_id}"
            await save_local_now()
            await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
            if ok and STATE.get("is_running") and STATE.get("default_channel_id") == chat_obj.id:
                added, _ = await scan_last_10_posts(context.bot, chat_obj.id)
                if added:
                    await msg.reply_text(f"🗳️ تم إضافة أزرار التصويت لـ {added} منشورات.")
        except Exception as e:
            await msg.reply_text(f"⚠️ تعذر الوصول لهذه الدردشة: {e}\nتأكد أن البوت مضاف إليها أولاً.")
        await send_or_edit_panel(msg, edit=False)
        return

    await send_or_edit_panel(msg, edit=False)


async def handle_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("p:"):
        return

    if not is_owner(query.from_user.id):
        await query.answer("⛔ هذا التحكم لصاحب البوت فقط.", show_alert=True)
        return

    data = query.data
    parts = data.split(":")
    action = parts[1]

    if action == "home":
        await query.answer()
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "chs":
        await query.answer()
        if not STATE.get("channels"):
            await query.message.edit_text(
                "📢 <b>لا توجد قنوات مكتشفة حالياً.</b>\n\n"
                "1️⃣ أضف البوت كمشرف في قناتك مع صلاحية (نشر الرسائل + تعديل رسائل الآخرين).\n"
                "2️⃣ أعد توجيه أي منشور من القناة هنا، أو أرسل معرفها <code>@username</code>، ثم اضغط تحديث.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="p:home")]]),
                parse_mode="HTML",
            )
            return
        await query.message.edit_text(
            "📢 <b>اختر القناة الافتراضية التي سيعمل عليها البوت:</b>",
            reply_markup=build_channels_list_markup(),
            parse_mode="HTML",
        )
        return

    if action == "sch" and len(parts) == 3:
        ch_id = int(parts[2])
        if str(ch_id) not in STATE.get("channels", {}):
            await query.answer("⚠️ القناة لم تعد متوفرة.", show_alert=True)
            return
        STATE["default_channel_id"] = ch_id
        # إذا كان للقناة مجموعة نقاش مرتبطة، نقترح ضبطها تلقائياً إن لم يكن هناك مجموعة محددة
        linked_grp = STATE["channels"][str(ch_id)].get("linked_group_id")
        if linked_grp and str(linked_grp) in STATE.get("known_groups", {}):
            STATE["default_group_id"] = int(linked_grp)
        await save_local_now()
        await query.answer("✅ تم تعيين القناة الافتراضية.")
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "grps":
        await query.answer()
        await query.message.edit_text(
            "💬 <b>اختر المجموعة الافتراضية (مجموعة النقاش):</b>",
            reply_markup=build_groups_list_markup(),
            parse_mode="HTML",
        )
        return

    if action == "sgrp" and len(parts) == 3:
        val = parts[2]
        if val == "none":
            STATE["default_group_id"] = None
            await query.answer("✅ تم إلغاء المجموعة الافتراضية.")
        else:
            grp_id = int(val)
            STATE["default_group_id"] = grp_id
            await query.answer("✅ تم تعيين المجموعة الافتراضية.")
        await save_local_now()
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "toggle":
        if STATE.get("is_running"):
            STATE["is_running"] = False
            await save_local_now()
            await sync_local_to_github_now()
            await query.answer("⏸ تم إيقاف البوت مؤقتاً.")
            await send_or_edit_panel(query.message, edit=True)
        else:
            def_ch = STATE.get("default_channel_id")
            if not def_ch or str(def_ch) not in STATE.get("channels", {}):
                await query.answer("⚠️ اختر القناة الافتراضية أولاً قبل البدء!", show_alert=True)
                return
            ch_info = STATE["channels"][str(def_ch)]
            if not ch_info.get("can_edit_messages", True):
                await query.answer(
                    "⛔ البوت لا يملك صلاحية (تعديل رسائل الآخرين) في هذه القناة!",
                    show_alert=True,
                )
                return
            STATE["is_running"] = True
            await save_local_now()
            await sync_local_to_github_now()
            await query.answer("▶️ تم بدء العمل! جاري فحص آخر 10 منشورات...")
            added, last_id = await scan_last_10_posts(context.bot, int(def_ch))
            await send_or_edit_panel(query.message, edit=True)
            if last_id == 0:
                await query.message.reply_text(
                    "ℹ️ تم التشغيل! للتعامل مع آخر 10 منشورات قديمة فوراً: أعد توجيه آخر منشور من القناة إلى هنا (أو أرسل رابطه)."
                )
            elif added > 0:
                await query.message.reply_text(f"✅ تم فحص آخر 10 منشورات وإضافة الأزرار لـ {added} منشور.")
        return

    if action == "scan10":
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await query.answer("⚠️ اختر القناة الافتراضية أولاً.", show_alert=True)
            return
        await query.answer("🔍 جاري فحص آخر 10 منشورات...")
        added, last_id = await scan_last_10_posts(context.bot, int(def_ch), force_recheck_tracked=True)
        if last_id == 0:
            await query.message.reply_text(
                "⚠️ لا يوجد رقم منشور معروف بعد لهذه القناة.\nأعد توجيه آخر منشور من القناة هنا أو انشر منشوراً جديداً."
            )
        else:
            await query.message.reply_text(
                f"✅ اكتمل فحص آخر 10 منشورات (حتى رقم {last_id}).\nتمت إضافة/تحديث الأزرار في {added} منشور."
            )
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "refresh":
        await query.answer("🔄 جاري تحديث القنوات والمجموعات...")
        await verify_all_registered_chats(context.bot)
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "sync":
        await query.answer("💾 جاري حفظ s.json إلى GitHub...")
        ok = await sync_local_to_github_now()
        await query.message.reply_text(
            "✅ تم رفع محتوى ملف s.json المحلي إلى GitHub بنجاح."
            if ok
            else "⚠️ تعذر المزامنة مع GitHub (تحقق من GITHUB_TOKEN)."
        )
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "help":
        await query.answer()
        help_text = (
            "❓ <b>تعليمات التشغيل السريعة:</b>\n\n"
            "1️⃣ في <b>@BotFather</b>: افتح <code>/mybots</code> ← اختر البوت ← <code>Bot Settings</code> ← <code>Group Privacy</code> ← <b>Turn off</b>.\n"
            "2️⃣ في <b>القناة</b>: أضف البوت مشرفاً وفعّل له صلاحيتي <b>نشر الرسائل</b> و<b>تعديل رسائل الآخرين</b>.\n"
            "3️⃣ في <b>المجموعة</b>: أضفه للمجموعة (يفضّل بعد تعطيل Privacy أو ترقيته لمشرف).\n"
            "4️⃣ إذا لم تظهر القناة/المجموعة في القائمة: أعد توجيه أي رسالة منها إلى هنا، أو أرسل <code>@username</code> أو رابط منشور منها.\n"
            "5️⃣ اختر القناة الافتراضية والمجموعة الافتراضية ثم اضغط <b>▶️ ابدأ العمل</b>."
        )
        await query.message.edit_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")]]),
            parse_mode="HTML",
        )


# ==================== معالجات القنوات والمجموعات والتصويت ====================
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if not cmu:
        return

    chat = cmu.chat
    new_status = getattr(cmu.new_chat_member, "status", "")

    if chat.type == "channel":
        if new_status in ("administrator", "creator"):
            logger.info(f"📢 تم تفعيل البوت كمشرف في القناة: {chat.title} ({chat.id})")
            await sync_single_chat(context.bot, chat.id)
        else:
            logger.info(f"🚪 خرج البوت أو سُحب إشرافه من القناة: {chat.title} ({chat.id})")
            remove_channel_data(chat.id)
        await save_local_now()

    elif chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator", "creator"):
            logger.info(f"💬 انضم البوت للمجموعة: {chat.title} ({chat.id})")
            await sync_single_chat(context.bot, chat.id)
        else:
            logger.info(f"🚪 خرج البوت من المجموعة: {chat.title} ({chat.id})")
            remove_group_data(chat.id)
        await save_local_now()


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.edited_channel_post
    if not msg or msg.chat.type != "channel":
        return

    if is_service_message(msg):
        return

    channel_id = msg.chat.id
    channel_title = getattr(msg.chat, "title", "") or "قناة"

    if str(channel_id) not in STATE.get("channels", {}):
        await sync_single_chat(context.bot, channel_id)
    ch_data = get_or_init_channel(channel_id, channel_title)
    if msg.message_id > int(ch_data.get("last_known_msg_id", 0) or 0):
        ch_data["last_known_msg_id"] = msg.message_id

    # لا نضيف أزراراً تلقائياً إلا إذا ضغط المالك ابدأ وكانت هذه هي القناة الافتراضية
    if not STATE.get("is_running") or STATE.get("default_channel_id") != channel_id:
        await save_local_now()
        return

    # في الألبوم نضع الأزرار على أول عنصر فقط
    mg_id = getattr(msg, "media_group_id", None)
    if mg_id and update.channel_post:
        now = time.time()
        for k in [k for k, ts in SEEN_MEDIA_GROUPS.items() if now - ts > 600]:
            SEEN_MEDIA_GROUPS.pop(k, None)
        mg_key = f"{channel_id}:{mg_id}"
        if mg_key in SEEN_MEDIA_GROUPS:
            return
        SEEN_MEDIA_GROUPS[mg_key] = now

    if not has_vote_markup(getattr(msg, "reply_markup", None)):
        await ensure_post_has_buttons(context.bot, channel_id, msg.message_id, force_edit=True)
    else:
        get_or_init_post(ch_data, msg.message_id)

    # التأكد من آخر 10 منشورات فقط
    await scan_last_10_posts(context.bot, channel_id, force_recheck_tracked=False)


async def _process_forwarded_channel_post_in_group(bot, fwd_msg):
    origin_ch_id, origin_msg_id, origin_title = extract_origin_from_forwarded_post(fwd_msg)
    if not origin_ch_id or not origin_msg_id:
        return

    if str(origin_ch_id) not in STATE.get("channels", {}):
        await sync_single_chat(bot, origin_ch_id)

    get_or_init_group(
        fwd_msg.chat.id,
        getattr(fwd_msg.chat, "title", "") or "مجموعة",
        linked_channel_id=origin_ch_id,
    )

    if not STATE.get("is_running"):
        await save_local_now()
        return

    def_ch = STATE.get("default_channel_id")
    def_grp = STATE.get("default_group_id")
    if origin_ch_id != def_ch:
        return
    if def_grp is not None and fwd_msg.chat.id != def_grp:
        return

    ch_data = get_or_init_channel(origin_ch_id, origin_title)
    post_data = get_or_init_post(ch_data, origin_msg_id)
    post_data["group_id"] = fwd_msg.chat.id
    post_data["group_msg_id"] = fwd_msg.message_id

    STATE.setdefault("group_post_map", {})[f"{fwd_msg.chat.id}:{fwd_msg.message_id}"] = f"{origin_ch_id}:{origin_msg_id}"

    await ensure_post_has_buttons(bot, origin_ch_id, origin_msg_id, force_edit=False)

    if not has_vote_markup(getattr(fwd_msg, "reply_markup", None)):
        await safe_edit_reply_markup(
            bot=bot,
            chat_id=fwd_msg.chat.id,
            message_id=fwd_msg.message_id,
            channel_id=origin_ch_id,
            channel_msg_id=origin_msg_id,
            likes=post_data.get("likes", 0),
            dislikes=post_data.get("dislikes", 0),
            silent_not_editable=True,
        )

    await save_local_now()


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        return

    if str(msg.chat.id) not in STATE.get("known_groups", {}):
        await sync_single_chat(context.bot, msg.chat.id)
        await save_local_now()

    if getattr(msg, "is_automatic_forward", False):
        await _process_forwarded_channel_post_in_group(context.bot, msg)
        return

    reply_msg = getattr(msg, "reply_to_message", None)
    if reply_msg and getattr(reply_msg, "is_automatic_forward", False):
        gkey = f"{msg.chat.id}:{reply_msg.message_id}"
        if gkey not in STATE.get("group_post_map", {}):
            await _process_forwarded_channel_post_in_group(context.bot, reply_msg)
        return


async def handle_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("v:"):
        return

    parts = query.data.split(":")
    if len(parts) != 4:
        await query.answer()
        return

    _, action, ch_id_str, msg_id_str = parts
    if action not in ("l", "d", "i"):
        await query.answer()
        return

    channel_id = int(ch_id_str)
    channel_msg_id = int(msg_id_str)
    user_id_str = str(query.from_user.id)

    ch_data = STATE.get("channels", {}).get(ch_id_str)
    if not ch_data:
        ok, _ = await sync_single_chat(context.bot, channel_id)
        if not ok:
            await query.answer("⚠️ هذه القناة لم تعد متوفرة لدى البوت.", show_alert=True)
            return
        ch_data = STATE["channels"][ch_id_str]

    post_data = get_or_init_post(ch_data, channel_msg_id)
    voters = post_data.setdefault("voters", {})

    if query.message and query.message.chat.id != channel_id:
        post_data["group_id"] = query.message.chat.id
        post_data["group_msg_id"] = query.message.message_id
        STATE.setdefault("group_post_map", {})[
            f"{query.message.chat.id}:{query.message.message_id}"
        ] = f"{ch_id_str}:{msg_id_str}"

    likes = int(post_data.get("likes", 0))
    dislikes = int(post_data.get("dislikes", 0))
    total = likes + dislikes
    like_pct = round((likes / total) * 100) if total > 0 else 0
    dislike_pct = (100 - like_pct) if total > 0 else 0

    # زر التحقق من الحالة / إجمالي المصوتين
    if action == "i":
        if user_id_str in voters:
            prev_label = "👍 إعجاب" if voters[user_id_str] == "l" else "👎 عدم إعجاب"
            await query.answer(
                f"🔒 تم قفل اختيارات التصويت لك على هذا المنشور.\n"
                f"اختيارك المسجل: {prev_label}\n\n"
                f"👍 إعجاب: {likes} ({like_pct}%)\n"
                f"👎 عدم إعجاب: {dislikes} ({dislike_pct}%)\n"
                f"👥 إجمالي المصوتين: {total}",
                show_alert=True,
            )
        else:
            await query.answer(
                f"🗳️ لم تصوّت بعد على هذا المنشور.\n"
                f"👍 إعجاب: {likes} ({like_pct}%)\n"
                f"👎 عدم إعجاب: {dislikes} ({dislike_pct}%)\n"
                f"اضغط على أحد الخيارين للتصويت.",
                show_alert=True,
            )
        return

    # منع التصويت المكرر نهائياً وإظهار أن الاختيارات غير مفعلة له
    if user_id_str in voters:
        prev_choice = voters[user_id_str]
        prev_label = "👍 إعجاب" if prev_choice == "l" else "👎 عدم إعجاب"
        like_mark = "☑️" if prev_choice == "l" else "🔒"
        dislike_mark = "☑️" if prev_choice == "d" else "🔒"
        await query.answer(
            f"🔒 التصويت مغلق لك — لقد صوّت مسبقاً ({prev_label})\n\n"
            f"{like_mark} 👍 إعجاب: {likes} ({like_pct}%)\n"
            f"{dislike_mark} 👎 عدم إعجاب: {dislikes} ({dislike_pct}%)\n"
            f"👥 إجمالي المصوتين: {total}",
            show_alert=True,
        )
        return

    # تسجيل الصوت الجديد فوراً
    voters[user_id_str] = action
    if action == "l":
        likes += 1
        post_data["likes"] = likes
        choice_label = "👍 إعجاب"
    else:
        dislikes += 1
        post_data["dislikes"] = dislikes
        choice_label = "👎 عدم إعجاب"

    total = likes + dislikes
    like_pct = round((likes / total) * 100)
    dislike_pct = 100 - like_pct

    await save_local_now()

    await query.answer(
        f"✅ تم تسجيل تصويتك: {choice_label}\n"
        f"🔒 تم قفل التصويت لك على هذا المنشور.\n\n"
        f"👍 {likes} ({like_pct}%)  |  👎 {dislikes} ({dislike_pct}%)",
        show_alert=True,
    )

    # تحديث الأزرار على منشور القناة فوراً
    await safe_edit_reply_markup(
        bot=context.bot,
        chat_id=channel_id,
        message_id=channel_msg_id,
        channel_id=channel_id,
        channel_msg_id=channel_msg_id,
        likes=likes,
        dislikes=dislikes,
    )

    # تحديث الأزرار على نسخة المجموعة إن وجدت
    if post_data.get("group_id") and post_data.get("group_msg_id"):
        await safe_edit_reply_markup(
            bot=context.bot,
            chat_id=int(post_data["group_id"]),
            message_id=int(post_data["group_msg_id"]),
            channel_id=channel_id,
            channel_msg_id=channel_msg_id,
            likes=likes,
            dislikes=dislikes,
            silent_not_editable=True,
        )


# ==================== حلقة المزامنة (كل 9 دقائق) وإعادة التشغيل الآمن ====================
async def background_loop(app):
    global LAST_GITHUB_SYNC_TS
    await asyncio.sleep(3)

    try:
        await verify_all_registered_chats(app.bot)
        # إذا كان البوت في وضع التشغيل بالفعل بعد تبديل الجلسة، يفحص آخر 10 منشورات فوراً
        if STATE.get("is_running") and STATE.get("default_channel_id"):
            def_ch = int(STATE["default_channel_id"])
            if str(def_ch) in STATE.get("channels", {}):
                added, _ = await scan_last_10_posts(app.bot, def_ch, force_recheck_tracked=False)
                logger.info(f"🔍 فحص البدء لآخر 10 منشورات في القناة الافتراضية ({def_ch}): تمت إضافة {added}")
    except Exception as e:
        logger.warning(f"تنبيه أثناء الفحص الابتدائي: {e}")

    LAST_GITHUB_SYNC_TS = time.time()
    last_verify = time.time()

    while True:
        try:
            now = time.time()

            # قبل الإغلاق الآمن للجلسة: رفع محتوى s.json المحلي إلى GitHub
            if now - START_TIME >= MAX_SERVER_LIFETIME:
                logger.info("⏳ انتهت مدة جلسة البوت الثاني، جاري حفظ s.json على GitHub والإغلاق الآمن...")
                await sync_local_to_github_now()
                os._exit(0)

            # كل 9 دقائق: يأخذ محتوى ملف s.json من الجهاز ويحفظه على GitHub
            if now - LAST_GITHUB_SYNC_TS >= GITHUB_SYNC_INTERVAL:
                if STATE_DIRTY:
                    await sync_local_to_github_now()
                else:
                    LAST_GITHUB_SYNC_TS = now

            # فحص دوري كل 5 دقائق للقنوات والمجموعات وآخر 10 منشورات عند التشغيل
            if now - last_verify >= 300:
                await verify_all_registered_chats(app.bot)
                if STATE.get("is_running") and STATE.get("default_channel_id"):
                    def_ch = int(STATE["default_channel_id"])
                    if str(def_ch) in STATE.get("channels", {}):
                        await scan_last_10_posts(app.bot, def_ch, force_recheck_tracked=False)
                last_verify = time.time()

            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"خطأ في الحلقة الخلفية: {e}")
            await asyncio.sleep(5)


async def on_post_init(app):
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم تشغيل البوت الثاني بنجاح (حفظ محلي + مزامنة GitHub كل 9 دقائق).")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT2_TOKEN)
        .concurrent_updates(8)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(CommandHandler(["start", "panel", "menu"], cmd_start))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_group_message))
    app.add_handler(CallbackQueryHandler(handle_panel_callback, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(handle_vote_callback, pattern=r"^v:[ldi]:"))

    print("🚀 تم تشغيل البوت الثاني (bot2.py).")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
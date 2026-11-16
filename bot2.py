import os
import sys
import json
import base64
import time
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest, RetryAfter, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
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
        # نأخذ الجزء الثاني فقط (توكن جيت هب) ونتجاهل توكن البوت الأول
        GITHUB_TOKEN = parts[1].strip()
        break

if not GITHUB_TOKEN:
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip() or None

REPO_OWNER = "ahmedhassan2382006-art"
REPO_NAME = "Articles"
TOKEN_FILE_PATH = "t.txt"
STATE_FILE_PATH = "s.json"

GITHUB_TOKEN_FILE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{TOKEN_FILE_PATH}"
GITHUB_STATE_FILE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{STATE_FILE_PATH}"

START_TIME = time.time()
MAX_SERVER_LIFETIME = (5 * 3600) - 300  # إغلاق آمن قبل نهاية الـ 5 ساعات

STATE_LOCK = asyncio.Lock()
GITHUB_STATE_SHA: Optional[str] = None
STATE_DIRTY = False

# تتبع مجموعات الألبومات لتجنب تكرار الأزرار على كل صورة في الألبوم الواحد
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
    """جلب توكن البوت الثاني من ملف t.txt على GitHub (بدون الاعتماد على توكن البوت الأول)"""
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

    # بديل احتياطي إذا كان الملف موجوداً داخل بيئة التشغيل
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
    logger.error("❌ لم يتم العثور على توكن البوت الثاني في ملف t.txt! أضف التوكن في t.txt بجانب config.json.")
    sys.exit(1)


# ==================== إدارة قاعدة بيانات s.json على GitHub ====================
def _default_state() -> Dict[str, Any]:
    return {
        "channels": {},
        "group_post_map": {},
    }


def _normalize_state(cfg: Any) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("channels", {})
    cfg.setdefault("group_post_map", {})
    return cfg


def _sync_get_or_create_state() -> Dict[str, Any]:
    """جلب s.json من GitHub، وإذا لم يوجد يتم إنشاؤه تلقائياً"""
    global GITHUB_STATE_SHA
    default_cfg = _default_state()

    if not GITHUB_TOKEN:
        logger.warning("⚠️ لا يوجد توكن جيت هب؛ سيعمل البوت بالذاكرة المؤقتة.")
        return default_cfg

    try:
        res = requests.get(GITHUB_STATE_FILE_URL, headers=_github_headers(), timeout=12)
        if res.status_code == 200:
            data = res.json()
            GITHUB_STATE_SHA = data.get("sha")
            raw_content = base64.b64decode(data.get("content", "")).decode("utf-8")
            parsed = json.loads(raw_content) if raw_content.strip() else default_cfg
            return _normalize_state(parsed)
        elif res.status_code == 404:
            logger.info("📄 ملف s.json غير موجود على GitHub، جاري إنشاؤه الآن...")
            _sync_save_state(default_cfg)
            return default_cfg
    except Exception as e:
        logger.error(f"خطأ قراءة s.json من GitHub: {e}")

    return default_cfg


def _sync_save_state(state_obj: Dict[str, Any]) -> bool:
    """حفظ s.json مباشرة على GitHub بدون تخزين ملفات على الجهاز"""
    global GITHUB_STATE_SHA
    if not GITHUB_TOKEN:
        return False

    for attempt in range(3):
        try:
            if not GITHUB_STATE_SHA:
                res_check = requests.get(GITHUB_STATE_FILE_URL, headers=_github_headers(), timeout=8)
                if res_check.status_code == 200:
                    GITHUB_STATE_SHA = res_check.json().get("sha")

            content_bytes = json.dumps(state_obj, ensure_ascii=False, indent=2).encode("utf-8")
            b64_content = base64.b64encode(content_bytes).decode("utf-8")
            payload: Dict[str, Any] = {
                "message": "Update s.json [Vote Bot Auto Sync]",
                "content": b64_content,
            }
            if GITHUB_STATE_SHA:
                payload["sha"] = GITHUB_STATE_SHA

            res = requests.put(GITHUB_STATE_FILE_URL, headers=_github_headers(), json=payload, timeout=12)
            if res.status_code in (200, 201):
                GITHUB_STATE_SHA = res.json().get("content", {}).get("sha", GITHUB_STATE_SHA)
                return True
            elif res.status_code == 409:
                GITHUB_STATE_SHA = None
                time.sleep(0.3)
            else:
                logger.warning(f"⚠️ رد غير متوقع عند حفظ s.json: {res.status_code} - {res.text[:150]}")
        except Exception as e:
            logger.error(f"خطأ حفظ s.json على GitHub: {e}")
            GITHUB_STATE_SHA = None
    return False


STATE: Dict[str, Any] = _sync_get_or_create_state()


async def save_state_now():
    global STATE_DIRTY
    async with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        ok = await asyncio.to_thread(_sync_save_state, snapshot)
        if ok:
            STATE_DIRTY = False


def mark_state_dirty():
    global STATE_DIRTY
    STATE_DIRTY = True


# ==================== تصميم أزرار التصويت (نمط مربعات التحديد / الاستفتاء) ====================
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
    like_box = "☑️" if likes > 0 else "☐"
    dislike_box = "☑️" if dislikes > 0 else "☐"

    like_text = f"{like_box}  👍 إعجاب   •   {likes}"
    dislike_text = f"{dislike_box}  👎 عدم إعجاب   •   {dislikes}"

    like_cb = f"v:l:{channel_id}:{msg_id}"
    dislike_cb = f"v:d:{channel_id}:{msg_id}"

    # كل خيار في صف مستقل لمحاكاة مربعات الاختيار في الاستفتاءات داخل نفس المنشور
    keyboard = [
        [make_styled_button(like_text, like_cb, style="success", use_style=use_style)],
        [make_styled_button(dislike_text, dislike_cb, style="danger", use_style=use_style)],
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
) -> bool:
    """تعديل أزرار المنشور بأمان مع دعم التراجع التلقائي عن style إذا لزم"""
    for use_style in (True, False):
        markup = build_vote_markup(channel_id, channel_msg_id, likes, dislikes, use_style=use_style)
        for _ in range(2):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                )
                return True
            except RetryAfter as ra:
                await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            except BadRequest as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return True
                if use_style and "style" in err:
                    break  # إعادة المحاولة بدون style
                if not silent_not_editable:
                    logger.debug(f"تعذر تعديل أزرار ({chat_id}, {message_id}): {e}")
                return False
            except Forbidden as e:
                if not silent_not_editable:
                    logger.warning(f"لا توجد صلاحية تعديل في ({chat_id}): {e}")
                return False
            except TelegramError as e:
                if not silent_not_editable:
                    logger.debug(f"خطأ تيليجرام عند التعديل ({chat_id}, {message_id}): {e}")
                return False
    return False


# ==================== إدارة القنوات ومجموعات النقاش ====================
def get_or_init_channel(channel_id: int, title: str = "") -> Dict[str, Any]:
    ch_key = str(channel_id)
    channels = STATE.setdefault("channels", {})
    if ch_key not in channels:
        channels[ch_key] = {
            "title": title or "قناة",
            "linked_group_id": None,
            "linked_group_title": None,
            "last_known_msg_id": 0,
            "posts": {},
        }
    elif title:
        channels[ch_key]["title"] = title
    return channels[ch_key]


def remove_channel_data(channel_id: int | str):
    ch_key = str(channel_id)
    channels = STATE.get("channels", {})
    if ch_key in channels:
        channels.pop(ch_key, None)

    gmap = STATE.get("group_post_map", {})
    keys_to_del = [k for k, v in gmap.items() if str(v).startswith(f"{ch_key}:")]
    for k in keys_to_del:
        gmap.pop(k, None)
    mark_state_dirty()


def remove_group_links(group_id: int | str):
    grp_int = int(group_id)
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
    mark_state_dirty()


async def is_bot_active_in_chat(bot, chat_id: int, require_admin: bool = False) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        status = getattr(member, "status", "")
        if require_admin:
            return status in ("administrator", "creator")
        return status in ("administrator", "creator", "member", "restricted")
    except (BadRequest, Forbidden, TelegramError):
        return False


async def sync_channel_and_linked_group(bot, channel_id: int) -> bool:
    """التحقق من وجود البوت في القناة واكتشاف مجموعة النقاش المرتبطة بها"""
    try:
        if not await is_bot_active_in_chat(bot, channel_id, require_admin=True):
            remove_channel_data(channel_id)
            return False

        chat = await bot.get_chat(channel_id)
        ch_data = get_or_init_channel(channel_id, getattr(chat, "title", "") or "قناة")
        linked_id = getattr(chat, "linked_chat_id", None)

        if linked_id:
            if await is_bot_active_in_chat(bot, linked_id, require_admin=False):
                try:
                    grp_chat = await bot.get_chat(linked_id)
                    ch_data["linked_group_id"] = linked_id
                    ch_data["linked_group_title"] = getattr(grp_chat, "title", "") or "مجموعة النقاش"
                except Exception:
                    ch_data["linked_group_id"] = linked_id
            else:
                ch_data["linked_group_id"] = None
                ch_data["linked_group_title"] = None
        else:
            ch_data["linked_group_id"] = None
            ch_data["linked_group_title"] = None

        mark_state_dirty()
        return True
    except (BadRequest, Forbidden):
        remove_channel_data(channel_id)
        return False
    except Exception as e:
        logger.debug(f"تعذر مزامنة القناة {channel_id}: {e}")
        return False


async def verify_all_registered_chats(bot):
    """التحقق من جميع القنوات المحفوظة وتنظيف أي قناة تجريبية تم إخراج البوت منها"""
    ch_ids = list(STATE.get("channels", {}).keys())
    changed = False
    for ch_key in ch_ids:
        before = json.dumps(STATE["channels"].get(ch_key), sort_keys=True, ensure_ascii=False)
        await sync_channel_and_linked_group(bot, int(ch_key))
        after = json.dumps(STATE["channels"].get(ch_key), sort_keys=True, ensure_ascii=False)
        if before != after:
            changed = True
    if changed:
        await save_state_now()


# ==================== معالجة المنشورات وإضافة الأزرار ====================
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
        mark_state_dirty()
    return posts[m_key]


async def ensure_post_has_buttons(
    bot,
    channel_id: int,
    channel_msg_id: int,
    channel_title: str = "",
    force_edit: bool = False,
) -> bool:
    ch_data = get_or_init_channel(channel_id, channel_title)
    posts = ch_data.setdefault("posts", {})
    m_key = str(channel_msg_id)
    already_tracked = m_key in posts
    post_data = get_or_init_post(ch_data, channel_msg_id)

    if already_tracked and not force_edit:
        return True

    ok = await safe_edit_reply_markup(
        bot=bot,
        chat_id=channel_id,
        message_id=channel_msg_id,
        channel_id=channel_id,
        channel_msg_id=channel_msg_id,
        likes=post_data.get("likes", 0),
        dislikes=post_data.get("dislikes", 0),
    )
    if not ok and not already_tracked:
        # لم يعد منشوراً صالحاً أو رسالة خدمة غير قابلة للتعديل
        posts.pop(m_key, None)
        return False

    if ok and post_data.get("group_id") and post_data.get("group_msg_id"):
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

    mark_state_dirty()
    return ok


async def scan_missing_channel_posts(bot, channel_id: int, current_msg_id: int):
    """إذا وُجدت منشورات في القناة بدون أزرار إعجاب/عدم إعجاب يضيفها تلقائياً"""
    ch_data = get_or_init_channel(channel_id)
    prev_last = int(ch_data.get("last_known_msg_id", 0) or 0)
    posts = ch_data.setdefault("posts", {})

    if prev_last == 0:
        # عند أول منشور يُشاهد في القناة: فحص آخر 15 منشوراً سبقته
        start_id = max(1, current_msg_id - 15)
        end_id = current_msg_id
    elif current_msg_id > prev_last + 1:
        # سد أي فجوة لمنشورات سابقة لم تصل
        start_id = prev_last + 1
        end_id = min(current_msg_id, prev_last + 30)
    else:
        ch_data["last_known_msg_id"] = max(prev_last, current_msg_id)
        return

    added_any = False
    for mid in range(start_id, end_id):
        if str(mid) in posts:
            continue
        success = await ensure_post_has_buttons(bot, channel_id, mid, force_edit=True)
        if success:
            added_any = True
        await asyncio.sleep(0.25)

    ch_data["last_known_msg_id"] = max(prev_last, current_msg_id)
    if added_any:
        mark_state_dirty()


def extract_origin_from_forwarded_post(msg) -> Tuple[Optional[int], Optional[int], str]:
    """استخراج معرف القناة ومعرف المنشور الأصلي من المنشور المحوّل تلقائياً إلى مجموعة النقاش"""
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None)
        origin_msg_id = getattr(origin, "message_id", None)
        if origin_chat and getattr(origin_chat, "type", "") == "channel" and origin_msg_id:
            return origin_chat.id, origin_msg_id, getattr(origin_chat, "title", "") or ""

    # دعم التوافق الخلفي
    fwd_chat = getattr(msg, "forward_from_chat", None)
    fwd_msg_id = getattr(msg, "forward_from_message_id", None)
    if fwd_chat and getattr(fwd_chat, "type", "") == "channel" and fwd_msg_id:
        return fwd_chat.id, fwd_msg_id, getattr(fwd_chat, "title", "") or ""

    return None, None, ""


# ==================== معالجات التحديثات ====================
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اكتشاف إضافة أو إزالة البوت من القنوات ومجموعات النقاش لحظياً"""
    cmu = update.my_chat_member
    if not cmu:
        return

    chat = cmu.chat
    new_status = getattr(cmu.new_chat_member, "status", "")

    if chat.type == "channel":
        if new_status in ("administrator", "creator"):
            logger.info(f"📢 تم تفعيل البوت كمشرف في القناة: {chat.title} ({chat.id})")
            await sync_channel_and_linked_group(context.bot, chat.id)
            await save_state_now()
        elif new_status in ("left", "kicked", "member", "restricted"):
            logger.info(f"🚪 تمت إزالة البوت أو سحب صلاحياته من القناة: {chat.title} ({chat.id})")
            remove_channel_data(chat.id)
            await save_state_now()

    elif chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator", "creator"):
            try:
                full_chat = await context.bot.get_chat(chat.id)
                linked_ch_id = getattr(full_chat, "linked_chat_id", None)
                if linked_ch_id:
                    logger.info(f"💬 تم ربط مجموعة النقاش '{chat.title}' ({chat.id}) بالقناة ({linked_ch_id})")
                    await sync_channel_and_linked_group(context.bot, linked_ch_id)
                    await save_state_now()
            except Exception as e:
                logger.debug(f"تعذر فحص مجموعة النقاش {chat.id}: {e}")
        elif new_status in ("left", "kicked"):
            logger.info(f"🚪 خرج البوت من المجموعة: {chat.title} ({chat.id})")
            remove_group_links(chat.id)
            await save_state_now()


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تتبع أي منشور في القناة وإضافة أزرار الإعجاب وعدم الإعجاب إليه مباشرة"""
    msg = update.channel_post or update.edited_channel_post
    if not msg or msg.chat.type != "channel":
        return

    if is_service_message(msg):
        return

    channel_id = msg.chat.id
    channel_title = getattr(msg.chat, "title", "") or "قناة"
    is_new_channel = str(channel_id) not in STATE.get("channels", {})

    if is_new_channel:
        await sync_channel_and_linked_group(context.bot, channel_id)
    else:
        get_or_init_channel(channel_id, channel_title)

    # إذا كان المنشور جزءاً من ألبوم، نضع التصويت على أول عنصر من الألبوم فقط
    mg_id = getattr(msg, "media_group_id", None)
    if mg_id and update.channel_post:
        now = time.time()
        # تنظيف الذاكرة المؤقتة للألبومات القديمة
        for k in [k for k, ts in SEEN_MEDIA_GROUPS.items() if now - ts > 600]:
            SEEN_MEDIA_GROUPS.pop(k, None)
        mg_key = f"{channel_id}:{mg_id}"
        if mg_key in SEEN_MEDIA_GROUPS:
            return
        SEEN_MEDIA_GROUPS[mg_key] = now

    already_has_buttons = has_vote_markup(getattr(msg, "reply_markup", None))
    if not already_has_buttons:
        await ensure_post_has_buttons(
            context.bot,
            channel_id=channel_id,
            channel_msg_id=msg.message_id,
            channel_title=channel_title,
            force_edit=True,
        )
    else:
        ch_data = get_or_init_channel(channel_id, channel_title)
        get_or_init_post(ch_data, msg.message_id)

    # فحص أي منشورات فائتة أو سابقة في القناة لا تحتوي على الأزرار
    await scan_missing_channel_posts(context.bot, channel_id, msg.message_id)
    await save_state_now()


async def _process_forwarded_channel_post_in_group(bot, fwd_msg):
    """ربط المنشور المحول في مجموعة النقاش بمنشور القناة الأصلي ومزامنة الأزرار"""
    origin_ch_id, origin_msg_id, origin_title = extract_origin_from_forwarded_post(fwd_msg)
    if not origin_ch_id or not origin_msg_id:
        return

    # نتأكد أولاً أن البوت فعلاً مشرف في تلك القناة
    if str(origin_ch_id) not in STATE.get("channels", {}):
        ok = await sync_channel_and_linked_group(bot, origin_ch_id)
        if not ok:
            return

    ch_data = get_or_init_channel(origin_ch_id, origin_title)
    ch_data["linked_group_id"] = fwd_msg.chat.id
    ch_data["linked_group_title"] = getattr(fwd_msg.chat, "title", "") or "مجموعة النقاش"

    post_data = get_or_init_post(ch_data, origin_msg_id)
    post_data["group_id"] = fwd_msg.chat.id
    post_data["group_msg_id"] = fwd_msg.message_id

    gmap = STATE.setdefault("group_post_map", {})
    gmap[f"{fwd_msg.chat.id}:{fwd_msg.message_id}"] = f"{origin_ch_id}:{origin_msg_id}"

    # التأكد من أن منشور القناة الأصلي يحتوي على الأزرار
    await ensure_post_has_buttons(
        bot,
        channel_id=origin_ch_id,
        channel_msg_id=origin_msg_id,
        channel_title=origin_title,
        force_edit=(str(origin_msg_id) not in ch_data.get("posts", {})),
    )

    # محاولة إضافة/تحديث الأزرار على نسخة مجموعة النقاش أيضاً إن سمح تيليجرام بذلك
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

    mark_state_dirty()


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة منشورات القناة المحولة تلقائياً لمجموعة النقاش فقط وتجاهل رسائل المجموعة العادية"""
    msg = update.message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        return

    # 1) إذا كانت الرسالة نفسها منشور قناة محول تلقائياً للمجموعة
    if getattr(msg, "is_automatic_forward", False):
        await _process_forwarded_channel_post_in_group(context.bot, msg)
        return

    # 2) إذا رد عضو على منشور قناة محول للمجموعة ولم يكن مربوطاً بعد
    reply_msg = getattr(msg, "reply_to_message", None)
    if reply_msg and getattr(reply_msg, "is_automatic_forward", False):
        gkey = f"{msg.chat.id}:{reply_msg.message_id}"
        if gkey not in STATE.get("group_post_map", {}):
            await _process_forwarded_channel_post_in_group(context.bot, reply_msg)
        return

    # أي رسالة مجموعة عادية أخرى يتم تجاهلها تماماً حسب المطلوب
    return


async def handle_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة تصويت المستخدمين ومنع تكرار التصويت نهائياً ومزامنة العدّاد"""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith("v:"):
        return

    parts = data.split(":")
    if len(parts) != 4:
        await query.answer()
        return

    _, action, ch_id_str, msg_id_str = parts
    if action not in ("l", "d"):
        await query.answer()
        return

    channel_id = int(ch_id_str)
    channel_msg_id = int(msg_id_str)
    user_id_str = str(query.from_user.id)

    ch_data = STATE.get("channels", {}).get(ch_id_str)
    if not ch_data:
        # محاولة التحقق السريع إذا كانت القناة ما زالت تضم البوت
        ok = await sync_channel_and_linked_group(context.bot, channel_id)
        if not ok:
            await query.answer("⚠️ هذه القناة لم تعد مفعلة لدى البوت.", show_alert=True)
            return
        ch_data = STATE["channels"][ch_id_str]

    post_data = get_or_init_post(ch_data, channel_msg_id)
    voters = post_data.setdefault("voters", {})

    # إذا جاء التصويت من نسخة المجموعة، نسجل رابطها بالمرة
    if query.message and query.message.chat.id != channel_id:
        post_data["group_id"] = query.message.chat.id
        post_data["group_msg_id"] = query.message.message_id
        STATE.setdefault("group_post_map", {})[
            f"{query.message.chat.id}:{query.message.message_id}"
        ] = f"{ch_id_str}:{msg_id_str}"

    # التحقق الصارم: هل صوّت هذا المستخدم مسبقاً على هذا المنشور؟
    if user_id_str in voters:
        prev_choice = voters[user_id_str]
        prev_label = "👍 إعجاب" if prev_choice == "l" else "👎 عدم إعجاب"
        await query.answer(
            f"🚫 لقد قمت بالتصويت مسبقاً ({prev_label}).\nلا يمكنك التصويت مرة أخرى على هذا المنشور.",
            show_alert=True,
        )
        return

    # تسجيل التصويت الجديد
    voters[user_id_str] = action
    if action == "l":
        post_data["likes"] = int(post_data.get("likes", 0)) + 1
        choice_label = "👍 إعجاب"
    else:
        post_data["dislikes"] = int(post_data.get("dislikes", 0)) + 1
        choice_label = "👎 عدم إعجاب"

    likes = post_data["likes"]
    dislikes = post_data["dislikes"]
    mark_state_dirty()

    await query.answer(
        f"✅ تم تسجيل تصويتك: {choice_label}\n(👍 {likes}  |  👎 {dislikes})",
        show_alert=False,
    )

    # تحديث الأزرار على منشور القناة
    await safe_edit_reply_markup(
        bot=context.bot,
        chat_id=channel_id,
        message_id=channel_msg_id,
        channel_id=channel_id,
        channel_msg_id=channel_msg_id,
        likes=likes,
        dislikes=dislikes,
    )

    # تحديث الأزرار على نسخة مجموعة النقاش إن وُجدت وكانت قابلة للتعديل
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


# ==================== حلقة المزامنة والحفظ والمدة التشغيلية ====================
async def background_loop(app):
    await asyncio.sleep(3)
    try:
        await verify_all_registered_chats(app.bot)
    except Exception as e:
        logger.debug(f"خطأ التحقق الابتدائي: {e}")

    last_verify = time.time()

    while True:
        try:
            # قبل الإغلاق المجدول نحفظ أي تغييرات معلقة في s.json
            if time.time() - START_TIME >= MAX_SERVER_LIFETIME:
                logger.info("⏳ انتهت فترة الـ 5 ساعات للبوت الثاني، جاري حفظ s.json والإغلاق النظيف...")
                if STATE_DIRTY:
                    await save_state_now()
                os._exit(0)

            # حفظ التغييرات المتراكمة (الأصوات الجديدة) على GitHub كل 3 ثوانٍ
            if STATE_DIRTY:
                await save_state_now()

            # فحص دوري كل 5 دقائق للتحقق من القنوات ومجموعات النقاش
            if time.time() - last_verify >= 300:
                await verify_all_registered_chats(app.bot)
                last_verify = time.time()

            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"خطأ في حلقة البوت الثاني: {e}")
            await asyncio.sleep(5)


async def on_post_init(app):
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم بدء بوت التصويت والمتابعة (bot2.py) بنجاح.")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT2_TOKEN)
        .concurrent_updates(8)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_channel_post)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_group_message)
    )
    app.add_handler(CallbackQueryHandler(handle_vote_callback, pattern=r"^v:[ld]:"))

    print("🚀 تم تشغيل البوت الثاني (bot2.py) لتتبع منشورات القنوات والتصويت.")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
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
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    MessageEntity,
)
try:
    from telegram import KeyboardButtonRequestChat
except ImportError:
    KeyboardButtonRequestChat = None

from telegram.error import BadRequest, RetryAfter, Forbidden, TelegramError, Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    TypeHandler,
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

if not GITHUB_TOKEN and SECRET_RAW.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_")):
    GITHUB_TOKEN = SECRET_RAW

if not GITHUB_TOKEN:
    GITHUB_TOKEN = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    ).strip() or None

# مستودع حفظ الحالة s.json
REPO_OWNER = "ahmedhassan2382006-art"
REPO_NAME = "Articles"
STATE_FILE_PATH = "s.json"
LOCAL_STATE_PATH = "s.json"

# المستودع الخاص بالمفاتيح (قراءة t.txt)
TOKEN_REPO_OWNER = "ahmedhassan2382006-art"
TOKEN_REPO_NAME = "kys"
TOKEN_FILE_PATH = "t.txt"

GITHUB_TOKEN_FILE_URL = f"https://api.github.com/repos/{TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME}/contents/{TOKEN_FILE_PATH}"
GITHUB_STATE_FILE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{STATE_FILE_PATH}"

START_TIME = time.time()
MAX_SERVER_LIFETIME = (5 * 3600) - 420
GITHUB_SYNC_INTERVAL = 9 * 60

STATE_LOCK = asyncio.Lock()
VOTE_LOCK = asyncio.Lock()
GITHUB_STATE_SHA: Optional[str] = None
STATE_DIRTY = False
LAST_GITHUB_SYNC_TS = 0.0

SEEN_MEDIA_GROUPS: Dict[str, float] = {}

# متغيرات التشخيص الحي
LAST_SEEN_CHANNEL_POST: Optional[Dict[str, Any]] = None
LAST_PROCESS_ERROR: str = ""
CONFLICT_COUNT: int = 0
CONFLICT_NOTIFIED: bool = False

DEFAULT_BUTTON_OPTIONS = ["👍 إعجاب", "👎 عدم إعجاب"]
DEFAULT_TRIGGER_WORD = "@like"


def _github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Like-Buttons-Bot2",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _extract_telegram_bot_token(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    for line in raw_text.splitlines():
        cleaned = line.strip().strip('"').strip("'")
        if not cleaned or cleaned.startswith("#"):
            continue
        m = re.search(r"\b(\d{6,}:[A-Za-z0-9_-]{20,})\b", cleaned)
        if m:
            return m.group(1)
        if ":" in cleaned and not cleaned.startswith(("http://", "https://", "ghp_", "github_pat_")):
            return cleaned
    return None


def load_bot2_token() -> Optional[str]:
    like_secret = os.environ.get("LIKE", "").strip()
    if like_secret:
        tok = _extract_telegram_bot_token(like_secret)
        if tok:
            logger.info("🔑 تم قراءة توكن البوت من السكرت LIKE بنجاح.")
            return tok
        logger.warning("⚠️ المتغير LIKE لا يحتوي على توكن صالح، جاري المحاولة من المستودع الخاص kys...")

    if GITHUB_TOKEN:
        try:
            res = requests.get(GITHUB_TOKEN_FILE_URL, headers=_github_headers(), timeout=12)
            if res.status_code == 200:
                raw_b64 = res.json().get("content", "")
                content = base64.b64decode(raw_b64).decode("utf-8-sig")
                tok = _extract_telegram_bot_token(content)
                if tok:
                    logger.info(f"🔑 تم قراءة {TOKEN_FILE_PATH} من {TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME} بنجاح.")
                    return tok
            else:
                logger.warning(f"⚠️ تعذر قراءة {TOKEN_FILE_PATH} من kys (كود: {res.status_code})")
        except Exception as e:
            logger.error(f"خطأ جلب {TOKEN_FILE_PATH} من kys: {e}")

    if os.path.exists(TOKEN_FILE_PATH):
        try:
            with open(TOKEN_FILE_PATH, "r", encoding="utf-8-sig") as f:
                tok = _extract_telegram_bot_token(f.read())
                if tok:
                    return tok
        except Exception as e:
            logger.error(f"خطأ قراءة {TOKEN_FILE_PATH} محلياً: {e}")

    return None


BOT2_TOKEN = load_bot2_token()
if not BOT2_TOKEN:
    logger.error("❌ لم يتم العثور على توكن البوت في السكرت LIKE أو في المستودع الخاص kys/t.txt!")
    sys.exit(1)


# ==================== إدارة الملف المحلي s.json والمزامنة كل 9 دقائق ====================
def _default_settings() -> Dict[str, Any]:
    return {
        "trigger_word": DEFAULT_TRIGGER_WORD,
        "case_sensitive_trigger": False,
        "button_options": list(DEFAULT_BUTTON_OPTIONS),
        "allows_multiple_answers": False,
        "allows_revoting": True,
        "show_vote_counts": True,
        "vertical_buttons": False,
    }


def _default_state() -> Dict[str, Any]:
    return {
        "owner_id": None,
        "is_running": False,
        "default_channel_id": None,
        "default_group_id": None,
        "known_groups": {},
        "channels": {},
        "group_post_map": {},
        "settings": _default_settings(),
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

    defaults = _default_settings()
    settings = cfg.get("settings")
    if not isinstance(settings, dict):
        settings = {}

    if "button_options" not in settings and isinstance(settings.get("poll_options"), list):
        settings["button_options"] = list(settings["poll_options"])

    for old_key in (
        "poll_question",
        "use_post_text_as_question",
        "shuffle_options",
        "hide_results_until_closes",
        "delete_trigger_only_post",
    ):
        settings.pop(old_key, None)

    for k, v in defaults.items():
        if k not in settings or settings[k] is None:
            settings[k] = list(v) if isinstance(v, list) else v

    if not isinstance(settings.get("button_options"), list):
        settings["button_options"] = list(DEFAULT_BUTTON_OPTIONS)
    settings["button_options"] = [
        str(opt).strip()[:60]
        for opt in settings["button_options"]
        if str(opt).strip()
    ][:12]

    trig = str(settings.get("trigger_word", DEFAULT_TRIGGER_WORD)).strip()
    settings["trigger_word"] = trig or DEFAULT_TRIGGER_WORD

    cfg["settings"] = settings
    return cfg


def get_settings() -> Dict[str, Any]:
    return STATE.setdefault("settings", _default_settings())


def write_local_state_sync(state_obj: Dict[str, Any]) -> bool:
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
    global GITHUB_STATE_SHA
    if not GITHUB_TOKEN or not os.path.exists(LOCAL_STATE_PATH):
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
    global STATE_DIRTY
    async with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        ok = await asyncio.to_thread(write_local_state_sync, snapshot)
        if ok:
            STATE_DIRTY = True


async def sync_local_to_github_now() -> bool:
    global STATE_DIRTY, LAST_GITHUB_SYNC_TS
    async with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        await asyncio.to_thread(write_local_state_sync, snapshot)
        ok = await asyncio.to_thread(_sync_push_local_file_to_github)
        if ok:
            STATE_DIRTY = False
            LAST_GITHUB_SYNC_TS = time.time()
        return ok


# ==================== معالجة الكلمة المفتاحية وبناء الأزرار ====================
def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clamp_entities(text: str, entities: List[MessageEntity]) -> List[MessageEntity]:
    total_u16 = _utf16_len(text)
    clamped: List[MessageEntity] = []
    for ent in entities:
        if ent.offset < 0 or ent.offset >= total_u16:
            continue
        new_len = min(ent.length, total_u16 - ent.offset)
        if new_len <= 0:
            continue
        clamped.append(
            MessageEntity(
                type=ent.type,
                offset=ent.offset,
                length=new_len,
                url=getattr(ent, "url", None),
                user=getattr(ent, "user", None),
                language=getattr(ent, "language", None),
                custom_emoji_id=getattr(ent, "custom_emoji_id", None),
            )
        )
    return clamped


def extract_trigger_and_clean_text(
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    trigger_word: str,
    case_sensitive: bool = False,
) -> Tuple[bool, str, List[MessageEntity]]:
    """
    يتحقق من وجود الكلمة المفتاحية في بداية النص:
    - يقبل أي حرف عربي/علامة/رقم/إيموجي/مسافة بعدها مباشرة (مثل: @likeالسلام، @like. مرحبا، $botالسلام).
    - الحالة الوحيدة المستثناة: إذا تلاها حرف إنجليزي مباشرة بدون فاصل (مثل: @likehello).
    """
    if not raw_text or not trigger_word:
        return False, raw_text or "", list(entities or [])

    flags = 0 if case_sensitive else re.IGNORECASE
    leading_ws = r"[\s\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]*"
    pattern = rf"^{leading_ws}{re.escape(trigger_word)}(?![A-Za-z]){leading_ws}"
    m = re.match(pattern, raw_text, flags=flags)
    if not m:
        return False, raw_text, list(entities or [])

    cut_char_idx = m.end()
    cleaned_text = raw_text[cut_char_idx:].rstrip()
    cut_utf16 = _utf16_len(raw_text[:cut_char_idx])

    adjusted_entities: List[MessageEntity] = []
    for ent in (entities or []):
        ent_start = ent.offset
        ent_end = ent.offset + ent.length
        if ent_end <= cut_utf16:
            continue
        if ent_start < cut_utf16:
            new_offset = 0
            new_length = ent_end - cut_utf16
        else:
            new_offset = ent_start - cut_utf16
            new_length = ent.length

        if new_length <= 0:
            continue

        adjusted_entities.append(
            MessageEntity(
                type=ent.type,
                offset=new_offset,
                length=new_length,
                url=getattr(ent, "url", None),
                user=getattr(ent, "user", None),
                language=getattr(ent, "language", None),
                custom_emoji_id=getattr(ent, "custom_emoji_id", None),
            )
        )

    adjusted_entities = _clamp_entities(cleaned_text, adjusted_entities)
    return True, cleaned_text, adjusted_entities


def _serialize_entities(entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ent in (entities or []):
        item: Dict[str, Any] = {
            "type": ent.type,
            "offset": ent.offset,
            "length": ent.length,
        }
        if getattr(ent, "url", None):
            item["url"] = ent.url
        if getattr(ent, "language", None):
            item["language"] = ent.language
        if getattr(ent, "custom_emoji_id", None):
            item["custom_emoji_id"] = ent.custom_emoji_id
        out.append(item)
    return out


def _deserialize_entities(raw_list: Optional[List[Dict[str, Any]]]) -> List[MessageEntity]:
    out: List[MessageEntity] = []
    for item in (raw_list or []):
        try:
            out.append(
                MessageEntity(
                    type=item["type"],
                    offset=int(item["offset"]),
                    length=int(item["length"]),
                    url=item.get("url"),
                    language=item.get("language"),
                    custom_emoji_id=item.get("custom_emoji_id"),
                )
            )
        except Exception:
            continue
    return out


def _is_media_message(msg) -> bool:
    for attr in ("photo", "video", "animation", "document", "audio", "voice", "video_note"):
        if getattr(msg, attr, None):
            return True
    return False


def _compute_option_counts(options_len: int, votes: Dict[str, List[int]]) -> List[int]:
    counts = [0] * options_len
    for chosen_list in (votes or {}).values():
        if not isinstance(chosen_list, list):
            continue
        for idx in chosen_list:
            if isinstance(idx, int) and 0 <= idx < options_len:
                counts[idx] += 1
    return counts


def build_post_buttons_markup(
    options: List[str],
    counts: Optional[List[int]] = None,
    show_counts: Optional[bool] = None,
    vertical: Optional[bool] = None,
) -> InlineKeyboardMarkup:
    settings = get_settings()
    if show_counts is None:
        show_counts = bool(settings.get("show_vote_counts", True))
    if vertical is None:
        vertical = bool(settings.get("vertical_buttons", False))

    buttons: List[InlineKeyboardButton] = []
    for idx, opt in enumerate(options):
        cnt = counts[idx] if (counts and idx < len(counts)) else 0
        label = f"{opt} ({cnt})" if (show_counts and cnt > 0) else opt
        buttons.append(InlineKeyboardButton(text=label[:64], callback_data=f"v:{idx}"))

    if not buttons:
        return InlineKeyboardMarkup([])

    if vertical:
        return InlineKeyboardMarkup([[b] for b in buttons])

    max_len = max(len(b.text) for b in buttons)
    if len(buttons) <= 4 and max_len <= 18:
        return InlineKeyboardMarkup([buttons])

    per_row = 2 if max_len <= 24 else 1
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup(rows)


async def safe_edit_post_with_buttons(
    bot,
    chat_id: int,
    message_id: int,
    cleaned_text: str,
    entities: List[MessageEntity],
    is_media: bool,
    markup: InlineKeyboardMarkup,
) -> str:
    """
    يعدّل نفس المنشور في مكانه لحذف الكلمة المفتاحية ووضع الأزرار أسفله (دون إعادة نشره أبداً).
    """
    global LAST_PROCESS_ERROR
    final_text = cleaned_text.strip()
    if not is_media and not final_text:
        final_text = "👇"
        entities = []

    clamped_entities = _clamp_entities(final_text, entities)

    for use_entities in (True, False):
        for _ in range(2):
            try:
                if is_media:
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        caption=final_text,
                        caption_entities=(clamped_entities if (use_entities and clamped_entities and final_text) else None),
                        reply_markup=markup,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=final_text,
                        entities=(clamped_entities if (use_entities and clamped_entities) else None),
                        reply_markup=markup,
                    )
                LAST_PROCESS_ERROR = ""
                return "ok"
            except RetryAfter as ra:
                await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            except BadRequest as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    LAST_PROCESS_ERROR = ""
                    return "ok"
                if use_entities and clamped_entities:
                    break
                LAST_PROCESS_ERROR = f"edit BadRequest: {e}"
                if "message to edit not found" in err or "message_id_invalid" in err:
                    return "not_found"
                if "message can't be edited" in err or "message cannot be edited" in err:
                    return "not_editable"
                logger.warning(f"⚠️ تعذر تعديل المنشور ({chat_id}, {message_id}): {e}")
                return "error"
            except Forbidden as e:
                LAST_PROCESS_ERROR = f"edit Forbidden: {e}"
                logger.warning(f"⛔ لا توجد صلاحية تعديل في ({chat_id}): {e}")
                return "forbidden"
            except TelegramError as e:
                LAST_PROCESS_ERROR = f"edit Error: {e}"
                logger.warning(f"⚠️ خطأ تيليجرام أثناء تعديل المنشور ({chat_id}, {message_id}): {e}")
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
            "can_post_messages": True,
            "can_edit_messages": True,
            "can_delete_messages": True,
            "last_known_msg_id": 0,
            "posts": {},
            "candidates": {},
        }
    else:
        if title:
            channels[ch_key]["title"] = title
        channels[ch_key].setdefault("can_post_messages", True)
        channels[ch_key].setdefault("can_edit_messages", True)
        channels[ch_key].setdefault("can_delete_messages", True)
        channels[ch_key].setdefault("last_known_msg_id", 0)
        channels[ch_key].setdefault("posts", {})
        channels[ch_key].setdefault("candidates", {})
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

    if str(STATE.get("default_channel_id")) == ch_key:
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


async def inspect_chat_membership(
    bot, chat_id: int
) -> Tuple[str, bool, bool, bool, bool, str, str, Optional[int], Optional[int]]:
    """
    يرجع status_state كأحد القيم:
    - "ok": البوت موجود في الدردشة
    - "removed": البوت غير موجود / مطرود نهائياً
    - "transient_error": خطأ مؤقت في الشبكة (لا نحذف القناة من s.json!)
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        status = getattr(member, "status", "")
        is_admin = status in ("administrator", "creator")
        is_active = status in ("administrator", "creator", "member", "restricted")
        if not is_active:
            return "removed", False, False, False, False, "", "", None, None

        if status == "creator":
            can_post = True
            can_edit = True
            can_delete = True
        elif status == "administrator":
            can_post = bool(getattr(member, "can_post_messages", True))
            can_edit = bool(getattr(member, "can_edit_messages", False))
            can_delete = bool(getattr(member, "can_delete_messages", False))
        else:
            can_post = False
            can_edit = False
            can_delete = False

        chat = await bot.get_chat(chat_id)
        chat_type = getattr(chat, "type", "")
        title = getattr(chat, "title", "") or str(chat_id)
        linked_id = getattr(chat, "linked_chat_id", None)
        pinned = getattr(chat, "pinned_message", None)
        pinned_id = getattr(pinned, "message_id", None) if pinned else None
        return "ok", is_admin, can_post, can_edit, can_delete, chat_type, title, linked_id, pinned_id
    except Forbidden:
        return "removed", False, False, False, False, "", "", None, None
    except BadRequest as e:
        err = str(e).lower()
        if any(x in err for x in ("chat not found", "bot is not a member", "user not found", "member not found", "channel_private", "chat_id_invalid")):
            return "removed", False, False, False, False, "", "", None, None
        return "transient_error", False, False, False, False, "", "", None, None
    except TelegramError:
        return "transient_error", False, False, False, False, "", "", None, None


async def sync_single_chat(bot, chat_id: int, auto_select: bool = False) -> Tuple[bool, str]:
    state_res, is_admin, can_post, can_edit, can_delete, ctype, title, linked_id, pinned_id = await inspect_chat_membership(bot, chat_id)
    if state_res == "transient_error":
        return False, "تعذر التحقق حالياً بسبب خطأ مؤقت في الاتصال (تم الاحتفاظ بالبيانات)."
    if state_res == "removed":
        remove_channel_data(chat_id)
        remove_group_data(chat_id)
        return False, "البوت غير موجود في هذه الدردشة. أضفه كمشرف أولاً."

    if ctype == "channel":
        if not is_admin:
            remove_channel_data(chat_id)
            return False, f"البوت في القناة '{title}' ليس مشرفاً! رقّهِ لمشرف مع صلاحية تعديل رسائل الآخرين."
        ch_data = get_or_init_channel(chat_id, title)
        ch_data["can_post_messages"] = can_post
        ch_data["can_edit_messages"] = can_edit
        ch_data["can_delete_messages"] = can_delete
        if pinned_id and pinned_id > int(ch_data.get("last_known_msg_id", 0) or 0):
            ch_data["last_known_msg_id"] = pinned_id

        if linked_id:
            g_res, _, _, _, _, gtype, gtitle, _, _ = await inspect_chat_membership(bot, linked_id)
            if g_res == "ok" and gtype in ("group", "supergroup"):
                ch_data["linked_group_id"] = linked_id
                ch_data["linked_group_title"] = gtitle
                get_or_init_group(linked_id, gtitle, linked_channel_id=chat_id)
            elif g_res == "removed":
                ch_data["linked_group_id"] = None
                ch_data["linked_group_title"] = None
        else:
            ch_data["linked_group_id"] = None
            ch_data["linked_group_title"] = None

        if auto_select or not STATE.get("default_channel_id"):
            STATE["default_channel_id"] = chat_id
            if ch_data.get("linked_group_id"):
                STATE["default_group_id"] = int(ch_data["linked_group_id"])

        return True, f"تم التعرف على القناة: {title}"

    if ctype in ("group", "supergroup"):
        get_or_init_group(chat_id, title, linked_channel_id=linked_id)
        if auto_select or not STATE.get("default_group_id"):
            STATE["default_group_id"] = chat_id

        extra_msg = ""
        if linked_id:
            c_res, c_admin, c_post, c_edit, c_del, _, ctitle, _, cpinned = await inspect_chat_membership(bot, linked_id)
            if c_res == "ok" and c_admin:
                ch_data = get_or_init_channel(linked_id, ctitle)
                ch_data["can_post_messages"] = c_post
                ch_data["can_edit_messages"] = c_edit
                ch_data["can_delete_messages"] = c_del
                ch_data["linked_group_id"] = chat_id
                ch_data["linked_group_title"] = title
                if cpinned and cpinned > int(ch_data.get("last_known_msg_id", 0) or 0):
                    ch_data["last_known_msg_id"] = cpinned
                if not STATE.get("default_channel_id"):
                    STATE["default_channel_id"] = linked_id
                extra_msg = f"\n📢 كما تم التعرف تلقائياً على القناة المرتبطة بها: {ctitle}"
        return True, f"تم التعرف على المجموعة: {title}{extra_msg}"

    return False, "نوع الدردشة غير مدعوم."


async def verify_all_registered_chats(bot):
    ch_ids = [int(k) for k in list(STATE.get("channels", {}).keys())]
    grp_ids = [int(k) for k in list(STATE.get("known_groups", {}).keys())]

    for cid in ch_ids:
        await sync_single_chat(bot, cid)
    for gid in grp_ids:
        await sync_single_chat(bot, gid)

    await save_local_now()


# ==================== معالجة منشورات الكلمة المفتاحية وإضافة الأزرار ====================
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
        "chat_shared",
    )
    return any(getattr(msg, attr, None) is not None for attr in service_attrs)


def _cache_channel_post_candidate(ch_data: Dict[str, Any], msg):
    raw_text = msg.text if msg.text is not None else (msg.caption or "")
    if not raw_text:
        return
    is_media = _is_media_message(msg)
    entities = msg.entities if msg.text is not None else msg.caption_entities
    _cache_channel_post_candidate_raw(
        ch_data,
        msg.message_id,
        raw_text,
        entities,
        is_media,
        is_album=bool(getattr(msg, "media_group_id", None)),
    )


def _cache_channel_post_candidate_raw(
    ch_data: Dict[str, Any],
    msg_id: int,
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    is_media: bool,
    is_album: bool = False,
):
    if not raw_text:
        return
    candidates = ch_data.setdefault("candidates", {})
    candidates[str(msg_id)] = {
        "text": raw_text,
        "entities": _serialize_entities(entities),
        "is_media": is_media,
        "is_album": is_album,
    }
    if len(candidates) > 100:
        sorted_keys = sorted(candidates.keys(), key=lambda x: int(x))
        for old_k in sorted_keys[:-100]:
            candidates.pop(old_k, None)


def _trim_channel_posts_history(ch_data: Dict[str, Any]):
    posts = ch_data.get("posts", {})
    if len(posts) > 500:
        sorted_keys = sorted(posts.keys(), key=lambda x: int(x))
        for old_k in sorted_keys[:-500]:
            posts.pop(old_k, None)


async def process_trigger_post(
    bot,
    channel_id: int,
    msg_id: int,
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    is_media: bool,
) -> str:
    global LAST_PROCESS_ERROR
    settings = get_settings()
    options = settings.get("button_options", [])
    if not options:
        LAST_PROCESS_ERROR = "لا توجد أزرار مضافة في الإعدادات."
        return "no_options"

    ch_data = get_or_init_channel(channel_id)
    posts = ch_data.setdefault("posts", {})
    m_key = str(msg_id)
    if m_key in posts:
        return "already"

    trigger = settings.get("trigger_word", DEFAULT_TRIGGER_WORD)
    case_sensitive = bool(settings.get("case_sensitive_trigger", False))

    matched, cleaned_text, adjusted_entities = extract_trigger_and_clean_text(
        raw_text=raw_text,
        entities=entities,
        trigger_word=trigger,
        case_sensitive=case_sensitive,
    )
    if not matched:
        return "no_trigger"

    markup = build_post_buttons_markup(options=options)

    # تعديل نفس المنشور في مكانه فقط (لا إعادة نشر أبداً)
    edit_status = await safe_edit_post_with_buttons(
        bot=bot,
        chat_id=channel_id,
        message_id=msg_id,
        cleaned_text=cleaned_text,
        entities=adjusted_entities,
        is_media=is_media,
        markup=markup,
    )

    if edit_status == "not_editable":
        LAST_PROCESS_ERROR = "هذا المنشور محوّل (Forwarded) وتيليجرام يمنع تعديل المنشورات المحوّلة."
        return "not_editable"

    if edit_status != "ok":
        return edit_status

    posts[m_key] = {
        "options": list(options),
        "votes": {},
        "created_at": int(time.time()),
    }
    ch_data.get("candidates", {}).pop(m_key, None)

    if int(ch_data.get("last_known_msg_id", 0) or 0) < msg_id:
        ch_data["last_known_msg_id"] = msg_id

    _trim_channel_posts_history(ch_data)
    await save_local_now()
    return "added"


async def _inspect_message_via_forward(bot, owner_id: int, channel_id: int, msg_id: int) -> Tuple[str, Optional[Any]]:
    for _ in range(3):
        try:
            fwd = await bot.forward_message(
                chat_id=owner_id,
                from_chat_id=channel_id,
                message_id=msg_id,
                disable_notification=True,
            )
            try:
                await bot.delete_message(chat_id=owner_id, message_id=fwd.message_id)
            except TelegramError:
                pass
            return "ok", fwd
        except RetryAfter as ra:
            await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
        except BadRequest as e:
            err = str(e).lower()
            if "protected content" in err or "can't be forwarded" in err:
                return "protected", None
            if "message to forward not found" in err or "message_id_invalid" in err or "message not found" in err:
                return "not_found", None
            return "error", None
        except Forbidden:
            return "error", None
        except TelegramError:
            return "error", None
    return "error", None


async def scan_all_channel_posts(bot, channel_id: int, owner_id: Optional[int] = None) -> Dict[str, Any]:
    """
    يفحص منشورات القناة (من الأحدث للأقدم + استكشاف أحدث المنشورات).
    يتخطى فقط المنشورات التي تم وضع الأزرار لها مسبقاً.
    """
    ch_data = get_or_init_channel(channel_id)
    last_id = int(ch_data.get("last_known_msg_id", 0) or 0)
    posts = ch_data.get("posts", {})
    candidates = ch_data.get("candidates", {})

    scanned_count = 0
    added_count = 0
    protected_hit = False

    if owner_id:
        probe_start = last_id + 1 if last_id > 0 else 1
        consecutive_missing = 0
        for probe_id in range(probe_start, probe_start + 50):
            st, fwd = await _inspect_message_via_forward(bot, owner_id, channel_id, probe_id)
            if st == "ok" and fwd:
                last_id = probe_id
                ch_data["last_known_msg_id"] = last_id
                consecutive_missing = 0
                if getattr(fwd, "poll", None) is None and not is_service_message(fwd):
                    raw_text = fwd.text if fwd.text is not None else (fwd.caption or "")
                    ents = fwd.entities if fwd.text is not None else fwd.caption_entities
                    _cache_channel_post_candidate_raw(
                        ch_data,
                        probe_id,
                        raw_text,
                        ents,
                        _is_media_message(fwd),
                        is_album=bool(getattr(fwd, "media_group_id", None)),
                    )
            elif st == "protected":
                protected_hit = True
                break
            elif st == "not_found":
                consecutive_missing += 1
                if consecutive_missing >= 15:
                    break
            await asyncio.sleep(0.12)

    if last_id <= 0:
        return {
            "last_id": 0,
            "scanned": 0,
            "added": 0,
            "protected": protected_hit,
        }

    for mid in range(last_id, 0, -1):
        m_key = str(mid)
        if m_key in posts:
            continue

        if owner_id and not protected_hit:
            st, fwd = await _inspect_message_via_forward(bot, owner_id, channel_id, mid)
            if st == "protected":
                protected_hit = True
            elif st == "not_found":
                continue
            elif st == "ok" and fwd:
                scanned_count += 1
                if getattr(fwd, "poll", None) is not None or is_service_message(fwd):
                    continue
                raw_text = fwd.text if fwd.text is not None else (fwd.caption or "")
                if not raw_text:
                    continue
                ents = fwd.entities if fwd.text is not None else fwd.caption_entities
                res = await process_trigger_post(
                    bot=bot,
                    channel_id=channel_id,
                    msg_id=mid,
                    raw_text=raw_text,
                    entities=ents,
                    is_media=_is_media_message(fwd),
                )
                if res == "added":
                    added_count += 1
                elif res in ("forbidden", "no_options"):
                    break
                await asyncio.sleep(0.15)
                continue

        cand = candidates.get(m_key)
        if cand:
            scanned_count += 1
            res = await process_trigger_post(
                bot=bot,
                channel_id=channel_id,
                msg_id=mid,
                raw_text=cand.get("text", ""),
                entities=_deserialize_entities(cand.get("entities")),
                is_media=bool(cand.get("is_media", False)),
            )
            if res == "added":
                added_count += 1
            elif res in ("forbidden", "no_options"):
                break
            await asyncio.sleep(0.12)

    await save_local_now()
    return {
        "last_id": last_id,
        "scanned": scanned_count,
        "added": added_count,
        "protected": protected_hit,
    }


# ==================== استخراج بيانات المنشور المحوّل / الروابط ====================
def extract_origin_from_forwarded_post(msg) -> Tuple[Optional[int], Optional[int], str]:
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None)
        origin_msg_id = getattr(origin, "message_id", None)
        if origin_chat and getattr(origin_chat, "type", "") in ("channel", "group", "supergroup"):
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

    m_priv_post = re.search(r"t\.me/c/(\d+)/(\d+)", text)
    if m_priv_post:
        return int(f"-100{m_priv_post.group(1)}"), int(m_priv_post.group(2))

    m_priv_only = re.search(r"t\.me/c/(\d+)(?:/|\?|$|\s)", text)
    if m_priv_only:
        return int(f"-100{m_priv_only.group(1)}"), None

    m_pub_post = re.search(r"t\.me/([A-Za-z0-9_]{4,})/(\d+)", text)
    if m_pub_post:
        return f"@{m_pub_post.group(1)}", int(m_pub_post.group(2))

    m_pub = re.search(r"t\.me/([A-Za-z0-9_]{4,})(?:/|\?|$|\s)", text)
    if m_pub and m_pub.group(1).lower() not in ("joinchat", "addstickers", "addemoji", "share"):
        return f"@{m_pub.group(1)}", None

    m_user = re.search(r"(?:^|\s)(@[A-Za-z0-9_]{4,})", text)
    if m_user:
        return m_user.group(1), None

    m_id = re.search(r"(?:^|\s)(-100\d{5,}|-\d{5,})(?:\s|$)", text)
    if m_id:
        return int(m_id.group(1)), None

    return None, None


# ==================== لوحة التحكم وإعدادات البوت للمالك ====================
def is_owner(user_id: int) -> bool:
    owner = STATE.get("owner_id")
    return owner is not None and int(owner) == int(user_id)


def _html_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_control_panel_text() -> str:
    settings = get_settings()
    is_running = bool(STATE.get("is_running", False))
    def_ch_id = STATE.get("default_channel_id")
    def_grp_id = STATE.get("default_group_id")

    channels = STATE.get("channels", {})
    groups = STATE.get("known_groups", {})

    status_line = "🟢 يعمل الآن (يراقب كل منشور جديد تلقائياً)" if is_running else "⏸ متوقف (بانتظار أمر البدء)"

    if def_ch_id and str(def_ch_id) in channels:
        ch_info = channels[str(def_ch_id)]
        ch_line = f"{_html_escape(ch_info.get('title', def_ch_id))} (<code>{def_ch_id}</code>)"
        if not ch_info.get("can_edit_messages", True):
            ch_line += "\n⚠️ <b>تحذير:</b> فعّل صلاحية <i>تعديل رسائل الآخرين</i> للبوت في هذه القناة!"
        last_mid = int(ch_info.get("last_known_msg_id", 0) or 0)
        last_mid_line = str(last_mid) if last_mid > 0 else "غير معروف بعد (أعد توجيه آخر منشور للبوت)"
    else:
        ch_line = "❌ لم تُحدد بعد"
        last_mid_line = "—"

    if def_grp_id and str(def_grp_id) in groups:
        grp_info = groups[str(def_grp_id)]
        grp_line = f"{_html_escape(grp_info.get('title', def_grp_id))} (<code>{def_grp_id}</code>)"
    else:
        grp_line = "بدون مجموعة افتراضية"

    options = settings.get("button_options", [])
    opts_preview = (
        "\n".join([f"   {i + 1}. {_html_escape(opt)}" for i, opt in enumerate(options)])
        if options
        else "   ❌ لا توجد أزرار مضافة!"
    )

    trig = _html_escape(settings.get("trigger_word", DEFAULT_TRIGGER_WORD))
    multi_label = "اختيار عدة أزرار معاً" if settings.get("allows_multiple_answers", False) else "اختيار زر واحد فقط"
    revote_label = "مسموح بتغيير/سحب التفاعل" if settings.get("allows_revoting", True) else "مقفل (لا يقبل التغيير)"

    if LAST_SEEN_CHANNEL_POST:
        seen_age = int(time.time() - LAST_SEEN_CHANNEL_POST["ts"])
        seen_line = (
            f"منشور #{LAST_SEEN_CHANNEL_POST['msg_id']} "
            f"(منذ {seen_age}ث): «{_html_escape(LAST_SEEN_CHANNEL_POST['preview'])}»"
        )
    else:
        seen_line = "لم يصل منشور قناة جديد بعد في هذه الجلسة"

    conflict_line = f"\n⚠️ <b>تحذير تعارض:</b> رُصد ({CONFLICT_COUNT}) تضارب مع نسخة أخرى للبوت!" if CONFLICT_COUNT > 0 else ""

    sync_age = (
        f"منذ {int((time.time() - LAST_GITHUB_SYNC_TS) // 60)} دقيقة"
        if LAST_GITHUB_SYNC_TS > 0
        else "لم تتم بعد"
    )

    return (
        "👋 <b>أهلاً بك في بوت الأزرار التفاعلية للمنشورات</b>\n"
        "👨‍💻 <b>المطور:</b> أحمد حسن عبد النبي\n\n"
        "🎛️ <b>لوحة تحكم البوت</b>\n\n"
        f"• <b>الحالة:</b> {status_line}\n"
        f"• <b>القناة الافتراضية:</b> {ch_line}\n"
        f"• <b>المجموعة الافتراضية:</b> {grp_line}\n"
        f"• <b>آخر رقم منشور معروف:</b> <code>{last_mid_line}</code>\n"
        f"• <b>آخر تحديث وصله البوت:</b> {seen_line}{conflict_line}\n"
        f"• <b>القنوات المكتشفة:</b> {len(channels)} | <b>المجموعات:</b> {len(groups)}\n\n"
        f"• <b>الكلمة المفتاحية:</b> <code>{trig}</code>\n"
        f"• <b>نمط التحديد:</b> {multi_label}\n"
        f"• <b>تغيير التفاعل:</b> {revote_label}\n"
        f"• <b>الأزرار الحالية ({len(options)}):</b>\n{opts_preview}\n\n"
        f"• <b>آخر مزامنة لـ s.json على GitHub:</b> {sync_age} (كل 9 دقائق تلقائياً)\n\n"
        "💡 <i>اضغط «📲 جلب قناة/مجموعة من قائمة تيليجرام» أو أعد توجيه منشور من قناتك هنا للتعرف عليها فوراً.</i>"
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
        [InlineKeyboardButton("🧩 إدارة الأزرار التفاعلية", callback_data="p:opts")],
        [InlineKeyboardButton("⚙️ إعدادات الكلمة المفتاحية والأزرار", callback_data="p:cfg")],
        [
            InlineKeyboardButton("📢 اختيار القناة الافتراضية", callback_data="p:chs"),
            InlineKeyboardButton("💬 اختيار المجموعة الافتراضية", callback_data="p:grps"),
        ],
        [InlineKeyboardButton("📲 جلب قناة/مجموعة من قائمة تيليجرام", callback_data="p:pick_chat")],
        [
            InlineKeyboardButton("🔍 فحص كل منشورات القناة", callback_data="p:scan_all"),
            InlineKeyboardButton("🩺 تشخيص الحالة والمنشورات", callback_data="p:diag"),
        ],
        [
            InlineKeyboardButton("🔄 تحديث القنوات والمجموعات", callback_data="p:refresh"),
            InlineKeyboardButton("💾 حفظ s.json على GitHub الآن", callback_data="p:sync"),
        ],
        [InlineKeyboardButton("❓ تعليمات الإعداد", callback_data="p:help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_options_menu_text() -> str:
    options = get_settings().get("button_options", [])
    lines = "\n".join([f"<b>{i + 1}.</b> {_html_escape(opt)}" for i, opt in enumerate(options)])
    if not lines:
        lines = "❌ لا توجد أزرار حالياً. أضف زراً واحداً على الأقل."
    return (
        "🧩 <b>إدارة الأزرار التفاعلية على المنشورات</b>\n\n"
        f"<b>الأزرار الحالية ({len(options)} / 12):</b>\n{lines}\n\n"
        "• يمكنك كتابة أي نص أو إيموجي لكل زر (حتى 60 حرفاً).\n"
        "• يمكنك إضافة زر واحد أو عدة أزرار (من 1 وحتى 12 زراً) تُوضع أسفل نفس المنشور مباشرة بدون أي سؤال."
    )


def build_options_menu_markup() -> InlineKeyboardMarkup:
    options = get_settings().get("button_options", [])
    rows: List[List[InlineKeyboardButton]] = []
    if len(options) < 12:
        rows.append([InlineKeyboardButton("➕ إضافة زر جديد (أو عدة أزرار)", callback_data="p:opt_add")])

    for idx, opt in enumerate(options):
        short_name = opt[:20] + ("…" if len(opt) > 20 else "")
        row: List[InlineKeyboardButton] = [
            InlineKeyboardButton(f"✏️ تعديل {idx + 1}: {short_name}", callback_data=f"p:opt_edit:{idx}"),
        ]
        if idx > 0:
            row.append(InlineKeyboardButton("⬆️", callback_data=f"p:opt_up:{idx}"))
        row.append(InlineKeyboardButton("🗑 حذف", callback_data=f"p:opt_del:{idx}"))
        rows.append(row)

    rows.append([
        InlineKeyboardButton("♻️ استعادة الافتراضي", callback_data="p:opt_reset"),
        InlineKeyboardButton("🧹 مسح الكل", callback_data="p:opt_clear"),
    ])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


def build_settings_menu_text() -> str:
    s = get_settings()
    trig = _html_escape(s.get("trigger_word", DEFAULT_TRIGGER_WORD))
    multi = "☑️ اختيار عدة أزرار معاً" if s.get("allows_multiple_answers", False) else "🔘 اختيار زر واحد فقط"
    revote = "🔓 مسموح بتغيير أو سحب التفاعل" if s.get("allows_revoting", True) else "🔒 مقفل (لا يمكن التغيير بعد الضغط)"
    counts = "🔢 يظهر عدد التفاعلات بجانب كل زر" if s.get("show_vote_counts", True) else "🙈 إخفاء عدد التفاعلات"
    layout = "⬇️ تحت بعض (عمودي)" if s.get("vertical_buttons", False) else "↔️ بجانب بعض تلقائياً (أفقي)"
    case_sens = "✅ حساس لحالة الأحرف" if s.get("case_sensitive_trigger", False) else "❌ غير حساس (A=a)"

    return (
        "⚙️ <b>إعدادات الكلمة المفتاحية والأزرار</b>\n\n"
        f"• <b>الكلمة المفتاحية:</b> <code>{trig}</code>\n"
        f"• <b>حساسية الأحرف:</b> {case_sens}\n"
        f"• <b>نمط التحديد:</b> {multi}\n"
        f"• <b>تغيير التفاعل:</b> {revote}\n"
        f"• <b>عداد الأصوات على الأزرار:</b> {counts}\n"
        f"• <b>ترتيب الأزرار:</b> {layout}"
    )


def build_settings_menu_markup() -> InlineKeyboardMarkup:
    s = get_settings()
    multi_btn = (
        "☑️ التحديد: عدة أزرار معاً"
        if s.get("allows_multiple_answers", False)
        else "🔘 التحديد: زر واحد فقط"
    )
    revote_btn = (
        "🔓 التغيير: مسموح بالتعديل/السحب"
        if s.get("allows_revoting", True)
        else "🔒 التغيير: مقفل (مرة واحدة)"
    )
    counts_btn = (
        "🔢 العداد: يظهر على الأزرار"
        if s.get("show_vote_counts", True)
        else "🙈 العداد: مخفي"
    )
    layout_btn = (
        "⬇️ الترتيب: أزرار تحت بعض"
        if s.get("vertical_buttons", False)
        else "↔️ الترتيب: بجانب بعض تلقائياً"
    )
    case_btn = "🔤 حساسية الأحرف: ✅" if s.get("case_sensitive_trigger", False) else "🔤 حساسية الأحرف: ❌"

    rows = [
        [InlineKeyboardButton(f"🏷️ تغيير الكلمة المفتاحية ({s.get('trigger_word', DEFAULT_TRIGGER_WORD)})", callback_data="p:cfg_trigger")],
        [InlineKeyboardButton(multi_btn, callback_data="p:cfg_tog:allows_multiple_answers")],
        [InlineKeyboardButton(revote_btn, callback_data="p:cfg_tog:allows_revoting")],
        [InlineKeyboardButton(counts_btn, callback_data="p:cfg_tog:show_vote_counts")],
        [InlineKeyboardButton(layout_btn, callback_data="p:cfg_tog:vertical_buttons")],
        [InlineKeyboardButton(case_btn, callback_data="p:cfg_tog:case_sensitive_trigger")],
        [InlineKeyboardButton("♻️ استعادة الإعدادات الافتراضية", callback_data="p:cfg_reset")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")],
    ]
    return InlineKeyboardMarkup(rows)


def build_channels_list_markup() -> InlineKeyboardMarkup:
    def_ch = STATE.get("default_channel_id")
    rows: List[List[InlineKeyboardButton]] = []
    for ch_key, ch_data in STATE.get("channels", {}).items():
        mark = "✅ " if str(def_ch) == ch_key else "📢 "
        warn = " ⚠️" if not ch_data.get("can_edit_messages", True) else ""
        title = (ch_data.get("title") or ch_key)[:30]
        rows.append([InlineKeyboardButton(f"{mark}{title}{warn}", callback_data=f"p:sch:{ch_key}")])
    rows.append([InlineKeyboardButton("📲 جلب قناة جديدة من قائمة تيليجرام", callback_data="p:pick_chat")])
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
    rows.append([InlineKeyboardButton("📲 جلب مجموعة جديدة من قائمة تيليجرام", callback_data="p:pick_chat")])
    rows.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


async def send_chat_picker_keyboard(target_msg):
    if KeyboardButtonRequestChat is None:
        await target_msg.reply_text(
            "📲 لإضافة القناة أو المجموعة:\n"
            "• أعد توجيه أي منشور من القناة إلى هنا،\n"
            "• أو أرسل معرف القناة <code>@username</code> أو رابطها أو رقمها <code>-100...</code>.",
            parse_mode="HTML",
        )
        return

    kb = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📢 اختيار قناة من قائمتك",
                    request_chat=KeyboardButtonRequestChat(request_id=101, chat_is_channel=True),
                ),
                KeyboardButton(
                    "💬 اختيار مجموعة من قائمتك",
                    request_chat=KeyboardButtonRequestChat(request_id=102, chat_is_channel=False),
                ),
            ],
            [KeyboardButton("❌ إغلاق القائمة")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await target_msg.reply_text(
        "📲 <b>اختر قناتك أو مجموعتك من الأزرار بالأسفل</b> (ستفتح لك قائمة تيليجرام مباشرة):\n\n"
        "💡 كما يمكنك أيضاً إعادة توجيه أي منشور من القناة هنا، أو إرسال معرفها <code>@username</code>.",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def send_or_edit_view(target_msg, text: str, markup: InlineKeyboardMarkup, edit: bool = False):
    if edit:
        try:
            await target_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest:
            pass
    await target_msg.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def send_or_edit_panel(target_msg, edit: bool = False):
    await send_or_edit_view(
        target_msg,
        build_control_panel_text(),
        build_control_panel_markup(),
        edit=edit,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return

    context.user_data.pop("pending_action", None)
    user_id = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = user_id
        await save_local_now()
        await sync_local_to_github_now()
    elif not is_owner(user_id):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.\n👨‍💻 المطور: أحمد حسن عبد النبي")
        return

    await verify_all_registered_chats(context.bot)
    await send_or_edit_panel(msg, edit=False)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return
    if not is_owner(msg.from_user.id):
        return
    context.user_data.pop("pending_action", None)
    await msg.reply_text("✅ تم إلغاء العملية الحالية.", reply_markup=ReplyKeyboardRemove())
    await send_or_edit_panel(msg, edit=False)


async def handle_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return
    if not is_owner(msg.from_user.id):
        return

    shared = getattr(msg, "chat_shared", None)
    if not shared:
        return

    shared_chat_id = shared.chat_id
    ok, info = await sync_single_chat(context.bot, shared_chat_id, auto_select=True)
    await save_local_now()
    await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}", reply_markup=ReplyKeyboardRemove())
    await send_or_edit_panel(msg, edit=False)


async def _handle_pending_input(msg, context: ContextTypes.DEFAULT_TYPE, pending: Dict[str, Any]) -> bool:
    text = (msg.text or "").strip()
    if text.lower() in ("/cancel", "الغاء", "إلغاء", "رجوع", "❌ إغلاق القائمة"):
        context.user_data.pop("pending_action", None)
        await msg.reply_text("✅ تم إلغاء التعديل.", reply_markup=ReplyKeyboardRemove())
        await send_or_edit_panel(msg, edit=False)
        return True

    if not text:
        await msg.reply_text("⚠️ أرسل نصاً صالحاً، أو أرسل /cancel للإلغاء.")
        return True

    settings = get_settings()
    ptype = pending.get("type")

    if ptype == "add_option":
        options = settings.setdefault("button_options", [])
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            await msg.reply_text("⚠️ لم يتم العثور على نص صالح. أعد المحاولة أو أرسل /cancel.")
            return True

        added = []
        skipped_full = 0
        for line in lines:
            if len(options) >= 12:
                skipped_full += 1
                continue
            opt_clean = line[:60]
            options.append(opt_clean)
            added.append(opt_clean)

        context.user_data.pop("pending_action", None)
        await save_local_now()
        note = f"✅ تمت إضافة {len(added)} زر بنجاح."
        if skipped_full:
            note += f"\n⚠️ تم تجاهل {skipped_full} لبلوغ الحد الأقصى (12 زراً)."
        await msg.reply_text(note)
        await send_or_edit_view(msg, build_options_menu_text(), build_options_menu_markup(), edit=False)
        return True

    if ptype == "edit_option":
        idx = int(pending.get("idx", -1))
        options = settings.setdefault("button_options", [])
        if not (0 <= idx < len(options)):
            context.user_data.pop("pending_action", None)
            await msg.reply_text("⚠️ هذا الزر لم يعد موجوداً.")
            await send_or_edit_view(msg, build_options_menu_text(), build_options_menu_markup(), edit=False)
            return True

        options[idx] = text[:60]
        context.user_data.pop("pending_action", None)
        await save_local_now()
        await msg.reply_text(f"✅ تم تعديل الزر رقم {idx + 1}.")
        await send_or_edit_view(msg, build_options_menu_text(), build_options_menu_markup(), edit=False)
        return True

    if ptype == "set_trigger":
        new_trig = text.split()[0].strip()
        if not new_trig:
            await msg.reply_text("⚠️ الكلمة المفتاحية لا يمكن أن تكون فارغة.")
            return True
        settings["trigger_word"] = new_trig[:64]
        context.user_data.pop("pending_action", None)
        await save_local_now()
        await msg.reply_text(f"✅ تم تغيير الكلمة المفتاحية إلى: {settings['trigger_word']}")
        await send_or_edit_view(msg, build_settings_menu_text(), build_settings_menu_markup(), edit=False)
        return True

    return False


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return

    user_id = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = user_id
        await save_local_now()
    elif not is_owner(user_id):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.\n👨‍💻 المطور: أحمد حسن عبد النبي")
        return

    if getattr(msg, "chat_shared", None) is not None:
        await handle_chat_shared(update, context)
        return

    pending = context.user_data.get("pending_action")
    if isinstance(pending, dict):
        handled = await _handle_pending_input(msg, context, pending)
        if handled:
            return

    fwd_chat_id, fwd_msg_id, _ = extract_origin_from_forwarded_post(msg)
    if fwd_chat_id:
        ok, info = await sync_single_chat(context.bot, fwd_chat_id, auto_select=True)
        if ok and fwd_msg_id and str(fwd_chat_id) in STATE.get("channels", {}):
            ch_data = STATE["channels"][str(fwd_chat_id)]
            if fwd_msg_id > int(ch_data.get("last_known_msg_id", 0) or 0):
                ch_data["last_known_msg_id"] = fwd_msg_id
            info += f"\n🔢 تم تسجيل رقم المنشور: {fwd_msg_id}"

            raw_fwd = msg.text if msg.text is not None else (msg.caption or "")
            if raw_fwd:
                fwd_ents = msg.entities if msg.text is not None else msg.caption_entities
                _cache_channel_post_candidate_raw(
                    ch_data,
                    fwd_msg_id,
                    raw_fwd,
                    fwd_ents,
                    _is_media_message(msg),
                    is_album=bool(getattr(msg, "media_group_id", None)),
                )

        await save_local_now()
        await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        if ok and STATE.get("is_running") and str(STATE.get("default_channel_id")) == str(fwd_chat_id) and fwd_msg_id:
            cand = STATE["channels"][str(fwd_chat_id)].get("candidates", {}).get(str(fwd_msg_id))
            if cand:
                res = await process_trigger_post(
                    bot=context.bot,
                    channel_id=fwd_chat_id,
                    msg_id=fwd_msg_id,
                    raw_text=cand.get("text", ""),
                    entities=_deserialize_entities(cand.get("entities")),
                    is_media=bool(cand.get("is_media", False)),
                )
                if res == "added":
                    await msg.reply_text("✅ تم تعديل المنشور وإضافة الأزرار عليه بنجاح.")
        await send_or_edit_panel(msg, edit=False)
        return

    text = (msg.text or "").strip()
    if not text:
        return

    if text == "❌ إغلاق القائمة":
        await msg.reply_text("✅ تم إغلاق القائمة.", reply_markup=ReplyKeyboardRemove())
        await send_or_edit_panel(msg, edit=False)
        return

    lower_text = text.lower()
    if lower_text in ("ابدأ", "ابدا", "تشغيل", "start", "/run"):
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await msg.reply_text("⚠️ حدد <b>القناة الافتراضية</b> أولاً ثم اضغط «ابدأ العمل».", parse_mode="HTML")
            await send_or_edit_panel(msg, edit=False)
            return
        await sync_single_chat(context.bot, int(def_ch))
        if not get_settings().get("button_options"):
            await msg.reply_text("⚠️ أضف زراً واحداً على الأقل من «🧩 إدارة الأزرار التفاعلية» أولاً.")
            await send_or_edit_panel(msg, edit=False)
            return

        STATE["is_running"] = True
        await save_local_now()
        await sync_local_to_github_now()
        trig = get_settings().get("trigger_word", DEFAULT_TRIGGER_WORD)
        await msg.reply_text(
            f"▶️ تم بدء المراقبة على القناة الافتراضية! أي منشور جديد يبدأ بـ {trig} سيتم تعديله ووضع الأزرار عليه تلقائياً.\n"
            "💡 لفحص كل المنشورات القديمة في القناة، اضغط زر «🔍 فحص كل منشورات القناة»."
        )
        await send_or_edit_panel(msg, edit=False)
        return

    if lower_text in ("ايقاف", "إيقاف", "توقف", "قف", "stop", "/stop"):
        STATE["is_running"] = False
        await save_local_now()
        await sync_local_to_github_now()
        await msg.reply_text("⏸ تم إيقاف المراقبة مؤقتاً وحفظ الحالة.")
        await send_or_edit_panel(msg, edit=False)
        return

    chat_ref, link_msg_id = parse_chat_or_link(text)
    if chat_ref is not None:
        try:
            chat_obj = await context.bot.get_chat(chat_ref)
            ok, info = await sync_single_chat(context.bot, chat_obj.id, auto_select=True)
            if ok and link_msg_id and str(chat_obj.id) in STATE.get("channels", {}):
                ch_data = STATE["channels"][str(chat_obj.id)]
                if link_msg_id > int(ch_data.get("last_known_msg_id", 0) or 0):
                    ch_data["last_known_msg_id"] = link_msg_id
                info += f"\n🔢 تم تسجيل رقم المنشور: {link_msg_id}"
            await save_local_now()
            await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        except Exception as e:
            await msg.reply_text(f"⚠️ تعذر الوصول لهذه الدردشة: {e}\nتأكد أن البوت مضاف إليها أولاً كمشرف.")
        await send_or_edit_panel(msg, edit=False)
        return

    await send_or_edit_panel(msg, edit=False)


async def run_diagnostic_report(bot, owner_id: int) -> str:
    """فحص وتشخيص حالة البوت والقناة وإمكانية قراءة وتعديل المنشورات"""
    me = await bot.get_me()
    wh = await bot.get_webhook_info()
    wh_url = getattr(wh, "url", "") or "غير مفعّل (Polling يعمل)"
    wh_err = getattr(wh, "last_error_message", "") or "لا يوجد"

    lines = [
        "🩺 <b>تقرير التشخيص الحي للبوت</b>",
        f"• <b>حساب البوت:</b> @{_html_escape(me.username)} (<code>{me.id}</code>)",
        f"• <b>الوضع الحالي:</b> {'🟢 يعمل' if STATE.get('is_running') else '⏸ متوقف'}",
        f"• <b>Webhook:</b> {_html_escape(wh_url)}",
        f"• <b>آخر خطأ Webhook:</b> {_html_escape(wh_err)}",
        f"• <b>تعارض نسخ (Conflict):</b> {CONFLICT_COUNT}",
        f"• <b>الكلمة المفتاحية:</b> <code>{_html_escape(get_settings().get('trigger_word'))}</code>",
    ]

    if LAST_SEEN_CHANNEL_POST:
        lines.append(
            f"• <b>آخر تحديث قناة التقطه البوت:</b> #{LAST_SEEN_CHANNEL_POST['msg_id']} "
            f"في <code>{LAST_SEEN_CHANNEL_POST['chat_id']}</code> "
            f"«{_html_escape(LAST_SEEN_CHANNEL_POST['preview'])}»"
        )
    else:
        lines.append("• <b>آخر تحديث قناة التقطه البوت:</b> لم يصل أي تحديث channel_post منذ بدء الجلسة الحالية")

    if LAST_PROCESS_ERROR:
        lines.append(f"• <b>آخر خطأ تنفيذ:</b> <code>{_html_escape(LAST_PROCESS_ERROR)}</code>")

    def_ch = STATE.get("default_channel_id")
    if not def_ch:
        lines.append("\n❌ <b>لا توجد قناة افتراضية محددة!</b>\nاضغط «📲 جلب قناة/مجموعة من قائمة تيليجرام» لتحديد قناتك.")
    else:
        state_res, is_admin, can_post, can_edit, can_delete, ctype, title, _, _ = await inspect_chat_membership(bot, int(def_ch))
        lines.append(f"\n📢 <b>القناة الافتراضية:</b> {_html_escape(title or str(def_ch))} (<code>{def_ch}</code>)")
        lines.append(f"• الحالة: {'✅ موجود' if state_res == 'ok' else '⚠️ ' + state_res} | مشرف: {'✅' if is_admin else '❌'}")
        lines.append(f"• تعديل رسائل الآخرين (للتعديل وإضافة الأزرار): {'✅' if can_edit else '❌'}")
        lines.append(f"• نشر: {'✅' if can_post else '❌'} | حذف: {'✅' if can_delete else '❌'}")

        ch_data = STATE.get("channels", {}).get(str(def_ch), {})
        last_id = int(ch_data.get("last_known_msg_id", 0) or 0)
        lines.append(f"• آخر رقم منشور مسجل: <code>{last_id}</code>")

        probe_start = max(1, last_id)
        found_id = 0
        found_preview = ""
        prot_note = ""
        for pid in range(probe_start, probe_start + 15):
            st, fwd = await _inspect_message_via_forward(bot, owner_id, int(def_ch), pid)
            if st == "ok" and fwd:
                found_id = pid
                txt = (fwd.text or fwd.caption or "[وسائط بدون نص]")
                found_preview = txt[:60]
            elif st == "protected":
                prot_note = "⚠️ حماية المحتوى مفعّلة في القناة."
                break
            await asyncio.sleep(0.1)

        if found_id > 0:
            if found_id > last_id:
                ch_data["last_known_msg_id"] = found_id
                await save_local_now()
            lines.append(f"• <b>اختبار قراءة أحدث منشور فعلي:</b> ✅ منشور #{found_id}: «{_html_escape(found_preview)}»")
        elif prot_note:
            lines.append(f"• {prot_note}")
        else:
            lines.append("• <b>اختبار القراءة:</b> تعذر العثور على منشورات حول الرقم المسجل. أعد توجيه آخر منشور من القناة إلى هنا.")

    return "\n".join(lines)


async def handle_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("p:"):
        return

    await query.answer()

    if not is_owner(query.from_user.id):
        return

    context.user_data.pop("pending_action", None)
    data = query.data
    parts = data.split(":")
    action = parts[1]
    settings = get_settings()

    if action == "home":
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "pick_chat":
        await send_chat_picker_keyboard(query.message)
        return

    if action == "diag":
        wait_msg = await query.message.reply_text("🩺 جاري فحص وتشخيص القناة والمنشورات...")
        report = await run_diagnostic_report(context.bot, query.from_user.id)
        await wait_msg.edit_text(report, parse_mode="HTML")
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "opts":
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_add":
        if len(settings.get("button_options", [])) >= 12:
            await query.message.reply_text("⚠️ وصلت للحد الأقصى (12 زراً). احذف زراً أو عدّل الموجود.")
            return
        context.user_data["pending_action"] = {"type": "add_option"}
        await query.message.reply_text(
            "➕ <b>إضافة زر جديد</b>\n\n"
            "أرسل نص الزر أو الإيموجي الذي تريده (حتى 60 حرفاً).\n"
            "💡 يمكنك إضافة عدة أزرار دفعة واحدة بحيث تضع كل زر في سطر مستقل.\n\n"
            "للتراجع أرسل /cancel",
            parse_mode="HTML",
        )
        return

    if action == "opt_edit" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("button_options", [])
        if not (0 <= idx < len(options)):
            await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
            return
        context.user_data["pending_action"] = {"type": "edit_option", "idx": idx}
        await query.message.reply_text(
            f"✏️ <b>تعديل الزر رقم {idx + 1}:</b> <code>{_html_escape(options[idx])}</code>\n\n"
            "أرسل الاسم الجديد الآن (حتى 60 حرفاً)، أو /cancel للإلغاء.",
            parse_mode="HTML",
        )
        return

    if action == "opt_up" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("button_options", [])
        if 1 <= idx < len(options):
            options[idx - 1], options[idx] = options[idx], options[idx - 1]
            await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_del" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("button_options", [])
        if 0 <= idx < len(options):
            options.pop(idx)
            await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_clear":
        settings["button_options"] = []
        await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_reset":
        settings["button_options"] = list(DEFAULT_BUTTON_OPTIONS)
        await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "cfg":
        await send_or_edit_view(query.message, build_settings_menu_text(), build_settings_menu_markup(), edit=True)
        return

    if action == "cfg_trigger":
        context.user_data["pending_action"] = {"type": "set_trigger"}
        await query.message.reply_text(
            f"🏷️ الكلمة المفتاحية الحالية: <code>{_html_escape(settings.get('trigger_word', DEFAULT_TRIGGER_WORD))}</code>\n\n"
            "أرسل الكلمة المفتاحية الجديدة التي يبدأ بها المنشور (مثال: <code>@like</code> أو <code>$bot</code>).\n"
            "للتراجع أرسل /cancel",
            parse_mode="HTML",
        )
        return

    if action == "cfg_tog" and len(parts) == 3:
        key = parts[2]
        allowed_bool_keys = {
            "allows_multiple_answers",
            "allows_revoting",
            "show_vote_counts",
            "vertical_buttons",
            "case_sensitive_trigger",
        }
        if key in allowed_bool_keys:
            settings[key] = not bool(settings.get(key, False))
            await save_local_now()
        await send_or_edit_view(query.message, build_settings_menu_text(), build_settings_menu_markup(), edit=True)
        return

    if action == "cfg_reset":
        STATE["settings"] = _default_settings()
        await save_local_now()
        await send_or_edit_view(query.message, build_settings_menu_text(), build_settings_menu_markup(), edit=True)
        return

    if action == "chs":
        if not STATE.get("channels"):
            await query.message.edit_text(
                "📢 <b>لا توجد قنوات مسجلة بعد.</b>\n\n"
                "للتعرف على قناتك فوراً (حتى لو كانت قديمة أو خاصة):\n"
                "• اضغط الزر بالأسفل <b>«📲 جلب قناة من قائمة تيليجرام»</b> واختر قناتك،\n"
                "• أو أعد توجيه أي منشور من القناة هنا، أو أرسل <code>@username</code>.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📲 جلب قناة من قائمة تيليجرام", callback_data="p:pick_chat")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="p:home")],
                ]),
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
            await query.message.reply_text("⚠️ القناة لم تعد متوفرة.")
            await send_or_edit_panel(query.message, edit=True)
            return
        STATE["default_channel_id"] = ch_id
        linked_grp = STATE["channels"][str(ch_id)].get("linked_group_id")
        if linked_grp and str(linked_grp) in STATE.get("known_groups", {}):
            STATE["default_group_id"] = int(linked_grp)
        await sync_single_chat(context.bot, ch_id)
        await save_local_now()
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "grps":
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
        else:
            STATE["default_group_id"] = int(val)
        await save_local_now()
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "toggle":
        if STATE.get("is_running"):
            STATE["is_running"] = False
            await save_local_now()
            await sync_local_to_github_now()
            await send_or_edit_panel(query.message, edit=True)
        else:
            def_ch = STATE.get("default_channel_id")
            if not def_ch or str(def_ch) not in STATE.get("channels", {}):
                await query.message.reply_text("⚠️ اختر القناة الافتراضية أولاً قبل البدء!")
                return
            if not settings.get("button_options"):
                await query.message.reply_text("⚠️ لا توجد أزرار! أضف زراً واحداً على الأقل من «🧩 إدارة الأزرار التفاعلية».")
                return
            await sync_single_chat(context.bot, int(def_ch))
            ch_info = STATE.get("channels", {}).get(str(def_ch), {})
            if not ch_info.get("can_edit_messages", True):
                await query.message.reply_text(
                    "⛔ البوت يحتاج صلاحية <b>(تعديل رسائل الآخرين)</b> في هذه القناة ليتمكن من التعديل ووضع الأزرار!",
                    parse_mode="HTML",
                )
                return
            STATE["is_running"] = True
            await save_local_now()
            await sync_local_to_github_now()
            await send_or_edit_panel(query.message, edit=True)
            trig = settings.get("trigger_word", DEFAULT_TRIGGER_WORD)
            await query.message.reply_text(
                f"▶️ تم تشغيل المراقبة الفورية! أي منشور جديد يبدأ بـ {trig} سيتم تعديله ووضع الأزرار عليه فوراً.\n"
                "💡 لفحص كل المنشورات السابقة في القناة يدوياً، اضغط زر «🔍 فحص كل منشورات القناة»."
            )
        return

    if action in ("scan_all", "scan10"):
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await query.message.reply_text("⚠️ اختر القناة الافتراضية أولاً.")
            return
        if not settings.get("button_options"):
            await query.message.reply_text("⚠️ أضف الأزرار أولاً من «🧩 إدارة الأزرار التفاعلية».")
            return

        status_msg = await query.message.reply_text("🔍 جاري فحص كل منشورات القناة، برجاء الانتظار...")
        res = await scan_all_channel_posts(
            context.bot,
            int(def_ch),
            owner_id=query.from_user.id,
        )
        trig = settings.get("trigger_word", DEFAULT_TRIGGER_WORD)
        if res["last_id"] == 0:
            await status_msg.edit_text(
                "⚠️ لم أجد أي منشورات معروفة في القناة حتى الآن.\n"
                "أعد توجيه آخر منشور من القناة إلى هنا ثم اضغط «🔍 فحص كل منشورات القناة»."
            )
        else:
            extra = (
                "\n⚠️ ملاحظة: القناة مفعّل فيها حماية المحتوى، لذا تم فحص المنشورات المخزنة فقط."
                if res["protected"]
                else ""
            )
            err_note = f"\n⚠️ آخر خطأ: {LAST_PROCESS_ERROR}" if LAST_PROCESS_ERROR else ""
            await status_msg.edit_text(
                f"✅ اكتمل فحص منشورات القناة (حتى منشور رقم {res['last_id']}).\n"
                f"• منشورات تم فحصها الآن: {res['scanned']}\n"
                f"• منشورات تبدأ بـ {trig} وتم تعديلها ووضع الأزرار عليها: {res['added']}{extra}{err_note}"
            )
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "refresh":
        await verify_all_registered_chats(context.bot)
        if not STATE.get("channels"):
            await send_chat_picker_keyboard(query.message)
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "sync":
        ok = await sync_local_to_github_now()
        await query.message.reply_text(
            "✅ تم رفع محتوى ملف s.json المحلي إلى GitHub بنجاح."
            if ok
            else "⚠️ تعذر المزامنة مع GitHub (تحقق من GITHUB_TOKEN)."
        )
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "help":
        trig = _html_escape(settings.get("trigger_word", DEFAULT_TRIGGER_WORD))
        help_text = (
            "❓ <b>تعليمات التشغيل:</b>\n\n"
            "1️⃣ في <b>القناة</b>: أضف البوت مشرفاً وفعّل له صلاحية <b>تعديل رسائل الآخرين</b>.\n"
            "2️⃣ لتعريف البوت بقناتك أو مجموعتك: اضغط <b>📲 جلب قناة/مجموعة من قائمة تيليجرام</b> واخترها مباشرة، أو أعد توجيه منشور منها هنا.\n"
            f"3️⃣ لأي منشور تريد وضع الأزرار عليه: ابدأ المنشور بـ <code>{trig}</code>.\n"
            "4️⃣ سيقوم البوت <b>بتعديل نفس المنشور</b> وحذف الكلمة المفتاحية ووضع الأزرار أسفله مباشرة (دون إعادة نشره باسم البوت ودون أي سؤال).\n"
            "5️⃣ عند الضغط على <b>▶️ ابدأ العمل</b> يراقب البوت كل منشور جديد تلقائياً."
        )
        await query.message.edit_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")]]),
            parse_mode="HTML",
        )


# ==================== معالج الضغط على أزرار المنشورات في القناة ====================
def _recover_options_from_message(msg) -> List[str]:
    rm = getattr(msg, "reply_markup", None)
    if not rm or not getattr(rm, "inline_keyboard", None):
        return list(get_settings().get("button_options", DEFAULT_BUTTON_OPTIONS))
    recovered: List[str] = []
    for row in rm.inline_keyboard:
        for btn in row:
            txt = (getattr(btn, "text", "") or "").strip()
            txt = re.sub(r"\s*\(\d+\)$", "", txt).strip()
            if txt:
                recovered.append(txt)
    return recovered or list(get_settings().get("button_options", DEFAULT_BUTTON_OPTIONS))


async def handle_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("v:"):
        return

    msg = query.message
    if not msg or not getattr(msg, "chat", None):
        await query.answer("⚠️ تعذر تحديد المنشور.", show_alert=False)
        return

    try:
        idx = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("⚠️ زر غير صالح.", show_alert=False)
        return

    channel_id = msg.chat.id
    msg_id = msg.message_id
    user_id = query.from_user.id
    user_key = str(user_id)

    settings = get_settings()
    allow_multi = bool(settings.get("allows_multiple_answers", False))
    allow_revote = bool(settings.get("allows_revoting", True))

    async with VOTE_LOCK:
        ch_data = get_or_init_channel(channel_id, getattr(msg.chat, "title", "") or "")
        posts = ch_data.setdefault("posts", {})
        m_key = str(msg_id)
        post_data = posts.get(m_key)

        if not isinstance(post_data, dict) or not post_data.get("options"):
            post_data = {
                "options": _recover_options_from_message(msg),
                "votes": {},
                "created_at": int(time.time()),
            }
            posts[m_key] = post_data

        options: List[str] = post_data["options"]
        if not (0 <= idx < len(options)):
            await query.answer("⚠️ هذا الاختيار لم يعد متاحاً.", show_alert=False)
            return

        votes: Dict[str, List[int]] = post_data.setdefault("votes", {})
        user_choices = list(votes.get(user_key, []))

        feedback = ""
        changed = False

        if allow_multi:
            if idx in user_choices:
                if not allow_revote:
                    await query.answer("🔒 لقد اخترت هذا الزر مسبقاً ولا يمكن تغييره.", show_alert=False)
                    return
                user_choices.remove(idx)
                if user_choices:
                    votes[user_key] = user_choices
                else:
                    votes.pop(user_key, None)
                feedback = f"↩️ تم إلغاء: {options[idx]}"
                changed = True
            else:
                user_choices.append(idx)
                votes[user_key] = user_choices
                feedback = f"✅ تم اختيار: {options[idx]}"
                changed = True
        else:
            if idx in user_choices:
                if not allow_revote:
                    await query.answer("🔒 لقد قمت بالتصويت مسبقاً ولا يمكن تغييره.", show_alert=False)
                    return
                votes.pop(user_key, None)
                feedback = "↩️ تم سحب تفاعلك."
                changed = True
            else:
                if user_choices and not allow_revote:
                    await query.answer("🔒 لقد قمت بالتصويت مسبقاً ولا يمكن تغييره.", show_alert=False)
                    return
                votes[user_key] = [idx]
                feedback = f"✅ تم اختيار: {options[idx]}"
                changed = True

        counts = _compute_option_counts(len(options), votes)
        new_markup = build_post_buttons_markup(options=options, counts=counts)

    await query.answer(feedback, show_alert=False)

    if changed:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=channel_id,
                message_id=msg_id,
                reply_markup=new_markup,
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"⚠️ تعذر تحديث أرقام الأزرار ({channel_id}, {msg_id}): {e}")
        except RetryAfter as ra:
            await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=channel_id,
                    message_id=msg_id,
                    reply_markup=new_markup,
                )
            except TelegramError:
                pass
        except TelegramError as e:
            logger.warning(f"⚠️ خطأ تحديث أزرار المنشور ({channel_id}, {msg_id}): {e}")

        await save_local_now()


# ==================== معالجات القنوات والمجموعات والتتبع التشخيصي ====================
async def track_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """متعقّب غير معوّق في group=-1 لتسجيل كل تحديث قناة يصل للبوت فعلياً"""
    global LAST_SEEN_CHANNEL_POST
    cp = getattr(update, "channel_post", None) or getattr(update, "edited_channel_post", None)
    if cp and getattr(cp, "chat", None):
        txt = (cp.text or cp.caption or "[وسائط]")[:45]
        LAST_SEEN_CHANNEL_POST = {
            "chat_id": cp.chat.id,
            "msg_id": cp.message_id,
            "preview": txt,
            "ts": time.time(),
        }
        logger.info(f"📩 وصل تحديث قناة: chat={cp.chat.id}, msg={cp.message_id}, text='{txt}'")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global CONFLICT_COUNT, CONFLICT_NOTIFIED, LAST_PROCESS_ERROR
    err = context.error
    if isinstance(err, Conflict):
        CONFLICT_COUNT += 1
        logger.error(f"🚨 تعارض (Conflict): توجد نسخة أخرى تعمل بنفس التوكن! ({CONFLICT_COUNT})")
        if not CONFLICT_NOTIFIED and STATE.get("owner_id"):
            CONFLICT_NOTIFIED = True
            try:
                await context.bot.send_message(
                    chat_id=int(STATE["owner_id"]),
                    text=(
                        "🚨 <b>تنبيه هام (سبب عدم رؤية المنشورات):</b>\n"
                        "توجد نسخة ثانية من البوت تعمل في نفس الوقت بنفس التوكن (Conflict)، "
                        "ولذلك تسرق التحديثات والمنشورات!\n"
                        "أوقف أي تشغيل قديم على GitHub Actions واترك نسخة واحدة فقط تعمل."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    LAST_PROCESS_ERROR = str(err)
    logger.exception("خطأ غير متوقع في معالج البوت:", exc_info=err)


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if not cmu:
        return

    chat = cmu.chat
    new_status = getattr(cmu.new_chat_member, "status", "")

    if chat.type == "channel":
        if new_status in ("administrator", "creator"):
            logger.info(f"📢 تم تفعيل البوت كمشرف في القناة: {chat.title} ({chat.id})")
            await sync_single_chat(context.bot, chat.id, auto_select=False)
        else:
            logger.info(f"🚪 خرج البوت أو سُحب إشرافه من القناة: {chat.title} ({chat.id})")
            remove_channel_data(chat.id)
        await save_local_now()

    elif chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator", "creator"):
            logger.info(f"💬 انضم البوت للمجموعة: {chat.title} ({chat.id})")
            await sync_single_chat(context.bot, chat.id, auto_select=False)
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

    if getattr(msg, "poll", None) is not None:
        return

    channel_id = msg.chat.id
    channel_title = getattr(msg.chat, "title", "") or "قناة"

    if str(channel_id) not in STATE.get("channels", {}):
        await sync_single_chat(context.bot, channel_id, auto_select=False)
    ch_data = get_or_init_channel(channel_id, channel_title)
    if msg.message_id > int(ch_data.get("last_known_msg_id", 0) or 0):
        ch_data["last_known_msg_id"] = msg.message_id

    _cache_channel_post_candidate(ch_data, msg)

    def_ch = STATE.get("default_channel_id")
    if not STATE.get("is_running") or str(def_ch) != str(channel_id):
        await save_local_now()
        return

    raw_text = msg.text if msg.text is not None else (msg.caption or "")
    if not raw_text:
        await save_local_now()
        return

    entities = msg.entities if msg.text is not None else msg.caption_entities
    is_media = _is_media_message(msg)
    mg_id = getattr(msg, "media_group_id", None)

    settings = get_settings()
    matched, _, _ = extract_trigger_and_clean_text(
        raw_text=raw_text,
        entities=entities,
        trigger_word=settings.get("trigger_word", DEFAULT_TRIGGER_WORD),
        case_sensitive=bool(settings.get("case_sensitive_trigger", False)),
    )
    if not matched:
        await save_local_now()
        return

    if mg_id:
        now = time.time()
        for k in [k for k, ts in SEEN_MEDIA_GROUPS.items() if now - ts > 600]:
            SEEN_MEDIA_GROUPS.pop(k, None)
        mg_key = f"{channel_id}:{mg_id}"
        if mg_key in SEEN_MEDIA_GROUPS:
            return
        SEEN_MEDIA_GROUPS[mg_key] = now

    res = await process_trigger_post(
        bot=context.bot,
        channel_id=channel_id,
        msg_id=msg.message_id,
        raw_text=raw_text,
        entities=entities,
        is_media=is_media,
    )

    if res in ("forbidden", "no_options", "error", "not_editable") and STATE.get("owner_id"):
        try:
            await context.bot.send_message(
                chat_id=int(STATE["owner_id"]),
                text=(
                    f"⚠️ تعذر تعديل المنشور #{msg.message_id} في «{channel_title}» لوضع الأزرار.\n"
                    f"السبب: {LAST_PROCESS_ERROR or res}"
                ),
            )
        except Exception:
            pass


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        return

    if str(msg.chat.id) not in STATE.get("known_groups", {}):
        await sync_single_chat(context.bot, msg.chat.id, auto_select=False)
        await save_local_now()


# ==================== حلقة المزامنة (كل 9 دقائق) وإعادة التشغيل الآمن ====================
async def background_loop(app):
    global LAST_GITHUB_SYNC_TS
    await asyncio.sleep(3)

    try:
        await verify_all_registered_chats(app.bot)
    except Exception as e:
        logger.warning(f"تنبيه أثناء الفحص الابتدائي: {e}")

    LAST_GITHUB_SYNC_TS = time.time()
    last_verify = time.time()

    while True:
        try:
            now = time.time()

            if now - START_TIME >= MAX_SERVER_LIFETIME:
                logger.info("⏳ انتهت مدة جلسة البوت الثاني، جاري حفظ s.json على GitHub والإغلاق الآمن...")
                await sync_local_to_github_now()
                os._exit(0)

            if now - LAST_GITHUB_SYNC_TS >= GITHUB_SYNC_INTERVAL:
                if STATE_DIRTY:
                    await sync_local_to_github_now()
                else:
                    LAST_GITHUB_SYNC_TS = now

            if now - last_verify >= 300:
                await verify_all_registered_chats(app.bot)
                last_verify = time.time()

            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"خطأ في الحلقة الخلفية: {e}")
            await asyncio.sleep(5)


async def on_post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logger.warning(f"تنبيه delete_webhook: {e}")
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم تشغيل البوت الثاني بنجاح (تعديل نفس المنشور + أزرار تفاعلية + جلب القنوات والمجموعات).")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT2_TOKEN)
        .concurrent_updates(8)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, track_any_update), group=-1)
    app.add_handler(CommandHandler(["start", "panel", "menu"], cmd_start))
    app.add_handler(CommandHandler(["cancel"], cmd_cancel))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    if getattr(filters.StatusUpdate, "CHAT_SHARED", None) is not None:
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.StatusUpdate.CHAT_SHARED, handle_chat_shared))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_group_message))
    app.add_handler(CallbackQueryHandler(handle_panel_callback, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(handle_vote_callback, pattern=r"^v:"))
    app.add_error_handler(global_error_handler)

    print("🚀 تم تشغيل البوت الثاني (bot2.py).")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
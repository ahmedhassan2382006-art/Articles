import os
import re
import sys
import json
import base64
import time
import inspect
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple, List

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaAnimation,
)
try:
    from telegram import ReplyParameters
except ImportError:
    ReplyParameters = None

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
# 4 ساعات و53 دقيقة: يغلق قبل وصول موعد تشغيل البوت القديم عند الساعة 5:00
MAX_SERVER_LIFETIME = (5 * 3600) - 420
GITHUB_SYNC_INTERVAL = 9 * 60  # حفظ محتوى الملف المحلي على GitHub كل 9 دقائق

STATE_LOCK = asyncio.Lock()
GITHUB_STATE_SHA: Optional[str] = None
STATE_DIRTY = False
LAST_GITHUB_SYNC_TS = 0.0

SEEN_MEDIA_GROUPS: Dict[str, float] = {}

DEFAULT_POLL_OPTIONS = ["👍 إعجاب", "👎 عدم إعجاب"]
DEFAULT_POLL_QUESTION = "📊 قيّم هذا المنشور:"
DEFAULT_TRIGGER_WORD = "@like"


def _github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Telegram-Vote-Tracker-Bot2",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _extract_telegram_bot_token(raw_text: str) -> Optional[str]:
    """استخراج توكن بوت تيليجرام صالح من النص"""
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
    """
    جلب توكن البوت بالترتيب التالي:
    1) من السكرت LIKE (أولوية أولى).
    2) من ملف t.txt على المستودع الخاص kys باستخدام نفس GITHUB_TOKEN الخاص بالحفظ.
    3) من ملف t.txt المحلي إذا وُجد.
    """
    like_secret = os.environ.get("LIKE", "").strip()
    if like_secret:
        tok = _extract_telegram_bot_token(like_secret)
        if tok:
            logger.info("🔑 تم قراءة توكن البوت من السكرت LIKE بنجاح.")
            return tok
        logger.warning("⚠️ المتغير LIKE موجود لكنه لا يحتوي على صيغة توكن تيليجرام صالحة، جاري المحاولة من المستودع الخاص kys...")

    if GITHUB_TOKEN:
        try:
            res = requests.get(GITHUB_TOKEN_FILE_URL, headers=_github_headers(), timeout=12)
            if res.status_code == 200:
                raw_b64 = res.json().get("content", "")
                content = base64.b64decode(raw_b64).decode("utf-8-sig")
                tok = _extract_telegram_bot_token(content)
                if tok:
                    logger.info(f"🔑 تم قراءة {TOKEN_FILE_PATH} من المستودع الخاص {TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME} بنجاح.")
                    return tok
                logger.warning(f"⚠️ الملف {TOKEN_FILE_PATH} في {TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME} لا يحتوي على توكن صالح.")
            else:
                logger.warning(
                    f"⚠️ تعذر قراءة {TOKEN_FILE_PATH} من {TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME} "
                    f"(كود: {res.status_code} - {res.text[:140]})"
                )
        except Exception as e:
            logger.error(f"خطأ أثناء جلب {TOKEN_FILE_PATH} من {TOKEN_REPO_OWNER}/{TOKEN_REPO_NAME}: {e}")
    else:
        logger.warning("⚠️ لم يتم العثور على GITHUB_TOKEN لقراءة المستودع الخاص kys.")

    if os.path.exists(TOKEN_FILE_PATH):
        try:
            with open(TOKEN_FILE_PATH, "r", encoding="utf-8-sig") as f:
                tok = _extract_telegram_bot_token(f.read())
                if tok:
                    logger.info(f"🔑 تم قراءة توكن البوت من الملف المحلي {TOKEN_FILE_PATH}.")
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
        "poll_question": DEFAULT_POLL_QUESTION,
        "poll_options": list(DEFAULT_POLL_OPTIONS),
        "allows_multiple_answers": True,
        "allows_revoting": False,
        "shuffle_options": False,
        "hide_results_until_closes": False,
        "delete_trigger_only_post": True,
        "use_post_text_as_question": False,
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
    for k, v in defaults.items():
        if k not in settings or settings[k] is None:
            settings[k] = list(v) if isinstance(v, list) else v

    if not isinstance(settings.get("poll_options"), list):
        settings["poll_options"] = list(DEFAULT_POLL_OPTIONS)
    settings["poll_options"] = [
        str(opt).strip()[:100]
        for opt in settings["poll_options"]
        if str(opt).strip()
    ][:12]

    trig = str(settings.get("trigger_word", DEFAULT_TRIGGER_WORD)).strip()
    settings["trigger_word"] = trig or DEFAULT_TRIGGER_WORD

    q_txt = str(settings.get("poll_question", DEFAULT_POLL_QUESTION)).strip()
    settings["poll_question"] = (q_txt or DEFAULT_POLL_QUESTION)[:300]

    cfg["settings"] = settings
    return cfg


def get_settings() -> Dict[str, Any]:
    return STATE.setdefault("settings", _default_settings())


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


# ==================== إرسال الاستفتاء الأصلي ومعالجة الكلمة المفتاحية ====================
def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def extract_trigger_and_clean_text(
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    trigger_word: str,
    case_sensitive: bool = False,
) -> Tuple[bool, str, List[MessageEntity]]:
    """
    يتحقق من وجود الكلمة المفتاحية في بداية النص:
    - يقبل أي حرف/علامة/إيموجي/مسافة/نص عربي بعدها مباشرة (مثل: @likeالسلام، @like. مرحبا).
    - الحالة الوحيدة المستثناة: إذا تلاها حرف إنجليزي مباشرة بدون فاصل (مثل: @likehello).
    - يحذف الكلمة المفتاحية والمسافات التي تليها مع إعادة ضبط مواضع التنسيقات بدقة.
    """
    if not raw_text or not trigger_word:
        return False, raw_text or "", list(entities or [])

    flags = 0 if case_sensitive else re.IGNORECASE
    leading_ws = r"[\s\u200b-\u200f\u202a-\u202e\ufeff]*"
    pattern = rf"^{leading_ws}{re.escape(trigger_word)}(?![A-Za-z]){leading_ws}"
    m = re.match(pattern, raw_text, flags=flags)
    if not m:
        return False, raw_text, list(entities or [])

    cut_char_idx = m.end()
    cleaned_text = raw_text[cut_char_idx:]
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


def _extract_poll_compatible_media(msg) -> Optional[Tuple[str, str]]:
    """استخراج نوع ومعرّف الوسائط المدعومة داخل رسالة الاستفتاء (InputPollMedia)"""
    if getattr(msg, "photo", None):
        return "photo", msg.photo[-1].file_id
    if getattr(msg, "video", None):
        return "video", msg.video.file_id
    if getattr(msg, "audio", None):
        return "audio", msg.audio.file_id
    if getattr(msg, "document", None):
        return "document", msg.document.file_id
    if getattr(msg, "animation", None):
        return "animation", msg.animation.file_id
    return None


def _is_media_message(msg) -> bool:
    media_attrs = (
        "photo",
        "video",
        "animation",
        "document",
        "audio",
        "voice",
        "video_note",
    )
    for attr in media_attrs:
        if getattr(msg, attr, None):
            return True
    return False


async def send_native_poll_safe(
    bot,
    chat_id: int,
    question: str,
    options: List[str],
    reply_to_message_id: Optional[int] = None,
    description: Optional[str] = None,
    description_entities: Optional[List[MessageEntity]] = None,
    media_info: Optional[Tuple[str, str]] = None,
    strict_combined: bool = False,
) -> Optional[Any]:
    """
    يرسل استفتاء تيليجرام الأصلي (مع إمكانية تضمين نص المنشور في description والوسائط في media
    لتكون في رسالة واحدة فقط عند الإمكان).
    """
    settings = get_settings()
    clean_options = [str(o).strip()[:100] for o in options if str(o).strip()][:12]
    if not clean_options:
        logger.warning("⚠️ لا توجد عناصر تصويت محددة في الإعدادات.")
        return None

    clean_question = (question or DEFAULT_POLL_QUESTION).strip()[:300]
    if not clean_question:
        clean_question = DEFAULT_POLL_QUESTION

    sig_params = inspect.signature(bot.send_poll).parameters
    kwargs: Dict[str, Any] = {
        "chat_id": chat_id,
        "question": clean_question,
        "options": clean_options,
        "is_anonymous": True,
        "type": "regular",
        "allows_multiple_answers": bool(settings.get("allows_multiple_answers", True)),
    }

    if reply_to_message_id:
        if "reply_parameters" in sig_params and ReplyParameters is not None:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        else:
            kwargs["reply_to_message_id"] = reply_to_message_id

    api_kwargs: Dict[str, Any] = {}

    # تضمين وصف المنشور (0-1024 حرف) داخل الاستفتاء نفسه
    if description:
        if "description" in sig_params:
            kwargs["description"] = description
            if description_entities:
                kwargs["description_entities"] = description_entities
        else:
            api_kwargs["description"] = description
            if description_entities:
                api_kwargs["description_entities"] = [e.to_dict() for e in description_entities]

    # تضمين الوسائط داخل الاستفتاء نفسه (Bot API 10.0+)
    if media_info:
        mtype, file_id = media_info
        media_cls_map = {
            "photo": InputMediaPhoto,
            "video": InputMediaVideo,
            "audio": InputMediaAudio,
            "document": InputMediaDocument,
            "animation": InputMediaAnimation,
        }
        if "media" in sig_params and mtype in media_cls_map:
            kwargs["media"] = media_cls_map[mtype](media=file_id)
        else:
            api_kwargs["media"] = {"type": mtype, "media": file_id}

    extra_96 = {
        "allows_revoting": bool(settings.get("allows_revoting", False)),
        "shuffle_options": bool(settings.get("shuffle_options", False)),
        "hide_results_until_closes": bool(settings.get("hide_results_until_closes", False)),
    }

    for k, v in extra_96.items():
        if k in sig_params:
            kwargs[k] = v
        elif k == "allows_revoting" or v:
            api_kwargs[k] = v

    if api_kwargs and "api_kwargs" in sig_params:
        kwargs["api_kwargs"] = api_kwargs

    for attempt in range(3):
        try:
            return await bot.send_poll(**kwargs)
        except RetryAfter as ra:
            await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
        except BadRequest as e:
            err = str(e).lower()
            if "reply" in err and ("reply_parameters" in kwargs or "reply_to_message_id" in kwargs):
                kwargs.pop("reply_parameters", None)
                kwargs.pop("reply_to_message_id", None)
                continue
            # في الوضع المدمج الصارم: إذا رُفض الوصف أو الوسائط لا نحذف المنشور بل نتراجع للوضع الاحتياطي
            if strict_combined and any(w in err for w in ("description", "media", "entity", "entities")):
                logger.warning(f"⚠️ تعذر إرسال الاستفتاء المدمج في ({chat_id}): {e}")
                return None
            if attempt == 0:
                for k in ("allows_revoting", "shuffle_options", "hide_results_until_closes"):
                    kwargs.pop(k, None)
                if "api_kwargs" in kwargs and isinstance(kwargs["api_kwargs"], dict):
                    for k in ("allows_revoting", "shuffle_options", "hide_results_until_closes"):
                        kwargs["api_kwargs"].pop(k, None)
                    if not kwargs["api_kwargs"]:
                        kwargs.pop("api_kwargs", None)
                continue
            logger.warning(f"⚠️ تعذر إرسال الاستفتاء في ({chat_id}): {e}")
            return None
        except Forbidden as e:
            logger.warning(f"⛔ لا توجد صلاحية نشر استفتاء في ({chat_id}): {e}")
            return None
        except TelegramError as e:
            logger.warning(f"⚠️ خطأ تيليجرام أثناء إرسال الاستفتاء ({chat_id}): {e}")
            return None
    return None


async def safe_edit_post_without_trigger(
    bot,
    chat_id: int,
    message_id: int,
    cleaned_text: str,
    entities: List[MessageEntity],
    is_media: bool,
) -> str:
    """
    يعدل نص المنشور أو وصف الوسائط لحذف الكلمة المفتاحية فقط.
    يرجع: 'ok' | 'not_modified' | 'not_found' | 'not_editable' | 'forbidden' | 'error'
    """
    for use_entities in (True, False):
        for _ in range(2):
            try:
                if is_media:
                    if cleaned_text:
                        await bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption=cleaned_text,
                            caption_entities=(entities if (use_entities and entities) else None),
                        )
                    else:
                        await bot.edit_message_caption(
                            chat_id=chat_id,
                            message_id=message_id,
                            caption="",
                        )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=cleaned_text,
                        entities=(entities if (use_entities and entities) else None),
                    )
                return "ok"
            except RetryAfter as ra:
                await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            except BadRequest as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return "not_modified"
                if use_entities and entities:
                    break
                if "message to edit not found" in err or "message_id_invalid" in err:
                    return "not_found"
                if "message can't be edited" in err or "message cannot be edited" in err:
                    return "not_editable"
                logger.warning(f"⚠️ تعذر تعديل المنشور ({chat_id}, {message_id}): {e}")
                return "error"
            except Forbidden as e:
                logger.warning(f"⛔ لا توجد صلاحية تعديل في ({chat_id}): {e}")
                return "forbidden"
            except TelegramError as e:
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
            "checked_ids": [],
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
        channels[ch_key].setdefault("checked_ids", [])
    return channels[ch_key]


def mark_msg_checked(ch_data: Dict[str, Any], msg_id: int):
    checked = ch_data.setdefault("checked_ids", [])
    if msg_id not in checked:
        checked.append(msg_id)
        if len(checked) > 1500:
            del checked[:len(checked) - 1500]


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


async def inspect_chat_membership(bot, chat_id: int) -> Tuple[bool, bool, bool, bool, bool, str, str, Optional[int], Optional[int]]:
    """
    يرجع: (is_active, is_admin, can_post_messages, can_edit_messages, can_delete_messages, chat_type, title, linked_chat_id, pinned_msg_id)
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        status = getattr(member, "status", "")
        is_admin = status in ("administrator", "creator")
        is_active = status in ("administrator", "creator", "member", "restricted")
        if not is_active:
            return False, False, False, False, False, "", "", None, None

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
        return True, is_admin, can_post, can_edit, can_delete, chat_type, title, linked_id, pinned_id
    except (BadRequest, Forbidden, TelegramError):
        return False, False, False, False, False, "", "", None, None


async def sync_single_chat(bot, chat_id: int) -> Tuple[bool, str]:
    """فحص وتحديث قناة أو مجموعة وإضافتها للذاكرة المحلية"""
    active, is_admin, can_post, can_edit, can_delete, ctype, title, linked_id, pinned_id = await inspect_chat_membership(bot, chat_id)
    if not active:
        remove_channel_data(chat_id)
        remove_group_data(chat_id)
        return False, "البوت غير موجود في هذه الدردشة."

    if ctype == "channel":
        if not is_admin:
            remove_channel_data(chat_id)
            return False, f"البوت في القناة '{title}' ليس مشرفاً!"
        ch_data = get_or_init_channel(chat_id, title)
        ch_data["can_post_messages"] = can_post
        ch_data["can_edit_messages"] = can_edit
        ch_data["can_delete_messages"] = can_delete
        if pinned_id and pinned_id > int(ch_data.get("last_known_msg_id", 0) or 0):
            ch_data["last_known_msg_id"] = pinned_id

        if linked_id:
            g_active, _, _, _, _, gtype, gtitle, _, _ = await inspect_chat_membership(bot, linked_id)
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
            c_active, c_admin, c_post, c_edit, c_del, _, ctitle, _, cpinned = await inspect_chat_membership(bot, linked_id)
            if c_active and c_admin:
                ch_data = get_or_init_channel(linked_id, ctitle)
                ch_data["can_post_messages"] = c_post
                ch_data["can_edit_messages"] = c_edit
                ch_data["can_delete_messages"] = c_del
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


# ==================== معالجة منشورات @like وتطبيق الاستفتاء ====================
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


def _cache_channel_post_candidate(ch_data: Dict[str, Any], msg):
    """يحفظ بيانات آخر المنشورات مؤقتاً للتعامل معها عند التشغيل أو الفحص اليدوي"""
    raw_text = msg.text if msg.text is not None else (msg.caption or "")
    if not raw_text:
        return
    is_media = _is_media_message(msg)
    media_info = _extract_poll_compatible_media(msg)
    entities = msg.entities if msg.text is not None else msg.caption_entities
    _cache_channel_post_candidate_raw(
        ch_data,
        msg.message_id,
        raw_text,
        entities,
        is_media,
        media_info=media_info,
        is_album=bool(getattr(msg, "media_group_id", None)),
    )


def _cache_channel_post_candidate_raw(
    ch_data: Dict[str, Any],
    msg_id: int,
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    is_media: bool,
    media_info: Optional[Tuple[str, str]] = None,
    is_album: bool = False,
):
    if not raw_text:
        return
    candidates = ch_data.setdefault("candidates", {})
    candidates[str(msg_id)] = {
        "text": raw_text,
        "entities": _serialize_entities(entities),
        "is_media": is_media,
        "media_info": list(media_info) if media_info else None,
        "is_album": is_album,
    }
    if len(candidates) > 60:
        sorted_keys = sorted(candidates.keys(), key=lambda x: int(x))
        for old_k in sorted_keys[:-60]:
            candidates.pop(old_k, None)


def _trim_channel_posts_history(ch_data: Dict[str, Any]):
    posts = ch_data.get("posts", {})
    if len(posts) > 300:
        sorted_keys = sorted(posts.keys(), key=lambda x: int(x))
        for old_k in sorted_keys[:-300]:
            posts.pop(old_k, None)


async def process_trigger_post(
    bot,
    channel_id: int,
    msg_id: int,
    raw_text: str,
    entities: Optional[Tuple[MessageEntity, ...] | List[MessageEntity]],
    is_media: bool,
    media_info: Optional[Tuple[str, str]] = None,
    is_album: bool = False,
) -> str:
    """
    يفحص المنشور: إذا بدأ بالكلمة المفتاحية (@like)، يحذفها ويرسل التصويت داخل نفس المنشور
    (عبر sendPoll مع description و media وحذف الأصل)، أو يتراجع للرد إذا لم يدعم الحجم/النوع.
    يرجع: 'added' | 'already' | 'no_trigger' | 'no_options' | 'forbidden' | 'error'
    """
    settings = get_settings()
    options = settings.get("poll_options", [])
    if not options:
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
        mark_msg_checked(ch_data, msg_id)
        return "no_trigger"

    cleaned_stripped = cleaned_text.strip()
    use_post_q = bool(settings.get("use_post_text_as_question", False))
    delete_trigger_only = bool(settings.get("delete_trigger_only_post", True))

    poll_question = settings.get("poll_question", DEFAULT_POLL_QUESTION)
    can_use_post_as_q = use_post_q and (1 <= len(cleaned_stripped) <= 300)

    can_delete = bool(ch_data.get("can_delete_messages", True))
    can_combine_text = len(cleaned_stripped) <= 1024
    can_combine_media = (not is_media) or (media_info is not None and not is_album)

    sent_poll_msg = None

    # === 1) الدمج في رسالة واحدة فقط (نفس المنشور نفسه) ===
    if can_delete and can_combine_text and can_combine_media:
        if not cleaned_stripped:
            # منشور يحتوي على @like فقط (مع أو بدون وسائط)
            q_val = poll_question
            desc_val = None
            desc_ents = None
        elif can_use_post_as_q:
            q_val = cleaned_stripped
            desc_val = None
            desc_ents = None
        else:
            q_val = poll_question
            desc_val = cleaned_stripped
            desc_ents = adjusted_entities

        sent_poll_msg = await send_native_poll_safe(
            bot=bot,
            chat_id=channel_id,
            question=q_val,
            options=options,
            reply_to_message_id=None,
            description=desc_val,
            description_entities=desc_ents,
            media_info=media_info,
            strict_combined=True,
        )

        if sent_poll_msg:
            try:
                await bot.delete_message(chat_id=channel_id, message_id=msg_id)
            except TelegramError as e:
                logger.warning(f"⚠️ تم إرسال المنشور المدمج لكن تعذر حذف الأصل ({msg_id}): {e}")
                ch_data["can_delete_messages"] = False

            poll_obj = getattr(sent_poll_msg, "poll", None)
            posts[m_key] = {
                "poll_msg_id": sent_poll_msg.message_id,
                "poll_id": getattr(poll_obj, "id", None) if poll_obj else None,
                "combined": True,
                "created_at": int(time.time()),
            }
            mark_msg_checked(ch_data, msg_id)
            mark_msg_checked(ch_data, sent_poll_msg.message_id)
            ch_data.get("candidates", {}).pop(m_key, None)

            if int(ch_data.get("last_known_msg_id", 0) or 0) < sent_poll_msg.message_id:
                ch_data["last_known_msg_id"] = sent_poll_msg.message_id

            _trim_channel_posts_history(ch_data)
            await save_local_now()
            return "added"

    # === 2) المسار الاحتياطي (إذا كان النص > 1024 حرفاً، أو صوتية/ألبوم، أو بدون صلاحية حذف) ===
    reply_target_id: Optional[int] = msg_id
    deleted_original = False

    if not is_media and not cleaned_stripped:
        if delete_trigger_only and can_delete:
            try:
                await bot.delete_message(chat_id=channel_id, message_id=msg_id)
                deleted_original = True
                reply_target_id = None
            except TelegramError:
                deleted_original = False

        if not deleted_original:
            await safe_edit_post_without_trigger(
                bot=bot,
                chat_id=channel_id,
                message_id=msg_id,
                cleaned_text=poll_question,
                entities=[],
                is_media=False,
            )
    else:
        edit_status = await safe_edit_post_without_trigger(
            bot=bot,
            chat_id=channel_id,
            message_id=msg_id,
            cleaned_text=(cleaned_text if is_media else cleaned_stripped),
            entities=adjusted_entities,
            is_media=is_media,
        )
        if edit_status == "forbidden":
            return "forbidden"
        if edit_status == "not_found":
            return "error"

    sent_poll_msg = await send_native_poll_safe(
        bot=bot,
        chat_id=channel_id,
        question=(cleaned_stripped if can_use_post_as_q else poll_question),
        options=options,
        reply_to_message_id=reply_target_id,
    )

    if not sent_poll_msg:
        return "error"

    poll_obj = getattr(sent_poll_msg, "poll", None)
    posts[m_key] = {
        "poll_msg_id": sent_poll_msg.message_id,
        "poll_id": getattr(poll_obj, "id", None) if poll_obj else None,
        "combined": False,
        "created_at": int(time.time()),
    }
    mark_msg_checked(ch_data, msg_id)
    mark_msg_checked(ch_data, sent_poll_msg.message_id)
    ch_data.get("candidates", {}).pop(m_key, None)

    if int(ch_data.get("last_known_msg_id", 0) or 0) < sent_poll_msg.message_id:
        ch_data["last_known_msg_id"] = sent_poll_msg.message_id

    _trim_channel_posts_history(ch_data)
    await save_local_now()
    return "added"


async def _inspect_message_via_forward(bot, owner_id: int, channel_id: int, msg_id: int) -> Tuple[str, Optional[Any]]:
    """
    جلب بيانات منشور قديم في القناة عبر توجيهه صامتاً للمالك ثم حذفه فوراً.
    يرجع: ('ok', Message) | ('not_found', None) | ('protected', None) | ('error', None)
    """
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
    فحص يدوي لجميع منشورات القناة (من 1 وحتى آخر منشور معروف + استكشاف أي منشورات أحدث).
    يفحص المنشورات المخزنة وكذلك المنشورات القديمة التي لم يرها البوت سابقاً.
    """
    ch_data = get_or_init_channel(channel_id)
    last_id = int(ch_data.get("last_known_msg_id", 0) or 0)
    candidates = ch_data.get("candidates", {})
    posts = ch_data.get("posts", {})
    checked_set = set(int(x) for x in ch_data.get("checked_ids", []))

    scanned_count = 0
    added_count = 0
    protected_hit = False

    # 1) استكشاف للأمام إذا كانت هناك منشورات أحدث من آخر رقم معروف
    if owner_id and last_id > 0:
        consecutive_missing = 0
        for probe_id in range(last_id + 1, last_id + 31):
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
                        media_info=_extract_poll_compatible_media(fwd),
                        is_album=bool(getattr(fwd, "media_group_id", None)),
                    )
                else:
                    mark_msg_checked(ch_data, probe_id)
                    checked_set.add(probe_id)
            elif st == "protected":
                protected_hit = True
                break
            elif st == "not_found":
                consecutive_missing += 1
                if consecutive_missing >= 5:
                    break
            await asyncio.sleep(0.15)

    if last_id <= 0:
        return {
            "last_id": 0,
            "scanned": 0,
            "added": 0,
            "protected": protected_hit,
        }

    # 2) فحص كل منشورات القناة من 1 إلى last_id (مع تخطي ما تم فحصه سابقاً)
    for mid in range(1, last_id + 1):
        m_key = str(mid)
        if m_key in posts or mid in checked_set:
            continue

        cand = candidates.get(m_key)
        if cand:
            scanned_count += 1
            media_list = cand.get("media_info")
            media_tuple = (media_list[0], media_list[1]) if isinstance(media_list, list) and len(media_list) == 2 else None
            res = await process_trigger_post(
                bot=bot,
                channel_id=channel_id,
                msg_id=mid,
                raw_text=cand.get("text", ""),
                entities=_deserialize_entities(cand.get("entities")),
                is_media=bool(cand.get("is_media", False)),
                media_info=media_tuple,
                is_album=bool(cand.get("is_album", False)),
            )
            if res == "added":
                added_count += 1
            elif res == "no_trigger":
                mark_msg_checked(ch_data, mid)
                checked_set.add(mid)
            elif res in ("forbidden", "no_options"):
                break
            await asyncio.sleep(0.15)
            continue

        if not owner_id or protected_hit:
            continue

        st, fwd = await _inspect_message_via_forward(bot, owner_id, channel_id, mid)
        if st == "protected":
            protected_hit = True
            continue
        if st == "not_found":
            mark_msg_checked(ch_data, mid)
            checked_set.add(mid)
            continue
        if st != "ok" or not fwd:
            continue

        scanned_count += 1
        if getattr(fwd, "poll", None) is not None or is_service_message(fwd):
            mark_msg_checked(ch_data, mid)
            checked_set.add(mid)
            continue

        raw_text = fwd.text if fwd.text is not None else (fwd.caption or "")
        if not raw_text:
            mark_msg_checked(ch_data, mid)
            checked_set.add(mid)
            continue

        ents = fwd.entities if fwd.text is not None else fwd.caption_entities
        res = await process_trigger_post(
            bot=bot,
            channel_id=channel_id,
            msg_id=mid,
            raw_text=raw_text,
            entities=ents,
            is_media=_is_media_message(fwd),
            media_info=_extract_poll_compatible_media(fwd),
            is_album=bool(getattr(fwd, "media_group_id", None)),
        )
        if res == "added":
            added_count += 1
        elif res == "no_trigger":
            mark_msg_checked(ch_data, mid)
            checked_set.add(mid)
        elif res in ("forbidden", "no_options"):
            break

        await asyncio.sleep(0.2)

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
    m_priv = re.search(r"t\.me/c/(\d+)/(\d+)", text)
    if m_priv:
        return int(f"-100{m_priv.group(1)}"), int(m_priv.group(2))

    m_pub_post = re.search(r"t\.me/([A-Za-z0-9_]{4,})/(\d+)", text)
    if m_pub_post:
        return f"@{m_pub_post.group(1)}", int(m_pub_post.group(2))

    m_pub = re.search(r"t\.me/([A-Za-z0-9_]{4,})$", text)
    if m_pub:
        return f"@{m_pub.group(1)}", None

    if text.startswith("@") and len(text) >= 5:
        return text.split()[0], None

    if re.fullmatch(r"-100\d{5,}", text) or re.fullmatch(r"-\d{5,}", text):
        return int(text), None

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
        warnings = []
        if not ch_info.get("can_post_messages", True):
            warnings.append("نشر الرسائل")
        if not ch_info.get("can_edit_messages", True):
            warnings.append("تعديل رسائل الآخرين")
        if not ch_info.get("can_delete_messages", True):
            warnings.append("حذف الرسائل")
        if warnings:
            ch_line += f"\n⚠️ <b>تحذير:</b> فعّل صلاحية <i>{' و '.join(warnings)}</i> للبوت في هذه القناة!"
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

    options = settings.get("poll_options", [])
    opts_preview = (
        "\n".join([f"   {i + 1}. {_html_escape(opt)}" for i, opt in enumerate(options)])
        if options
        else "   ❌ لا توجد عناصر مضافة!"
    )

    trig = _html_escape(settings.get("trigger_word", DEFAULT_TRIGGER_WORD))
    q_txt = _html_escape(settings.get("poll_question", DEFAULT_POLL_QUESTION))
    multi_label = "مربعات تحديد + زر تقديم التصويت التلقائي" if settings.get("allows_multiple_answers", True) else "اختيار واحد فوري"
    revote_label = "مسموح بتغيير التصويت" if settings.get("allows_revoting", False) else "مقفل (لا يقبل التصويت الثاني)"

    sync_age = (
        f"منذ {int((time.time() - LAST_GITHUB_SYNC_TS) // 60)} دقيقة"
        if LAST_GITHUB_SYNC_TS > 0
        else "لم تتم بعد"
    )

    return (
        "👋 <b>أهلاً بك في بوت الاستفتاء والتصويت</b>\n"
        "👨‍💻 <b>المطور:</b> أحمد حسن عبد النبي\n\n"
        "🎛️ <b>لوحة تحكم البوت</b>\n\n"
        f"• <b>الحالة:</b> {status_line}\n"
        f"• <b>القناة الافتراضية:</b> {ch_line}\n"
        f"• <b>المجموعة الافتراضية:</b> {grp_line}\n"
        f"• <b>آخر رقم منشور معروف:</b> <code>{last_mid_line}</code>\n"
        f"• <b>القنوات المكتشفة:</b> {len(channels)} | <b>المجموعات:</b> {len(groups)}\n\n"
        f"• <b>الكلمة المفتاحية في بداية المنشور:</b> <code>{trig}</code>\n"
        f"• <b>سؤال الاستفتاء:</b> {q_txt}\n"
        f"• <b>نمط التحديد:</b> {multi_label}\n"
        f"• <b>التصويت المتكرر:</b> {revote_label}\n"
        f"• <b>عناصر التصويت ({len(options)}):</b>\n{opts_preview}\n\n"
        f"• <b>آخر مزامنة لـ s.json على GitHub:</b> {sync_age} (كل 9 دقائق تلقائياً)\n\n"
        "💡 <i>اكتب «ابدأ» أو «إيقاف»، أو أعد توجيه أي منشور من قناتك هنا للتعرف عليها فوراً.</i>"
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
        [InlineKeyboardButton("🧩 إدارة عناصر التصويت", callback_data="p:opts")],
        [InlineKeyboardButton("⚙️ إعدادات البوت والاستفتاء", callback_data="p:cfg")],
        [
            InlineKeyboardButton("📢 اختيار القناة الافتراضية", callback_data="p:chs"),
            InlineKeyboardButton("💬 اختيار المجموعة الافتراضية", callback_data="p:grps"),
        ],
        [
            InlineKeyboardButton("🔍 فحص كل منشورات القناة", callback_data="p:scan_all"),
            InlineKeyboardButton("🔄 تحديث القنوات والمجموعات", callback_data="p:refresh"),
        ],
        [
            InlineKeyboardButton("💾 حفظ s.json على GitHub الآن", callback_data="p:sync"),
            InlineKeyboardButton("❓ تعليمات الإعداد", callback_data="p:help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_options_menu_text() -> str:
    options = get_settings().get("poll_options", [])
    lines = "\n".join([f"<b>{i + 1}.</b> {_html_escape(opt)}" for i, opt in enumerate(options)])
    if not lines:
        lines = "❌ لا توجد عناصر حالياً. أضف عنصراً واحداً على الأقل."
    return (
        "🧩 <b>إدارة عناصر التصويت (الاستفتاء)</b>\n\n"
        f"<b>العناصر الحالية ({len(options)} / 12):</b>\n{lines}\n\n"
        "• يمكنك كتابة أي اسم أو إيموجي لكل عنصر (حتى 100 حرف).\n"
        "• يمكنك إضافة أكثر من عنصرين (من 1 وحتى 12 عنصراً حسب حدود تيليجرام)."
    )


def build_options_menu_markup() -> InlineKeyboardMarkup:
    options = get_settings().get("poll_options", [])
    rows: List[List[InlineKeyboardButton]] = []
    if len(options) < 12:
        rows.append([InlineKeyboardButton("➕ إضافة عنصر جديد (أو عدة عناصر)", callback_data="p:opt_add")])

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
    q_txt = _html_escape(s.get("poll_question", DEFAULT_POLL_QUESTION))
    multi = "☑️ مربعات تحديد + زر تقديم التصويت التلقائي" if s.get("allows_multiple_answers", True) else "🔘 اختيار واحد فوري"
    revote = "🔓 مسموح بتغيير التصويت" if s.get("allows_revoting", False) else "🔒 مقفل (لا يقبل التصويت الثاني)"
    shuf = "✅ مفعّل" if s.get("shuffle_options", False) else "❌ معطّل"
    hide_res = "✅ مخفية حتى إغلاق الاستفتاء" if s.get("hide_results_until_closes", False) else "👁️ تظهر للشخص فور تصويته فقط"
    case_sens = "✅ حساس لحالة الأحرف" if s.get("case_sensitive_trigger", False) else "❌ غير حساس (A=a)"
    del_only = "✅ نعم (يحذف الرسالة ويرسل الاستفتاء)" if s.get("delete_trigger_only_post", True) else "❌ لا"
    use_post_q = "✅ نعم (إذا كان نصاً قصيراً ≤ 300 حرف)" if s.get("use_post_text_as_question", False) else "❌ لا (يستخدم سؤال الإعدادات + النص في الوصف)"

    return (
        "⚙️ <b>إعدادات البوت والاستفتاء الشاملة</b>\n\n"
        f"• <b>الكلمة المفتاحية:</b> <code>{trig}</code>\n"
        f"• <b>حساسية الأحرف:</b> {case_sens}\n"
        f"• <b>سؤال الاستفتاء:</b> {q_txt}\n"
        f"• <b>نمط التحديد:</b> {multi}\n"
        f"• <b>إعادة التصويت:</b> {revote}\n"
        f"• <b>ظهور عدد المصوتين:</b> {hide_res}\n"
        f"• <b>خلط ترتيب العناصر:</b> {shuf}\n"
        f"• <b>حذف المنشور إذا احتوى الكلمة فقط:</b> {del_only}\n"
        f"• <b>استخدام نص المنشور كسؤال:</b> {use_post_q}"
    )


def build_settings_menu_markup() -> InlineKeyboardMarkup:
    s = get_settings()
    multi_btn = (
        "☑️ التحديد: مربعات + زر تأكيد"
        if s.get("allows_multiple_answers", True)
        else "🔘 التحديد: اختيار واحد فوري"
    )
    revote_btn = (
        "🔓 التكرار: مسموح بتعديل الصوت"
        if s.get("allows_revoting", False)
        else "🔒 التكرار: مقفل (يُرفض الثاني)"
    )
    hide_btn = (
        "🙈 النتائج: مخفية حتى الإغلاق"
        if s.get("hide_results_until_closes", False)
        else "👁️ النتائج: تظهر للمصوّت فقط"
    )
    shuf_btn = "🔀 خلط الخيارات: ✅" if s.get("shuffle_options", False) else "🔀 خلط الخيارات: ❌"
    case_btn = "🔤 حساسية الأحرف: ✅" if s.get("case_sensitive_trigger", False) else "🔤 حساسية الأحرف: ❌"
    del_btn = "🧹 حذف المنشور الفارغ: ✅" if s.get("delete_trigger_only_post", True) else "🧹 حذف المنشور الفارغ: ❌"
    postq_btn = "📝 سؤال من نص المنشور: ✅" if s.get("use_post_text_as_question", False) else "📝 سؤال من نص المنشور: ❌"

    rows = [
        [InlineKeyboardButton(f"🏷️ تغيير الكلمة المفتاحية ({s.get('trigger_word', DEFAULT_TRIGGER_WORD)})", callback_data="p:cfg_trigger")],
        [InlineKeyboardButton("❓ تغيير سؤال الاستفتاء", callback_data="p:cfg_question")],
        [InlineKeyboardButton(multi_btn, callback_data="p:cfg_tog:allows_multiple_answers")],
        [InlineKeyboardButton(revote_btn, callback_data="p:cfg_tog:allows_revoting")],
        [InlineKeyboardButton(hide_btn, callback_data="p:cfg_tog:hide_results_until_closes")],
        [InlineKeyboardButton(shuf_btn, callback_data="p:cfg_tog:shuffle_options")],
        [InlineKeyboardButton(case_btn, callback_data="p:cfg_tog:case_sensitive_trigger")],
        [InlineKeyboardButton(del_btn, callback_data="p:cfg_tog:delete_trigger_only_post")],
        [InlineKeyboardButton(postq_btn, callback_data="p:cfg_tog:use_post_text_as_question")],
        [InlineKeyboardButton("♻️ استعادة كافة الإعدادات الافتراضية", callback_data="p:cfg_reset")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")],
    ]
    return InlineKeyboardMarkup(rows)


def build_channels_list_markup() -> InlineKeyboardMarkup:
    def_ch = STATE.get("default_channel_id")
    rows: List[List[InlineKeyboardButton]] = []
    for ch_key, ch_data in STATE.get("channels", {}).items():
        mark = "✅ " if str(def_ch) == ch_key else "📢 "
        warn = " ⚠️" if (
            not ch_data.get("can_edit_messages", True)
            or not ch_data.get("can_post_messages", True)
            or not ch_data.get("can_delete_messages", True)
        ) else ""
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
    if not msg or msg.chat.type != "private":
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
    if not msg or msg.chat.type != "private":
        return
    if not is_owner(msg.from_user.id):
        return
    context.user_data.pop("pending_action", None)
    await msg.reply_text("✅ تم إلغاء العملية الحالية.")
    await send_or_edit_panel(msg, edit=False)


async def _handle_pending_input(msg, context: ContextTypes.DEFAULT_TYPE, pending: Dict[str, Any]) -> bool:
    text = (msg.text or "").strip()
    if text.lower() in ("/cancel", "الغاء", "إلغاء", "رجوع"):
        context.user_data.pop("pending_action", None)
        await msg.reply_text("✅ تم إلغاء التعديل.")
        await send_or_edit_panel(msg, edit=False)
        return True

    if not text:
        await msg.reply_text("⚠️ أرسل نصاً صالحاً، أو أرسل /cancel للإلغاء.")
        return True

    settings = get_settings()
    ptype = pending.get("type")

    if ptype == "add_option":
        options = settings.setdefault("poll_options", [])
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
            opt_clean = line[:100]
            options.append(opt_clean)
            added.append(opt_clean)

        context.user_data.pop("pending_action", None)
        await save_local_now()
        note = f"✅ تمت إضافة {len(added)} عنصر بنجاح."
        if skipped_full:
            note += f"\n⚠️ تم تجاهل {skipped_full} لبلوغ الحد الأقصى (12 عنصراً)."
        await msg.reply_text(note)
        await send_or_edit_view(msg, build_options_menu_text(), build_options_menu_markup(), edit=False)
        return True

    if ptype == "edit_option":
        idx = int(pending.get("idx", -1))
        options = settings.setdefault("poll_options", [])
        if not (0 <= idx < len(options)):
            context.user_data.pop("pending_action", None)
            await msg.reply_text("⚠️ هذا العنصر لم يعد موجوداً.")
            await send_or_edit_view(msg, build_options_menu_text(), build_options_menu_markup(), edit=False)
            return True

        options[idx] = text[:100]
        context.user_data.pop("pending_action", None)
        await save_local_now()
        await msg.reply_text(f"✅ تم تعديل العنصر رقم {idx + 1}.")
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

    if ptype == "set_question":
        settings["poll_question"] = text[:300]
        context.user_data.pop("pending_action", None)
        await save_local_now()
        await msg.reply_text("✅ تم تحديث سؤال الاستفتاء بنجاح.")
        await send_or_edit_view(msg, build_settings_menu_text(), build_settings_menu_markup(), edit=False)
        return True

    return False


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private":
        return

    user_id = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = user_id
        await save_local_now()
    elif not is_owner(user_id):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.\n👨‍💻 المطور: أحمد حسن عبد النبي")
        return

    pending = context.user_data.get("pending_action")
    if isinstance(pending, dict):
        handled = await _handle_pending_input(msg, context, pending)
        if handled:
            return

    fwd_chat_id, fwd_msg_id, _ = extract_origin_from_forwarded_post(msg)
    if fwd_chat_id:
        ok, info = await sync_single_chat(context.bot, fwd_chat_id)
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
                    media_info=_extract_poll_compatible_media(msg),
                    is_album=bool(getattr(msg, "media_group_id", None)),
                )

        await save_local_now()
        await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        if ok and STATE.get("is_running") and STATE.get("default_channel_id") == fwd_chat_id and fwd_msg_id:
            cand = STATE["channels"][str(fwd_chat_id)].get("candidates", {}).get(str(fwd_msg_id))
            if cand:
                m_list = cand.get("media_info")
                m_tuple = (m_list[0], m_list[1]) if isinstance(m_list, list) and len(m_list) == 2 else None
                res = await process_trigger_post(
                    bot=context.bot,
                    channel_id=fwd_chat_id,
                    msg_id=fwd_msg_id,
                    raw_text=cand.get("text", ""),
                    entities=_deserialize_entities(cand.get("entities")),
                    is_media=bool(cand.get("is_media", False)),
                    media_info=m_tuple,
                    is_album=bool(cand.get("is_album", False)),
                )
                if res == "added":
                    await msg.reply_text("🗳️ تم تحويل المنشور المحوّل إلى منشور تصويت بنجاح.")
        await send_or_edit_panel(msg, edit=False)
        return

    text = (msg.text or "").strip()
    if not text:
        return

    lower_text = text.lower()
    if lower_text in ("ابدأ", "ابدا", "تشغيل", "start", "/run"):
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await msg.reply_text("⚠️ حدد <b>القناة الافتراضية</b> أولاً من القائمة ثم اضغط «ابدأ العمل».", parse_mode="HTML")
            await send_or_edit_panel(msg, edit=False)
            return
        if not get_settings().get("poll_options"):
            await msg.reply_text("⚠️ أضف عنصر تصويت واحداً على الأقل من «🧩 إدارة عناصر التصويت» أولاً.")
            await send_or_edit_panel(msg, edit=False)
            return

        STATE["is_running"] = True
        await save_local_now()
        await sync_local_to_github_now()
        trig = get_settings().get("trigger_word", DEFAULT_TRIGGER_WORD)
        await msg.reply_text(
            f"▶️ تم بدء المراقبة على القناة الافتراضية! أي منشور جديد أوله {trig} سيتم تحويله للتصويت تلقائياً.\n"
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
            ok, info = await sync_single_chat(context.bot, chat_obj.id)
            if ok and link_msg_id and str(chat_obj.id) in STATE.get("channels", {}):
                ch_data = STATE["channels"][str(chat_obj.id)]
                if link_msg_id > int(ch_data.get("last_known_msg_id", 0) or 0):
                    ch_data["last_known_msg_id"] = link_msg_id
                info += f"\n🔢 تم تسجيل رقم المنشور: {link_msg_id}"
            await save_local_now()
            await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        except Exception as e:
            await msg.reply_text(f"⚠️ تعذر الوصول لهذه الدردشة: {e}\nتأكد أن البوت مضاف إليها أولاً.")
        await send_or_edit_panel(msg, edit=False)
        return

    await send_or_edit_panel(msg, edit=False)


async def handle_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("p:"):
        return

    # إغلاق استعلام الزر بصمت تام بدون أي رسائل منبثقة نهائياً
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

    if action == "opts":
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_add":
        if len(settings.get("poll_options", [])) >= 12:
            await query.message.reply_text("⚠️ وصلت للحد الأقصى (12 عنصراً). احذف عنصراً أو عدّل الموجود.")
            return
        context.user_data["pending_action"] = {"type": "add_option"}
        await query.message.reply_text(
            "➕ <b>إضافة عنصر تصويت جديد</b>\n\n"
            "أرسل اسم العنصر الذي تريده (بإيموجي أو بدون، حتى 100 حرف).\n"
            "💡 يمكنك إضافة عدة عناصر دفعة واحدة بحيث تضع كل عنصر في سطر مستقل.\n\n"
            "للتراجع أرسل /cancel",
            parse_mode="HTML",
        )
        return

    if action == "opt_edit" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("poll_options", [])
        if not (0 <= idx < len(options)):
            await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
            return
        context.user_data["pending_action"] = {"type": "edit_option", "idx": idx}
        await query.message.reply_text(
            f"✏️ <b>تعديل العنصر رقم {idx + 1}:</b> <code>{_html_escape(options[idx])}</code>\n\n"
            "أرسل الاسم الجديد الآن (حتى 100 حرف)، أو /cancel للإلغاء.",
            parse_mode="HTML",
        )
        return

    if action == "opt_up" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("poll_options", [])
        if 1 <= idx < len(options):
            options[idx - 1], options[idx] = options[idx], options[idx - 1]
            await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_del" and len(parts) == 3:
        idx = int(parts[2])
        options = settings.get("poll_options", [])
        if 0 <= idx < len(options):
            options.pop(idx)
            await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_clear":
        settings["poll_options"] = []
        await save_local_now()
        await send_or_edit_view(query.message, build_options_menu_text(), build_options_menu_markup(), edit=True)
        return

    if action == "opt_reset":
        settings["poll_options"] = list(DEFAULT_POLL_OPTIONS)
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
            "أرسل الكلمة المفتاحية الجديدة التي يبدأ بها المنشور (مثال: <code>@like</code> أو <code>#تصويت</code>).\n"
            "للتراجع أرسل /cancel",
            parse_mode="HTML",
        )
        return

    if action == "cfg_question":
        context.user_data["pending_action"] = {"type": "set_question"}
        await query.message.reply_text(
            f"❓ سؤال الاستفتاء الحالي:\n<code>{_html_escape(settings.get('poll_question', DEFAULT_POLL_QUESTION))}</code>\n\n"
            "أرسل السؤال الجديد (حتى 300 حرف)، أو /cancel للإلغاء.",
            parse_mode="HTML",
        )
        return

    if action == "cfg_tog" and len(parts) == 3:
        key = parts[2]
        allowed_bool_keys = {
            "allows_multiple_answers",
            "allows_revoting",
            "hide_results_until_closes",
            "shuffle_options",
            "case_sensitive_trigger",
            "delete_trigger_only_post",
            "use_post_text_as_question",
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
                "📢 <b>لا توجد قنوات مكتشفة حالياً.</b>\n\n"
                "1️⃣ أضف البوت كمشرف في قناتك مع صلاحية (نشر الرسائل + تعديل رسائل الآخرين + حذف الرسائل).\n"
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
            await query.message.reply_text("⚠️ القناة لم تعد متوفرة.")
            await send_or_edit_panel(query.message, edit=True)
            return
        STATE["default_channel_id"] = ch_id
        linked_grp = STATE["channels"][str(ch_id)].get("linked_group_id")
        if linked_grp and str(linked_grp) in STATE.get("known_groups", {}):
            STATE["default_group_id"] = int(linked_grp)
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
            if not settings.get("poll_options"):
                await query.message.reply_text("⚠️ لا توجد عناصر تصويت! أضف عنصراً واحداً على الأقل من «🧩 إدارة عناصر التصويت».")
                return
            ch_info = STATE["channels"][str(def_ch)]
            if not ch_info.get("can_post_messages", True) or not ch_info.get("can_edit_messages", True):
                await query.message.reply_text(
                    "⛔ البوت يحتاج صلاحيتي (نشر الرسائل) و(تعديل رسائل الآخرين) في هذه القناة!"
                )
                return
            STATE["is_running"] = True
            await save_local_now()
            await sync_local_to_github_now()
            await send_or_edit_panel(query.message, edit=True)
            trig = settings.get("trigger_word", DEFAULT_TRIGGER_WORD)
            await query.message.reply_text(
                f"▶️ تم تشغيل المراقبة الفورية! أي منشور جديد يبدأ بـ {trig} سيتم تحويله للتصويت فوراً.\n"
                "💡 لفحص المنشورات السابقة في القناة يدوياً، اضغط زر «🔍 فحص كل منشورات القناة»."
            )
        return

    if action in ("scan_all", "scan10"):
        def_ch = STATE.get("default_channel_id")
        if not def_ch or str(def_ch) not in STATE.get("channels", {}):
            await query.message.reply_text("⚠️ اختر القناة الافتراضية أولاً.")
            return
        if not settings.get("poll_options"):
            await query.message.reply_text("⚠️ أضف عناصر التصويت أولاً من «🧩 إدارة عناصر التصويت».")
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
                "⚠️ لا يوجد رقم منشور معروف بعد لهذه القناة.\n"
                "أعد توجيه آخر منشور من القناة هنا حتى يتعرف البوت على عدد منشوراتها ثم اضغط فحص."
            )
        else:
            extra = (
                "\n⚠️ ملاحظة: القناة مفعّل فيها حماية المحتوى، لذا تم فحص المنشورات التي وصلت للبوت فقط."
                if res["protected"]
                else ""
            )
            await status_msg.edit_text(
                f"✅ اكتمل فحص كل منشورات القناة (حتى منشور رقم {res['last_id']}).\n"
                f"• منشورات جديدة تم فحصها الآن: {res['scanned']}\n"
                f"• منشورات تبدأ بـ {trig} وتمت معالجتها: {res['added']}{extra}"
            )
        await send_or_edit_panel(query.message, edit=True)
        return

    if action == "refresh":
        await verify_all_registered_chats(context.bot)
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
            "1️⃣ في <b>القناة</b>: أضف البوت مشرفاً وفعّل له صلاحيات <b>نشر الرسائل</b> و<b>تعديل رسائل الآخرين</b> و<b>حذف الرسائل</b>.\n"
            f"2️⃣ لأي منشور تريد إضافة التصويت له: ابدأ المنشور بـ <code>{trig}</code> (سواءً تلاها مسافة أو علامة ترقيم أو كلام عربي مباشرة، ويُستثنى فقط الحروف الإنجليزية المتصلة مثل @likehello).\n"
            "3️⃣ سيقوم البوت بحذف الكلمة المفتاحية ووضع المنشور والتصويت في <b>رسالة واحدة</b>.\n"
            "4️⃣ عند الضغط على <b>▶️ ابدأ العمل</b> يراقب البوت كل منشور جديد تلقائياً، وللفحص اليدوي لكل منشورات القناة اضغط <b>🔍 فحص كل منشورات القناة</b>."
        )
        await query.message.edit_text(
            help_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="p:home")]]),
            parse_mode="HTML",
        )


# ==================== معالجات القنوات والمجموعات ====================
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

    if getattr(msg, "poll", None) is not None:
        return

    channel_id = msg.chat.id
    channel_title = getattr(msg.chat, "title", "") or "قناة"

    if str(channel_id) not in STATE.get("channels", {}):
        await sync_single_chat(context.bot, channel_id)
    ch_data = get_or_init_channel(channel_id, channel_title)
    if msg.message_id > int(ch_data.get("last_known_msg_id", 0) or 0):
        ch_data["last_known_msg_id"] = msg.message_id

    _cache_channel_post_candidate(ch_data, msg)

    if not STATE.get("is_running") or STATE.get("default_channel_id") != channel_id:
        await save_local_now()
        return

    raw_text = msg.text if msg.text is not None else (msg.caption or "")
    if not raw_text:
        mark_msg_checked(ch_data, msg.message_id)
        await save_local_now()
        return

    entities = msg.entities if msg.text is not None else msg.caption_entities
    is_media = _is_media_message(msg)
    media_info = _extract_poll_compatible_media(msg)
    mg_id = getattr(msg, "media_group_id", None)

    settings = get_settings()
    matched, _, _ = extract_trigger_and_clean_text(
        raw_text=raw_text,
        entities=entities,
        trigger_word=settings.get("trigger_word", DEFAULT_TRIGGER_WORD),
        case_sensitive=bool(settings.get("case_sensitive_trigger", False)),
    )
    if not matched:
        mark_msg_checked(ch_data, msg.message_id)
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

    await process_trigger_post(
        bot=context.bot,
        channel_id=channel_id,
        msg_id=msg.message_id,
        raw_text=raw_text,
        entities=entities,
        is_media=is_media,
        media_info=media_info,
        is_album=bool(mg_id),
    )


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type not in ("group", "supergroup"):
        return

    if str(msg.chat.id) not in STATE.get("known_groups", {}):
        await sync_single_chat(context.bot, msg.chat.id)
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
    asyncio.create_task(background_loop(app))
    logger.info("✅ تم تشغيل البوت الثاني بنجاح (المراقبة الفورية + الدمج في منشور واحد + الفحص الشامل اليدوي).")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT2_TOKEN)
        .concurrent_updates(8)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(CommandHandler(["start", "panel", "menu"], cmd_start))
    app.add_handler(CommandHandler(["cancel"], cmd_cancel))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_group_message))
    app.add_handler(CallbackQueryHandler(handle_panel_callback, pattern=r"^p:"))

    print("🚀 تم تشغيل البوت الثاني (bot2.py).")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
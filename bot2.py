import os, re, sys, json, base64, time, logging, asyncio
from typing import Dict, Any, Optional, Tuple, List
import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, MessageEntity,
)
try:
    from telegram import KeyboardButtonRequestChat
except ImportError:
    KeyboardButtonRequestChat = None
from telegram.error import BadRequest, RetryAfter, Forbidden, TelegramError, Conflict
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, TypeHandler, ContextTypes, filters,
)

logging.basicConfig(format="%(asctime)s - [BOT2] - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("bot2")

# ==================== الإعدادات وقراءة التوكن ====================
SECRET_RAW = (os.environ.get("BOT_SECRET") or os.environ.get("3bot") or os.environ.get("BOT_TOKEN") or "").strip()
GITHUB_TOKEN = None
for sep in ["\\", "/", "\n", "|"]:
    if sep in SECRET_RAW:
        GITHUB_TOKEN = SECRET_RAW.split(sep, 1)[1].strip()
        break
if not GITHUB_TOKEN and SECRET_RAW.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_")):
    GITHUB_TOKEN = SECRET_RAW
if not GITHUB_TOKEN:
    GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip() or None

REPO_OWNER, REPO_NAME = "ahmedhassan2382006-art", "Articles"
STATE_FILE_PATH, LOCAL_STATE_PATH = "s.json", "s.json"
GITHUB_STATE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{STATE_FILE_PATH}"
TOKEN_URLS = [
    "https://api.github.com/repos/ahmedhassan2382006-art/kys/contents/t.txt",
    f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/t.txt",
]

START_TIME = time.time()
MAX_LIFETIME = (5 * 3600) - 420
SYNC_INTERVAL = 9 * 60
STATE_LOCK, VOTE_LOCK = asyncio.Lock(), asyncio.Lock()
GITHUB_SHA: Optional[str] = None
STATE_DIRTY, LAST_SYNC_TS = False, 0.0
SEEN_GROUPS: Dict[str, float] = {}
LAST_SEEN: Optional[Dict[str, Any]] = None
LAST_ERR, CONFLICT_COUNT, CONFLICT_NOTIFIED = "", 0, False

DEFAULT_OPTIONS = ["👍 إعجاب", "👎 عدم إعجاب"]
DEFAULT_TRIGGER = "@like"


def _gh_headers() -> Dict[str, str]:
    h = {"Accept": "application/vnd.github.v3+json", "User-Agent": "Bot2"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _extract_tok(raw: str) -> Optional[str]:
    for line in (raw or "").splitlines():
        s = line.strip().strip('"').strip("'")
        if not s or s.startswith("#"):
            continue
        m = re.search(r"\b(\d{6,}:[A-Za-z0-9_-]{20,})\b", s)
        if m:
            return m.group(1)
        if ":" in s and not s.startswith(("http://", "https://", "ghp_", "github_pat_")):
            return s
    return None


def load_token() -> Optional[str]:
    if tok := _extract_tok(os.environ.get("LIKE", "")):
        return tok
    if GITHUB_TOKEN:
        for url in TOKEN_URLS:
            try:
                r = requests.get(url, headers=_gh_headers(), timeout=12)
                if r.status_code == 200:
                    txt = base64.b64decode(r.json().get("content", "")).decode("utf-8-sig")
                    if tok := _extract_tok(txt):
                        return tok
            except Exception as e:
                logger.warning(f"خطأ جلب التوكن من {url}: {e}")
    if os.path.exists("t.txt"):
        with open("t.txt", "r", encoding="utf-8-sig") as f:
            return _extract_tok(f.read())
    return None


BOT_TOKEN = load_token()
if not BOT_TOKEN:
    logger.error("❌ لم يتم العثور على توكن البوت!")
    sys.exit(1)


# ==================== إدارة الحالة s.json ====================
def _def_settings() -> Dict[str, Any]:
    return {
        "trigger_word": DEFAULT_TRIGGER,
        "case_sensitive_trigger": False,
        "button_options": list(DEFAULT_OPTIONS),
        "allows_multiple_answers": False,
        "allows_revoting": True,
        "show_vote_counts": True,
        "vertical_buttons": False,
    }


def _def_state() -> Dict[str, Any]:
    return {
        "owner_id": None, "is_running": False,
        "default_channel_id": None, "default_group_id": None,
        "known_groups": {}, "channels": {}, "settings": _def_settings(),
    }


def _normalize(cfg: Any) -> Dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    for k, v in _def_state().items():
        if k != "settings":
            cfg.setdefault(k, v if not isinstance(v, dict) else {})
    for ch in cfg["channels"].values():
        if isinstance(ch, dict):
            ch.pop("poll_msg_ids", None)
            posts = ch.get("posts")
            if isinstance(posts, dict):
                for mk in [k for k, v in posts.items() if not isinstance(v, dict) or "votes" not in v]:
                    posts.pop(mk, None)
    s = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
    if "button_options" not in s and isinstance(s.get("poll_options"), list):
        s["button_options"] = list(s["poll_options"])
    for k, v in _def_settings().items():
        if k not in s or s[k] is None:
            s[k] = list(v) if isinstance(v, list) else v
    s["button_options"] = [str(o).strip()[:60] for o in (s.get("button_options") or []) if str(o).strip()][:12]
    s["trigger_word"] = str(s.get("trigger_word") or DEFAULT_TRIGGER).strip() or DEFAULT_TRIGGER
    cfg["settings"] = s
    return cfg


def get_s() -> Dict[str, Any]:
    return STATE.setdefault("settings", _def_settings())


def _write_local(obj: Dict[str, Any]) -> bool:
    try:
        tmp = f"{LOCAL_STATE_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LOCAL_STATE_PATH)
        return True
    except Exception as e:
        logger.error(f"خطأ حفظ {LOCAL_STATE_PATH}: {e}")
        return False


def _push_github() -> bool:
    global GITHUB_SHA
    if not GITHUB_TOKEN or not os.path.exists(LOCAL_STATE_PATH):
        return False
    with open(LOCAL_STATE_PATH, "rb") as f:
        raw = f.read()
    for _ in range(3):
        try:
            if not GITHUB_SHA:
                rc = requests.get(GITHUB_STATE_URL, headers=_gh_headers(), timeout=10)
                if rc.status_code == 200:
                    GITHUB_SHA = rc.json().get("sha")
            payload = {"message": "Update s.json [skip ci]", "content": base64.b64encode(raw).decode()}
            if GITHUB_SHA:
                payload["sha"] = GITHUB_SHA
            r = requests.put(GITHUB_STATE_URL, headers=_gh_headers(), json=payload, timeout=15)
            if r.status_code in (200, 201):
                GITHUB_SHA = r.json().get("content", {}).get("sha", GITHUB_SHA)
                return True
            if r.status_code == 409:
                GITHUB_SHA = None
                time.sleep(0.4)
            else:
                return False
        except Exception:
            GITHUB_SHA = None
            time.sleep(0.5)
    return False


def _init_state() -> Dict[str, Any]:
    global GITHUB_SHA
    local = None
    if os.path.exists(LOCAL_STATE_PATH):
        try:
            with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
                local = _normalize(json.load(f))
        except Exception:
            pass
    if GITHUB_TOKEN:
        try:
            r = requests.get(GITHUB_STATE_URL, headers=_gh_headers(), timeout=12)
            if r.status_code == 200:
                d = r.json()
                GITHUB_SHA = d.get("sha")
                txt = base64.b64decode(d.get("content", "")).decode("utf-8")
                parsed = _normalize(json.loads(txt) if txt.strip() else _def_state())
                _write_local(parsed)
                return parsed
            if r.status_code == 404:
                cfg = local or _def_state()
                _write_local(cfg)
                _push_github()
                return cfg
        except Exception as e:
            logger.error(f"خطأ جلب s.json: {e}")
    cfg = local or _def_state()
    _write_local(cfg)
    return cfg


STATE: Dict[str, Any] = _init_state()


async def save_local():
    global STATE_DIRTY
    async with STATE_LOCK:
        snap = json.loads(json.dumps(STATE, ensure_ascii=False))
        if await asyncio.to_thread(_write_local, snap):
            STATE_DIRTY = True


async def sync_github() -> bool:
    global STATE_DIRTY, LAST_SYNC_TS
    async with STATE_LOCK:
        snap = json.loads(json.dumps(STATE, ensure_ascii=False))
        await asyncio.to_thread(_write_local, snap)
        ok = await asyncio.to_thread(_push_github)
        if ok:
            STATE_DIRTY = False
            LAST_SYNC_TS = time.time()
        return ok


# ==================== معالجة النصوص والأزرار ====================
def _u16(t: str) -> int:
    return len(t.encode("utf-16-le")) // 2


def _clamp_ents(text: str, ents: List[MessageEntity]) -> List[MessageEntity]:
    tot = _u16(text)
    out = []
    for e in ents:
        if 0 <= e.offset < tot:
            nl = min(e.length, tot - e.offset)
            if nl > 0:
                out.append(MessageEntity(
                    type=e.type, offset=e.offset, length=nl,
                    url=getattr(e, "url", None), user=getattr(e, "user", None),
                    language=getattr(e, "language", None), custom_emoji_id=getattr(e, "custom_emoji_id", None),
                ))
    return out


def extract_trigger(raw: str, ents: Any, trig: str, case_sens: bool = False) -> Tuple[bool, str, List[MessageEntity]]:
    if not raw or not trig:
        return False, raw or "", list(ents or [])
    ws = r"[\s\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]*"
    m = re.match(rf"^{ws}{re.escape(trig)}(?![A-Za-z]){ws}", raw, 0 if case_sens else re.IGNORECASE)
    if not m:
        return False, raw, list(ents or [])
    cut, clean = m.end(), raw[m.end():].rstrip()
    cut16 = _u16(raw[:cut])
    adj = []
    for e in (ents or []):
        s, end = e.offset, e.offset + e.length
        if end <= cut16:
            continue
        adj.append(MessageEntity(
            type=e.type, offset=(0 if s < cut16 else s - cut16),
            length=(end - cut16 if s < cut16 else e.length),
            url=getattr(e, "url", None), user=getattr(e, "user", None),
            language=getattr(e, "language", None), custom_emoji_id=getattr(e, "custom_emoji_id", None),
        ))
    return True, clean, _clamp_ents(clean, adj)


def _ser_ents(ents: Any) -> List[Dict[str, Any]]:
    out = []
    for e in (ents or []):
        d = {"type": e.type, "offset": e.offset, "length": e.length}
        for k in ("url", "language", "custom_emoji_id"):
            if getattr(e, k, None):
                d[k] = getattr(e, k)
        out.append(d)
    return out


def _deser_ents(raw: Any) -> List[MessageEntity]:
    out = []
    for d in (raw or []):
        try:
            out.append(MessageEntity(type=d["type"], offset=int(d["offset"]), length=int(d["length"]),
                                     url=d.get("url"), language=d.get("language"), custom_emoji_id=d.get("custom_emoji_id")))
        except Exception:
            pass
    return out


def _is_media(msg) -> bool:
    return any(getattr(msg, a, None) for a in ("photo", "video", "animation", "document", "audio", "voice", "video_note"))


def _has_v_buttons(rm: Optional[InlineKeyboardMarkup]) -> bool:
    return bool(rm and getattr(rm, "inline_keyboard", None) and any(
        (getattr(b, "callback_data", "") or "").startswith("v:") for r in rm.inline_keyboard for b in r
    ))


def build_buttons(opts: List[str], counts: Optional[List[int]] = None) -> InlineKeyboardMarkup:
    s = get_s()
    show, vert = bool(s.get("show_vote_counts", True)), bool(s.get("vertical_buttons", False))
    btns = [
        InlineKeyboardButton(
            text=(f"{o} ({counts[i]})" if (show and counts and i < len(counts) and counts[i] > 0) else o)[:64],
            callback_data=f"v:{i}",
        )
        for i, o in enumerate(opts)
    ]
    if not btns:
        return InlineKeyboardMarkup([])
    if vert:
        return InlineKeyboardMarkup([[b] for b in btns])
    mx = max(len(b.text) for b in btns)
    if len(btns) <= 4 and mx <= 18:
        return InlineKeyboardMarkup([btns])
    per = 2 if mx <= 24 else 1
    return InlineKeyboardMarkup([btns[i:i + per] for i in range(0, len(btns), per)])


async def edit_post_with_buttons(bot, chat_id: int, msg_id: int, cleaned: str, ents: List[MessageEntity], is_media: bool, markup: InlineKeyboardMarkup) -> str:
    global LAST_ERR
    txt = cleaned.strip()
    if not is_media and not txt:
        txt, ents = "👇", []
    ents = _clamp_ents(txt, ents)

    for use_ents in (True, False):
        for _ in range(2):
            try:
                if is_media:
                    await bot.edit_message_caption(
                        chat_id=chat_id, message_id=msg_id, caption=txt,
                        caption_entities=(ents if (use_ents and ents and txt) else None), reply_markup=markup,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=txt,
                        entities=(ents if (use_ents and ents) else None), reply_markup=markup,
                    )
                LAST_ERR = ""
                return "ok"
            except RetryAfter as ra:
                await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
            except BadRequest as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    LAST_ERR = ""
                    return "ok"
                if use_ents and ents:
                    break
                LAST_ERR = f"تعديل: {e}"
                if "not found" in err or "message_id_invalid" in err:
                    return "not_found"
                if "can't be edited" in err or "cannot be edited" in err:
                    return "not_editable"
                return "error"
            except Forbidden as e:
                LAST_ERR = f"صلاحية: {e}"
                return "forbidden"
            except TelegramError as e:
                LAST_ERR = f"خطأ: {e}"
                return "error"
    return "error"


# ==================== إدارة القنوات والمجموعات ====================
def get_ch(cid: int, title: str = "") -> Dict[str, Any]:
    k = str(cid)
    ch = STATE.setdefault("channels", {}).setdefault(k, {
        "title": title or f"قناة ({cid})", "linked_group_id": None,
        "can_edit_messages": True, "last_known_msg_id": 0, "posts": {}, "candidates": {},
    })
    if title:
        ch["title"] = title
    for d, v in (("can_edit_messages", True), ("last_known_msg_id", 0), ("posts", {}), ("candidates", {})):
        ch.setdefault(d, v)
    return ch


def get_grp(gid: int, title: str = "", linked: Optional[int] = None) -> Dict[str, Any]:
    g = STATE.setdefault("known_groups", {}).setdefault(str(gid), {"title": title or f"مجموعة ({gid})", "linked_channel_id": linked})
    if title:
        g["title"] = title
    if linked is not None:
        g["linked_channel_id"] = linked
    return g


async def inspect_chat(bot, cid: int) -> Tuple[str, bool, bool, str, str, Optional[int], Optional[int], bool]:
    try:
        m = await bot.get_chat_member(cid, bot.id)
        st = getattr(m, "status", "")
        if st not in ("administrator", "creator", "member", "restricted"):
            return "removed", False, False, "", "", None, None, False
        is_admin = st in ("administrator", "creator")
        can_edit = True if st == "creator" else (bool(getattr(m, "can_edit_messages", False)) if st == "administrator" else False)
        c = await bot.get_chat(cid)
        pin = getattr(c, "pinned_message", None)
        return "ok", is_admin, can_edit, getattr(c, "type", ""), getattr(c, "title", "") or str(cid), getattr(c, "linked_chat_id", None), (getattr(pin, "message_id", None) if pin else None), bool(getattr(c, "has_protected_content", False))
    except Forbidden:
        return "removed", False, False, "", "", None, None, False
    except BadRequest as e:
        if any(x in str(e).lower() for x in ("chat not found", "not a member", "channel_private", "chat_id_invalid")):
            return "removed", False, False, "", "", None, None, False
        return "err", False, False, "", "", None, None, False
    except TelegramError:
        return "err", False, False, "", "", None, None, False


async def sync_chat(bot, cid: int, auto_select: bool = False) -> Tuple[bool, str]:
    res, is_admin, can_edit, ctype, title, linked, pin_id, _ = await inspect_chat(bot, cid)
    if res == "err":
        return False, "تعذر التحقق مؤقتاً (تم الاحتفاظ بالبيانات)."
    if res == "removed":
        STATE.get("channels", {}).pop(str(cid), None)
        STATE.get("known_groups", {}).pop(str(cid), None)
        if str(STATE.get("default_channel_id")) == str(cid):
            STATE["default_channel_id"], STATE["is_running"] = None, False
        if STATE.get("default_group_id") == cid:
            STATE["default_group_id"] = None
        return False, "البوت غير موجود في هذه الدردشة."

    if ctype == "channel":
        if not is_admin:
            STATE.get("channels", {}).pop(str(cid), None)
            return False, f"البوت في '{title}' ليس مشرفاً! رقّهِ لمشرف مع صلاحية تعديل رسائل الآخرين."
        ch = get_ch(cid, title)
        ch["can_edit_messages"] = can_edit
        if pin_id and pin_id > int(ch.get("last_known_msg_id", 0) or 0):
            ch["last_known_msg_id"] = pin_id
        if linked:
            gr, _, _, gt, gtitle, _, _, _ = await inspect_chat(bot, linked)
            if gr == "ok" and gt in ("group", "supergroup"):
                ch["linked_group_id"] = linked
                get_grp(linked, gtitle, cid)
        if auto_select or not STATE.get("default_channel_id"):
            STATE["default_channel_id"] = cid
            if ch.get("linked_group_id"):
                STATE["default_group_id"] = int(ch["linked_group_id"])
        return True, f"تم التعرف على القناة: {title}"

    if ctype in ("group", "supergroup"):
        get_grp(cid, title, linked)
        if auto_select or not STATE.get("default_group_id"):
            STATE["default_group_id"] = cid
        extra = ""
        if linked:
            cr, ca, ce, _, ct, _, cp, _ = await inspect_chat(bot, linked)
            if cr == "ok" and ca:
                ch = get_ch(linked, ct)
                ch["can_edit_messages"], ch["linked_group_id"] = ce, cid
                if cp and cp > int(ch.get("last_known_msg_id", 0) or 0):
                    ch["last_known_msg_id"] = cp
                if not STATE.get("default_channel_id"):
                    STATE["default_channel_id"] = linked
                extra = f"\n📢 والقناة المرتبطة بها: {ct}"
        return True, f"تم التعرف على المجموعة: {title}{extra}"
    return False, "نوع الدردشة غير مدعوم."


async def verify_all(bot):
    for cid in list(STATE.get("channels", {}).keys()):
        await sync_chat(bot, int(cid))
    for gid in list(STATE.get("known_groups", {}).keys()):
        await sync_chat(bot, int(gid))
    await save_local()


# ==================== معالجة المنشورات والفحص ====================
def _is_srv(msg) -> bool:
    return any(getattr(msg, a, None) is not None for a in (
        "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
        "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
        "channel_chat_created", "pinned_message", "chat_shared",
    ))


def _cache_cand(ch: Dict[str, Any], mid: int, txt: str, ents: Any, is_med: bool):
    if not txt:
        return
    c = ch.setdefault("candidates", {})
    c[str(mid)] = {"text": txt, "entities": _ser_ents(ents), "is_media": is_med}
    if len(c) > 100:
        for k in sorted(c.keys(), key=int)[:-100]:
            c.pop(k, None)


async def process_post(bot, ch_id: int, mid: int, raw: str, ents: Any, is_med: bool, force: bool = False) -> str:
    global LAST_ERR
    opts = get_s().get("button_options", [])
    if not opts:
        LAST_ERR = "لا توجد أزرار مضافة في الإعدادات."
        return "no_options"

    ch = get_ch(ch_id)
    posts = ch.setdefault("posts", {})
    mk = str(mid)
    matched, cleaned, adj_ents = extract_trigger(raw, ents, get_s().get("trigger_word", DEFAULT_TRIGGER), bool(get_s().get("case_sensitive_trigger", False)))
    if not matched:
        return "no_trigger"

    # إذا كان النص ما زال يبدأ بالكلمة المفتاحية، فهذا دليل أنه لم يُعدَّل بعد حتى لو كان رقمه مسجلاً
    if mk in posts and not force and not matched:
        return "already"

    st = await edit_post_with_buttons(bot, ch_id, mid, cleaned, adj_ents, is_med, build_buttons(opts))
    if st == "not_editable":
        LAST_ERR = "هذا المنشور محوّل (Forwarded) وتيليجرام يمنع تعديله."
        return "not_editable"
    if st != "ok":
        return st

    posts[mk] = {"options": list(opts), "votes": {}, "created_at": int(time.time())}
    ch.get("candidates", {}).pop(mk, None)
    if int(ch.get("last_known_msg_id", 0) or 0) < mid:
        ch["last_known_msg_id"] = mid
    if len(posts) > 500:
        for k in sorted(posts.keys(), key=int)[:-500]:
            posts.pop(k, None)
    await save_local()
    return "added"


async def _fwd_read(bot, owner_id: int, ch_id: int, mid: int, is_prot: bool) -> Tuple[str, Optional[Any]]:
    global LAST_ERR
    for _ in range(3):
        try:
            fwd = await bot.forward_message(chat_id=owner_id, from_chat_id=ch_id, message_id=mid, disable_notification=True)
            try:
                await bot.delete_message(owner_id, fwd.message_id)
            except TelegramError:
                pass
            return "ok", fwd
        except RetryAfter as ra:
            await asyncio.sleep(int(getattr(ra, "retry_after", 2)) + 1)
        except BadRequest as e:
            err = str(e).lower()
            if "protected content" in err or "chat_forwards_restricted" in err:
                return "protected", None
            if "can't be forwarded" in err:
                return ("protected" if is_prot else "unforwardable"), None
            if "not found" in err or "message_id_invalid" in err:
                return "not_found", None
            LAST_ERR = f"قراءة #{mid}: {e}"
            return "error", None
        except TelegramError as e:
            LAST_ERR = f"قراءة #{mid}: {e}"
            return "error", None
    return "error", None


async def scan_channel(bot, ch_id: int, owner_id: int) -> Dict[str, Any]:
    ch = get_ch(ch_id)
    last_id = int(ch.get("last_known_msg_id", 0) or 0)
    cands = ch.get("candidates", {})

    is_prot = False
    try:
        is_prot = bool(getattr(await bot.get_chat(ch_id), "has_protected_content", False))
    except TelegramError:
        pass

    scanned, added = 0, 0

    if not is_prot:
        probe, miss = (last_id + 1 if last_id > 0 else 1), 0
        for pid in range(probe, probe + 50):
            st, fwd = await _fwd_read(bot, owner_id, ch_id, pid, is_prot)
            if st == "ok" and fwd:
                last_id = ch["last_known_msg_id"] = pid
                miss = 0
                if getattr(fwd, "poll", None) is None and not _is_srv(fwd):
                    txt = fwd.text if fwd.text is not None else (fwd.caption or "")
                    _cache_cand(ch, pid, txt, fwd.entities if fwd.text is not None else fwd.caption_entities, _is_media(fwd))
            elif st == "unforwardable":
                last_id = ch["last_known_msg_id"] = pid
                miss = 0
            elif st == "protected":
                is_prot = True
                break
            elif st == "not_found":
                miss += 1
                if miss >= 15:
                    break
            await asyncio.sleep(0.1)

    if last_id <= 0:
        return {"last_id": 0, "scanned": 0, "added": 0, "protected": is_prot}

    for mid in range(last_id, 0, -1):
        mk = str(mid)
        if not is_prot:
            st, fwd = await _fwd_read(bot, owner_id, ch_id, mid, is_prot)
            if st == "protected":
                is_prot = True
            elif st == "not_found":
                cands.pop(mk, None)
                continue
            elif st == "unforwardable":
                continue
            elif st == "ok" and fwd:
                if getattr(fwd, "poll", None) is not None or _is_srv(fwd):
                    continue
                txt = fwd.text if fwd.text is not None else (fwd.caption or "")
                if not txt:
                    continue
                scanned += 1
                res = await process_post(bot, ch_id, mid, txt, fwd.entities if fwd.text is not None else fwd.caption_entities, _is_media(fwd), force=True)
                if res == "added":
                    added += 1
                elif res in ("forbidden", "no_options"):
                    break
                await asyncio.sleep(0.12)
                continue

        # مسار احتياطي إذا كانت القناة محمية أو تعذرت القراءة المباشرة
        if cand := cands.get(mk):
            scanned += 1
            res = await process_post(bot, ch_id, mid, cand.get("text", ""), _deser_ents(cand.get("entities")), bool(cand.get("is_media", False)), force=True)
            if res == "added":
                added += 1
            elif res in ("forbidden", "no_options"):
                break
            await asyncio.sleep(0.1)

    await save_local()
    return {"last_id": last_id, "scanned": scanned, "added": added, "protected": is_prot}


# ==================== الروابط والتوجيه ولوحة التحكم ====================
def _extract_fwd(msg) -> Tuple[Optional[int], Optional[int]]:
    orig = getattr(msg, "forward_origin", None)
    if orig:
        chat = getattr(orig, "chat", None) or getattr(orig, "sender_chat", None)
        if chat:
            return chat.id, getattr(orig, "message_id", None)
    if fc := getattr(msg, "forward_from_chat", None):
        return fc.id, getattr(msg, "forward_from_message_id", None)
    return None, None


def _parse_link(text: str) -> Tuple[Optional[int | str], Optional[int]]:
    t = text.strip()
    if m := re.search(r"t\.me/c/(\d+)/(\d+)", t):
        return int(f"-100{m.group(1)}"), int(m.group(2))
    if m := re.search(r"t\.me/c/(\d+)(?:/|\?|$|\s)", t):
        return int(f"-100{m.group(1)}"), None
    if m := re.search(r"t\.me/([A-Za-z0-9_]{4,})/(\d+)", t):
        return f"@{m.group(1)}", int(m.group(2))
    if m := re.search(r"t\.me/([A-Za-z0-9_]{4,})(?:/|\?|$|\s)", t):
        if m.group(1).lower() not in ("joinchat", "addstickers", "addemoji", "share"):
            return f"@{m.group(1)}", None
    if m := re.search(r"(?:^|\s)(@[A-Za-z0-9_]{4,})", t):
        return m.group(1), None
    if m := re.search(r"(?:^|\s)(-100\d{5,}|-\d{5,})(?:\s|$)", t):
        return int(m.group(1)), None
    return None, None


def is_owner(uid: int) -> bool:
    return STATE.get("owner_id") is not None and int(STATE["owner_id"]) == int(uid)


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def panel_text() -> str:
    s = get_s()
    run = bool(STATE.get("is_running"))
    dch, dgrp = STATE.get("default_channel_id"), STATE.get("default_group_id")
    chs, grps = STATE.get("channels", {}), STATE.get("known_groups", {})

    if dch and str(dch) in chs:
        ci = chs[str(dch)]
        ch_line = f"{_esc(ci.get('title', dch))} (<code>{dch}</code>)"
        if not ci.get("can_edit_messages", True):
            ch_line += "\n⚠️ <b>فعّل صلاحية تعديل رسائل الآخرين للبوت!</b>"
        last_mid = str(ci.get("last_known_msg_id") or "غير معروف بعد")
    else:
        ch_line, last_mid = "❌ لم تُحدد بعد", "—"

    grp_line = _esc(grps[str(dgrp)].get("title", dgrp)) if (dgrp and str(dgrp) in grps) else "بدون مجموعة افتراضية"
    opts = s.get("button_options", [])
    opts_txt = "\n".join(f"  {i+1}. {_esc(o)}" for i, o in enumerate(opts)) or "  ❌ لا توجد أزرار!"
    seen = f"#{LAST_SEEN['msg_id']} «{_esc(LAST_SEEN['preview'])}»" if LAST_SEEN else "لا يوجد بعد"
    conf = f"\n⚠️ <b>تعارض نسخ ({CONFLICT_COUNT})!</b>" if CONFLICT_COUNT else ""

    return (
        "🎛️ <b>لوحة تحكم بوت الأزرار التفاعلية</b>\n"
        "👨‍💻 <b>المطور:</b> أحمد حسن عبد النبي\n\n"
        f"• <b>الحالة:</b> {'🟢 يعمل الآن' if run else '⏸ متوقف'}\n"
        f"• <b>القناة:</b> {ch_line}\n"
        f"• <b>المجموعة:</b> {grp_line}\n"
        f"• <b>آخر منشور:</b> <code>{last_mid}</code> | <b>آخر تحديث وصل:</b> {seen}{conf}\n"
        f"• <b>الكلمة المفتاحية:</b> <code>{_esc(s.get('trigger_word'))}</code>\n"
        f"• <b>الأزرار ({len(opts)}):</b>\n{opts_txt}"
    )


def panel_kb() -> InlineKeyboardMarkup:
    run = bool(STATE.get("is_running"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت" if run else "▶️ ابدأ العمل", callback_data="p:toggle")],
        [InlineKeyboardButton("🧩 إدارة الأزرار", callback_data="p:opts"), InlineKeyboardButton("⚙️ الإعدادات", callback_data="p:cfg")],
        [InlineKeyboardButton("📢 القناة الافتراضية", callback_data="p:chs"), InlineKeyboardButton("💬 المجموعة الافتراضية", callback_data="p:grps")],
        [InlineKeyboardButton("📲 جلب قناة/مجموعة من قائمة تيليجرام", callback_data="p:pick")],
        [InlineKeyboardButton("🔍 فحص منشورات القناة", callback_data="p:scan"), InlineKeyboardButton("🩺 تشخيص", callback_data="p:diag")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="p:refresh"), InlineKeyboardButton("💾 حفظ GitHub", callback_data="p:sync")],
    ])


def opts_view() -> Tuple[str, InlineKeyboardMarkup]:
    opts = get_s().get("button_options", [])
    txt = "🧩 <b>إدارة الأزرار التفاعلية (1 - 12)</b>\n\n" + ("\n".join(f"<b>{i+1}.</b> {_esc(o)}" for i, o in enumerate(opts)) or "❌ لا توجد أزرار.")
    rows = [[InlineKeyboardButton("➕ إضافة زر", callback_data="p:oadd")]] if len(opts) < 12 else []
    for i, o in enumerate(opts):
        r = [InlineKeyboardButton(f"✏️ {i+1}: {o[:18]}", callback_data=f"p:oedit:{i}")]
        if i > 0:
            r.append(InlineKeyboardButton("⬆️", callback_data=f"p:oup:{i}"))
        r.append(InlineKeyboardButton("🗑", callback_data=f"p:odel:{i}"))
        rows.append(r)
    rows.append([InlineKeyboardButton("♻️ افتراضي", callback_data="p:orst"), InlineKeyboardButton("🧹 مسح الكل", callback_data="p:oclr")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="p:home")])
    return txt, InlineKeyboardMarkup(rows)


def cfg_view() -> Tuple[str, InlineKeyboardMarkup]:
    s = get_s()
    txt = (
        f"⚙️ <b>الإعدادات</b>\n\n"
        f"• الكلمة المفتاحية: <code>{_esc(s.get('trigger_word'))}</code>\n"
        f"• التحديد: {'عدة أزرار' if s.get('allows_multiple_answers') else 'زر واحد فقط'}\n"
        f"• تغيير التصويت: {'مسموح' if s.get('allows_revoting') else 'مقفل'}\n"
        f"• إظهار العداد: {'نعم' if s.get('show_vote_counts') else 'لا'}\n"
        f"• الترتيب: {'عمودي' if s.get('vertical_buttons') else 'أفقي تلقائي'}\n"
        f"• حساسية الأحرف: {'نعم' if s.get('case_sensitive_trigger') else 'لا'}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏷️ الكلمة المفتاحية ({s.get('trigger_word')})", callback_data="p:ctrig")],
        [InlineKeyboardButton(f"☑️ متعدد: {'✅' if s.get('allows_multiple_answers') else '❌'}", callback_data="p:ctog:allows_multiple_answers"),
         InlineKeyboardButton(f"🔓 التغيير: {'✅' if s.get('allows_revoting') else '❌'}", callback_data="p:ctog:allows_revoting")],
        [InlineKeyboardButton(f"🔢 العداد: {'✅' if s.get('show_vote_counts') else '❌'}", callback_data="p:ctog:show_vote_counts"),
         InlineKeyboardButton(f"↕️ عمودي: {'✅' if s.get('vertical_buttons') else '❌'}", callback_data="p:ctog:vertical_buttons")],
        [InlineKeyboardButton(f"🔤 حساسية الأحرف: {'✅' if s.get('case_sensitive_trigger') else '❌'}", callback_data="p:ctog:case_sensitive_trigger")],
        [InlineKeyboardButton("♻️ استعادة الافتراضي", callback_data="p:crst"), InlineKeyboardButton("🔙 رجوع", callback_data="p:home")],
    ])
    return txt, kb


async def show_view(msg, txt: str, kb: InlineKeyboardMarkup, edit: bool = False):
    if edit:
        try:
            await msg.edit_text(txt, reply_markup=kb, parse_mode="HTML")
            return
        except BadRequest:
            pass
    await msg.reply_text(txt, reply_markup=kb, parse_mode="HTML")


async def show_panel(msg, edit: bool = False):
    await show_view(msg, panel_text(), panel_kb(), edit=edit)


async def send_picker(msg):
    if not KeyboardButtonRequestChat:
        await msg.reply_text("أعد توجيه منشور من قناتك هنا، أو أرسل معرفها <code>@username</code>.", parse_mode="HTML")
        return
    kb = ReplyKeyboardMarkup([
        [KeyboardButton("📢 اختيار قناة", request_chat=KeyboardButtonRequestChat(request_id=101, chat_is_channel=True)),
         KeyboardButton("💬 اختيار مجموعة", request_chat=KeyboardButtonRequestChat(request_id=102, chat_is_channel=False))],
        [KeyboardButton("❌ إغلاق القائمة")],
    ], resize_keyboard=True, one_time_keyboard=True)
    await msg.reply_text("📲 اختر قناتك أو مجموعتك من الأزرار بالأسفل:", reply_markup=kb)


# ==================== أوامر ورسائل المالك ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return
    context.user_data.pop("act", None)
    uid = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = uid
        await save_local()
        await sync_github()
    elif not is_owner(uid):
        await msg.reply_text("⛔ هذا البوت مخصص لصاحبه فقط.")
        return
    await verify_all(context.bot)
    await show_panel(msg)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg and msg.chat.type == "private" and msg.from_user and is_owner(msg.from_user.id):
        context.user_data.pop("act", None)
        await msg.reply_text("✅ تم الإلغاء.", reply_markup=ReplyKeyboardRemove())
        await show_panel(msg)


async def handle_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or not is_owner(msg.from_user.id) or not getattr(msg, "chat_shared", None):
        return
    ok, info = await sync_chat(context.bot, msg.chat_shared.chat_id, auto_select=True)
    await save_local()
    await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}", reply_markup=ReplyKeyboardRemove())
    await show_panel(msg)


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private" or not msg.from_user:
        return
    uid = msg.from_user.id
    if STATE.get("owner_id") is None:
        STATE["owner_id"] = uid
        await save_local()
    elif not is_owner(uid):
        return

    if getattr(msg, "chat_shared", None):
        await handle_shared(update, context)
        return

    if act := context.user_data.get("act"):
        t = (msg.text or "").strip()
        if t.lower() in ("/cancel", "الغاء", "إلغاء", "❌ إغلاق القائمة"):
            context.user_data.pop("act", None)
            await msg.reply_text("✅ تم الإلغاء.", reply_markup=ReplyKeyboardRemove())
            await show_panel(msg)
            return
        if not t:
            await msg.reply_text("⚠️ أرسل نصاً أو /cancel.")
            return
        s = get_s()
        if act["t"] == "add":
            opts = s.setdefault("button_options", [])
            for line in [x.strip() for x in t.splitlines() if x.strip()]:
                if len(opts) < 12:
                    opts.append(line[:60])
        elif act["t"] == "edit" and 0 <= act["i"] < len(s.get("button_options", [])):
            s["button_options"][act["i"]] = t[:60]
        elif act["t"] == "trig":
            s["trigger_word"] = t.split()[0].strip()[:64]
        context.user_data.pop("act", None)
        await save_local()
        if act["t"] == "trig":
            await show_view(msg, *cfg_view())
        else:
            await show_view(msg, *opts_view())
        return

    fc, fm = _extract_fwd(msg)
    if fc:
        ok, info = await sync_chat(context.bot, fc, auto_select=True)
        if ok and fm and str(fc) in STATE.get("channels", {}):
            ch = STATE["channels"][str(fc)]
            if fm > int(ch.get("last_known_msg_id", 0) or 0):
                ch["last_known_msg_id"] = fm
            raw = msg.text if msg.text is not None else (msg.caption or "")
            _cache_cand(ch, fm, raw, msg.entities if msg.text is not None else msg.caption_entities, _is_media(msg))
            info += f"\n🔢 رقم المنشور: {fm}"
        await save_local()
        await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        if ok and STATE.get("is_running") and str(STATE.get("default_channel_id")) == str(fc) and fm:
            if c := STATE["channels"][str(fc)].get("candidates", {}).get(str(fm)):
                r = await process_post(context.bot, fc, fm, c["text"], _deser_ents(c.get("entities")), bool(c.get("is_media")), force=True)
                if r == "added":
                    await msg.reply_text("✅ تم تعديل المنشور ووضع الأزرار عليه.")
        await show_panel(msg)
        return

    t = (msg.text or "").strip()
    if t == "❌ إغلاق القائمة":
        await msg.reply_text("✅ تم الإغلاق.", reply_markup=ReplyKeyboardRemove())
        await show_panel(msg)
        return
    if t.lower() in ("ابدأ", "ابدا", "start", "/run"):
        dch = STATE.get("default_channel_id")
        if not dch or str(dch) not in STATE.get("channels", {}):
            await msg.reply_text("⚠️ حدد القناة الافتراضية أولاً.")
            return
        STATE["is_running"] = True
        await save_local()
        await sync_github()
        await msg.reply_text(f"▶️ بدأ العمل! أي منشور يبدأ بـ {get_s().get('trigger_word')} سيتم تعديله ووضع الأزرار عليه.")
        await show_panel(msg)
        return
    if t.lower() in ("ايقاف", "إيقاف", "stop", "/stop"):
        STATE["is_running"] = False
        await save_local()
        await sync_github()
        await msg.reply_text("⏸ تم الإيقاف.")
        await show_panel(msg)
        return

    ref, lmid = _parse_link(t)
    if ref is not None:
        try:
            co = await context.bot.get_chat(ref)
            ok, info = await sync_chat(context.bot, co.id, auto_select=True)
            if ok and lmid and str(co.id) in STATE.get("channels", {}):
                ch = STATE["channels"][str(co.id)]
                if lmid > int(ch.get("last_known_msg_id", 0) or 0):
                    ch["last_known_msg_id"] = lmid
                info += f"\n🔢 رقم المنشور: {lmid}"
            await save_local()
            await msg.reply_text(f"{'✅' if ok else '⚠️'} {info}")
        except Exception as e:
            await msg.reply_text(f"⚠️ تعذر الوصول: {e}")
    await show_panel(msg)


async def diag_report(bot, uid: int) -> str:
    me = await bot.get_me()
    out = [
        "🩺 <b>التشخيص الحي</b>",
        f"• البوت: @{_esc(me.username)} | الحالة: {'🟢 يعمل' if STATE.get('is_running') else '⏸ متوقف'} | Conflict: {CONFLICT_COUNT}",
        f"• الكلمة المفتاحية: <code>{_esc(get_s().get('trigger_word'))}</code>",
        f"• آخر تحديث قناة: {f'#{LAST_SEEN[\"msg_id\"]} ({_esc(LAST_SEEN[\"preview\"])})' if LAST_SEEN else 'لم يصل بعد'}",
    ]
    if LAST_ERR:
        out.append(f"• آخر خطأ: <code>{_esc(LAST_ERR)}</code>")
    dch = STATE.get("default_channel_id")
    if not dch:
        out.append("\n❌ لا توجد قناة افتراضية محددة.")
        return "\n".join(out)
    res, adm, edit, _, title, _, _, prot = await inspect_chat(bot, int(dch))
    ch = STATE.get("channels", {}).get(str(dch), {})
    out.append(f"\n📢 <b>{_esc(title or dch)}</b> (<code>{dch}</code>)")
    out.append(f"• مشرف: {'✅' if adm else '❌'} | تعديل رسائل الآخرين: {'✅' if edit else '❌'} | حماية المحتوى: {'⚠️ نعم' if prot else '✅ لا'}")
    out.append(f"• آخر رقم مسجل: <code>{ch.get('last_known_msg_id', 0)}</code> | منشورات بأزرار: {len(ch.get('posts', {}))}")
    if not prot and res == "ok":
        lid = int(ch.get("last_known_msg_id", 0) or 0)
        for pid in range(max(1, lid), max(1, lid) + 10):
            st, fwd = await _fwd_read(bot, uid, int(dch), pid, False)
            if st == "ok" and fwd:
                txt = (fwd.text or fwd.caption or "[وسائط]")[:50]
                out.append(f"• اختبار قراءة #{pid}: ✅ «{_esc(txt)}»")
                if pid > lid:
                    ch["last_known_msg_id"] = pid
                    await save_local()
    return "\n".join(out)


async def handle_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("p:"):
        return
    await q.answer()
    if not is_owner(q.from_user.id):
        return

    context.user_data.pop("act", None)
    p = q.data.split(":")
    act, s = p[1], get_s()

    if act == "home":
        await show_panel(q.message, True)
    elif act == "pick":
        await send_picker(q.message)
    elif act == "diag":
        m = await q.message.reply_text("🩺 جاري التشخيص...")
        await m.edit_text(await diag_report(context.bot, q.from_user.id), parse_mode="HTML")
    elif act == "opts":
        await show_view(q.message, *opts_view(), edit=True)
    elif act == "oadd":
        context.user_data["act"] = {"t": "add"}
        await q.message.reply_text("➕ أرسل اسم الزر (أو كل زر في سطر)، أو /cancel.")
    elif act == "oedit":
        context.user_data["act"] = {"t": "edit", "i": int(p[2])}
        await q.message.reply_text("✏️ أرسل النص الجديد للزر، أو /cancel.")
    elif act == "oup":
        i, o = int(p[2]), s["button_options"]
        if 1 <= i < len(o):
            o[i - 1], o[i] = o[i], o[i - 1]
            await save_local()
        await show_view(q.message, *opts_view(), edit=True)
    elif act == "odel":
        i, o = int(p[2]), s["button_options"]
        if 0 <= i < len(o):
            o.pop(i)
            await save_local()
        await show_view(q.message, *opts_view(), edit=True)
    elif act == "oclr":
        s["button_options"] = []
        await save_local()
        await show_view(q.message, *opts_view(), edit=True)
    elif act == "orst":
        s["button_options"] = list(DEFAULT_OPTIONS)
        await save_local()
        await show_view(q.message, *opts_view(), edit=True)
    elif act == "cfg":
        await show_view(q.message, *cfg_view(), edit=True)
    elif act == "ctrig":
        context.user_data["act"] = {"t": "trig"}
        await q.message.reply_text(f"🏷️ الحالية: <code>{_esc(s.get('trigger_word'))}</code>\nأرسل الجديدة أو /cancel:", parse_mode="HTML")
    elif act == "ctog":
        s[p[2]] = not bool(s.get(p[2], False))
        await save_local()
        await show_view(q.message, *cfg_view(), edit=True)
    elif act == "crst":
        STATE["settings"] = _def_settings()
        await save_local()
        await show_view(q.message, *cfg_view(), edit=True)
    elif act == "chs":
        dch = STATE.get("default_channel_id")
        rows = [[InlineKeyboardButton(f"{'✅ ' if str(dch)==k else '📢 '}{v.get('title',k)[:30]}", callback_data=f"p:sch:{k}")] for k, v in STATE.get("channels", {}).items()]
        rows += [[InlineKeyboardButton("📲 جلب من قائمة تيليجرام", callback_data="p:pick")], [InlineKeyboardButton("🔙 رجوع", callback_data="p:home")]]
        await show_view(q.message, "📢 <b>اختر القناة الافتراضية:</b>", InlineKeyboardMarkup(rows), edit=True)
    elif act == "sch":
        cid = int(p[2])
        STATE["default_channel_id"] = cid
        if lg := STATE.get("channels", {}).get(str(cid), {}).get("linked_group_id"):
            STATE["default_group_id"] = int(lg)
        await sync_chat(context.bot, cid)
        await save_local()
        await show_panel(q.message, True)
    elif act == "grps":
        dgrp = STATE.get("default_group_id")
        rows = [[InlineKeyboardButton(f"{'✅ ' if str(dgrp)==k else '💬 '}{v.get('title',k)[:30]}", callback_data=f"p:sgrp:{k}")] for k, v in STATE.get("known_groups", {}).items()]
        rows += [[InlineKeyboardButton("🚫 بدون مجموعة", callback_data="p:sgrp:none")], [InlineKeyboardButton("📲 جلب من قائمة تيليجرام", callback_data="p:pick")], [InlineKeyboardButton("🔙 رجوع", callback_data="p:home")]]
        await show_view(q.message, "💬 <b>اختر المجموعة الافتراضية:</b>", InlineKeyboardMarkup(rows), edit=True)
    elif act == "sgrp":
        STATE["default_group_id"] = None if p[2] == "none" else int(p[2])
        await save_local()
        await show_panel(q.message, True)
    elif act == "toggle":
        if STATE.get("is_running"):
            STATE["is_running"] = False
            await save_local()
            await sync_github()
        else:
            dch = STATE.get("default_channel_id")
            if not dch or str(dch) not in STATE.get("channels", {}):
                await q.message.reply_text("⚠️ اختر القناة الافتراضية أولاً!")
                return
            await sync_chat(context.bot, int(dch))
            if not STATE["channels"][str(dch)].get("can_edit_messages", True):
                await q.message.reply_text("⛔ البوت يحتاج صلاحية (تعديل رسائل الآخرين)!")
                return
            if not s.get("button_options"):
                await q.message.reply_text("⚠️ أضف زراً واحداً على الأقل أولاً.")
                return
            STATE["is_running"] = True
            await save_local()
            await sync_github()
        await show_panel(q.message, True)
    elif act == "scan":
        dch = STATE.get("default_channel_id")
        if not dch:
            await q.message.reply_text("⚠️ اختر القناة الافتراضية أولاً.")
            return
        m = await q.message.reply_text("🔍 جاري فحص منشورات القناة...")
        r = await scan_channel(context.bot, int(dch), q.from_user.id)
        trig = s.get("trigger_word")
        msg_txt = (
            f"✅ اكتمل الفحص (حتى رقم {r['last_id']}).\n"
            f"• منشورات موجودة تم فحصها: {r['scanned']}\n"
            f"• منشورات تبدأ بـ {trig} وتم وضع الأزرار عليها: {r['added']}"
        )
        if r["protected"]:
            msg_txt += "\n⚠️ القناة مفعّل فيها حماية المحتوى."
        if LAST_ERR:
            msg_txt += f"\n⚠️ ملاحظة: {LAST_ERR}"
        await m.edit_text(msg_txt)
        await show_panel(q.message, True)
    elif act == "refresh":
        await verify_all(context.bot)
        if not STATE.get("channels"):
            await send_picker(q.message)
        await show_panel(q.message, True)
    elif act == "sync":
        ok = await sync_github()
        await q.message.reply_text("✅ تم الحفظ على GitHub." if ok else "⚠️ تعذر الحفظ.")


# ==================== التصويت ومعالجات القناة ====================
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("v:") or not q.message:
        return
    try:
        idx = int(q.data.split(":", 1)[1])
    except ValueError:
        return

    cid, mid, uid = q.message.chat.id, q.message.message_id, str(q.from_user.id)
    s = get_s()
    multi, revote = bool(s.get("allows_multiple_answers", False)), bool(s.get("allows_revoting", True))

    async with VOTE_LOCK:
        posts = get_ch(cid).setdefault("posts", {})
        pdata = posts.get(str(mid))
        if not isinstance(pdata, dict) or not pdata.get("options"):
            rm = getattr(q.message, "reply_markup", None)
            opts = [re.sub(r"\s*\(\d+\)$", "", b.text).strip() for r in rm.inline_keyboard for b in r] if rm else list(s.get("button_options", DEFAULT_OPTIONS))
            pdata = posts[str(mid)] = {"options": opts, "votes": {}, "created_at": int(time.time())}

        opts = pdata["options"]
        if not (0 <= idx < len(opts)):
            await q.answer("⚠️ خيار غير متاح.")
            return

        votes: Dict[str, List[int]] = pdata.setdefault("votes", {})
        cur = list(votes.get(uid, []))
        changed, fb = False, ""

        if idx in cur:
            if not revote:
                await q.answer("🔒 لا يمكن تغيير التصويت.")
                return
            cur.remove(idx)
            if cur:
                votes[uid] = cur
            else:
                votes.pop(uid, None)
            fb, changed = "↩️ تم سحب اختيارك.", True
        else:
            if cur and not multi and not revote:
                await q.answer("🔒 صوّت مسبقاً ولا يمكن التغيير.")
                return
            votes[uid] = (cur + [idx]) if multi else [idx]
            fb, changed = f"✅ {opts[idx]}", True

        cnts = [0] * len(opts)
        for lst in votes.values():
            for i in lst:
                if 0 <= i < len(opts):
                    cnts[i] += 1
        markup = build_buttons(opts, cnts)

    await q.answer(fb)
    if changed:
        try:
            await context.bot.edit_message_reply_markup(cid, mid, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"تحديث الأزرار: {e}")
        except TelegramError:
            pass
        await save_local()


async def track_upd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_SEEN
    cp = getattr(update, "channel_post", None) or getattr(update, "edited_channel_post", None)
    if cp and getattr(cp, "chat", None):
        LAST_SEEN = {"chat_id": cp.chat.id, "msg_id": cp.message_id, "preview": (cp.text or cp.caption or "[وسائط]")[:40], "ts": time.time()}


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    global CONFLICT_COUNT, CONFLICT_NOTIFIED, LAST_ERR
    if isinstance(context.error, Conflict):
        CONFLICT_COUNT += 1
        if not CONFLICT_NOTIFIED and STATE.get("owner_id"):
            CONFLICT_NOTIFIED = True
            try:
                await context.bot.send_message(int(STATE["owner_id"]), "🚨 توجد نسخة أخرى تعمل بنفس التوكن (Conflict)!")
            except Exception:
                pass
        return
    LAST_ERR = str(context.error)
    logger.exception("خطأ:", exc_info=context.error)


async def on_my_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    if cmu and cmu.chat.type in ("channel", "group", "supergroup"):
        await sync_chat(context.bot, cmu.chat.id)
        await save_local()


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.edited_channel_post
    if not msg or msg.chat.type != "channel" or _is_srv(msg) or getattr(msg, "poll", None):
        return
    cid = msg.chat.id
    if str(cid) not in STATE.get("channels", {}):
        await sync_chat(context.bot, cid)
    ch = get_ch(cid, getattr(msg.chat, "title", "") or "")
    if msg.message_id > int(ch.get("last_known_msg_id", 0) or 0):
        ch["last_known_msg_id"] = msg.message_id

    raw = msg.text if msg.text is not None else (msg.caption or "")
    ents = msg.entities if msg.text is not None else msg.caption_entities
    _cache_cand(ch, msg.message_id, raw, ents, _is_media(msg))

    if not STATE.get("is_running") or str(STATE.get("default_channel_id")) != str(cid) or not raw:
        await save_local()
        return

    matched, _, _ = extract_trigger(raw, ents, get_s().get("trigger_word", DEFAULT_TRIGGER), bool(get_s().get("case_sensitive_trigger", False)))
    if not matched or _has_v_buttons(getattr(msg, "reply_markup", None)):
        await save_local()
        return

    if mg := getattr(msg, "media_group_id", None):
        now = time.time()
        for k in [k for k, ts in SEEN_GROUPS.items() if now - ts > 600]:
            SEEN_GROUPS.pop(k, None)
        if f"{cid}:{mg}" in SEEN_GROUPS:
            return
        SEEN_GROUPS[f"{cid}:{mg}"] = now

    res = await process_post(context.bot, cid, msg.message_id, raw, ents, _is_media(msg), force=True)
    if res in ("forbidden", "no_options", "error", "not_editable") and STATE.get("owner_id"):
        try:
            await context.bot.send_message(int(STATE["owner_id"]), f"⚠️ تعذر تعديل #{msg.message_id}: {LAST_ERR or res}")
        except Exception:
            pass


async def on_group_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and str(update.message.chat.id) not in STATE.get("known_groups", {}):
        await sync_chat(context.bot, update.message.chat.id)
        await save_local()


async def bg_loop(app):
    global LAST_SYNC_TS
    await asyncio.sleep(3)
    try:
        await verify_all(app.bot)
    except Exception:
        pass
    LAST_SYNC_TS = last_v = time.time()
    while True:
        now = time.time()
        if now - START_TIME >= MAX_LIFETIME:
            await sync_github()
            os._exit(0)
        if now - LAST_SYNC_TS >= SYNC_INTERVAL:
            if STATE_DIRTY:
                await sync_github()
            else:
                LAST_SYNC_TS = now
        if now - last_v >= 300:
            await verify_all(app.bot)
            last_v = time.time()
        await asyncio.sleep(5)


async def post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    asyncio.create_task(bg_loop(app))


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(8).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, track_upd), group=-1)
    app.add_handler(CommandHandler(["start", "panel", "menu"], cmd_start))
    app.add_handler(CommandHandler(["cancel"], cmd_cancel))
    app.add_handler(ChatMemberHandler(on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
    if getattr(filters.StatusUpdate, "CHAT_SHARED", None) is not None:
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.StatusUpdate.CHAT_SHARED, handle_shared))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_msg))
    app.add_handler(CallbackQueryHandler(handle_panel_cb, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^v:"))
    app.add_error_handler(on_error)
    print("🚀 تم تشغيل البوت.")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
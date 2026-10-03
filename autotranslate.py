"""
Auto-translation of EVERY message the bot sends, in the language the user picked.

How it works
------------
install() wraps pyrogram's send / edit methods (send_message, send_photo, edit_message_text,
captions, inline + reply keyboards, callback-query alerts). Right before a message leaves the bot,
the text is translated into the chat's language (user_lang(chat_id)). No call site in bot.py has to
change, and a language that has no hand-written translation still works.

What is NOT translated (kept exactly as is):
  * anything wrapped by protect() -- esc() does this for all dynamic data (video titles, channel
    names, user names, error details) and T() does it for the hand-written lang.py texts
  * HTML tags, <code>/<pre>/<a> contents, URLs, @mentions, numbers / times / sizes / speeds
  * the language picker itself (its buttons are native language names)
Small-caps / bold-italic unicode styling is converted to plain letters for translation and re-applied
to the result, so the bot's look is kept for Latin-script languages.

Translations are cached (memory + a small JSON file), so a text is translated once per language.
If the translator is slow / down, the ORIGINAL text is sent -- the bot never blocks or breaks.
Set AUTO_TRANSLATE=0 to switch it off. Needs: pip install deep-translator (Google, no API key).
"""
import asyncio
import atexit
import copy
import functools
import html
import inspect
import json
import logging
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("ytbot")

ENABLED = os.getenv("AUTO_TRANSLATE", "1").strip() != "0"
CACHE_FILE = os.getenv("AUTO_TRANSLATE_CACHE", "translate_cache.json")
TIMEOUT = float(os.getenv("AUTO_TRANSLATE_TIMEOUT", "8") or 8)  # seconds to wait before sending the original

# invisible markers: text between them is never translated (and the markers are stripped before sending)
OPEN, CLOSE = "\u2063", "\u2064"
_S_OPEN, _S_CLOSE = "\ue000", "\ue001"  # internal sentinels while a message is being processed

# bot language code -> Google Translate code (everything else is passed through as is)
GOOGLE_CODES = {"zh": "zh-CN", "fil": "tl"}


def protect(s) -> str:
    """Mark `s` as 'do not translate'. Invisible if it ever leaks somewhere else."""
    s = "" if s is None else str(s)
    return f"{OPEN}{s}{CLOSE}" if s else s


def strip_marks(s):
    return s.replace(OPEN, "").replace(CLOSE, "") if isinstance(s, str) else s


# ---------------------------------------------------------------------------
# unicode styles used by the bot (small caps via SC(), bold-italic via _bi())
# ---------------------------------------------------------------------------
_SC_FROM = "abcdefghijklmnopqrstuvwxyz"
_SC_TO = "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"
_SC_FWD = {ord(a): b for a, b in zip(_SC_FROM, _SC_TO)}
_SC_FWD.update({ord(a.upper()): b for a, b in zip(_SC_FROM, _SC_TO)})
_SC_REV = {ord(b): a for a, b in zip(_SC_FROM, _SC_TO) if a != b}


def _bi_char(ch: str) -> str:
    if "A" <= ch <= "Z":
        return chr(0x1D63C + ord(ch) - ord("A"))
    if "a" <= ch <= "z":
        return chr(0x1D656 + ord(ch) - ord("a"))
    if "0" <= ch <= "9":
        return chr(0x1D7EC + ord(ch) - ord("0"))
    return ch


def _plainify(s: str):
    """Styled unicode -> plain letters. Returns (plain_text, style) with style in {None, 'sc', 'bi'}."""
    out, sc, bi, latin = [], 0, 0, 0
    for ch in s:
        o = ord(ch)
        if o in _SC_REV:
            out.append(_SC_REV[o])
            sc += 1
        elif 0x1D400 <= o <= 0x1D7FF:
            out.append(unicodedata.normalize("NFKC", ch))
            bi += 1
        else:
            out.append(ch)
            if "a" <= ch <= "z" or "A" <= ch <= "Z":
                latin += 1
    style = "sc" if sc and sc >= latin else ("bi" if bi and bi >= latin else None)
    return "".join(out), style


def _restyle(s: str, style) -> str:
    if style == "sc":
        return s.translate(_SC_FWD)
    if style == "bi":
        return "".join(_bi_char(c) for c in s)
    return s


# ---------------------------------------------------------------------------
# translator backend + cache
# ---------------------------------------------------------------------------
def _google(text: str, lang: str) -> str:
    from deep_translator import GoogleTranslator  # imported lazily: bot still runs without it
    return GoogleTranslator(source="auto", target=GOOGLE_CODES.get(lang, lang)).translate(text)


BACKEND = _google  # (text, lang) -> translated text ; replaceable (tests)

_pool = ThreadPoolExecutor(max_workers=int(os.getenv("AUTO_TRANSLATE_WORKERS", "6") or 6), thread_name_prefix="autotr")
_cache: dict = {}            # lang -> {plain template: raw translation}
_cache_lock = threading.Lock()
_inflight: dict = {}
_fail = {"n": 0, "until": 0.0}
_dirty = [0]
REVERSE: dict = {}           # translated reply-keyboard label -> original label (see bot.py handler)


def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _cache_lock:
                for lang, d in data.items():
                    if isinstance(d, dict):
                        _cache.setdefault(lang, {}).update(d)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"autotranslate: cache load failed: {e}")


def save_cache():
    try:
        with _cache_lock:
            snap = json.dumps(_cache, ensure_ascii=False)
            _dirty[0] = 0
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snap)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logger.warning(f"autotranslate: cache save failed: {e}")


def _worker(text: str, lang: str):
    try:
        out = BACKEND(text, lang)
        if not isinstance(out, str) or not out.strip():
            raise ValueError("empty translation")
        _fail["n"] = 0
        with _cache_lock:
            _cache.setdefault(lang, {})[text] = out
            _dirty[0] += 1
            need_save = _dirty[0] >= 25
        if need_save:
            save_cache()
        return out
    except Exception as e:
        _fail["n"] += 1
        if _fail["n"] >= 3:  # translator down / blocked -> stop trying for a minute (bot stays fast)
            _fail["until"] = time.time() + 60
        logger.warning(f"autotranslate [{lang}] failed: {str(e)[:120]}")
        return None


async def _get(text: str, lang: str):
    """Raw translation of `text` (cached), or None."""
    with _cache_lock:
        hit = (_cache.get(lang) or {}).get(text)
    if hit is not None:
        return hit
    if time.time() < _fail["until"]:
        return None
    key = (lang, text)
    fut = _inflight.get(key)
    if fut is None:
        fut = _pool.submit(_worker, text, lang)
        _inflight[key] = fut
        fut.add_done_callback(lambda f, k=key: _inflight.pop(k, None))
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), TIMEOUT)
    except Exception:  # timeout etc. -> original text goes out; the worker still fills the cache
        return None


# ---------------------------------------------------------------------------
# message translation
# ---------------------------------------------------------------------------
_PROT_RE = re.compile(OPEN + r"(.*?)" + CLOSE, re.S)
_TAG_RE = re.compile(r"(<[^>]*>)")
_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9-]*)")
_SKIP_TAGS = {"code", "pre", "a", "emoji", "tg-emoji"}
_MASK_RE = re.compile(
    "(" + _S_OPEN + r"\d+" + _S_CLOSE
    + r"|[⬢⬡█░▓▒■□▰▱●○]{3,}"  # progress bars
    + r"|https?://\S+|t\.me/\S+|@[A-Za-z][A-Za-z0-9_]{3,31}"
    + r"|(?<![A-Za-z])\d[\d.,:/]*(?:%|[A-Za-z]{1,2}\b)?)"
)
_PH_RE = re.compile(r"\{\d+\}")
_PH_LOOSE_RE = re.compile(r"\{\s*(\d+)\s*\}")
_LETTER_RE = re.compile(r"[^\W\d_]")
_WS_RE = re.compile(r"^(\s*)(.*?)(\s*)$", re.S)

# Romanised Hindi (Hinglish) marker words. For English users only texts containing one of these are
# translated, so the bot's real English texts stay byte-for-byte as they are.
_HINGLISH = {
    "apna", "apni", "apne", "bhejo", "bhej", "nahi", "nhi", "kro", "karo", "karna", "hai", "hain", "hoga", "gaya",
    "gayi", "hua", "rha", "raha", "rhi", "ka", "ki", "ke", "ko", "se", "ye", "yeh", "wo", "woh", "aap", "aapka",
    "aapki", "abhi", "baad", "thodi", "thoda", "dobara", "pehle", "sirf", "boss", "bhai", "kya", "kaise", "kaun",
    "mein", "diya", "liya", "chahiye", "wala", "wali", "aur", "bas", "jaldi", "shuru", "khatam", "dabao", "dalo",
    "likho", "batao", "koi", "kuch", "tum", "tumhara", "tera", "mera", "hamara", "idhar", "udhar", "yahan", "wahan",
    "phir", "toh", "bhi", "agar", "lekin", "magar", "isliye", "liye", "milega", "milegi", "hogi", "hoon", "sakte",
    "sakta", "paoge", "padega", "padegi", "wapas", "kijiye", "karein", "kare", "kiya", "gaye", "rahe", "naam",
}


def _is_hinglish(plain: str) -> bool:
    return any(w in _HINGLISH for w in re.findall(r"[a-z]+", plain.lower()))


def _valid(out: str, n: int) -> bool:
    ids = sorted(int(x) for x in _PH_LOOSE_RE.findall(out))
    return ids == list(range(n))


async def _piecewise(plain: str, lang: str):
    """Fallback when the translator mangled a {n} placeholder: translate the pieces between them."""
    async def one(p):
        if _PH_RE.fullmatch(p) or not _LETTER_RE.search(p):
            return p
        lead, core, trail = _WS_RE.match(p).groups()
        t = await _get(core, lang)
        return None if t is None else lead + t.strip() + trail

    res = await asyncio.gather(*[one(p) for p in re.split(r"(\{\d+\})", plain)])
    return None if any(r is None for r in res) else "".join(res)


async def _unit(unit: str, lang: str) -> str:
    lead, core, trail = _WS_RE.match(unit).groups()
    if not core:
        return unit
    plain, style = _plainify(html.unescape(core))
    tokens = []

    def _mask(m):
        tokens.append(m.group(0))
        return "{%d}" % (len(tokens) - 1)

    tpl = _MASK_RE.sub(_mask, plain)
    if not _LETTER_RE.search(_PH_RE.sub("", tpl)):
        return unit  # nothing to translate (numbers, bars, emoji ...)
    if lang == "en" and not _is_hinglish(tpl):
        return unit
    out = await _get(tpl, lang)
    if out is not None:
        out = _PH_LOOSE_RE.sub(r"{\1}", out)
        if not _valid(out, len(tokens)):
            out = None
    if out is None:
        out = await _piecewise(tpl, lang)
        if out is None or not _valid(out, len(tokens)):
            return unit
    res = []
    for p in re.split(r"(\{\d+\})", out):
        m = re.fullmatch(r"\{(\d+)\}", p)
        if m:
            tok = tokens[int(m.group(1))]
            res.append(_restyle(tok, style) if style and tok[:1].isdigit() else tok)
        else:
            res.append(html.escape(_restyle(p, style), quote=False))
    return lead + "".join(res) + trail


async def translate_html(text: str, lang: str) -> str:
    """Translate a Telegram-HTML text into `lang` (markers are stripped from the result)."""
    if not isinstance(text, str) or not text:
        return text
    spans = []

    def _hide(m):
        spans.append(m.group(1))
        return f"{_S_OPEN}{len(spans) - 1}{_S_CLOSE}"

    work = _PROT_RE.sub(_hide, text)
    parts = _TAG_RE.split(work)
    jobs, skip = {}, 0
    for i, part in enumerate(parts):
        if i % 2:  # an HTML tag
            m = _TAG_NAME_RE.match(part)
            if m and m.group(1).lower() in _SKIP_TAGS:
                skip = max(0, skip - 1) if part.startswith("</") else skip + 1
        elif part and not skip:
            jobs[i] = part
    if jobs:
        done = await asyncio.gather(*[_unit(p, lang) for p in jobs.values()])
        for i, new in zip(jobs.keys(), done):
            parts[i] = new
    joined = "".join(parts)
    joined = re.sub(_S_OPEN + r"(\d+)" + _S_CLOSE, lambda m: spans[int(m.group(1))], joined)
    return strip_marks(joined)


def _norm_label(s: str) -> str:
    return strip_marks(s).replace("\ufe0f", "").strip()


async def translate_markup(markup, lang):
    """Return a copy of an inline / reply keyboard with translated labels (original is never modified)."""
    if markup is None:
        return None
    attr = "inline_keyboard" if getattr(markup, "inline_keyboard", None) is not None else (
        "keyboard" if getattr(markup, "keyboard", None) is not None else None)
    if attr is None:
        return markup
    rows = getattr(markup, attr)
    is_reply = attr == "keyboard"
    labels = []
    for row in rows:
        for b in row:
            lab = b if isinstance(b, str) else getattr(b, "text", None)
            keep = isinstance(getattr(b, "callback_data", None), str) and b.callback_data.startswith("lg|")
            labels.append(None if (lab is None or keep) else lab)
    if not any(l and (OPEN in l or lang) for l in labels):
        return markup

    async def one(lab):
        if lab is None:
            return None
        if not lang:
            return strip_marks(lab)
        try:
            return await translate_html(lab, lang)
        except Exception as e:
            logger.warning(f"autotranslate: label failed: {e}")
            return strip_marks(lab)

    new_labels = await asyncio.gather(*[one(l) for l in labels])
    it = iter(new_labels)
    new_rows = []
    for row in rows:
        nr = []
        for b in row:
            new = next(it)
            if new is None:
                nr.append(b)
                continue
            if is_reply and lang and new != strip_marks(b if isinstance(b, str) else b.text):
                REVERSE[_norm_label(new)] = strip_marks(b if isinstance(b, str) else b.text)
            if isinstance(b, str):
                nr.append(new)
            else:
                nb = copy.copy(b)
                nb.text = new
                nr.append(nb)
        new_rows.append(nr)
    nm = copy.copy(markup)
    setattr(nm, attr, new_rows)
    return nm


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------
# method name -> (text parameter, max length)
CLIENT_METHODS = {
    "send_message": ("text", 4096),
    "edit_message_text": ("text", 4096),
    "send_photo": ("caption", 1024),
    "send_video": ("caption", 1024),
    "send_audio": ("caption", 1024),
    "send_document": ("caption", 1024),
    "send_animation": ("caption", 1024),
    "send_voice": ("caption", 1024),
    "edit_message_caption": ("caption", 1024),
    "copy_message": ("caption", 1024),
    "edit_message_reply_markup": (None, 0),
}


def _is_html(parse_mode) -> bool:
    return parse_mode is None or str(parse_mode).lower().endswith("html")


async def _process(text: str, lang, limit: int, html_ok: bool):
    clean = strip_marks(text)
    if not lang or not html_ok:
        return clean
    try:
        new = await translate_html(text, lang)
    except Exception as e:
        logger.warning(f"autotranslate failed: {e}")
        return clean
    if limit and len(new) > limit >= len(clean):  # longer than Telegram allows -> send the original
        return clean
    return new


def _wrap(cls, name, text_param, limit, who, resolver):
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_autotr", False) or not inspect.iscoroutinefunction(orig):
        return False
    sig = inspect.signature(orig)

    @functools.wraps(orig)
    async def wrapper(self, *a, **k):
        try:
            bound = sig.bind(self, *a, **k)
            args = bound.arguments
            uid = who(args)
            lang = resolver(uid) if (ENABLED and isinstance(uid, int) and uid > 0) else None
            if text_param and isinstance(args.get(text_param), str):
                args[text_param] = await _process(args[text_param], lang, limit, _is_html(args.get("parse_mode")))
            if args.get("reply_markup") is not None:
                args["reply_markup"] = await translate_markup(args["reply_markup"], lang)
            call_a, call_k = bound.args, bound.kwargs
        except Exception as e:
            logger.warning(f"autotranslate hook {name}: {e}")
            call_a, call_k = (self,) + a, k
        return await orig(*call_a, **call_k)

    wrapper._autotr = True
    setattr(cls, name, wrapper)
    return True


def install(resolver, client_cls=None, callback_cls=None) -> bool:
    """resolver(user_id) -> language code or None. Patches pyrogram once; safe to call twice."""
    try:
        if client_cls is None:
            from pyrogram import Client as client_cls
            from pyrogram.types import CallbackQuery as callback_cls
        _load_cache()
        atexit.register(save_cache)
        n = 0
        for name, (tp, limit) in CLIENT_METHODS.items():
            n += _wrap(client_cls, name, tp, limit, lambda args: args.get("chat_id"), resolver)
        if callback_cls is not None:
            n += _wrap(callback_cls, "answer", "text", 200,
                       lambda args: getattr(getattr(args.get("self"), "from_user", None), "id", None), resolver)
        logger.info(f"autotranslate: {'on' if ENABLED else 'off (AUTO_TRANSLATE=0)'}, {n} methods hooked")
        return True
    except Exception as e:
        logger.warning(f"autotranslate: install failed, messages stay untranslated: {e}")
        return False

"""
Telegram premium (custom) emoji for every message the bot sends.

How it works: install() patches pyrogram's HTML parser once, so EVERY outgoing HTML message/caption
(reply, send_message, edit_text, photo captions...) is run through emojify(): plain emoji that have an
ID below become <emoji id="..."> tags, which pyrogram turns into custom-emoji entities. No call site in
bot.py needs to change.

If Telegram does not allow the bot to use custom emoji, it simply shows the normal emoji — nothing breaks.
Set PREMIUM_EMOJI=0 in the env to switch it off.
"""
import inspect
import logging
import os
import re

logger = logging.getLogger("ytbot")

ENABLED = os.getenv("PREMIUM_EMOJI", "1").strip() != "0"
# Inline buttons: show the leading emoji of a button label as a custom-emoji icon (needs a Telegram/kurigram
# build that supports icon_custom_emoji_id; otherwise the plain label is used). BUTTON_ICONS=0 switches it off.
BUTTON_ICONS = ENABLED and os.getenv("BUTTON_ICONS", "1").strip() != "0"
# Telegram caps the number of entities per message — never turn more than this many emoji in one text.
MAX_PER_TEXT = 25

# Only emoji the bot really uses (unused ones are left out on purpose).
# If an emoji is in two maps, the first one wins.
# "⚡" (plain) and "⚡️" (with variation selector) are different keys on purpose — the bot uses both.
PREMIUM_EMOJI_IDS = {
    "✅": "6298612102709909362",
    "❌": "6206110936789423908",
    "⚡": "6026367225466720832",
    "⚡️": "5042334757040423886",
    "💠": "5971837723676249096",
    "▶️": "6285315214673975495",
    "🛑": "5420323339723881652",
    "📊": "5971837723676249096",
    "📦": "6066395745139824604",
    "📋": "5974235702701853774",
    "🔄": "5971837723676249096",
    "⏳": "5325583469344989152",
    "🚀": "6282977077427702833",
    "⚠️": "5420323339723881652",
    "💎": "5462902520215002477",
    "⭐": "5267500801240092311",
    "💳": "5472250091332993630",
    "👑": "5039727497143387500",
    "🎯": "5444197605947085469",
    "📌": "5420323339723881652",
    # --- from the "Global Emojis Map" (only emoji the bot uses; overlaps keep the IDs above) ---
    "➖": "5870818207383686839",
    "🌐": "5334590977837403844",
    "⌛": "5337172996211648018",
    "👤": "5352861489541714456",
    "👋": "5353027129250453493",
    "1️⃣": "5352651766288652742",
    "2️⃣": "5355186458418257716",
    "3️⃣": "5352867219028091093",
    "📤": "5353001161878182134",
    "✨": "5352552689983067014",
    "🔹": "5352638632278660622",
    "📅": "5352585194295564660",
    "📱": "5337132498965010628",
    "🔗": "5420517437885943844",
    "➕": "5420323438508155202",
    "🎁": "5420396762189831222",
    "🛡": "5190447043545438788",
    "🟢": "5192812028632274956",
    "📢": "5789428375261023681",
    "🆔": "5226929552319594190",
    "🔔": "5352980533150259581",
    "🔍": "5463352748751753567",
    "🔑": "5197288647275071607",
    "🏆": "5240021484516185513",
    "👥": "5420145051336485498",
    "🎉": "5420396762189831222",
    # --- from the third map (only emoji the bot uses that were not mapped yet) ---
    "🔒": "5296369303661067030",
    "🤖": "5323262125420871113",
    "👨\u200d💻": "5301083932211550593",
    "📚": "5373098009640836781",
    "⏱️": "5382194935057372936",
    # --- from the user's theme list (not used in bot.py yet; ready for when it is) ---
    "🔥": "5427009714745579975",
    # --- from the "Anuj Kumar" map (only emoji bot.py uses that were not mapped yet) ---
    "🆓": "5284976439051954090",
    "👉": "5471978009449731768",
    "🔴": "5416044289576673808",
    "ℹ️": "5334544901428229844",
    "❓": "5327917526372328990",
    # --- from the second "Anuj Kumar" list (only emoji bot.py uses that were still unmapped) ---
    "🎵": "5463107823946717464",
    "💡": "5041790387115524994",
    # --- from the third "Anuj Kumar" list (only emoji bot.py uses that were still unmapped) ---
    "🔙": "6159071084869066737",
    "📸": "5235837920081887219",
    "👇": "6136673152542973446",
    "✂️": "5870462219019358212",
    "🔧": "5258023599419171861",
    "🖼": "5775949822993371030",
    "📥": "6201809243574638159",
    # --- from the eMap list ---
    "📞": "5947494995798789024",
    # --- from the big emoji map (only emoji bot.py uses that were still unmapped) ---
    "🔎": "5188311512791393083",
    "📺": "5373330964372004748",
    "⏰": "5413704112220949842",
    # --- from the latest map (🍪 🆕 🧩 were the last text-message emoji still unmapped) ---
    "🍪": "5782979086729089520",
    "🆕": "6026115347109646923",
    "🧩": "6030802547998986847",
    # --- ⛔: taken from a real forwarded Telegram message (entity offset checked), so it is verified ---
    "⛔": "5201743838326037402",
    # --- 🎬: from the same big map that matched real Telegram IDs (📥 🖼 🗂 📊) ---
    "🎬": "5375464961822695044",
    # --- ☎️: bot.py uses it only in the Support reply-keyboard button, so no visible effect yet ---
    "☎️": "5465169893580086142",
    "🤨": "5370562939554111732",
}

_FE0F = "\ufe0f"


def _base(k: str) -> str:
    return k[:-1] if k.endswith(_FE0F) else k


# base emoji -> {"plain": id, "fe": id}: the same emoji may be typed with or without the variation selector.
_BY_BASE: dict = {}
for _k, _id in PREMIUM_EMOJI_IDS.items():
    _BY_BASE.setdefault(_base(_k), {})["fe" if _k.endswith(_FE0F) else "plain"] = _id
# Longest first; trailing FE0F optional, so "⚠" and "⚠️" both match.
_EMOJI_RE = re.compile("|".join(re.escape(b) + _FE0F + "?" for b in sorted(_BY_BASE, key=len, reverse=True)))


def _emoji_id(matched: str) -> str:
    has_fe = matched.endswith(_FE0F)
    ids = _BY_BASE[matched[:-1] if has_fe else matched]
    return (ids.get("fe") or ids["plain"]) if has_fe else (ids.get("plain") or ids["fe"])


_TAG_RE = re.compile(r"(<[^>]*>)")
_NO_TOUCH_OPEN = ("<code", "<pre", "<emoji", "<tg-emoji")
_NO_TOUCH_CLOSE = ("</code", "</pre", "</emoji", "</tg-emoji")


def emojify(text):
    """Wrap known emoji in <emoji id=...>. Skips tags, <code>/<pre> contents and already-wrapped emoji."""
    if not ENABLED or not isinstance(text, str) or not text:
        return text
    out, skip, used = [], 0, [0]

    def _sub(m):
        if used[0] >= MAX_PER_TEXT:
            return m.group(0)
        used[0] += 1
        return f'<emoji id="{_emoji_id(m.group(0))}">{m.group(0)}</emoji>'

    for part in _TAG_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith(_NO_TOUCH_OPEN):
                skip += 1
            elif low.startswith(_NO_TOUCH_CLOSE):
                skip = max(0, skip - 1)
            out.append(part)
        else:
            out.append(part if skip else _EMOJI_RE.sub(_sub, part))
    return "".join(out)


def split_button_icon(text):
    """'🔙 Back' -> ('Back', '<custom emoji id>'). Returns (text, None) if the label does not start with a
    known emoji or nothing would be left of the label."""
    if not BUTTON_ICONS or not isinstance(text, str):
        return text, None
    m = _EMOJI_RE.match(text.lstrip())
    if not m:
        return text, None
    rest = text.lstrip()[m.end():].strip()
    if not rest:
        return text, None
    return rest, _emoji_id(m.group(0))


def install() -> bool:
    """Patch pyrogram's HTML parser. Safe: on any problem it logs and leaves normal emoji."""
    if not ENABLED:
        logger.info("premium emoji: disabled (PREMIUM_EMOJI=0)")
        return False
    try:
        from pyrogram.parser.html import HTML

        orig = HTML.parse
        if getattr(orig, "_premium_emoji", False):
            return True
        if inspect.iscoroutinefunction(orig):
            async def parse(self, text, *a, **k):
                return await orig(self, emojify(text), *a, **k)
        else:
            def parse(self, text, *a, **k):
                return orig(self, emojify(text), *a, **k)
        parse._premium_emoji = True
        HTML.parse = parse
        logger.info(f"premium emoji: ON ({len(PREMIUM_EMOJI_IDS)} emoji mapped)")
        return True
    except Exception as e:
        logger.warning(f"premium emoji: not installed ({e}) — normal emoji will be used")
        return False

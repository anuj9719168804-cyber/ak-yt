"""YouTube Data API v3 helper — used ONLY for metadata (info card, search, playlist listing).

The API cannot give stream/format URLs, so yt-dlp is still what lists qualities and downloads.
Everything here fails soft: on any problem a function returns None / {} and the caller falls back to
yt-dlp, so a missing, wrong or quota-exhausted key never breaks the bot.

The key is read from the YT_API_KEY environment variable — never hardcode it in the source.
Quota: 10,000 units/day by default. videos.list and playlistItems.list cost 1 unit, search.list costs 100.
"""
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

API_KEY = (os.getenv("YT_API_KEY") or "AIzaSyCGfwA660Ba65cheWLn8ybj7eIbA4xhPQ0").strip()
BASE = "https://www.googleapis.com/youtube/v3"
TIMEOUT = float(os.getenv("YT_API_TIMEOUT", "8") or 8)

_disabled_until = 0.0
_warned = False

# YouTube videoCategory ids -> names (these ids are fixed worldwide)
CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals",
    "17": "Sports", "18": "Short Movies", "19": "Travel & Events", "20": "Gaming",
    "21": "Videoblogging", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education",
    "28": "Science & Technology", "29": "Nonprofits & Activism", "30": "Movies",
    "31": "Anime/Animation", "32": "Action/Adventure", "33": "Classics", "34": "Comedy",
    "35": "Documentary", "36": "Drama", "37": "Family", "38": "Foreign", "39": "Horror",
    "40": "Sci-Fi/Fantasy", "41": "Thriller", "42": "Shorts", "43": "Shows", "44": "Trailers",
}


def enabled() -> bool:
    return bool(API_KEY) and time.time() >= _disabled_until


def _disable(seconds: int, why: str):
    global _disabled_until
    _disabled_until = time.time() + seconds
    logger.warning(f"YouTube API paused for {seconds // 60} min: {why} (falling back to yt-dlp)")


def _get(endpoint: str, **params):
    """GET one API endpoint. Returns parsed JSON or None. Never logs the key."""
    global _warned
    if not enabled():
        if not API_KEY and not _warned:
            _warned = True
            logger.info("YT_API_KEY not set - using yt-dlp only for info/search/playlist")
        return None
    params["key"] = API_KEY
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reason = ""
        try:
            reason = (json.loads(e.read().decode("utf-8")).get("error", {}).get("errors") or [{}])[0].get("reason", "")
        except Exception:
            pass
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            _disable(3600, f"quota ({reason})")
        elif reason in ("keyInvalid", "keyExpired", "accessNotConfigured", "ipRefererBlocked",
                        "forbidden", "API_KEY_INVALID") or e.code == 400 and "key" in reason.lower():
            _disable(6 * 3600, f"key problem ({reason or e.code})")
        else:
            logger.info(f"YouTube API {endpoint} HTTP {e.code} {reason}")
        return None
    except Exception as e:
        logger.info(f"YouTube API {endpoint} failed: {type(e).__name__}")
        return None


def _iso_duration(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def video_details(ids: list) -> dict:
    """{video_id: {title, uploader, channel_id, duration, view_count, like_count, comment_count,
    upload_date, categories}} for up to 50 ids per call. Missing fields are simply absent."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 50):
        data = _get("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]),
                    fields="items(id,snippet(title,channelTitle,channelId,publishedAt,categoryId),"
                           "contentDetails(duration),statistics(viewCount,likeCount,commentCount))")
        if not data:
            continue
        for it in data.get("items") or []:
            sn, cd, st = it.get("snippet") or {}, it.get("contentDetails") or {}, it.get("statistics") or {}
            d = {}
            if sn.get("title"):
                d["title"] = sn["title"]
            if sn.get("channelTitle"):
                d["uploader"] = sn["channelTitle"]
            if sn.get("channelId"):
                d["channel_id"] = sn["channelId"]
            dur = _iso_duration(cd.get("duration", ""))
            if dur:
                d["duration"] = dur
            for src, dst in (("viewCount", "view_count"), ("likeCount", "like_count"),
                             ("commentCount", "comment_count")):
                n = _to_int(st.get(src))
                if n is not None:
                    d[dst] = n
            pub = (sn.get("publishedAt") or "")[:10].replace("-", "")
            if len(pub) == 8 and pub.isdigit():
                d["upload_date"] = pub
            cat = CATEGORIES.get(str(sn.get("categoryId") or ""))
            if cat:
                d["categories"] = [cat]
            out[it["id"]] = d
    return out


def enrich_info(info: dict) -> dict:
    """Overlay API metadata on a yt-dlp info dict (in place). The API's counts/title are authoritative;
    channel link and duration only fill gaps. Formats are never touched. Returns info."""
    try:
        vid = info.get("id")
        if not vid or not enabled():
            return info
        d = video_details([vid]).get(vid)
        if not d:
            return info
        for k in ("title", "uploader", "view_count", "like_count", "comment_count", "upload_date", "categories"):
            if d.get(k):
                info[k] = d[k]
        if not info.get("duration") and d.get("duration"):
            info["duration"] = d["duration"]
        if not (info.get("uploader_url") or info.get("channel_url")) and d.get("channel_id"):
            info["channel_url"] = f"https://www.youtube.com/channel/{d['channel_id']}"
    except Exception as e:
        logger.info(f"enrich_info skipped: {type(e).__name__}")
    return info


def search(query: str, count: int):
    """Video search -> [{id, title, uploader, duration_s}] (up to 50), or None to fall back to yt-dlp."""
    data = _get("search", part="snippet", type="video", q=query, maxResults=max(1, min(int(count), 50)),
                fields="items(id(videoId),snippet(title,channelTitle))")
    if data is None:
        return None
    items = [(it["id"]["videoId"], it.get("snippet") or {}) for it in data.get("items") or []
             if (it.get("id") or {}).get("videoId")]
    durs = video_details([v for v, _ in items])
    out = []
    for vid, sn in items:
        out.append({
            "id": vid,
            "title": html.unescape(sn.get("title") or "Untitled"),
            "uploader": html.unescape(sn.get("channelTitle") or ""),
            "duration_s": (durs.get(vid) or {}).get("duration") or 0,
        })
    return out


def playlist_id_from_url(url: str):
    try:
        return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("list") or [None])[0]
    except Exception:
        return None


def playlist(url: str, limit: int = 0):
    """Playlist -> {"title", "entries": [{id, title, url}]}, or None to fall back to yt-dlp.
    Auto-generated mixes (RD...), Watch Later / Liked etc. aren't served by the API -> None."""
    pid = playlist_id_from_url(url)
    if not pid or pid.startswith(("RD", "WL", "LL", "UU")):
        return None
    meta = _get("playlists", part="snippet", id=pid, fields="items(snippet(title))")
    if not meta or not meta.get("items"):
        return None
    title = (meta["items"][0].get("snippet") or {}).get("title") or "Playlist"
    entries, token = [], None
    for _ in range(40):  # 40 pages x 50 = 2000 videos at most
        params = dict(part="snippet", playlistId=pid, maxResults=50,
                      fields="nextPageToken,items(snippet(title,resourceId(videoId)))")
        if token:
            params["pageToken"] = token
        data = _get("playlistItems", **params)
        if data is None:
            return None  # partial listing is worse than a clean fallback
        for it in data.get("items") or []:
            sn = it.get("snippet") or {}
            vid = (sn.get("resourceId") or {}).get("videoId")
            t = sn.get("title") or "video"
            if not vid or t in ("Private video", "Deleted video", "[Private video]", "[Deleted video]"):
                continue
            entries.append({"id": vid, "title": t, "url": f"https://www.youtube.com/watch?v={vid}"})
        token = data.get("nextPageToken")
        if not token or (limit and len(entries) >= limit):
            break
    if limit:
        entries = entries[:limit]
    return {"title": title, "entries": entries}

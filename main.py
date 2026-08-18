"""
Render pe deploy karne wala YouTube info API.
Kaam: yt-dlp se video/playlist/search ka info nikalna aur HAR tarah ke
formats (audio-only, video-only, combined/muxed) ki list with direct
CDN url return karna, saath me subtitles, thumbnails, chapters,
playlist aur search bhi.

⚠️ COOKIES SYSTEM HATA DIYA GAYA HAI. Wajah: Render free tier ka
filesystem ephemeral hai (service so jaate hi disk wipe), isliye
"self-refreshing" cookies bhi practically 1-2 requests ke baad hi
purani ho jaati thi aur baar-baar manual re-upload ki zaroorat padti
thi — jo ki maintain karna possible nahi tha.

Iski jagah ab: agar YouTube seedha block kare ("Sign in to confirm
you're not a bot"), API automatically PIPED (piped.video jaisa free,
open-source YouTube-proxy network — koi login/cookies nahi chahiye)
pe fallback kar deta hai. Ye guarantee nahi hai (Piped instances khud
kabhi down/slow ho sakti hain, aur unki quality yt-dlp jitni high na
ho — usually 1080p tak), lekin bina kisi login/maintenance ke chalta
rehta hai, jo is use-case ke liye zyada practical hai.

Downloading, transcoding, muxing — sab kuch bot side pe hota hai,
ye API sirf "info + urls" deta hai.

Local test:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000
    curl "http://localhost:8000/info?url=https://youtu.be/XXXXXXXXXXX"

Render pe deploy:
    Build Command : pip install -r requirements.txt
    Start Command : uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import os
import re
import time
import requests
import yt_dlp

app = FastAPI(title="ytinfo-api", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Key protection ──────────────────────────────────────────────
# Render dashboard → Environment → API_KEY set karo (koi bhi random
# lamba string). Uske baad har request me ?apikey=... ya X-API-Key
# header dena zaroori hoga. Agar API_KEY set hi na karo to protection
# off rehta hai.
API_KEY = os.environ.get("API_KEY", "").strip()


def require_api_key(
    apikey: str | None = Query(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    if not API_KEY:
        return
    if (apikey or x_api_key) != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Pass ?apikey=... or X-API-Key header.")


# bgutil-ytdlp-pot-provider ka HTTP server URL (optional). Ye cookies
# nahi maangta, isliye rakhna free hai — lekin note karo ki maintainer
# khud keh chuke hain ki PO token ab zyada cases me bot-check bypass
# nahi karta. Isliye ye sirf "helps sometimes" hai, primary fix nahi.
BGUTIL_PROVIDER_URL = os.environ.get("BGUTIL_PROVIDER_URL", "").strip()

AUTH_ERROR_MARKERS = (
    "sign in to confirm",
    "login_required",
    "confirm you're not a bot",
    "confirm you\u2019re not a bot",
)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in AUTH_ERROR_MARKERS)


def _base_ydl_opts(logger=None, verbose: bool = False) -> dict:
    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {"player_client": ["android", "ios", "tv", "mweb", "web"]},
            **({"youtubepot-bgutilhttp": {"base_url": [BGUTIL_PROVIDER_URL]}} if BGUTIL_PROVIDER_URL else {}),
        },
    }
    if verbose:
        opts["verbose"] = True
    if logger is not None:
        opts["logger"] = logger
    return opts


def _warm_bgutil_provider(retries: int = 2, delay_seconds: int = 4) -> bool:
    if not BGUTIL_PROVIDER_URL:
        return False
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(f"{BGUTIL_PROVIDER_URL}/ping", timeout=30)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(delay_seconds)
    return False


# ══════════════════════════════════════════════════════════════════════
#   PIPED FALLBACK — cookies ke bina bhi kaam chalane ka tareeka
#   ─────────────────────────────────────────────────────────────────
#   Piped (github.com/TeamPiped/Piped) ek open-source, federated
#   YouTube-proxy network hai — dozens of free public instances jo khud
#   server-side pe stream URLs resolve karke dete hain, kisi login/
#   cookies ki zaroorat nahi. Jab yt-dlp "Sign in to confirm you're not
#   a bot" de, hum automatically inhi instances me se ek try karte hain.
#   Instance list dynamically fetch hoti hai (community ka suggestion
#   yahi hai, kyunki instances aate-jaate rehte hain) — fetch fail ho
#   to hardcoded fallback list use hoti hai.
# ══════════════════════════════════════════════════════════════════════

def _race_get(bases: list[str], path: str, params: dict | None = None, timeout: int = 10, max_workers: int = 15):
    """Kai instances ko EK SAATH (parallel) hit karta hai, jo bhi pehle
    valid jawab de use turant le leta hai. Pehle serial try hota tha —
    dead/slow instances ke saath 10-15 instances ko baari-baari try
    karne me hi 60-100+ second lag jaate the, jisme beech ke kayi
    genuinely fine instances bhi timeout ki wajah se miss ho jaate the.
    Parallel karne se poori race sirf ek timeout jitni der (~10s) me
    khatam ho jaati hai, chahe kitni bhi instances try karo."""
    bases = list(bases)
    random.shuffle(bases)  # hamesha wahi pehli 2-3 instances overload na ho
    errors = []

    def try_one(base: str):
        try:
            r = requests.get(base.rstrip("/") + path, params=params, timeout=timeout)
            if r.status_code != 200:
                return None, f"{base} -> HTTP {r.status_code}"
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                return None, f"{base} -> {data.get('message') or data.get('error')}"
            return (data, base), None
        except Exception as e:
            return None, f"{base} -> {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=min(max_workers, len(bases))) as ex:
        futures = {ex.submit(try_one, b): b for b in bases}
        for fut in as_completed(futures):
            result, err = fut.result()
            if result:
                return result
            errors.append(err)

    raise RuntimeError("Sabhi instances fail ho gaye:\n" + "\n".join(errors))


FALLBACK_PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.nosebs.ru",
    "https://pipedapi-libre.kavin.rocks",
    "https://piped-api.privacy.com.de",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
    "https://pipedapi.drgns.space",
    "https://pipedapi.owo.si",
    "https://pipedapi.ducks.party",
    "https://piped-api.codespace.cz",
    "https://pipedapi.reallyaweso.me",
    "https://api.piped.private.coffee",
    "https://pipedapi.darkness.services",
    "https://pipedapi.orangenet.cc",
]

_piped_instances_cache = {"ts": 0.0, "list": []}


def _get_piped_instances() -> list[str]:
    """Live instance list ko 1 ghante tak cache karta hai. Do alag
    sources try karta hai (instances-API pehle, phir docs repo ki
    markdown list) kyunki dono kabhi-kabhi down ho jaate hain — ek
    fail ho to doosra kaam aata hai. Dono fail ho to hardcoded list
    (jo abhi ke known-active instances se banayi gayi hai) use hoti hai."""
    now = time.time()
    if _piped_instances_cache["list"] and (now - _piped_instances_cache["ts"]) < 3600:
        return _piped_instances_cache["list"]

    fresh = []
    try:
        r = requests.get("https://piped-instances.kavin.rocks/", timeout=6)
        if r.status_code == 200:
            fresh = [i.get("api_url") for i in r.json() if i.get("api_url")]
    except Exception:
        pass

    if not fresh:
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/TeamPiped/documentation/main/content/docs/public-instances/index.md",
                timeout=6,
            )
            if r.status_code == 200:
                fresh = re.findall(r"\|\s*(https://\S+?)\s*\|", r.text)
        except Exception:
            pass

    instances = (fresh + [i for i in FALLBACK_PIPED_INSTANCES if i not in fresh]) if fresh else list(FALLBACK_PIPED_INSTANCES)
    _piped_instances_cache["ts"] = now
    _piped_instances_cache["list"] = instances
    return instances


def _piped_get(path: str, params: dict | None = None, max_tries: int = 15):
    """Sabhi (ya top max_tries) Piped instances ko parallel race karta
    hai — jo pehle jawab de wahi use ho jaata hai."""
    try:
        return _race_get(_get_piped_instances()[:max_tries], path, params=params)
    except RuntimeError as e:
        raise RuntimeError(f"Piped: {e}")


# ══════════════════════════════════════════════════════════════════════
#   INVIDIOUS FALLBACK — Piped bhi fail ho jaaye to doosra jaal
#   ─────────────────────────────────────────────────────────────────
#   Invidious bhi Piped jaisa hi ek open, federated YouTube-proxy
#   network hai, lekin ek bilkul alag codebase/community ke sath.
#   Dono networks ka ek saath down hona kaafi rare hai — isliye ye
#   dusra independent safety net hai.
# ══════════════════════════════════════════════════════════════════════

FALLBACK_INVIDIOUS_INSTANCES = [
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://iv.ggtyler.dev",
    "https://inv.nadeko.net",
    "https://invidious.jing.rocks",
    "https://inv.tux.pizza",
    "https://invidious.privacyredirect.com",
    "https://invidious.protokolla.fi",
]

_invidious_instances_cache = {"ts": 0.0, "list": []}


def _get_invidious_instances() -> list[str]:
    now = time.time()
    if _invidious_instances_cache["list"] and (now - _invidious_instances_cache["ts"]) < 3600:
        return _invidious_instances_cache["list"]

    instances = list(FALLBACK_INVIDIOUS_INSTANCES)
    try:
        r = requests.get("https://api.invidious.io/instances.json?sort_by=type,health", timeout=6)
        if r.status_code == 200:
            fresh = [
                entry[1].get("uri")
                for entry in r.json()
                if isinstance(entry, list) and len(entry) > 1 and entry[1].get("type") == "https" and entry[1].get("api") and entry[1].get("uri")
            ]
            if fresh:
                instances = fresh + [i for i in FALLBACK_INVIDIOUS_INSTANCES if i not in fresh]
    except Exception:
        pass

    _invidious_instances_cache["ts"] = now
    _invidious_instances_cache["list"] = instances
    return instances


def _invidious_get(path: str, params: dict | None = None, max_tries: int = 8):
    """Sabhi (ya top max_tries) Invidious instances ko parallel race
    karta hai."""
    try:
        return _race_get(_get_invidious_instances()[:max_tries], path, params=params)
    except RuntimeError as e:
        raise RuntimeError(f"Invidious: {e}")


YOUTUBE_ID_RE = re.compile(r"(?:v=|/videos/|embed/|youtu\.be/|shorts/|live/)([0-9A-Za-z_-]{11})")
PLAYLIST_ID_RE = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")


def _video_id_from_url(url: str) -> str | None:
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def _playlist_id_from_url(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url)
    if m:
        return m.group(1)
    return url if url.startswith(("PL", "UU", "LL", "FL")) else None


def _kbps(bitrate) -> float | None:
    if not bitrate:
        return None
    return round(bitrate / 1000, 1)


def _piped_normalize(pj: dict, video_id: str) -> dict:
    """Piped ke /streams response ko yt-dlp jaisi shape me convert
    karta hai, taaki neeche wala saara format/response-building code
    dono sources ke liye same rahe."""
    formats = []
    for i, a in enumerate(pj.get("audioStreams") or []):
        formats.append({
            "format_id":  f"piped-a{i}",
            "ext":        (a.get("format") or "m4a").lower(),
            "acodec":     a.get("codec"),
            "vcodec":     "none",
            "abr":        _kbps(a.get("bitrate")),
            "asr":        None,
            "height":     None,
            "width":      None,
            "fps":        None,
            "filesize":   None,
            "url":        a.get("url"),
            "format_note": a.get("quality"),
            "protocol":   "https",
            "language":   None,
            "dynamic_range": None,
            "audio_channels": None,
            "is_live":    bool(pj.get("livestream")),
        })
    for i, v in enumerate(pj.get("videoStreams") or []):
        video_only = v.get("videoOnly", True)
        formats.append({
            "format_id":  f"piped-v{i}",
            "ext":        (v.get("format") or "mp4").lower(),
            "acodec":     "none" if video_only else "unknown",
            "vcodec":     v.get("codec") or "unknown",
            "abr":        None if video_only else _kbps(v.get("bitrate")),
            "asr":        None,
            "height":     v.get("height"),
            "width":      v.get("width"),
            "fps":        v.get("fps"),
            "filesize":   None,
            "url":        v.get("url"),
            "format_note": v.get("quality"),
            "protocol":   "https",
            "language":   None,
            "dynamic_range": None,
            "audio_channels": None,
            "is_live":    bool(pj.get("livestream")),
        })

    subs, autosubs = {}, {}
    for s in pj.get("subtitles") or []:
        target = autosubs if s.get("autoGenerated") else subs
        target.setdefault(s.get("code") or "und", []).append(
            {"ext": "ttml", "name": s.get("name"), "url": s.get("url")}
        )

    return {
        "id":              video_id,
        "title":           pj.get("title"),
        "description":     pj.get("description"),
        "thumbnail":       pj.get("thumbnailUrl"),
        "thumbnails":      [{"url": pj.get("thumbnailUrl"), "width": None, "height": None}] if pj.get("thumbnailUrl") else [],
        "duration":        pj.get("duration"),
        "duration_string": None,
        "uploader":        pj.get("uploader"),
        "uploader_id":     None,
        "channel_url":     pj.get("uploaderUrl"),
        "upload_date":     pj.get("uploadDate"),
        "view_count":      pj.get("views"),
        "like_count":      pj.get("likes"),
        "comment_count":   None,
        "categories":      None,
        "tags":            None,
        "is_live":         bool(pj.get("livestream")),
        "was_live":        False,
        "availability":    None,
        "age_limit":       None,
        "chapters":        [],
        "formats":         formats,
        "subtitles":       subs,
        "automatic_captions": autosubs,
        "_source":         "piped",
    }


def _invidious_normalize(v: dict, video_id: str) -> dict:
    """Invidious ke /api/v1/videos response ko yt-dlp jaisi shape me
    convert karta hai."""
    formats = []
    for f in v.get("adaptiveFormats") or []:
        mime = f.get("type") or ""
        is_audio = mime.startswith("audio/")
        formats.append({
            "format_id":  f"inv-{f.get('itag')}",
            "ext":        f.get("container") or (mime.split("/")[1].split(";")[0] if "/" in mime else "mp4"),
            "acodec":     f.get("encoding") if is_audio else "none",
            "vcodec":     "none" if is_audio else (f.get("encoding") or "unknown"),
            "abr":        _kbps(f.get("bitrate")) if is_audio else None,
            "asr":        f.get("audioSampleRate"),
            "height":     None if is_audio else f.get("height"),
            "width":      None if is_audio else f.get("width"),
            "fps":        None if is_audio else f.get("fps"),
            "filesize":   None,
            "url":        f.get("url"),
            "format_note": f.get("qualityLabel") or f.get("audioQuality"),
            "protocol":   "https",
            "language":   None,
            "dynamic_range": None,
            "audio_channels": None,
            "is_live":    bool(v.get("isLive")),
        })
    for f in v.get("formatStreams") or []:
        formats.append({
            "format_id":  f"inv-c{f.get('itag')}",
            "ext":        f.get("container") or "mp4",
            "acodec":     "aac",
            "vcodec":     f.get("encoding") or "h264",
            "abr":        None,
            "asr":        None,
            "height":     f.get("height"),
            "width":      f.get("width"),
            "fps":        f.get("fps"),
            "filesize":   None,
            "url":        f.get("url"),
            "format_note": f.get("qualityLabel"),
            "protocol":   "https",
            "language":   None,
            "dynamic_range": None,
            "audio_channels": None,
            "is_live":    bool(v.get("isLive")),
        })

    subs = {}
    for c in v.get("captions") or []:
        subs.setdefault(c.get("languageCode") or "und", []).append(
            {"ext": "vtt", "name": c.get("label"), "url": c.get("url")}
        )

    thumbs = v.get("videoThumbnails") or []
    return {
        "id":              video_id,
        "title":           v.get("title"),
        "description":     v.get("description"),
        "thumbnail":       thumbs[-1].get("url") if thumbs else None,
        "thumbnails":      [{"url": t.get("url"), "width": t.get("width"), "height": t.get("height")} for t in thumbs],
        "duration":        v.get("lengthSeconds"),
        "duration_string": None,
        "uploader":        v.get("author"),
        "uploader_id":     v.get("authorId"),
        "channel_url":     v.get("authorUrl"),
        "upload_date":     None,
        "view_count":      v.get("viewCount"),
        "like_count":      v.get("likeCount"),
        "comment_count":   None,
        "categories":      None,
        "tags":            v.get("keywords"),
        "is_live":         bool(v.get("isLive")),
        "was_live":        False,
        "availability":    None,
        "age_limit":       None,
        "chapters":        [],
        "formats":         formats,
        "subtitles":       subs,
        "automatic_captions": {},
        "_source":         "invidious",
    }


def get_video_data(url: str) -> dict:
    """Teen-level fallback chain:
      1. yt-dlp (cookies ke bina — android/ios/tv/mweb/web clients)
      2. Piped network (dozens of free public instances)
      3. Invidious network (alag codebase/community, dusra safety net)
    Teeno fail ho to hi error uthaata hai — matlab poori request tabhi
    fail hogi jab dono independent proxy-networks EK SAATH down hon,
    jo ki rare hai."""
    _warm_bgutil_provider()
    try:
        with yt_dlp.YoutubeDL(_base_ydl_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
        data["_source"] = "yt-dlp"
        return data
    except Exception as e:
        if not _is_auth_error(e):
            raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")
        ytdlp_err = e

    video_id = _video_id_from_url(url)
    if not video_id:
        raise HTTPException(status_code=500, detail=f"yt-dlp ne bot-check pe roka ({ytdlp_err}) aur URL se video ID bhi nahi nikal paya, fallback nahi ho saka.")

    piped_err = None
    try:
        pj, _inst = _piped_get(f"/streams/{video_id}")
        return _piped_normalize(pj, video_id)
    except Exception as pe:
        piped_err = pe

    try:
        vj, _inst = _invidious_get(f"/api/v1/videos/{video_id}")
        return _invidious_normalize(vj, video_id)
    except Exception as ie:
        raise HTTPException(
            status_code=502,
            detail=f"yt-dlp bot-check pe roka, aur Piped ({piped_err}) + Invidious ({ie}) dono fallback fail ho gaye.",
        )


# ══════════════════════════════════════════════════════════════════════
#   FORMAT HELPERS — audio / video / combined, A to Z detail ke saath
# ══════════════════════════════════════════════════════════════════════

def _human_size(num) -> str | None:
    if not num:
        return None
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TB"


def _quality_label(f: dict) -> str:
    height = f.get("height")
    fps = f.get("fps")
    abr = f.get("abr")
    vcodec = f.get("vcodec")
    acodec = f.get("acodec")
    has_video = vcodec not in (None, "none")
    has_audio = acodec not in (None, "none")

    if has_video and height:
        label = f"{height}p"
        if fps and fps > 30:
            label += str(int(fps))
        dr = f.get("dynamic_range")
        if dr and dr != "SDR":
            label += f" {dr}"
        return label
    if has_audio and abr:
        return f"{int(round(abr))}kbps"
    return f.get("format_note") or f.get("format_id") or "unknown"


def _format_entry(f: dict) -> dict:
    acodec = f.get("acodec")
    vcodec = f.get("vcodec")
    has_audio = acodec not in (None, "none")
    has_video = vcodec not in (None, "none")
    filesize = f.get("filesize") or f.get("filesize_approx")

    if has_audio and has_video:
        kind = "combined"
    elif has_audio:
        kind = "audio"
    elif has_video:
        kind = "video"
    else:
        kind = "other"

    return {
        "itag":              f.get("format_id"),
        "type":              kind,
        "ext":               f.get("ext"),
        "container":         f.get("ext"),
        "format_note":       f.get("format_note"),
        "quality_label":     _quality_label(f),
        "acodec":            acodec,
        "vcodec":            vcodec,
        "protocol":          f.get("protocol"),
        "tbr_kbps":          f.get("tbr"),
        "abr_kbps":          f.get("abr"),
        "vbr_kbps":          f.get("vbr"),
        "asr_hz":            f.get("asr"),
        "height":            f.get("height"),
        "width":             f.get("width"),
        "fps":               f.get("fps"),
        "dynamic_range":     f.get("dynamic_range"),
        "audio_channels":    f.get("audio_channels"),
        "language":          f.get("language"),
        "filesize_bytes":    filesize,
        "filesize_readable": _human_size(filesize),
        "is_live":           bool(f.get("is_live")),
        "url":               f.get("url"),
    }


def _split_formats(raw_formats: list) -> tuple[list, list, list]:
    audio, video, combined = [], [], []
    for f in raw_formats:
        entry = _format_entry(f)
        if entry["type"] == "audio":
            audio.append(entry)
        elif entry["type"] == "video":
            video.append(entry)
        elif entry["type"] == "combined":
            combined.append(entry)

    audio.sort(key=lambda x: (x["abr_kbps"] or 0), reverse=True)
    video.sort(key=lambda x: (x["height"] or 0, x["fps"] or 0), reverse=True)
    combined.sort(key=lambda x: (x["height"] or 0), reverse=True)
    return audio, video, combined


def _filter_formats(
    formats: list,
    ext: str | None = None,
    min_height: int | None = None,
    max_height: int | None = None,
    min_abr: float | None = None,
) -> list:
    out = formats
    if ext:
        wanted = {e.strip().lower() for e in ext.split(",") if e.strip()}
        out = [f for f in out if (f.get("ext") or "").lower() in wanted]
    if min_height is not None:
        out = [f for f in out if (f.get("height") or 0) >= min_height]
    if max_height is not None:
        out = [f for f in out if (f.get("height") or 0) <= max_height]
    if min_abr is not None:
        out = [f for f in out if (f.get("abr_kbps") or 0) >= min_abr]
    return out


def _extract_subs(data: dict, auto: bool = False) -> dict:
    src = data.get("automatic_captions") if auto else data.get("subtitles")
    src = src or {}
    return {
        lang: [
            {"ext": s.get("ext"), "name": s.get("name"), "url": s.get("url")}
            for s in items
        ]
        for lang, items in src.items()
    }


def _thumbnails(data: dict) -> list:
    thumbs = data.get("thumbnails") or []
    return [
        {"url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
        for t in thumbs
        if t.get("url")
    ]


def _chapters(data: dict) -> list:
    chapters = data.get("chapters") or []
    return [
        {"title": c.get("title"), "start_time": c.get("start_time"), "end_time": c.get("end_time")}
        for c in chapters
    ]


class _LogCapture:
    def __init__(self):
        self.lines: list[str] = []

    def debug(self, msg):
        self.lines.append(msg)

    def info(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append(f"WARNING: {msg}")

    def error(self, msg):
        self.lines.append(f"ERROR: {msg}")


# ══════════════════════════════════════════════════════════════════════
#   ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {
        "status": True,
        "name": "ytinfo-api",
        "version": "3.0",
        "note": "Cookies system hata diya gaya hai — bot-block hone pe API khud Piped network pe fallback karta hai.",
        "endpoints": {
            "GET /info":      "Single video ka poora info + audio/video/combined formats",
            "GET /best":      "Ek video, ek audio format — quickest 'best pick' shortcut",
            "GET /formats":   "Sirf format list (halka payload, metadata ke bina)",
            "GET /subtitles": "Available subtitle/caption languages + direct URLs",
            "GET /playlist":  "Playlist ke andar ki videos ki list",
            "GET /search":    "YouTube search results",
            "GET /debug":     "yt-dlp aur Piped fallback dono ka status (troubleshooting)",
            "GET /health":    "Health check",
        },
    }


@app.get("/info")
def info(
    url: str = Query(..., description="YouTube video URL"),
    ext: str | None = Query(None, description="Comma-separated container filter, e.g. mp4,webm"),
    min_height: int | None = Query(None),
    max_height: int | None = Query(None),
    min_abr: float | None = Query(None),
    include_subs: bool = Query(False),
    include_chapters: bool = Query(True),
    _auth=Depends(require_api_key),
):
    data = get_video_data(url)

    audio, video, combined = _split_formats(data.get("formats", []))
    audio = _filter_formats(audio, ext=ext, min_abr=min_abr)
    video = _filter_formats(video, ext=ext, min_height=min_height, max_height=max_height)
    combined = _filter_formats(combined, ext=ext, min_height=min_height, max_height=max_height)

    if not audio and not video and not combined:
        raise HTTPException(status_code=502, detail="No downloadable formats found")

    best_audio = audio[0] if audio else (combined[0] if combined else None)
    best_video = video[0] if video else (combined[0] if combined else None)

    result = {
        "status":            True,
        "source":            data.get("_source"),
        "videoId":           data.get("id"),
        "title":             data.get("title"),
        "description":       data.get("description"),
        "thumbnail":         data.get("thumbnail"),
        "thumbnails":        _thumbnails(data),
        "duration":          data.get("duration"),
        "duration_string":   data.get("duration_string"),
        "uploader":          data.get("uploader"),
        "uploader_id":       data.get("uploader_id"),
        "channel_url":       data.get("channel_url"),
        "upload_date":       data.get("upload_date"),
        "view_count":        data.get("view_count"),
        "like_count":        data.get("like_count"),
        "comment_count":     data.get("comment_count"),
        "categories":        data.get("categories"),
        "tags":              data.get("tags"),
        "is_live":           bool(data.get("is_live")),
        "was_live":          bool(data.get("was_live")),
        "availability":      data.get("availability"),
        "age_limit":         data.get("age_limit"),
        "audio_formats":     audio,
        "video_formats":     video,
        "combined_formats":  combined,
        "best": {"audio": best_audio, "video": best_video},
    }
    if include_chapters:
        result["chapters"] = _chapters(data)
    if include_subs:
        result["subtitles"] = _extract_subs(data, auto=False)
        result["automatic_captions"] = _extract_subs(data, auto=True)

    return result


@app.get("/formats")
def formats(
    url: str = Query(..., description="YouTube video URL"),
    _auth=Depends(require_api_key),
):
    data = get_video_data(url)
    audio, video, combined = _split_formats(data.get("formats", []))
    return {
        "status":           True,
        "source":           data.get("_source"),
        "videoId":          data.get("id"),
        "audio_formats":    audio,
        "video_formats":    video,
        "combined_formats": combined,
    }


@app.get("/best")
def best(
    url: str = Query(..., description="YouTube video URL"),
    height: int | None = Query(None, description="Is height ke barabar ya sabse kareeb video chuno"),
    ext: str | None = Query(None),
    _auth=Depends(require_api_key),
):
    data = get_video_data(url)
    audio, video, combined = _split_formats(data.get("formats", []))
    audio = _filter_formats(audio, ext=ext) or audio
    video_pool = _filter_formats(video, ext=ext) or video
    if not video_pool and combined:
        video_pool = combined

    chosen_video = None
    if video_pool:
        chosen_video = min(video_pool, key=lambda f: abs((f["height"] or 0) - height)) if height else video_pool[0]
    chosen_audio = audio[0] if audio else (combined[0] if combined else None)

    if not chosen_video and not chosen_audio:
        raise HTTPException(status_code=502, detail="No downloadable formats found")

    return {
        "status":  True,
        "source":  data.get("_source"),
        "videoId": data.get("id"),
        "title":   data.get("title"),
        "audio":   chosen_audio,
        "video":   chosen_video,
    }


@app.get("/subtitles")
def subtitles(
    url: str = Query(..., description="YouTube video URL"),
    auto: bool = Query(False),
    _auth=Depends(require_api_key),
):
    data = get_video_data(url)
    subs = _extract_subs(data, auto=auto)
    return {
        "status":         True,
        "source":         data.get("_source"),
        "videoId":        data.get("id"),
        "auto_generated": auto,
        "languages":      list(subs.keys()),
        "subtitles":      subs,
    }


@app.get("/playlist")
def playlist(
    url: str = Query(..., description="YouTube playlist URL"),
    limit: int = Query(50, ge=1, le=200),
    _auth=Depends(require_api_key),
):
    try:
        opts = _base_ydl_opts()
        opts.update({"noplaylist": False, "extract_flat": "in_playlist", "playlistend": limit})
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        entries = data.get("entries") or []
        videos = [
            {
                "videoId":   e.get("id"),
                "title":     e.get("title"),
                "url":       e.get("url") or (f"https://youtu.be/{e.get('id')}" if e.get("id") else None),
                "duration":  e.get("duration"),
                "uploader":  e.get("uploader") or e.get("channel"),
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
            }
            for e in entries
        ]
        return {"status": True, "source": "yt-dlp", "playlistId": data.get("id"), "playlistTitle": data.get("title"), "video_count": len(videos), "videos": videos}
    except Exception as e:
        if not _is_auth_error(e):
            raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

        playlist_id = _playlist_id_from_url(url)
        if not playlist_id:
            raise HTTPException(status_code=500, detail="yt-dlp bot-check pe roka aur playlist ID URL se nahi mila, Piped fallback nahi ho saka.")
        try:
            pj, _inst = _piped_get(f"/playlists/{playlist_id}")
        except Exception as pe:
            raise HTTPException(status_code=502, detail=f"yt-dlp bot-check pe roka aur Piped fallback bhi fail: {pe}")

        entries = (pj.get("relatedStreams") or [])[:limit]
        videos = [
            {
                "videoId":   _video_id_from_url(e.get("url") or ""),
                "title":     e.get("title"),
                "url":       f"https://youtu.be{e.get('url')}" if e.get("url") else None,
                "duration":  e.get("duration"),
                "uploader":  None,
                "thumbnail": e.get("thumbnail"),
            }
            for e in entries
        ]
        return {"status": True, "source": "piped", "playlistId": playlist_id, "playlistTitle": pj.get("name"), "video_count": len(videos), "videos": videos}


@app.get("/search")
def search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    _auth=Depends(require_api_key),
):
    try:
        opts = _base_ydl_opts()
        opts.update({"noplaylist": False, "extract_flat": "in_playlist"})
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
        entries = data.get("entries") or []
        results = [
            {
                "videoId":   e.get("id"),
                "title":     e.get("title"),
                "url":       e.get("url") or (f"https://youtu.be/{e.get('id')}" if e.get("id") else None),
                "duration":  e.get("duration"),
                "uploader":  e.get("uploader") or e.get("channel"),
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
            }
            for e in entries
        ]
        return {"status": True, "source": "yt-dlp", "query": q, "result_count": len(results), "results": results}
    except Exception as e:
        if not _is_auth_error(e):
            raise HTTPException(status_code=500, detail=f"yt-dlp search failed: {e}")

        try:
            pj, _inst = _piped_get("/search", params={"q": q, "filter": "videos"})
        except Exception as pe:
            raise HTTPException(status_code=502, detail=f"yt-dlp bot-check pe roka aur Piped search fallback bhi fail: {pe}")

        items = (pj.get("items") if isinstance(pj, dict) else pj) or []
        results = [
            {
                "videoId":   _video_id_from_url(it.get("url") or ""),
                "title":     it.get("title"),
                "url":       f"https://youtu.be{it.get('url')}" if it.get("url") else None,
                "duration":  it.get("duration"),
                "uploader":  it.get("uploaderName") or it.get("uploader"),
                "thumbnail": it.get("thumbnail"),
            }
            for it in items[:limit]
            if (it.get("url") or "").startswith("/watch")
        ]
        return {"status": True, "source": "piped", "query": q, "result_count": len(results), "results": results}


@app.get("/debug")
def debug(
    url: str = Query(..., description="YouTube video URL"),
    _auth=Depends(require_api_key),
):
    """yt-dlp ka poora verbose log + Piped fallback ka status — dono
    dikhata hai taaki pata chale kaun sa path use hua aur kyun."""
    logger = _LogCapture()
    warm_up_ok = _warm_bgutil_provider()

    ytdlp_ok = False
    ytdlp_format_count = 0
    ytdlp_error = None
    try:
        opts = _base_ydl_opts(logger=logger, verbose=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
            ytdlp_ok = True
            ytdlp_format_count = len(data.get("formats", []))
    except Exception as e:
        ytdlp_error = str(e)

    piped_ok = False
    piped_format_count = 0
    piped_error = None
    piped_instance_used = None
    invidious_ok = False
    invidious_format_count = 0
    invidious_error = None
    invidious_instance_used = None

    if not ytdlp_ok:
        video_id = _video_id_from_url(url)
        if video_id:
            try:
                pj, instance = _piped_get(f"/streams/{video_id}")
                piped_ok = True
                piped_instance_used = instance
                piped_format_count = len(pj.get("audioStreams", [])) + len(pj.get("videoStreams", []))
            except Exception as e:
                piped_error = str(e)

            if not piped_ok:
                try:
                    vj, instance = _invidious_get(f"/api/v1/videos/{video_id}")
                    invidious_ok = True
                    invidious_instance_used = instance
                    invidious_format_count = len(vj.get("adaptiveFormats", [])) + len(vj.get("formatStreams", []))
                except Exception as e:
                    invidious_error = str(e)
        else:
            piped_error = invidious_error = "video ID URL se nahi mila"

    return {
        "bgutil_provider_url_configured": bool(BGUTIL_PROVIDER_URL),
        "bgutil_provider_url": BGUTIL_PROVIDER_URL or None,
        "bgutil_warm_up_succeeded": warm_up_ok,
        "ytdlp_extracted_ok": ytdlp_ok,
        "ytdlp_format_count": ytdlp_format_count,
        "ytdlp_error": ytdlp_error,
        "piped_fallback_tried": not ytdlp_ok,
        "piped_extracted_ok": piped_ok,
        "piped_instance_used": piped_instance_used,
        "piped_format_count": piped_format_count,
        "piped_error": piped_error,
        "invidious_fallback_tried": not ytdlp_ok and not piped_ok,
        "invidious_extracted_ok": invidious_ok,
        "invidious_instance_used": invidious_instance_used,
        "invidious_format_count": invidious_format_count,
        "invidious_error": invidious_error,
        "logs": logger.lines,
    }


@app.get("/health")
def health():
    return {"status": True}

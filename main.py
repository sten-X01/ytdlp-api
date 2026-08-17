"""
Render pe deploy karne wala YouTube info API.
Kaam: yt-dlp se video/playlist/search ka info nikalna aur HAR tarah ke
formats (audio-only, video-only, combined/muxed — a-to-z sab) ki list
with direct CDN url return karna, saath me subtitles, thumbnails,
chapters, playlist aur search bhi.
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

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import base64
import os
import requests
import time
import yt_dlp

app = FastAPI(title="ytinfo-api", version="2.0")

# Browser / webapp se seedha call karna ho to CORS khula rakha hai.
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
# off rehta hai (jaisa pehle tha) — set karne ki strongly salaah hai
# warna koi bhi tumhari API free me use kar sakta hai.
#
# NOTE: pehle ye function bana hua tha lekin kisi bhi route pe laga
# hua nahi tha (bug) — ab har data-returning route pe Depends() laga
# diya gaya hai, taaki API_KEY set karne ka matlab ho.
API_KEY = os.environ.get("API_KEY", "").strip()


def require_api_key(
    apikey: str | None = Query(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    if not API_KEY:
        return
    if (apikey or x_api_key) != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Pass ?apikey=... or X-API-Key header.")


# bgutil-ytdlp-pot-provider ka HTTP server URL — Render dashboard me
# "Environment" tab se BGUTIL_PROVIDER_URL naam ka env var set karo
# (e.g. https://bgutil-pot-provider-xxxx.onrender.com). Isse PO token
# milta hai jo "web" client ko full quality formats dene deta hai.
BGUTIL_PROVIDER_URL = os.environ.get("BGUTIL_PROVIDER_URL", "").strip()

# ══════════════════════════════════════════════════════════════════════
#   COOKIE POOL — auto-rotating cookies system
#   ─────────────────────────────────────────────────────────────────
#   YouTube kabhi-kabhi "Sign in to confirm you're not a bot" degi
#   (Render jaise datacenter IPs ko flag karta hai). Iska ek hi real
#   fix hai: real browser session ki cookies. Poora automatic login
#   possible nahi hai (Google khud bot-login block karta hai), lekin
#   is se aage sab automatic hai:
#     • Env vars (COOKIES_1, COOKIES_2, ...) ya /cookies/upload se
#       daali gayi cookies.txt files ek "pool" banati hain
#     • Har extraction attempt pool ke har cookie-set ko baari-baari
#       try karta hai jab tak ek kaam na kare
#     • Jo cookie-set "Sign in to confirm"/LOGIN_REQUIRED de, use us
#       request ke liye skip karke agli try karta hai — matlab jab tak
#       pool me EK BHI valid cookie-set hai, downloads chalte rahenge
#     • /cookies/status se dekh sakte ho kaunsi cookies zinda hain
#     • Jab sab expire ho jaayein, /cookies/upload se nayi daal do
#       (browser se "Get cookies.txt LOCALLY" extension se export karo)
# ══════════════════════════════════════════════════════════════════════

COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies")
os.makedirs(COOKIE_DIR, exist_ok=True)

AUTH_ERROR_MARKERS = (
    "sign in to confirm",
    "login_required",
    "confirm you're not a bot",
    "confirm you\u2019re not a bot",
)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in AUTH_ERROR_MARKERS)


def _load_env_cookie_pool():
    """COOKIES_1, COOKIES_2, ... env vars (base64-encoded cookies.txt
    content) ko disk pe likhta hai — LEKIN SIRF agar file already exist
    nahi karti. Ye zaroori hai: yt-dlp har request ke baad is file me
    refreshed session tokens WAPAS likh deta hai (--cookies FILE load
    bhi karta hai aur save bhi usi file me). Agar hum har restart pe
    env var se dobara overwrite kar dete, to wo refresh hamesha udd
    jaata — isi wajah se cookies 'jaldi expire' hoti dikh rahi thi.
    Ab sirf pehli baar (fresh deploy ke baad) seed hoga, uske baad
    file khud-ba-khud apne aap fresh rehti hai."""
    i = 1
    while True:
        val = os.environ.get(f"COOKIES_{i}")
        if not val:
            break
        path = os.path.join(COOKIE_DIR, f"env_{i}.txt")
        if not os.path.exists(path):
            try:
                content = base64.b64decode(val).decode("utf-8")
                with open(path, "w") as f:
                    f.write(content)
            except Exception:
                pass
        i += 1


_load_env_cookie_pool()


def _cookie_pool_paths() -> list[str]:
    """Available cookie files ki list (env-seeded + upload se aayi hui)."""
    if not os.path.isdir(COOKIE_DIR):
        return []
    return sorted(
        os.path.join(COOKIE_DIR, f)
        for f in os.listdir(COOKIE_DIR)
        if f.endswith(".txt")
    )


def _base_ydl_opts(logger=None, verbose: bool = False) -> dict:
    opts = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {"player_client": ["tv", "mweb", "web"]},
            **({"youtubepot-bgutilhttp": {"base_url": [BGUTIL_PROVIDER_URL]}} if BGUTIL_PROVIDER_URL else {}),
        },
    }
    if verbose:
        opts["verbose"] = True
    if logger is not None:
        opts["logger"] = logger
    return opts


def _warm_bgutil_provider(retries: int = 3, delay_seconds: int = 5) -> bool:
    """Render free tier pe bgutil-pot-provider bhi inactivity ke baad so
    jaata hai, aur kabhi-kabhi ek hi ping try karne se cold-start ke beech
    502 mil jaata hai. Isliye ab ek nahi, teen attempts karte hain — jab
    tak 200 OK na mile ya retries khatam na ho jaayein. Returns True/False
    taaki /debug me confirm ho sake provider actually ready tha ya nahi."""
    if not BGUTIL_PROVIDER_URL:
        return False
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(f"{BGUTIL_PROVIDER_URL}/ping", timeout=60)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(delay_seconds)
    return False


def _extract(url: str, extra_opts: dict | None = None) -> dict:
    """Cookie pool ke har set ko baari-baari try karta hai (aur ek attempt
    bina cookies ke bhi, kyunki kabhi-kabhi wo bhi chal jaata hai). Sirf
    auth-error (LOGIN_REQUIRED) pe hi next cookie try karta hai — koi aur
    error ho to turant raise kar deta hai, chhupata nahi.

    extra_opts se yt-dlp options override/add kiye ja sakte hain — jaise
    playlist extraction ke liye noplaylist:False, ya search ke liye
    extract_flat, jisse ek hi function playlist/search/single-video sab
    ke liye reuse ho sake."""
    _warm_bgutil_provider()
    cookie_paths = _cookie_pool_paths()
    attempts = cookie_paths + [None]  # None = cookies ke bina, last resort

    last_error = None
    for cookiefile in attempts:
        opts = _base_ydl_opts()
        if extra_opts:
            opts.update(extra_opts)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = e
            if _is_auth_error(e):
                continue  # is cookie-set ka time khatam, agla try karo
            raise  # koi aur tarah ka error — foran surface karo, chhupao mat
    raise last_error


# ══════════════════════════════════════════════════════════════════════
#   FORMAT HELPERS — audio / video / combined, A to Z detail ke saath
# ══════════════════════════════════════════════════════════════════════

def _human_size(num) -> str | None:
    """Bytes ko readable string me convert karta hai (e.g. '4.7 MB')."""
    if not num:
        return None
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TB"


def _quality_label(f: dict) -> str:
    """Insaan-padhne-layak quality label banata hai — jaise '1080p60',
    '720p', '160kbps' — yt-dlp ke raw format_note se zyada consistent."""
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
    """Ek raw yt-dlp format dict ko poori detail wale clean dict me
    convert karta hai — audio, video, ya combined, sabke liye same
    shape (jo fields kisi format pe apply nahi hoti wo None rehti hain)."""
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
        kind = "other"  # storyboards / mhtml preview strips waghera

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
    """Raw yt-dlp formats ko teen categories me baantata hai:
      • audio  → audio-only streams (koi video nahi)
      • video  → video-only streams (koi audio nahi)
      • combined → ek hi stream me audio+video dono (progressive/muxed,
        jaise purana 360p mp4, ya live streams)
    Pehle combined skip ho jaata tha; ab A-to-Z coverage ke liye teeno
    return kiye jaate hain taaki bot jo bhi chahe use kar sake — agar
    seedha ek hi file chahiye (bina ffmpeg mux ke) to combined kaam
    aayega, agar best quality chahiye to audio-only + video-only mux
    karna hi behtar rehta hai (YouTube high quality sirf adaptive
    streams me deta hai)."""
    audio, video, combined = [], [], []
    for f in raw_formats:
        entry = _format_entry(f)
        if entry["type"] == "audio":
            audio.append(entry)
        elif entry["type"] == "video":
            video.append(entry)
        elif entry["type"] == "combined":
            combined.append(entry)
        # "other" (storyboards etc.) jaan-boojh kar drop kiya jaata hai

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
    """/info aur /best pe optional query filters lagane ke liye —
    e.g. ?ext=mp4 ya ?min_height=720 se sirf matching formats milte hain."""
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
    """info dict se subtitles / auto-captions ko clean {lang: [entries]}
    shape me nikalta hai."""
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
    """yt-dlp ke saare debug/warning/error messages ko capture karta hai
    taaki hum unhe HTTP response me dekh sakein (Render free tier pe
    Shell/SSH access nahi hota, isliye Logs tab ke alawa kahin aur se
    dekhna mushkil hai)."""
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
    """Chhota index — kaun se endpoints available hain."""
    return {
        "status": True,
        "name": "ytinfo-api",
        "version": "2.0",
        "endpoints": {
            "GET /info":            "Single video ka poora info + audio/video/combined formats",
            "GET /best":            "Ek video, ek audio format — quickest 'best pick' shortcut",
            "GET /formats":         "Sirf format list (halka payload, metadata ke bina)",
            "GET /subtitles":       "Available subtitle/caption languages + direct URLs",
            "GET /playlist":        "Playlist ke andar ki videos ki list",
            "GET /search":          "YouTube search results",
            "GET /debug":           "Verbose extraction log (troubleshooting ke liye)",
            "GET /cookies/status":  "Cookie pool me kaunsi cookies zinda hain",
            "GET /cookies/upload":  "Mobile browser se cookies.txt upload karne ka form",
            "POST /cookies/upload": "cookies.txt file upload (multipart/form-data)",
            "GET /health":          "Health check",
        },
    }


@app.get("/info")
def info(
    url: str = Query(..., description="YouTube video URL"),
    ext: str | None = Query(None, description="Comma-separated container filter, e.g. mp4,webm"),
    min_height: int | None = Query(None, description="Video formats ko is height se kam skip kar do"),
    max_height: int | None = Query(None, description="Video formats ko is height se zyada skip kar do"),
    min_abr: float | None = Query(None, description="Audio formats ko is bitrate (kbps) se kam skip kar do"),
    include_subs: bool = Query(False, description="Response me subtitles + auto-captions bhi jodo"),
    include_chapters: bool = Query(True, description="Response me chapters jodo (agar available hon)"),
    _auth=Depends(require_api_key),
):
    try:
        data = _extract(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

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
        "best": {
            "audio": best_audio,
            "video": best_video,
        },
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
    """Sirf formats — jab poora metadata (description, tags, waghera)
    nahi chahiye, bas quick format list chahiye, tab ye lighter hai."""
    try:
        data = _extract(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

    audio, video, combined = _split_formats(data.get("formats", []))
    return {
        "status":           True,
        "videoId":          data.get("id"),
        "audio_formats":    audio,
        "video_formats":    video,
        "combined_formats": combined,
    }


@app.get("/best")
def best(
    url: str = Query(..., description="YouTube video URL"),
    height: int | None = Query(None, description="Is height ke barabar ya sabse kareeb video chuno (default: sabse best)"),
    ext: str | None = Query(None, description="Preferred container, e.g. mp4"),
    _auth=Depends(require_api_key),
):
    """Ek-shot 'best pick' — bina poori list chhaan-be-chhaan kiye
    seedha ek best audio aur ek best (ya requested height ke kareeb)
    video format de deta hai. Bots ke liye sabse aasan route."""
    try:
        data = _extract(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

    audio, video, combined = _split_formats(data.get("formats", []))
    audio = _filter_formats(audio, ext=ext) or audio
    video_pool = _filter_formats(video, ext=ext) or video

    if not video_pool and combined:
        video_pool = combined

    chosen_video = None
    if video_pool:
        if height:
            chosen_video = min(video_pool, key=lambda f: abs((f["height"] or 0) - height))
        else:
            chosen_video = video_pool[0]

    chosen_audio = audio[0] if audio else (combined[0] if combined else None)

    if not chosen_video and not chosen_audio:
        raise HTTPException(status_code=502, detail="No downloadable formats found")

    return {
        "status":  True,
        "videoId": data.get("id"),
        "title":   data.get("title"),
        "audio":   chosen_audio,
        "video":   chosen_video,
    }


@app.get("/subtitles")
def subtitles(
    url: str = Query(..., description="YouTube video URL"),
    auto: bool = Query(False, description="True = auto-generated captions, False = uploader ki apni subtitles"),
    _auth=Depends(require_api_key),
):
    try:
        data = _extract(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

    subs = _extract_subs(data, auto=auto)
    return {
        "status":         True,
        "videoId":        data.get("id"),
        "auto_generated": auto,
        "languages":      list(subs.keys()),
        "subtitles":      subs,
    }


@app.get("/playlist")
def playlist(
    url: str = Query(..., description="YouTube playlist URL"),
    limit: int = Query(50, ge=1, le=200, description="Kitni videos tak fetch karni hain"),
    _auth=Depends(require_api_key),
):
    """Playlist ke andar ki videos ki halki list (extract_flat — har
    video ka poora format info nahi laata, isliye fast hai). Har entry
    ki poori detail chahiye ho to us video ka url /info pe bhejo."""
    try:
        data = _extract(url, extra_opts={"noplaylist": False, "extract_flat": "in_playlist", "playlistend": limit})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

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

    return {
        "status":        True,
        "playlistId":    data.get("id"),
        "playlistTitle": data.get("title"),
        "video_count":   len(videos),
        "videos":        videos,
    }


@app.get("/search")
def search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Kitne results chahiye"),
    _auth=Depends(require_api_key),
):
    """YouTube search — ytsearch{n}:query use karta hai. Result halka
    hai (extract_flat), poori detail ke liye us video ka url /info pe
    bhej do."""
    query = f"ytsearch{limit}:{q}"
    try:
        data = _extract(query, extra_opts={"noplaylist": False, "extract_flat": "in_playlist"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp search failed: {e}")

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

    return {"status": True, "query": q, "result_count": len(results), "results": results}


@app.get("/debug")
def debug(
    url: str = Query(..., description="YouTube video URL"),
    _auth=Depends(require_api_key),
):
    """yt-dlp ka poora verbose log return karta hai, cookie pool ke sath
    try karte hue. Dekho: 'Retrieved a gvs PO Token', 'JS runtimes: deno',
    aur agar cookies use hui to unka path bhi dikhega."""
    logger = _LogCapture()
    warm_up_ok = _warm_bgutil_provider()
    cookie_paths = _cookie_pool_paths()
    attempts = cookie_paths + [None]

    extracted_ok = False
    format_count = 0
    error_msg = None
    used_cookiefile = None
    for cookiefile in attempts:
        opts = _base_ydl_opts(logger=logger, verbose=True)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info_data = ydl.extract_info(url, download=False)
                extracted_ok = True
                format_count = len(info_data.get("formats", []))
                used_cookiefile = os.path.basename(cookiefile) if cookiefile else None
                break
        except Exception as e:
            error_msg = str(e)
            if _is_auth_error(e):
                continue
            break

    return {
        "bgutil_provider_url_configured": bool(BGUTIL_PROVIDER_URL),
        "bgutil_provider_url": BGUTIL_PROVIDER_URL or None,
        "bgutil_warm_up_succeeded": warm_up_ok,
        "cookie_pool_size": len(cookie_paths),
        "used_cookiefile": used_cookiefile,
        "extracted_ok": extracted_ok,
        "format_count": format_count,
        "error": error_msg,
        "logs": logger.lines,
    }


@app.get("/cookies/upload")
def upload_cookies_form():
    """Mobile browser se seedha cookies.txt upload karne ka simple page —
    terminal/curl ki zaroorat nahi. Isi URL ko phone ke browser me kholo.
    Agar API_KEY set hai to form me wo bhi daalni hogi."""
    from fastapi.responses import HTMLResponse
    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Cookies Upload</title>
      <style>
        body { font-family: sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; }
        h2 { margin-bottom: 4px; }
        p { color: #555; }
        input[type=text], input[type=file] { margin: 10px 0; display: block; width: 100%; box-sizing: border-box; padding: 8px; }
        button { background: #16a34a; color: white; border: none; padding: 12px 20px;
                 border-radius: 6px; font-size: 16px; }
        #result { margin-top: 16px; padding: 12px; border-radius: 6px; display: none; white-space: pre-wrap; word-break: break-all; }
        #envBox { margin-top: 12px; display: none; }
        #envBox textarea { width: 100%; box-sizing: border-box; height: 100px; font-size: 12px; padding: 8px; }
        #copyBtn { background: #2563eb; margin-top: 8px; }
      </style>
    </head>
    <body>
      <h2>Cookies Upload</h2>
      <p>Apni exported <b>cookies.txt</b> file yahan choose karke Upload dabao.</p>
      <input type="text" id="apiKeyInput" placeholder="API key (agar set hai to)">
      <input type="file" id="fileInput" accept=".txt">
      <button onclick="upload()">Upload</button>
      <div id="result"></div>
      <div id="envBox">
        <p><b>Permanent save karne ke liye:</b> ye value copy karo, Render → Environment tab me <code>COOKIES_1</code> (ya agla free number) naam se paste karo.</p>
        <textarea id="envValue" readonly></textarea>
        <button id="copyBtn" onclick="copyEnv()">Copy value</button>
      </div>
      <script>
        async function upload() {
          const input = document.getElementById('fileInput');
          const apikey = document.getElementById('apiKeyInput').value.trim();
          const resultBox = document.getElementById('result');
          const envBox = document.getElementById('envBox');
          if (!input.files.length) { alert('Pehle file choose karo'); return; }
          const form = new FormData();
          form.append('file', input.files[0]);
          resultBox.style.display = 'block';
          resultBox.style.background = '#eee';
          resultBox.textContent = 'Uploading...';
          envBox.style.display = 'none';
          try {
            const qs = apikey ? ('?apikey=' + encodeURIComponent(apikey)) : '';
            const res = await fetch('/cookies/upload' + qs, { method: 'POST', body: form });
            const data = await res.json();
            if (res.ok) {
              resultBox.style.background = '#dcfce7';
              resultBox.textContent = 'Saved as: ' + data.saved_as + '\\nPool size: ' + data.pool_size;
              document.getElementById('envValue').value = data.env_var_value;
              envBox.style.display = 'block';
            } else {
              resultBox.style.background = '#fee2e2';
              resultBox.textContent = 'Failed:\\n' + JSON.stringify(data, null, 2);
            }
          } catch (e) {
            resultBox.style.background = '#fee2e2';
            resultBox.textContent = 'Error: ' + e;
          }
        }
        function copyEnv() {
          const box = document.getElementById('envValue');
          box.select();
          box.setSelectionRange(0, 99999);
          navigator.clipboard.writeText(box.value).then(() => alert('Copied!'));
        }
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/cookies/upload")
async def upload_cookies(file: UploadFile = File(...), _auth=Depends(require_api_key)):
    """Naya cookies.txt (Netscape format, browser se 'Get cookies.txt
    LOCALLY' extension se export kiya hua) pool me add karta hai. Isi
    URL pe file bhej do — turant pool me shaamil ho jaayegi.

    ⚠️ ZAROORI: Ye upload sirf tab tak zinda rehta hai jab tak service
    redeploy nahi hoti (Render free tier me permanent disk nahi hota,
    har naye deploy pe wipe ho jaata hai). Isliye response me mila
    "env_var_value" copy karke Render dashboard → Environment tab me
    COOKIES_1 (ya COOKIES_2, COOKIES_3...) naam se env var banao —
    wo hamesha ke liye survive karega, chahe kitni bhi baar redeploy ho.

    curl -F "file=@cookies.txt" https://<your-api>/cookies/upload
    Ya phir browser se GET /cookies/upload wala form use karo.
    """
    contents = await file.read()
    if not contents or b"\t" not in contents:
        raise HTTPException(status_code=400, detail="Ye valid Netscape cookies.txt nahi lagti — format check karo")

    existing = len(_cookie_pool_paths())
    path = os.path.join(COOKIE_DIR, f"upload_{existing + 1}_{int(time.time())}.txt")
    with open(path, "wb") as f:
        f.write(contents)

    return {
        "status": True,
        "saved_as": os.path.basename(path),
        "pool_size": len(_cookie_pool_paths()),
        "note": "Ye upload agle redeploy pe udd jaayega. Neeche wali env_var_value copy karke Render → Environment tab me COOKIES_1 (ya agla free number) naam se save karo — tabhi permanent rahega.",
        "env_var_value": base64.b64encode(contents).decode("ascii"),
    }


@app.get("/cookies/status")
def cookies_status(_auth=Depends(require_api_key)):
    """Pool ke har cookie-set ko ek chhote test-video pe try karta hai
    aur bata deta hai kaun zinda hai, kaun expire ho chuka. Regularly
    check karte raho — jab sab 'valid: false' ho jaayein, tabhi nayi
    cookies daalni hain."""
    results = []
    for path in _cookie_pool_paths():
        opts = _base_ydl_opts()
        opts["cookiefile"] = path
        valid = True
        err = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info("https://youtu.be/dQw4w9WgXcQ", download=False)
        except Exception as e:
            valid = not _is_auth_error(e)  # non-auth error = video-specific issue, cookie khud theek ho sakti hai
            err = str(e)[:200] if not valid else None
        results.append({"file": os.path.basename(path), "valid": valid, "error": err})

    return {"pool_size": len(results), "cookies": results}


@app.get("/health")
def health():
    return {"status": True}

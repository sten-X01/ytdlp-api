"""
Render pe deploy karne wala YouTube info API.
Kaam sirf itna: yt-dlp se video ka info nikalna aur audio-only /
video-only formats ki list (with direct CDN url) return karna.
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

from fastapi import FastAPI, HTTPException, Query
import os
import requests
import time
import yt_dlp

app = FastAPI(title="ytinfo-api")

# bgutil-ytdlp-pot-provider ka HTTP server URL — Render dashboard me
# "Environment" tab se BGUTIL_PROVIDER_URL naam ka env var set karo
# (e.g. https://bgutil-pot-provider-xxxx.onrender.com). Isse PO token
# milta hai jo "web" client ko full quality formats dene deta hai.
BGUTIL_PROVIDER_URL = os.environ.get("BGUTIL_PROVIDER_URL", "").strip()


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


def _extract(url: str) -> dict:
    _warm_bgutil_provider()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # "web" ab primary hai kyunki PO token (bgutil provider se) mil raha
        # hai — isi client se full adaptive format list(144p-4320p video +
        # sab audio bitrates) milti hai. Baaki clients sirf fallback hain
        # (agar provider down ho to bhi kam-se-kam 360p muxed to milega).
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "ios"],
            },
            **({"youtubepot-bgutilhttp": {"base_url": [BGUTIL_PROVIDER_URL]}} if BGUTIL_PROVIDER_URL else {}),
        },
        # yt-dlp by default hi "deno" try karta hai — Render build command
        # deno binary ko venv/bin me install karta hai (PATH pe rehta hai),
        # isliye yahan explicit js_runtimes config ki zaroorat nahi.
        # agar cookies.txt use karni ho (login-required/age-restricted videos ke liye):
        # "cookiefile": "cookies.txt",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def _split_formats(raw_formats: list) -> tuple[list, list]:
    """Raw yt-dlp formats ko audio-only aur video-only me alag karta hai.
    Combined (audio+video ek hi stream) formats ko jaan-boojh kar skip
    kiya jaata hai kyunki bot khud audio-only + video-only alag download
    karke ffmpeg se mux karega — usually behtar quality milti hai.
    Agar koi bhi pure adaptive stream na mile (kuch player clients sirf
    combined/progressive formats dete hain), to combined formats ko hi
    dono list me fallback ke taur pe include kar diya jaata hai — taaki
    API kabhi khaali response na de jab tak yt-dlp ne kuch bhi nikala ho."""
    audio, video, combined = [], [], []
    for f in raw_formats:
        acodec = f.get("acodec")
        vcodec = f.get("vcodec")
        has_audio = acodec not in (None, "none")
        has_video = vcodec not in (None, "none")

        entry = {
            "itag":     f.get("format_id"),
            "ext":      f.get("ext"),
            "acodec":   acodec,
            "vcodec":   vcodec,
            "abr":      f.get("abr"),
            "asr":      f.get("asr"),
            "height":   f.get("height"),
            "width":    f.get("width"),
            "fps":      f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url":      f.get("url"),
        }

        if has_audio and not has_video:
            audio.append(entry)
        elif has_video and not has_audio:
            video.append(entry)
        elif has_audio and has_video:
            combined.append(entry)

    if not audio and not video and combined:
        # Fallback: sirf combined formats mile — inhi ko dono list me de do.
        audio = combined
        video = combined

    audio.sort(key=lambda x: (x["abr"] or 0), reverse=True)
    video.sort(key=lambda x: (x["height"] or 0), reverse=True)
    return audio, video


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


@app.get("/debug")
def debug(url: str = Query(..., description="YouTube video URL")):
    """yt-dlp ka poora verbose log return karta hai — is line ko dhoondo:
    'PO Token Providers: bgutil:http-x.x.x (external)' — agar ye line
    hai to provider detect ho gaya hai. Agar isme 'not available' ya
    ye line hi missing hai, to provider connect nahi ho raha."""
    logger = _LogCapture()
    warm_up_ok = _warm_bgutil_provider()
    ydl_opts = {
        "quiet": True,
        "no_warnings": False,
        "verbose": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": logger,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web", "ios"]},
            **({"youtubepot-bgutilhttp": {"base_url": [BGUTIL_PROVIDER_URL]}} if BGUTIL_PROVIDER_URL else {}),
        },
    }
    extracted_ok = False
    format_count = 0
    error_msg = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_data = ydl.extract_info(url, download=False)
            extracted_ok = True
            format_count = len(info_data.get("formats", []))
    except Exception as e:
        error_msg = str(e)

    return {
        "bgutil_provider_url_configured": bool(BGUTIL_PROVIDER_URL),
        "bgutil_provider_url": BGUTIL_PROVIDER_URL or None,
        "bgutil_warm_up_succeeded": warm_up_ok,
        "extracted_ok": extracted_ok,
        "format_count": format_count,
        "error": error_msg,
        "logs": logger.lines,
    }


@app.get("/info")
def info(url: str = Query(..., description="YouTube video URL")):
    try:
        data = _extract(url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp extract failed: {e}")

    audio_formats, video_formats = _split_formats(data.get("formats", []))

    if not audio_formats and not video_formats:
        raise HTTPException(status_code=502, detail="No downloadable formats found")

    return {
        "status":         True,
        "videoId":        data.get("id"),
        "title":          data.get("title"),
        "thumbnail":      data.get("thumbnail"),
        "duration":       data.get("duration"),
        "uploader":       data.get("uploader"),
        "audio_formats":  audio_formats,
        "video_formats":  video_formats,
    }


@app.get("/health")
def health():
    return {"status": True}

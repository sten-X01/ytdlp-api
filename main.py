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

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
import base64
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

# ══════════════════════════════════════════════════════════════════════
#   PROXY (Tailscale exit-node ke through) — mobile-IP se requests
#   ─────────────────────────────────────────────────────────────────
#   start.sh Render ke andar tailscaled ko "userspace-networking" mode
#   me chalata hai jo localhost pe ek local SOCKS5 proxy expose karta
#   hai (127.0.0.1:1055). Tailscale ka "tailscale up --exit-node=..."
#   phone (jo exit node hai) ke through sab tailnet traffic route kar
#   deta hai. Isliye yt-dlp ko sirf itna pata hona chahiye ki uska
#   proxy 127.0.0.1:1055 hai — baaki tailscaled + phone sambhalte hain.
#   Disable karna ho to Render env var PROXY_URL khaali chhod do.
# ══════════════════════════════════════════════════════════════════════
PROXY_URL = os.environ.get("PROXY_URL", "socks5h://127.0.0.1:1055").strip()

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
    content) ko ek baar disk pe likh deta hai. Render dashboard se ye
    env vars set karo — value = poore cookies.txt file ko base64 me
    encode karke paste kar do."""
    i = 1
    while True:
        val = os.environ.get(f"COOKIES_{i}")
        if not val:
            break
        try:
            content = base64.b64decode(val).decode("utf-8")
            path = os.path.join(COOKIE_DIR, f"env_{i}.txt")
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
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
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


def _extract(url: str) -> dict:
    """Cookie pool ke har set ko baari-baari try karta hai (aur ek attempt
    bina cookies ke bhi, kyunki kabhi-kabhi wo bhi chal jaata hai). Sirf
    auth-error (LOGIN_REQUIRED) pe hi next cookie try karta hai — koi aur
    error ho to turant raise kar deta hai, chhupata nahi."""
    _warm_bgutil_provider()
    cookie_paths = _cookie_pool_paths()
    attempts = cookie_paths + [None]  # None = cookies ke bina, last resort

    last_error = None
    for cookiefile in attempts:
        opts = _base_ydl_opts()
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
        "proxy_configured": PROXY_URL or None,
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
    terminal/curl ki zaroorat nahi. Isi URL ko phone ke browser me kholo."""
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
        input[type=file] { margin: 16px 0; display: block; }
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
            const res = await fetch('/cookies/upload', { method: 'POST', body: form });
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
async def upload_cookies(file: UploadFile = File(...)):
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
def cookies_status():
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


@app.get("/proxy/check")
def proxy_check():
    """Confirm karta hai ki Tailscale exit-node (phone) ke through traffic
    fact me route ho raha hai ya nahi — dono IPs (direct Render IP vs
    proxy ke through IP) dikhata hai. Agar dono same hain, matlab proxy
    kaam nahi kar raha (tailscaled up nahi hua ya exit-node set nahi hua)."""
    result = {"proxy_configured": PROXY_URL or None}
    try:
        result["render_direct_ip"] = requests.get("https://api.ipify.org", timeout=10).text
    except Exception as e:
        result["render_direct_ip"] = f"error: {e}"

    if PROXY_URL:
        try:
            r = requests.get(
                "https://api.ipify.org",
                proxies={"http": PROXY_URL, "https": PROXY_URL},
                timeout=15,
            )
            result["via_proxy_ip"] = r.text
            result["tunnel_working"] = result["via_proxy_ip"] != result["render_direct_ip"]
        except Exception as e:
            result["via_proxy_ip"] = f"error: {e}"
            result["tunnel_working"] = False
    else:
        result["via_proxy_ip"] = None
        result["tunnel_working"] = None

    return result

# Universal, portable build — kaam karta hai Koyeb, Railway, Render (Docker
# environment), ya kisi bhi Docker-supporting free host pe, bina kisi
# platform-specific "Build Command" field pe depend kiye. Dockerfile khud
# hi build ka poora sach hai — koi ambiguity nahi ki kaunsa step chala.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno (JS runtime — PO-token script-mode aur yt-dlp signature-solving
# dono ke liye chahiye)
RUN curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# bgutil-ytdlp-pot-provider (script mode) — PO token bina cookies/login
# ke generate karta hai, seedha isi container ke andar
RUN git clone --single-branch --branch 1.3.1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        bgutil-ytdlp-pot-provider \
    && cd bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen

COPY main.py .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]

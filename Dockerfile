FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg — склейка видео со звуком, mp3, превью; curl — скачать WARP-утилиты ниже
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# deno — yt-dlp решает им JS-проверки YouTube, без него часть роликов не качается
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Cloudflare WARP без root и без VPN в системе: wgcf заводит бесплатный аккаунт,
# wireproxy поднимает его как SOCKS-прокси на 127.0.0.1:40000 — обход блокировки YouTube
RUN curl -fsSL -o /usr/local/bin/wgcf https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64 && \
    curl -fsSL https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz \
        | tar xz -C /usr/local/bin wireproxy && \
    chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["bash", "start.sh"]

#!/bin/bash
# YouTube ломает yt-dlp раз в несколько недель — при каждом запуске берём свежий.
# Если PyPI не ответил за минуту, запускаемся на том, что есть.
timeout 60 pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp не обновился, работаю на старом"

# ─── Cloudflare WARP как запасной выход в интернет для YouTube ───
# Включить: WARP=on. По умолчанию выключен: WARP ходит по UDP, а Railway исходящий UDP не пропускает.
# Аккаунт хранится на томе, чтобы не регистрироваться при каждом запуске.
if [ "${WARP:-off}" = "on" ] && command -v wgcf >/dev/null && command -v wireproxy >/dev/null; then
  WARP_DIR="${RAILWAY_VOLUME_MOUNT_PATH:-${DATA_DIR:-/app/data}}/warp"
  mkdir -p "$WARP_DIR"
  (
    cd "$WARP_DIR" || exit 0
    if [ ! -s wgcf-profile.conf ]; then
      timeout 60 wgcf register --accept-tos >/dev/null 2>&1 || rm -f wgcf-account.toml
      timeout 60 wgcf generate >/dev/null 2>&1
    fi
    if [ -s wgcf-profile.conf ]; then
      printf 'WGConfig = %s/wgcf-profile.conf\n\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' "$WARP_DIR" > wireproxy.conf
      # перезапуск, если упадёт
      while true; do wireproxy -s -c wireproxy.conf; echo "WARP упал, перезапускаю через 10 с"; sleep 10; done &
      echo "WARP поднимается на 127.0.0.1:40000"
    else
      echo "WARP не зарегистрировался — работаю без него"
    fi
  )
fi

# ─── Tor: ещё один выход в интернет для YouTube, работает поверх обычного TCP ───
# Выключить: TOR=off. Разные логины SOCKS = разные цепочки (IsolateSOCKSAuth), этим бот и меняет адрес.
if [ "${TOR:-on}" != "off" ] && command -v tor >/dev/null; then
  TOR_DIR="${RAILWAY_VOLUME_MOUNT_PATH:-${DATA_DIR:-/app/data}}/tor"
  mkdir -p "$TOR_DIR" && chmod 700 "$TOR_DIR"
  ( while true; do
      tor --SocksPort "127.0.0.1:9050 IsolateSOCKSAuth" --DataDirectory "$TOR_DIR" \
          --Log "warn stdout" --RunAsDaemon 0
      echo "Tor упал, перезапускаю через 10 с"; sleep 10
    done ) &
  echo "Tor поднимается на 127.0.0.1:9050"
fi

exec python bot.py

#!/bin/bash
# YouTube ломает yt-dlp раз в несколько недель — при каждом запуске берём свежий.
# Если PyPI не ответил за минуту, запускаемся на том, что есть.
timeout 60 pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp не обновился, работаю на старом"
exec python bot.py

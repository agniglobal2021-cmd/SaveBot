"""
Скачивание через yt-dlp: YouTube и всё, что yt-dlp умеет (TikTok, Instagram, X, SoundCloud, VK и сотни других).

probe(url)              → сведения о ролике без скачивания
video_choices(info)     → какие качества показать кнопками и сколько они примерно весят
download(url, opt, ...) → файл на диске, готовый к отправке в Telegram
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import yt_dlp

logger = logging.getLogger("savebot.dl")

# Качества, которые показываем кнопками (остальные высоты округляются к ближайшей снизу)
STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240, 144)

COOKIES_FILE = None          # путь к cookies.txt, если задан YT_COOKIES (см. init_cookies)


class DownloadError(Exception):
    """Понятная человеку причина, почему не вышло."""


# ═══════════════════════════════════════════════════════════════
# КУКИ YOUTUBE
# ═══════════════════════════════════════════════════════════════
def init_cookies(data_dir: str) -> None:
    """YouTube часто не пускает серверы без входа. Куки берутся из YT_COOKIES
    (содержимое cookies.txt целиком или в base64) или из файла cookies.txt на томе."""
    global COOKIES_FILE
    raw = os.getenv("YT_COOKIES", "").strip()
    path = os.path.join(data_dir, "cookies.txt")
    if raw:
        if not raw.startswith("#") and "\t" not in raw:
            import base64
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except Exception:
                logger.warning("YT_COOKIES не похож ни на cookies.txt, ни на base64 — пропускаю")
                return
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw if raw.endswith("\n") else raw + "\n")
    if os.path.exists(path):
        COOKIES_FILE = path
        logger.info("Куки для yt-dlp: %s", path)


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,          # ссылка на ролик внутри плейлиста = только этот ролик
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if COOKIES_FILE:
        # yt-dlp перезаписывает файл кук при работе — даём ему копию, оригинал не портим
        work = COOKIES_FILE + ".work"
        shutil.copyfile(COOKIES_FILE, work)
        opts["cookiefile"] = work
    return opts


def explain(err: Exception) -> str:
    """Перевод частых ошибок yt-dlp на человеческий."""
    text = str(err)
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube просит войти: сервер похож на бота. "
                "Добавь куки в переменную YT_COOKIES (как — в README).")
    if "private video" in low:
        return "Видео приватное."
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return "Видео 18+, без кук YouTube его не отдаёт. Добавь YT_COOKIES."
    if "unsupported url" in low:
        return "Эту ссылку я не умею открывать."
    if "not available in your country" in low or "geo" in low and "block" in low:
        return "Видео недоступно в стране сервера."
    if "live event will begin" in low or "is live" in low and "premiere" in low:
        return "Трансляция ещё не началась."
    if "requested format is not available" in low:
        return "Такого качества у этого ролика нет."
    first = text.replace("ERROR: ", "").strip().splitlines()[0] if text.strip() else repr(err)
    return first[:300]


# ═══════════════════════════════════════════════════════════════
# СВЕДЕНИЯ О РОЛИКЕ
# ═══════════════════════════════════════════════════════════════
def probe(url: str) -> dict:
    """Всё о ссылке без скачивания. Для плейлиста — список роликов без подробностей."""
    opts = _base_opts() | {"skip_download": True, "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(explain(e)) from e
    if not info:
        raise DownloadError("По ссылке ничего не нашлось.")
    return ydl.sanitize_info(info)


def is_playlist(info: dict) -> bool:
    return info.get("_type") in ("playlist", "multi_video") and bool(info.get("entries"))


def _fmt_size(f: dict, duration: float | None) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = int(f["tbr"] * 1000 / 8 * duration)
    return int(size) if size else None


def _is_video(f: dict) -> bool:
    return (f.get("vcodec") or "none") != "none" and bool(f.get("height"))


def _is_audio_only(f: dict) -> bool:
    return (f.get("acodec") or "none") != "none" and (f.get("vcodec") or "none") == "none"


def _snap_height(h: int) -> int:
    """Нестандартные высоты к ближайшей стандартной: 1072 → 1080, 700 → 720."""
    for std in STANDARD_HEIGHTS:
        if h >= std * 0.9:
            return std
    return h


def best_audio_size(info: dict) -> int | None:
    dur = info.get("duration")
    audios = [f for f in info.get("formats") or [] if _is_audio_only(f)]
    m4a = [f for f in audios if f.get("ext") == "m4a"] or audios
    if not m4a:
        return None
    best = max(m4a, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
    return _fmt_size(best, dur)


@dataclass
class Choice:
    key: str                 # что придёт в callback: v1080, vbest, mp3, m4a
    label: str               # подпись кнопки
    size: int | None = None  # примерный вес в байтах


def _is_avc(f: dict) -> bool:
    return (f.get("vcodec") or "").startswith(("avc1", "h264"))


def is_vertical(info: dict) -> bool:
    w, h = info.get("width"), info.get("height")
    if not (w and h):
        vids = [f for f in info.get("formats") or [] if _is_video(f) and f.get("width")]
        if vids:
            top = max(vids, key=lambda f: f["height"])
            w, h = top["width"], top["height"]
    return bool(w and h and h > w)


def video_choices(info: dict) -> list[Choice]:
    """Кнопки качества. Качество считается по короткой стороне, как на самом YouTube:
    вертикальный ролик 1080×1920 — это 1080p."""
    dur = info.get("duration")
    audio = best_audio_size(info) or 0
    groups: dict[int, list[tuple[bool, int | None]]] = {}
    for f in info.get("formats") or []:
        if not _is_video(f):
            continue
        short = min(f["height"], f.get("width") or f["height"])
        size = _fmt_size(f, dur)
        if size is not None and (f.get("acodec") or "none") == "none":
            size += audio                       # видео без звука: звук докачается отдельно
        groups.setdefault(_snap_height(short), []).append((_is_avc(f), size))

    choices = []
    for h in sorted(groups, reverse=True):
        variants = groups[h]
        # скачиваться будет лучший h264 этой высоты, если он есть, — по нему и оцениваем вес
        pool = [s for avc, s in variants if avc] or [s for _, s in variants]
        known = [s for s in pool if s]
        choices.append(Choice(key=f"v{h}", label=f"{h}p", size=max(known) if known else None))

    if not choices and _looks_like_video(info):
        choices = [Choice(key="vbest", label="Видео", size=_fmt_size(info, dur))]
    return choices


def _looks_like_video(info: dict) -> bool:
    if (info.get("vcodec") or "none") != "none":
        return True
    return info.get("ext") in ("mp4", "webm", "mov", "mkv") or any(
        (f.get("vcodec") or "none") != "none" for f in info.get("formats") or [])


def has_audio(info: dict) -> bool:
    fmts = info.get("formats") or [info]
    return any((f.get("acodec") or "none") != "none" or f.get("acodec") is None for f in fmts)


# ═══════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ
# ═══════════════════════════════════════════════════════════════
def video_format(height: int | None, vertical: bool = False) -> str:
    """Сначала h264 + m4a (такой mp4 играет в Telegram везде, в том числе на iPad),
    потом любое видео нужного качества, потом что есть.
    У вертикальных роликов качество — это ширина, поэтому фильтр по ширине."""
    if height is None:
        return "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b"
    side = "width" if vertical else "height"
    lim = f"[{side}<={int(height * 1.12)}]"      # 1080 пропускает и 1088, и 1200 на 1920 не пропустит
    return f"bv*{lim}[vcodec^=avc1]+ba[ext=m4a]/bv*{lim}+ba/b{lim}/bv*+ba/b"


@dataclass
class Result:
    path: str
    title: str
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    uploader: str | None = None
    thumb: str | None = None          # jpeg ≤320px для превью в Telegram


def download(url: str, opt: str, workdir: str, progress=None, vertical: bool = False) -> Result:
    """opt: v<высота> | vbest | mp3 | m4a.  progress(text) зовётся из потока yt-dlp."""
    os.makedirs(workdir, exist_ok=True)
    opts = _base_opts() | {
        "outtmpl": os.path.join(workdir, "%(title).120B.%(ext)s"),   # своя папка на каждое задание — id в имени не нужен
        "restrictfilenames": False,
        "windowsfilenames": True,
        "writethumbnail": True,
        "postprocessors": [],
    }

    if opt.startswith("v"):
        height = None if opt == "vbest" else int(opt[1:])
        opts["format"] = video_format(height, vertical)
        opts["merge_output_format"] = "mp4"
        # перепаковка без перекодирования; faststart — чтобы видео в Telegram начиналось сразу
        opts["postprocessors"].append({"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"})
        opts["postprocessor_args"] = {"videoremuxer": ["-movflags", "+faststart"],
                                      "merger": ["-movflags", "+faststart"]}
    elif opt == "mp3":
        opts["format"] = "ba/b"
        opts["postprocessors"] += [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "2"},  # VBR V2 ≈190 кбит/с: исходник с YouTube всё равно ~130
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
    elif opt == "m4a":
        opts["format"] = "ba[ext=m4a]/ba/b"
        opts["postprocessors"] += [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
    else:
        raise DownloadError(f"Неизвестный формат: {opt}")

    if progress:
        opts["progress_hooks"] = [_progress_hook(progress)]
        opts["postprocessor_hooks"] = [_pp_hook(progress)]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info = ydl.sanitize_info(info)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(explain(e)) from e

    path = _final_path(info, workdir, opt)
    if not path:
        raise DownloadError("Файл не появился после скачивания.")

    res = Result(
        path=path,
        title=info.get("title") or "file",
        duration=int(info["duration"]) if info.get("duration") else None,
        width=info.get("width"),
        height=info.get("height"),
        uploader=info.get("artist") or info.get("uploader") or info.get("channel"),
    )
    res.thumb = make_thumb(workdir, path)
    w, h, d = probe_media(path)
    if opt.startswith("v"):
        res.width, res.height = w or res.width, h or res.height
    res.duration = res.duration or d
    return res


def _final_path(info: dict, workdir: str, opt: str) -> str | None:
    want = {"mp3": ".mp3", "m4a": ".m4a"}.get(opt, ".mp4")
    for item in info.get("requested_downloads") or []:
        p = item.get("filepath")
        if p and os.path.exists(p):
            if p.endswith(want):
                return p
            alt = os.path.splitext(p)[0] + want
            if os.path.exists(alt):
                return alt
            return p
    # запасной путь: самый большой медиафайл в папке
    media = [os.path.join(workdir, n) for n in os.listdir(workdir)
             if n.lower().endswith((".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".mov"))]
    return max(media, key=os.path.getsize) if media else None


def _progress_hook(progress):
    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        what = "аудио" if (d.get("info_dict") or {}).get("vcodec") in ("none", None) and \
            (d.get("info_dict") or {}).get("acodec") not in ("none", None) else "видео"
        if total:
            progress(f"Качаю {what}: {done * 100 // total}% из {human_size(total)}")
        else:
            progress(f"Качаю {what}: {human_size(done)}")
    return hook


def _pp_hook(progress):
    names = {"Merger": "Склеиваю видео и звук", "FFmpegExtractAudio": "Перегоняю в аудио",
             "FFmpegVideoRemuxer": "Упаковываю в mp4"}

    def hook(d):
        if d.get("status") == "started" and d.get("postprocessor") in names:
            progress(names[d["postprocessor"]] + "…")
    return hook


# ═══════════════════════════════════════════════════════════════
# МЕЛОЧИ: превью, размеры, вес
# ═══════════════════════════════════════════════════════════════
def make_thumb(workdir: str, media_path: str, src: str | None = None) -> str | None:
    """Telegram хочет превью в JPEG не больше 320px и 200 КБ."""
    if not src:
        stem = os.path.splitext(media_path)[0]
        for ext in (".jpg", ".webp", ".png", ".jpeg"):
            if os.path.exists(stem + ext):
                src = stem + ext
                break
    if not src:
        cands = [os.path.join(workdir, n) for n in os.listdir(workdir)
                 if n.lower().endswith((".jpg", ".webp", ".png", ".jpeg")) and not n.startswith("_thumb")]
        src = cands[0] if cands else None
    out = os.path.join(workdir, "_thumb.jpg")
    pre = []
    if not src:
        if not media_path.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
            return None
        src, pre = media_path, ["-ss", "1"]          # у источника нет обложки — берём кадр с первой секунды
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *pre, "-i", src,
                        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-q:v", "4", "-frames:v", "1", out], check=True, timeout=30)
        return out if os.path.getsize(out) < 200 * 1024 else None
    except Exception as e:
        logger.warning("Превью не вышло: %s", e)
        return None


def probe_media(path: str) -> tuple[int | None, int | None, int | None]:
    """Ширина, высота и длительность готового файла — Telegram без них криво показывает видео."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=30)
        import json
        data = json.loads(r.stdout or "{}")
        v = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), {})
        dur = (data.get("format") or {}).get("duration")
        return v.get("width"), v.get("height"), int(float(dur)) if dur else None
    except Exception:
        return None, None, None


def shrink_audio(path: str, duration: int | None, limit: int) -> str | None:
    """Длинная запись (подкаст, лекция) не влезла в лимит — пережимаем в mp3 с битрейтом,
    при котором влезет. Ниже 48 кбит/с не опускаемся: дальше слушать уже неприятно."""
    if not duration:
        return None
    kbps = int(limit * 8 / duration / 1000 * 0.93)
    if kbps < 48:
        return None
    kbps = min(kbps, 128)
    out = os.path.splitext(path)[0] + f" ({kbps}k).mp3"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", "-map_metadata", "0",
                        "-c:a", "libmp3lame", "-b:a", f"{kbps}k", out], check=True, timeout=3600)
    except Exception as e:
        logger.warning("Не пережалось: %s", e)
        return None
    return out if os.path.getsize(out) <= limit else None


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} ГБ"


def human_duration(sec: int | float | None) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

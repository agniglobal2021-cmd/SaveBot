"""
Savebot — один свой бот вместо кучи чужих с рекламой и обязательными подписками.

Кидаешь ссылку:
  YouTube и почти любой сайт с видео (TikTok, Instagram, X, VK, SoundCloud…)
      → кнопки: видео в нужном качестве с примерным весом, MP3, M4A
  Spotify: трек      → сразу mp3 с тегами и обложкой
           альбом/плейлист → кнопка «скачать все», треки приходят по одному
  YouTube-плейлист   → кнопка «MP3 всех»

Отвечает только тем, чьи id в ALLOWED_IDS. Уже скачанное второй раз приходит мгновенно:
Telegram хранит файл, бот помнит его file_id.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time

from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
                      Message, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import dl
import spotify

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("savebot")

# ═══════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ALLOWED_IDS", os.getenv("ALLOWED_CHAT_ID", "")))}
LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "").rstrip("/")     # свой сервер Bot API → файлы до 2 ГБ
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
PARALLEL = int(os.getenv("PARALLEL_DOWNLOADS", "2"))
PLAYLIST_LIMIT = int(os.getenv("PLAYLIST_LIMIT", "50"))

# Облачный Bot API принимает от бота файлы до 50 МБ, свой сервер — до 2000 МБ
UPLOAD_LIMIT = (2000 if LOCAL_BOT_API_URL else 50) * 1024 * 1024

URL_RE = re.compile(r"https?://[^\s<>\"']+")

os.makedirs(DATA_DIR, exist_ok=True)
dl.init_cookies(DATA_DIR)
_slots = asyncio.Semaphore(PARALLEL)


# ═══════════════════════════════════════════════════════════════
# ПАМЯТЬ: file_id уже отправленного и выбор кнопок
# ═══════════════════════════════════════════════════════════════
class FileCache:
    """Ключ (источник + формат) → file_id в Telegram. Лежит на томе, переживает перезапуски."""

    def __init__(self, path: str, limit: int = 5000):
        self.path, self.limit = path, limit
        try:
            with open(path, encoding="utf-8") as f:
                self.data: dict = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, kind: str, file_id: str) -> None:
        self.data[key] = {"kind": kind, "file_id": file_id, "t": int(time.time())}
        if len(self.data) > self.limit:
            for k in sorted(self.data, key=lambda k: self.data[k].get("t", 0))[: len(self.data) - self.limit]:
                del self.data[k]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Кэш не записался: %s", e)


cache = FileCache(os.path.join(DATA_DIR, "file_ids.json"))


def remember(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> str:
    """В callback_data влезает только 64 байта, поэтому ссылку и прочее храним тут, а в кнопке — короткий ключ."""
    store: dict = context.bot_data.setdefault("sessions", {})
    key = secrets.token_urlsafe(6)
    store[key] = payload
    while len(store) > 300:
        store.pop(next(iter(store)))
    return key


def recall(context: ContextTypes.DEFAULT_TYPE, key: str) -> dict | None:
    return context.bot_data.get("sessions", {}).get(key)


# ═══════════════════════════════════════════════════════════════
# ПОМОЩНИКИ
# ═══════════════════════════════════════════════════════════════
def h(text) -> str:
    return html.escape(str(text or ""), quote=False)


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_IDS)


def find_url(msg: Message) -> str | None:
    for ent in (*(msg.entities or ()), *(msg.caption_entities or ())):
        if ent.type == "text_link" and ent.url:
            return ent.url
    m = URL_RE.search(msg.text or msg.caption or "")
    return m.group(0).rstrip(").,!?»") if m else None


def cache_key(info: dict | None, url: str, opt: str) -> str:
    if info and info.get("extractor_key") and info.get("id"):
        return f"{info['extractor_key']}:{info['id']}:{opt}"
    return f"url:{url}:{opt}"


class Status:
    """Одно сообщение «Качаю… 45%», которое обновляется из потока yt-dlp не чаще раза в пару секунд."""

    def __init__(self, msg: Message, loop: asyncio.AbstractEventLoop, prefix: str = ""):
        self.msg, self.loop, self.prefix = msg, loop, prefix
        self.last_text, self.last_t = "", 0.0

    def __call__(self, text: str) -> None:           # зовётся из рабочего потока
        now = time.monotonic()
        if text == self.last_text or now - self.last_t < 2.5:
            return
        self.last_text, self.last_t = text, now
        asyncio.run_coroutine_threadsafe(self._edit(self.prefix + text), self.loop)

    async def set(self, text: str) -> None:
        self.last_text, self.last_t = text, time.monotonic()
        await self._edit(self.prefix + text)

    async def _edit(self, text: str) -> None:
        try:
            await self.msg.edit_text(text, parse_mode=ParseMode.HTML,
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.debug("Статус не обновился: %s", e)
        except TelegramError as e:
            logger.debug("Статус не обновился: %s", e)

    async def done(self) -> None:
        try:
            await self.msg.delete()
        except TelegramError:
            pass


# ═══════════════════════════════════════════════════════════════
# ОТПРАВКА ФАЙЛОВ
# ═══════════════════════════════════════════════════════════════
async def send_result(bot, chat_id: int, res: dl.Result, kind: str, key: str, caption: str = "",
                      reply_to: int | None = None) -> None:
    size = os.path.getsize(res.path)
    if size > UPLOAD_LIMIT and kind == "audio":
        smaller = await asyncio.to_thread(dl.shrink_audio, res.path, res.duration, UPLOAD_LIMIT)
        if smaller:
            res.path, size = smaller, os.path.getsize(smaller)
    if size > UPLOAD_LIMIT:
        raise dl.DownloadError(
            f"Файл вышел {dl.human_size(size)}, а Telegram принимает от ботов до {dl.human_size(UPLOAD_LIMIT)}. "
            + ("Выбери качество пониже." if kind == "video" else "Запись слишком длинная даже в низком качестве.")
            + ("" if LOCAL_BOT_API_URL else " Лимит можно поднять до 2 ГБ — см. README, «Большие файлы».")
        )

    thumb = open(res.thumb, "rb") if res.thumb else None
    try:
        with open(res.path, "rb") as f:
            common = dict(chat_id=chat_id, caption=caption or None, parse_mode=ParseMode.HTML,
                          thumbnail=thumb, filename=os.path.basename(res.path),
                          reply_to_message_id=reply_to, duration=res.duration)
            if kind == "video":
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                sent = await bot.send_video(video=f, width=res.width, height=res.height,
                                            supports_streaming=True, **common)
                file_id = sent.video.file_id if sent.video else sent.document.file_id
            else:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
                sent = await bot.send_audio(audio=f, title=res.title, performer=res.uploader, **common)
                file_id = sent.audio.file_id if sent.audio else sent.document.file_id
    finally:
        if thumb:
            thumb.close()
    cache.put(key, kind, file_id)


async def send_cached(bot, chat_id: int, hit: dict, caption: str = "", reply_to: int | None = None) -> bool:
    try:
        if hit["kind"] == "video":
            await bot.send_video(chat_id, hit["file_id"], caption=caption or None, parse_mode=ParseMode.HTML,
                                 reply_to_message_id=reply_to, supports_streaming=True)
        else:
            await bot.send_audio(chat_id, hit["file_id"], caption=caption or None, parse_mode=ParseMode.HTML,
                                 reply_to_message_id=reply_to)
        return True
    except TelegramError as e:
        logger.info("file_id из кэша не подошёл (%s), качаю заново", e)
        return False


def workdir() -> str:
    base = os.path.join(DATA_DIR, "tmp")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="job-", dir=base)


def clean_tmp() -> None:
    """Хвосты от прошлого запуска, если бот упал посреди скачивания."""
    shutil.rmtree(os.path.join(DATA_DIR, "tmp"), ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════
HELP = (
    "Кидай ссылку — пришлю файл.\n\n"
    "• <b>YouTube</b> и почти любой сайт с видео: TikTok, Instagram, X, VK, SoundCloud — выберешь качество или аудио\n"
    "• <b>Spotify</b>: трек придёт mp3 с обложкой и тегами, альбом или плейлист — целиком\n"
    "• <b>Плейлист YouTube</b> — MP3 всех треков\n\n"
    "Лимит файла: {limit}. Без рекламы и подписок."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    await update.message.reply_text(HELP.format(limit=dl.human_size(UPLOAD_LIMIT)), parse_mode=ParseMode.HTML)


async def deny(update: Update):
    """Чужим — ничего, кроме подсказки с их id: так проще добавить в ALLOWED_IDS себя или близких."""
    user = update.effective_user
    if update.message and user:
        logger.info("Чужой: %s (@%s)", user.id, user.username)
        await update.message.reply_text(
            f"Это личный бот. Твой id: <code>{user.id}</code> — владелец может добавить его в ALLOWED_IDS.",
            parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════════
# ССЫЛКА ПРИШЛА
# ═══════════════════════════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await deny(update)
    msg = update.effective_message
    url = find_url(msg)
    if not url:
        await msg.reply_text("Пришли ссылку на видео или трек.")
        return

    if spotify.is_spotify(url):
        return await handle_spotify(update, context, url)

    status = await msg.reply_text("Смотрю ссылку…")
    try:
        info = await asyncio.to_thread(dl.probe, url)
    except dl.DownloadError as e:
        await status.edit_text(f"Не вышло: {h(e)}", parse_mode=ParseMode.HTML)
        return
    except Exception as e:
        logger.exception("probe упал на %s", url)
        await status.edit_text(f"Что-то сломалось: {h(e)}", parse_mode=ParseMode.HTML)
        return

    if dl.is_playlist(info):
        return await offer_playlist(status, context, url, info)
    await offer_formats(status, context, url, info)


async def offer_formats(status: Message, context, url: str, info: dict):
    page = info.get("webpage_url") or url
    key = remember(context, {"url": page, "title": info.get("title"), "vertical": dl.is_vertical(info),
                             "extractor_key": info.get("extractor_key"), "id": info.get("id"),
                             "sizes": {}})
    sess = recall(context, key)

    rows, row = [], []
    for c in dl.video_choices(info):
        sess["sizes"][c.key] = c.size
        too_big = c.size and c.size > UPLOAD_LIMIT * 1.1
        label = f"{'⛔ ' if too_big else '🎬 '}{c.label}" + (f" · {dl.human_size(c.size)}" if c.size else "")
        row.append(InlineKeyboardButton(label, callback_data=f"d:{key}:{c.key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if dl.has_audio(info):
        a = dl.best_audio_size(info)
        tail = f" · {dl.human_size(a)}" if a else ""
        rows.append([InlineKeyboardButton("🎵 MP3" + tail, callback_data=f"d:{key}:mp3"),
                     InlineKeyboardButton("🎧 M4A" + tail, callback_data=f"d:{key}:m4a")])
    if not rows:
        await status.edit_text("По ссылке нет ни видео, ни звука.")
        return

    meta = " · ".join(x for x in (info.get("uploader") or info.get("channel"), dl.human_duration(info.get("duration"))) if x)
    text = f"<b>{h(info.get('title') or 'Без названия')}</b>" + (f"\n{h(meta)}" if meta else "")
    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
                           link_preview_options=LinkPreviewOptions(is_disabled=True))


async def offer_playlist(status: Message, context, url: str, info: dict):
    entries = [e for e in info.get("entries") or [] if e and (e.get("url") or e.get("id"))]
    items = [{"url": e.get("url") or e.get("webpage_url") or e["id"], "title": e.get("title"),
              "id": e.get("id"), "ie_key": e.get("ie_key")} for e in entries][:PLAYLIST_LIMIT]
    key = remember(context, {"playlist": items, "title": info.get("title")})
    more = f" (первые {PLAYLIST_LIMIT})" if len(entries) > PLAYLIST_LIMIT else ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎵 MP3 всех — {len(items)}{more}",
                                                     callback_data=f"p:{key}:mp3")]])
    await status.edit_text(f"Плейлист <b>{h(info.get('title') or '')}</b>: {len(entries)} шт.\n"
                           "Чтобы скачать один ролик, пришли ссылку на него.",
                           parse_mode=ParseMode.HTML, reply_markup=kb)


# ═══════════════════════════════════════════════════════════════
# КНОПКИ
# ═══════════════════════════════════════════════════════════════
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("Это личный бот.", show_alert=True)
        return
    try:
        action, key, opt = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    sess = recall(context, key)
    if not sess:
        await q.answer("Кнопка устарела (бот перезапускался). Пришли ссылку ещё раз.", show_alert=True)
        return

    if action == "d":
        size = (sess.get("sizes") or {}).get(opt)
        if size and size > UPLOAD_LIMIT * 1.1:
            await q.answer(f"Примерно {dl.human_size(size)} — больше лимита Telegram "
                           f"({dl.human_size(UPLOAD_LIMIT)}). Возьми качество пониже.", show_alert=True)
            return
        await q.answer()
        asyncio.create_task(run_single(context, q.message, sess, opt))
    elif action == "p":
        await q.answer()
        await q.message.edit_reply_markup(None)
        asyncio.create_task(run_playlist(context, q.message, sess, opt))
    elif action == "s":
        await q.answer()
        await q.message.edit_reply_markup(None)
        asyncio.create_task(run_spotify_collection(context, q.message, sess))
    else:
        await q.answer()


async def run_single(context, menu: Message, sess: dict, opt: str):
    """Одно видео или аудио по кнопке. Меню остаётся — можно взять ещё и другой формат."""
    bot, chat_id = context.bot, menu.chat_id
    kind = "video" if opt.startswith("v") else "audio"
    key = cache_key(sess, sess["url"], opt)
    caption = f'<a href="{h(sess["url"])}">{h(sess.get("title") or "Источник")}</a>'

    hit = cache.get(key)
    if hit and await send_cached(bot, chat_id, hit, caption, reply_to=menu.message_id):
        return

    status_msg = await bot.send_message(chat_id, "В очереди…", reply_to_message_id=menu.message_id)
    status = Status(status_msg, asyncio.get_running_loop())
    wd = workdir()
    try:
        async with _slots:
            await status.set("Начинаю…")
            res = await asyncio.to_thread(dl.download, sess["url"], opt, wd, status, sess.get("vertical", False))
            await status.set(f"Отправляю {dl.human_size(os.path.getsize(res.path))}…")
            await send_result(bot, chat_id, res, kind, key, caption, reply_to=menu.message_id)
        await status.done()
    except dl.DownloadError as e:
        await status.set(f"Не вышло: {h(e)}")
    except TelegramError as e:
        logger.exception("Telegram не принял файл")
        await status.set(f"Telegram не принял файл: {h(e)}")
    except Exception as e:
        logger.exception("Сбой при скачивании %s", sess.get("url"))
        await status.set(f"Что-то сломалось: {h(e)}")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


async def run_playlist(context, menu: Message, sess: dict, opt: str):
    bot, chat_id = context.bot, menu.chat_id
    items = sess["playlist"]
    status = Status(await bot.send_message(chat_id, "Начинаю плейлист…"), asyncio.get_running_loop())
    ok, failed = 0, []
    for i, it in enumerate(items, 1):
        url = it["url"] if str(it["url"]).startswith("http") else f"https://www.youtube.com/watch?v={it['id']}"
        status.prefix = f"{i}/{len(items)} · {h(it.get('title') or '')}\n"
        key = f"{it.get('ie_key') or 'url'}:{it.get('id') or url}:{opt}"
        hit = cache.get(key)
        if hit and await send_cached(bot, chat_id, hit):
            ok += 1
            continue
        wd = workdir()
        try:
            async with _slots:
                await status.set("качаю…")
                res = await asyncio.to_thread(dl.download, url, opt, wd, status)
                await send_result(bot, chat_id, res, "audio", key)
            ok += 1
        except Exception as e:
            logger.warning("Плейлист: %s не вышел: %s", url, e)
            failed.append(it.get("title") or url)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
    await finish_batch(status, sess.get("title"), ok, failed)


async def finish_batch(status: Status, title: str | None, ok: int, failed: list[str]):
    status.prefix = ""
    text = f"Готово: <b>{h(title or '')}</b> — {ok} шт."
    if failed:
        text += f"\nНе вышло ({len(failed)}):\n" + "\n".join(f"• {h(t)}" for t in failed[:20])
    await status.set(text)


# ═══════════════════════════════════════════════════════════════
# SPOTIFY
# ═══════════════════════════════════════════════════════════════
async def handle_spotify(update: Update, context, url: str):
    msg = update.effective_message
    status_msg = await msg.reply_text("Spotify: смотрю трек…")
    try:
        parsed = await asyncio.to_thread(spotify.parse_link, url)
        if not parsed:
            raise dl.DownloadError("Не понял ссылку Spotify. Нужна ссылка на трек, альбом или плейлист.")
        coll = await asyncio.to_thread(spotify.fetch, *parsed)
    except dl.DownloadError as e:
        await status_msg.edit_text(f"Не вышло: {h(e)}", parse_mode=ParseMode.HTML)
        return
    except Exception as e:
        logger.exception("Spotify упал на %s", url)
        await status_msg.edit_text(f"Что-то сломалось: {h(e)}", parse_mode=ParseMode.HTML)
        return

    if coll.kind == "track":
        await run_spotify_track(context, status_msg, coll.tracks[0], reply_to=msg.message_id)
        return

    if not coll.tracks:
        await status_msg.edit_text("В этом плейлисте нет треков, которые можно скачать.")
        return
    tracks = coll.tracks[:PLAYLIST_LIMIT]
    key = remember(context, {"spotify": tracks, "title": coll.title})
    word = "Альбом" if coll.kind == "album" else "Плейлист"
    more = f" (первые {PLAYLIST_LIMIT})" if len(coll.tracks) > PLAYLIST_LIMIT else ""
    lines = "\n".join(f"{t.number or i}. {h(t.artist)} — {h(t.title)}" for i, t in enumerate(tracks[:15], 1))
    if len(tracks) > 15:
        lines += f"\n… и ещё {len(tracks) - 15}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎵 Скачать все — {len(tracks)}{more}",
                                                     callback_data=f"s:{key}:all")]])
    await status_msg.edit_text(f"{word} <b>{h(coll.title)}</b>" + (f" · {h(coll.owner)}" if coll.owner else "")
                               + f"\n\n{lines}", parse_mode=ParseMode.HTML, reply_markup=kb)


async def run_spotify_track(context, status_msg: Message, track: spotify.Track, reply_to: int | None = None,
                            status: Status | None = None) -> bool:
    bot, chat_id = context.bot, status_msg.chat_id
    key = f"spotify:{track.id}"
    own_status = status is None
    status = status or Status(status_msg, asyncio.get_running_loop(), prefix=f"{h(track.artist)} — {h(track.title)}\n")

    hit = cache.get(key)
    if hit and await send_cached(bot, chat_id, hit, reply_to=reply_to):
        if own_status:
            await status.done()
        return True

    wd = workdir()
    try:
        async with _slots:
            await status.set("ищу на YouTube Music…")
            res = await asyncio.to_thread(spotify.download_track, track, wd, status)
            await status.set("отправляю…")
            await send_result(bot, chat_id, res, "audio", key, reply_to=reply_to)
        if own_status:
            await status.done()
        return True
    except dl.DownloadError as e:
        if own_status:
            await status.set(f"Не вышло: {h(e)}")
        else:
            logger.warning("Spotify %s: %s", track.id, e)
        return False
    except Exception as e:
        logger.exception("Spotify-трек %s упал", track.id)
        if own_status:
            await status.set(f"Что-то сломалось: {h(e)}")
        return False
    finally:
        shutil.rmtree(wd, ignore_errors=True)


async def run_spotify_collection(context, menu: Message, sess: dict):
    tracks: list[spotify.Track] = sess["spotify"]
    status = Status(await context.bot.send_message(menu.chat_id, "Начинаю…"), asyncio.get_running_loop())
    ok, failed = 0, []
    for i, t in enumerate(tracks, 1):
        status.prefix = f"{i}/{len(tracks)} · {h(t.artist)} — {h(t.title)}\n"
        if await run_spotify_track(context, menu, t, status=status):
            ok += 1
        else:
            failed.append(f"{t.artist} — {t.title}")
    await finish_batch(status, sess.get("title"), ok, failed)


# ═══════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Любой сбой — в лог и ответом в чат, чтобы бот не молчал."""
    logger.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message and allowed(update):
        try:
            await update.effective_message.reply_text(f"Что-то сломалось: {h(context.error)}"[:1000])
        except TelegramError:
            pass


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Что я умею")])


def build_application() -> Application:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Нет TELEGRAM_TOKEN")
    if not ALLOWED_IDS:
        logger.warning("ALLOWED_IDS пуст — бот никому не отвечает, только подсказывает id. Напиши ему и впиши свой.")
    b = (Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).concurrent_updates(True)
         .connect_timeout(30).read_timeout(120).write_timeout(120).media_write_timeout(1800).pool_timeout(30))
    if LOCAL_BOT_API_URL:
        b = b.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
    app = b.build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
    return app


def main():
    clean_tmp()
    logger.info("Savebot: лимит файла %s, данные в %s, куки YouTube: %s",
                dl.human_size(UPLOAD_LIMIT), DATA_DIR, "есть" if dl.COOKIES_FILE else "нет")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()

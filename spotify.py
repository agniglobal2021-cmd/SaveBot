"""
Spotify без ключей и без Spotify API.

Звук в Spotify под DRM, поэтому, как и все такие боты, мы берём у Spotify только описание трека
(название, исполнители, длительность, обложка — со страницы плеера open.spotify.com/embed),
находим этот же трек на YouTube Music и качаем звук оттуда. Потом прописываем в mp3 теги и обложку
из Spotify, чтобы в Telegram и в плеере он выглядел как надо.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import httpx
import yt_dlp

import dl

logger = logging.getLogger("savebot.spotify")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

LINK_RE = re.compile(
    r"https?://(?:open|play)\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist)/([A-Za-z0-9]{22})")
SHORT_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi)/\S+")


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    duration: int | None = None          # секунды
    album: str | None = None
    cover: str | None = None             # url обложки
    number: int | None = None            # номер в альбоме/плейлисте

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def query(self) -> str:
        return f"{self.artists[0] if self.artists else ''} {self.title}".strip()


@dataclass
class Collection:
    kind: str                            # track | album | playlist
    id: str
    title: str
    tracks: list[Track] = field(default_factory=list)
    cover: str | None = None
    owner: str | None = None


def is_spotify(url: str) -> bool:
    return bool(LINK_RE.search(url) or SHORT_RE.search(url))


def parse_link(url: str) -> tuple[str, str] | None:
    """(тип, id) из ссылки или None. Короткие ссылки spotify.link раскрываем запросом."""
    m = LINK_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    if SHORT_RE.search(url):
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=15)
            m = LINK_RE.search(str(r.url)) or LINK_RE.search(r.text)
            if m:
                return m.group(1), m.group(2)
        except httpx.HTTPError as e:
            logger.warning("Не раскрылась короткая ссылка %s: %s", url, e)
    return None


# ═══════════════════════════════════════════════════════════════
# ОПИСАНИЕ СО СТРАНИЦЫ ПЛЕЕРА
# ═══════════════════════════════════════════════════════════════
def fetch(kind: str, sid: str) -> Collection:
    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en"},
                      follow_redirects=True, timeout=20)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise dl.DownloadError(f"Spotify не отдал страницу: {e}") from e
    return parse_embed(r.text, kind, sid)


def parse_embed(page: str, kind: str, sid: str) -> Collection:
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    entity = None
    if m:
        try:
            entity = _find_entity(json.loads(m.group(1)))
        except ValueError:
            entity = None
    if not entity:
        if kind == "track":
            return _parse_html_track(page, sid)
        raise dl.DownloadError("Spotify поменял страницу плеера — не нашёл список треков.")

    cover = _cover(entity)
    title = entity.get("name") or entity.get("title") or "Без названия"

    if kind == "track":
        t = Track(id=sid, title=title, artists=_artists(entity),
                  duration=_seconds(entity.get("duration")), cover=cover,
                  album=(entity.get("album") or {}).get("name") if isinstance(entity.get("album"), dict) else None)
        return Collection(kind="track", id=sid, title=t.title, tracks=[t], cover=cover)

    tracks = []
    for i, it in enumerate(entity.get("trackList") or [], 1):
        uri = it.get("uri") or ""
        tid = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else None
        if not tid:
            continue                      # подкасты и локальные файлы пропускаем
        artists = [a.strip() for a in (it.get("subtitle") or "").replace(" ", " ").split(",") if a.strip()]
        tracks.append(Track(id=tid, title=it.get("title") or "?", artists=artists or ["?"],
                            duration=_seconds(it.get("duration")),
                            album=title if kind == "album" else None,
                            cover=cover if kind == "album" else None, number=i))
    owner = entity.get("subtitle") or None
    return Collection(kind=kind, id=sid, title=title, tracks=tracks, cover=cover, owner=owner)


def _parse_html_track(page: str, sid: str) -> Collection:
    """Запасной путь, если JSON со страницы пропал: название, исполнители по ссылкам /artist/ и обложка из разметки."""
    import html as _html
    title = None
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', r'<h1[^>]*>(?:<[^>]+>)*([^<]+)',
                r'<title>([^<|]+)'):
        mm = re.search(pat, page)
        if mm:
            title = _html.unescape(mm.group(1)).strip()
            break
    artists = [_html.unescape(a).strip() for a in
               re.findall(r'href="(?:https://open\.spotify\.com)?/artist/[A-Za-z0-9]+[^"]*"[^>]*>(?:<[^>]+>)*([^<]+)<', page)]
    artists = list(dict.fromkeys(a for a in artists if a))
    cover = None
    mm = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page) or \
        re.search(r'(https://(?:i\.scdn\.co|image-cdn-[a-z]+\.spotifycdn\.com)/image/[A-Za-z0-9]+)', page)
    if mm:
        cover = mm.group(1)
    if not title:
        raise dl.DownloadError("Spotify поменял страницу плеера — не нашёл название трека.")
    t = Track(id=sid, title=title, artists=artists or ["?"], cover=cover)
    return Collection(kind="track", id=sid, title=title, tracks=[t], cover=cover)


def _find_entity(node):
    """Ищем объект с trackList или с исполнителями где бы он ни лежал: так переживём
    перестановки внутри JSON (обычно props.pageProps.state.data.entity)."""
    try:
        e = node["props"]["pageProps"]["state"]["data"]["entity"]
        if isinstance(e, dict):
            return e
    except (KeyError, TypeError):
        pass
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if ("trackList" in cur or "artists" in cur) and ("name" in cur or "title" in cur):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _artists(entity: dict) -> list[str]:
    arts = entity.get("artists")
    if isinstance(arts, list):
        names = [a.get("name") if isinstance(a, dict) else str(a) for a in arts]
        names = [n for n in names if n]
        if names:
            return names
    sub = entity.get("subtitle") or ""
    return [a.strip() for a in sub.split(",") if a.strip()] or ["?"]


def _cover(entity: dict) -> str | None:
    for key in ("coverArt", "visualIdentity"):
        block = entity.get(key) or {}
        srcs = block.get("sources") or block.get("image") or []
        if isinstance(srcs, list) and srcs:
            best = max(srcs, key=lambda s: (s.get("width") or s.get("maxWidth") or 0))
            if best.get("url"):
                return best["url"]
    images = entity.get("images") or []
    if images and isinstance(images, list):
        return images[0].get("url")
    return None


def _seconds(ms) -> int | None:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    return ms // 1000 if ms > 10000 else ms     # кое-где длительность уже в секундах


# ═══════════════════════════════════════════════════════════════
# ПОИСК НА YOUTUBE MUSIC
# ═══════════════════════════════════════════════════════════════
def find_on_youtube(track: Track) -> str:
    """Ссылка на этот трек на YouTube. Сначала раздел «Песни» YouTube Music (там официальные
    аудио без клипов и вставок), потом обычный поиск YouTube. Выбираем по длительности."""
    candidates: list[dict] = []
    q = f"{track.artist} {track.title}"
    for url in (f"https://music.youtube.com/search?q={quote_plus(q)}#songs",
                f"ytsearch6:{track.query} audio"):
        try:
            opts = dl._base_opts() | {"extract_flat": True, "skip_download": True, "playlistend": 6}
            with yt_dlp.YoutubeDL(opts) as ydl:
                res = ydl.extract_info(url, download=False) or {}
            candidates = [e for e in (res.get("entries") or []) if e and e.get("id")]
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Поиск %s не удался: %s", url, e)
            candidates = []
        best = pick_candidate(candidates, track)
        if best:
            return best
    raise dl.DownloadError(f"Не нашёл на YouTube: {track.artist} — {track.title}")


def pick_candidate(entries: list[dict], track: Track) -> str | None:
    """Лучший кандидат: ближе всех по длительности, без «live»/«cover»/«remix», если их нет в названии трека."""
    if not entries:
        return None
    bad_words = [w for w in ("live", "cover", "remix", "karaoke", "instrumental", "sped up", "slowed", "8d")
                 if w not in track.title.lower()]

    def score(i_e):
        i, e = i_e
        s = i * 2.0                                          # порядок выдачи тоже важен
        title = (e.get("title") or "").lower()
        s += sum(15 for w in bad_words if w in title)
        if track.duration and e.get("duration"):
            diff = abs(track.duration - e["duration"])
            s += diff if diff <= 15 else 40 + diff / 10
        elif track.duration:
            s += 8                                           # длительность неизвестна — лёгкий штраф
        return s

    i, best = min(enumerate(entries), key=score)
    if track.duration and best.get("duration") and abs(track.duration - best["duration"]) > 60:
        # совсем не та длина: скорее всего, нашёлся клип или сборник
        close = [e for e in entries if e.get("duration") and abs(track.duration - e["duration"]) <= 60]
        if not close:
            return None
        best = close[0]
    vid = best["id"]
    return best.get("url") if (best.get("url") or "").startswith("http") else f"https://www.youtube.com/watch?v={vid}"


# ═══════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ И ТЕГИ
# ═══════════════════════════════════════════════════════════════
def download_track(track: Track, workdir: str, progress=None) -> dl.Result:
    yt_url = find_on_youtube(track)
    logger.info("Spotify %s → %s", track.id, yt_url)
    res = dl.download(yt_url, "mp3", workdir, progress=progress)

    cover_path = None
    if track.cover:
        try:
            r = httpx.get(track.cover, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            cover_path = os.path.join(workdir, "_cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(r.content)
        except httpx.HTTPError as e:
            logger.warning("Обложка не скачалась: %s", e)
            cover_path = None

    write_tags(res.path, track, cover_path)
    res.title = track.title
    res.uploader = track.artist
    if cover_path:
        res.thumb = dl.make_thumb(workdir, res.path, src=cover_path) or res.thumb

    # имя файла «Исполнитель — Название.mp3», как в нормальной медиатеке
    nice = os.path.join(workdir, safe_name(f"{track.artist} — {track.title}") + ".mp3")
    if nice != res.path:
        os.replace(res.path, nice)
        res.path = nice
    return res


def write_tags(path: str, track: Track, cover_path: str | None) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK
    tags = ID3()          # с чистого листа: теги с YouTube (дата загрузки, ссылка) тут лишние
    tags.setall("TIT2", [TIT2(encoding=3, text=track.title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=track.artists)])
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=track.album)])
    if track.number:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track.number))])
    if cover_path:
        with open(cover_path, "rb") as f:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=f.read()))
    tags.save(path, v2_version=3)


def safe_name(name: str, limit: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    return name[:limit] or "track"

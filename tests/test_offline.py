"""Проверки без интернета: python -m pytest tests"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DATA_DIR", "/tmp/savebot-test-data")

import dl  # noqa: E402
import spotify  # noqa: E402


def fmt(fid, h, w, vcodec="avc1.640028", acodec="none", size=None, ext="mp4", tbr=None):
    return {"format_id": fid, "height": h, "width": w, "vcodec": vcodec, "acodec": acodec,
            "filesize": size, "ext": ext, "tbr": tbr}


YT_INFO = {
    "title": "Test", "duration": 300, "width": 1920, "height": 1080,
    "formats": [
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "abr": 129, "filesize": 5_000_000},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm", "abr": 140, "filesize": 5_200_000},
        fmt("137", 1080, 1920, size=60_000_000),
        fmt("248", 1080, 1920, vcodec="vp9", size=40_000_000, ext="webm"),
        fmt("136", 720, 1280, size=25_000_000),
        fmt("135", 480, 854, size=12_000_000),
        fmt("18", 360, 640, acodec="mp4a.40.2", size=None, tbr=500),
        fmt("313", 2160, 3840, vcodec="vp9", size=400_000_000, ext="webm"),
        fmt("sb0", None, None, vcodec="none"),          # раскадровка — не видео
    ],
}


def test_video_choices_landscape():
    ch = dl.video_choices(YT_INFO)
    assert [c.key for c in ch] == ["v2160", "v1080", "v720", "v480", "v360"]
    by = {c.key: c.size for c in ch}
    assert by["v1080"] == 65_000_000             # h264 1080 + m4a
    assert by["v720"] == 30_000_000
    assert by["v360"] == 500 * 1000 // 8 * 300   # оценка по битрейту, звук уже внутри
    assert by["v2160"] == 405_000_000            # h264 нет — берём что есть


def test_vertical_short():
    info = {"duration": 30, "formats": [
        {"format_id": "a", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "abr": 128, "filesize": 500_000},
        fmt("v1", 1920, 1080, size=10_000_000),
        fmt("v2", 1280, 720, size=5_000_000),
    ]}
    assert dl.is_vertical(info)
    assert [c.key for c in dl.video_choices(info)] == ["v1080", "v720"]
    assert "[width<=" in dl.video_format(1080, vertical=True)
    assert "[height<=" in dl.video_format(1080, vertical=False)


def test_video_format_limits():
    f = dl.video_format(720)
    assert "[height<=806]" in f and f.startswith("bv*[height<=806][vcodec^=avc1]+ba[ext=m4a]")
    assert f.endswith("bv*+ba/b")
    assert dl.video_format(None).endswith("/b")


def test_snap_height():
    assert dl._snap_height(1072) == 1080
    assert dl._snap_height(700) == 720
    assert dl._snap_height(404) == 360
    assert dl._snap_height(100) == 100


def test_audio_only_source():
    info = {"duration": 200, "formats": [
        {"format_id": "mp3", "vcodec": "none", "acodec": "mp3", "ext": "mp3", "abr": 128, "filesize": 3_000_000}]}
    assert dl.video_choices(info) == []
    assert dl.has_audio(info)
    assert dl.best_audio_size(info) == 3_000_000


def test_direct_file_without_formats():
    info = {"ext": "mp4", "url": "http://x/y.mp4", "duration": 10}
    ch = dl.video_choices(info)
    assert [c.key for c in ch] == ["vbest"]
    assert dl.has_audio(info)


def test_explain():
    assert "YT_COOKIES" in dl.explain(Exception("ERROR: [youtube] abc: Sign in to confirm you're not a bot"))
    assert dl.explain(Exception("ERROR: Unsupported URL: https://x")) == "Эту ссылку я не умею открывать."
    assert dl.explain(Exception("ERROR: something odd\nmore")) == "something odd"


def test_human():
    assert dl.human_size(500) == "500 Б"
    assert dl.human_size(52_428_800) == "50 МБ"
    assert dl.human_size(65_000_000) == "62 МБ"
    assert dl.human_size(2_097_152_000) == "2 ГБ"
    assert dl.human_size(None) == "?"
    assert dl.human_duration(3725) == "1:02:05"
    assert dl.human_duration(65) == "1:05"


def test_cookies_base64(tmp_path, monkeypatch):
    import base64
    txt = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    monkeypatch.setenv("YT_COOKIES", base64.b64encode(txt.encode()).decode())
    dl.init_cookies(str(tmp_path))
    assert open(dl.COOKIES_FILE).read() == txt
    opts = dl._base_opts()
    assert opts["cookiefile"].endswith(".work")
    dl.COOKIES_FILE = None


# ─────────────────────────── Spotify ───────────────────────────
def embed_page(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_spotify_links():
    assert spotify.parse_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc") == \
        ("track", "4uLU6hMCjMI75M1A2tKUQC")
    assert spotify.parse_link("https://open.spotify.com/intl-ru/album/1DFixLWuPkv3KT3TnV35m3") == \
        ("album", "1DFixLWuPkv3KT3TnV35m3")
    assert spotify.parse_link("check https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M yo")[0] == "playlist"
    assert spotify.is_spotify("https://spotify.link/abcDEF")
    assert not spotify.is_spotify("https://youtube.com/watch?v=x")


def test_spotify_track_embed():
    page = embed_page({
        "type": "track", "name": "Bohemian Rhapsody", "title": "Bohemian Rhapsody",
        "artists": [{"name": "Queen", "uri": "spotify:artist:1"}],
        "duration": 354320,
        "coverArt": {"sources": [{"url": "https://i.scdn.co/small", "width": 64},
                                 {"url": "https://i.scdn.co/big", "width": 640}]},
    })
    c = spotify.parse_embed(page, "track", "x" * 22)
    t = c.tracks[0]
    assert (t.title, t.artists, t.duration, t.cover) == ("Bohemian Rhapsody", ["Queen"], 354, "https://i.scdn.co/big")


def test_spotify_album_embed():
    page = embed_page({
        "type": "album", "name": "Discovery", "subtitle": "Daft Punk",
        "coverArt": {"sources": [{"url": "https://i.scdn.co/disc", "width": 300}]},
        "trackList": [
            {"uri": "spotify:track:aaaaaaaaaaaaaaaaaaaaaa", "title": "One More Time", "subtitle": "Daft Punk", "duration": 320357},
            {"uri": "spotify:episode:bbbbbbbbbbbbbbbbbbbbbb", "title": "Podcast", "subtitle": "x", "duration": 1},
            {"uri": "spotify:track:cccccccccccccccccccccc", "title": "Digital Love",
             "subtitle": "Daft Punk, DJ Sneak", "duration": 301000},
        ],
    })
    c = spotify.parse_embed(page, "album", "y" * 22)
    assert c.title == "Discovery" and len(c.tracks) == 2
    assert c.tracks[1].artists == ["Daft Punk", "DJ Sneak"]
    assert c.tracks[1].album == "Discovery" and c.tracks[1].cover == "https://i.scdn.co/disc"


def test_spotify_entity_moved():
    """Если Spotify переложит entity в другое место JSON — всё равно найдём."""
    data = {"props": {"pageProps": {"somethingNew": {"x": [{"name": "Song", "artists": [{"name": "A"}],
                                                             "duration": 200000}]}}}}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    c = spotify.parse_embed(page, "track", "z" * 22)
    assert c.tracks[0].artist == "A"


def test_pick_candidate():
    t = spotify.Track(id="1", title="Song", artists=["Artist"], duration=200)
    entries = [
        {"id": "clip", "title": "Artist - Song (Official Video)", "duration": 260},
        {"id": "live", "title": "Artist - Song (Live)", "duration": 202},
        {"id": "good", "title": "Song", "duration": 201},
    ]
    assert spotify.pick_candidate(entries, t).endswith("v=good")
    assert spotify.pick_candidate([{"id": "mix", "title": "3 hour mix", "duration": 10800}], t) is None
    # live в названии трека — тогда live не штрафуем
    tl = spotify.Track(id="2", title="Song (Live)", artists=["Artist"], duration=202)
    assert spotify.pick_candidate(entries, tl).endswith("v=live")


def test_safe_name():
    assert spotify.safe_name('AC/DC — Back: "In" Black?') == "ACDC — Back In Black"


def test_spotify_html_fallback():
    page = ('<html><head><title>Never Gonna Give You Up</title></head><body>'
            '<img src="https://image-cdn-fa.spotifycdn.com/image/ab67616d00001e02255e">'
            '<h1><a href="https://open.spotify.com/track/x">Never Gonna Give You Up</a></h1>'
            '<a href="https://open.spotify.com/artist/0gxyHStUsqpMadRV0Di1Qt?si=1"><span>Rick Astley</span></a>'
            '</body></html>')
    t = spotify.parse_embed(page, "track", "q" * 22).tracks[0]
    assert t.title == "Never Gonna Give You Up" and t.artists == ["Rick Astley"]
    assert t.cover.startswith("https://image-cdn-fa.spotifycdn.com/image/")


def test_find_url_real_message():
    """Telegram отдаёт entities кортежем — раньше на этом падало."""
    from datetime import datetime
    from telegram import Chat, Message, MessageEntity
    import bot
    chat = Chat(1, "private")
    m = Message(1, datetime.now(), chat, text="глянь https://youtu.be/abc).",
                entities=(MessageEntity("url", 6, 21),))
    assert bot.find_url(m) == "https://youtu.be/abc"
    m = Message(2, datetime.now(), chat, text="тут", entities=(MessageEntity("text_link", 0, 3, url="https://x.com/a"),))
    assert bot.find_url(m) == "https://x.com/a"
    m = Message(3, datetime.now(), chat, caption="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
    assert "spotify" in bot.find_url(m)
    assert bot.find_url(Message(4, datetime.now(), chat, text="привет")) is None


def test_spotify_album_html_fallback():
    rows = "".join(
        f'<li><div><span>{i}</span></div><div><h3 class="x"><span>{t}</span></h3>'
        f'<h4>Carly Rae Jepsen</h4></div><div>{d}</div></li>'
        for i, (t, d) in enumerate([("Run Away With Me", "04:11"), ("Emotion", "03:17"), ("I Didn&#x27;t Just", "1:02:03")], 1))
    page = ('<html><h1>Emotion (Deluxe)</h1><img src="https://image-cdn-fa.spotifycdn.com/image/abc123">'
            f'<ol>{rows}</ol></html>')
    c = spotify.parse_embed(page, "album", "e" * 22)
    assert c.title == "Emotion (Deluxe)" and len(c.tracks) == 3
    t1, _, t3 = c.tracks
    assert (t1.title, t1.artists, t1.duration, t1.album) == ("Run Away With Me", ["Carly Rae Jepsen"], 251, "Emotion (Deluxe)")
    assert t3.title == "I Didn't Just" and t3.duration == 3723
    assert t1.id != c.tracks[1].id and len(t1.id) == 22
    assert t1.cover.startswith("https://image-cdn-fa")


# ─────────────────────── обходы блокировки ───────────────────────
class FakeYDL:
    calls = []
    rule = staticmethod(lambda o: True)

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, url, download):
        import yt_dlp
        FakeYDL.calls.append((self.opts.get("proxy"), (self.opts.get("extractor_args") or {}).get("youtube")))
        if not FakeYDL.rule(self.opts):
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] x: Sign in to confirm you're not a bot")
        return {"id": "x", "title": "ok"}

    def sanitize_info(self, i):
        return i


def test_strategies_fall_through_to_warp(monkeypatch):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(dl, "warp_up", lambda: True)
    monkeypatch.setattr(dl, "_working", 0)
    FakeYDL.calls, FakeYDL.rule = [], staticmethod(lambda o: bool(o.get("proxy")))
    assert dl.extract("u", {}, False)["title"] == "ok"
    assert len(FakeYDL.calls) == 4 and FakeYDL.calls[-1][0] == dl.WARP_PROXY
    # второй раз сразу начинаем с WARP
    FakeYDL.calls = []
    dl.extract("u", {}, False)
    assert len(FakeYDL.calls) == 1


def test_strategies_no_warp_and_blocked(monkeypatch):
    import yt_dlp
    monkeypatch.setattr(dl, "_blocked_until", 0.0)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(dl, "warp_up", lambda: False)
    monkeypatch.setattr(dl, "_working", 0)
    FakeYDL.calls, FakeYDL.rule = [], staticmethod(lambda o: False)
    try:
        dl.extract("u", {}, False)
        assert False
    except dl.DownloadError as e:
        assert "WARP не работает" in str(e) and "YT_COOKIES" in str(e)
    assert len(FakeYDL.calls) == 3           # без WARP — только прямые способы


def test_other_errors_not_retried(monkeypatch):
    import yt_dlp

    class Boom(FakeYDL):
        def extract_info(self, url, download):
            FakeYDL.calls.append(1)
            raise yt_dlp.utils.DownloadError("ERROR: Private video")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Boom)
    monkeypatch.setattr(dl, "_working", 0)
    FakeYDL.calls = []
    try:
        dl.extract("u", {}, False)
    except dl.DownloadError as e:
        assert str(e) == "Видео приватное."
    assert FakeYDL.calls == [1]


def test_sc_match():
    t = spotify.Track(id="1", title="Fahr zur Hölle", artists=["Bohren & Der Club Of Gore"], duration=600)
    good = {"title": "Fahr zur Hölle", "uploader": "Bohren & Der Club Of Gore", "duration": 601}
    fan = {"title": "Bohren - Fahr Zur Hölle", "uploader": "jazzfan", "duration": 598}
    preview = {"title": "Fahr zur Hölle", "uploader": "x", "duration": 30}
    other = {"title": "Im Rauch", "uploader": "Bohren & Der Club Of Gore", "duration": 600}
    remix = {"title": "Fahr zur Hölle (Remix)", "uploader": "dj", "duration": 605}
    assert spotify.sc_match(preview, t) is None and spotify.sc_match(other, t) is None
    assert spotify.sc_match(good, t) < spotify.sc_match(fan, t) < spotify.sc_match(remix, t)


def test_blocked_youtube_goes_straight_to_soundcloud(monkeypatch, tmp_path):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(dl, "warp_up", lambda: False)
    monkeypatch.setattr(dl, "_working", 0)
    monkeypatch.setattr(dl, "_blocked_until", 0.0)
    FakeYDL.calls, FakeYDL.rule = [], staticmethod(lambda o: False)
    t = spotify.Track(id="1", title="Song", artists=["A"], duration=200)
    sc = []
    monkeypatch.setattr(spotify, "from_soundcloud", lambda tr, wd, pr: sc.append(tr) or None)
    for _ in range(3):
        try:
            spotify.download_track(t, str(tmp_path))
        except dl.DownloadError as e:
            assert "SoundCloud" in str(e)
    # YouTube пробовали только в первый раз (3 прямых способа, один поиск), дальше — сразу SoundCloud
    assert len(FakeYDL.calls) == 3 and len(sc) == 3
    assert dl.youtube_blocked()

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

"""Offline tests for the metadata layer in music_sources.

Every client takes an injectable ``fetch`` callable, so we exercise the parse +
merge logic with canned JSON — no network, fully reproducible.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from music_sources import (  # noqa: E402
    DeezerClient, MusicBrainzClient, GetSongBpmClient, MetadataResolver, TrackMetadata,
)


# ----------------------------------------------------------------- Deezer ----

def _deezer_fetch_factory():
    def fetch(url):
        if "/search" in url:
            return {"data": [
                {"id": 111, "title": "Strobe", "duration": 600,
                 "artist": {"name": "deadmau5"}},
                {"id": 222, "title": "Some Remix", "duration": 200,
                 "artist": {"name": "Other"}},
            ]}
        if "/track/111" in url:
            return {"id": 111, "bpm": 128.0, "album": {"id": 9}}
        if "/album/9" in url:
            return {"id": 9, "genres": {"data": [{"name": "Electro"}]}}
        return None
    return fetch


def test_deezer_lookup_bpm_and_genre():
    c = DeezerClient(fetch=_deezer_fetch_factory())
    meta = c.lookup("Strobe", "deadmau5", 600_000)
    assert meta is not None
    assert meta.bpm == 128.0
    assert meta.bpm_source == "deezer"
    assert meta.genre == "Electro"
    assert "deezer" in meta.sources


def test_deezer_no_match_returns_none():
    c = DeezerClient(fetch=lambda url: {"data": [
        {"id": 1, "title": "Totally Different", "duration": 100,
         "artist": {"name": "Nobody"}}]})
    assert c.lookup("Strobe", "deadmau5", 600_000) is None


# ------------------------------------------------------------ MusicBrainz ----

def test_musicbrainz_genre_from_tags():
    def fetch(url):
        return {"recordings": [{
            "title": "Strobe", "score": 100,
            "artist-credit": [{"artist": {"name": "deadmau5"}}],
            "tags": [{"name": "progressive house", "count": 5},
                     {"name": "electronic", "count": 2}],
        }]}
    c = MusicBrainzClient(fetch=fetch)
    meta = c.lookup("Strobe", "deadmau5", None)
    assert meta is not None
    assert meta.genre == "progressive house"   # most-voted tag wins
    assert meta.bpm is None                      # MB doesn't supply BPM
    assert "musicbrainz" in meta.sources


# -------------------------------------------------------------- GetSongBPM --

def test_getsongbpm_requires_key():
    c = GetSongBpmClient(fetch=lambda url: {"search": []})
    assert c.available() is False
    assert c.lookup("Strobe", "deadmau5", None) is None
    c.set_api_key("abc123")
    assert c.available() is True


def test_getsongbpm_parses_tempo_and_key():
    def fetch(url):
        return {"search": [{"title": "Strobe", "tempo": "128",
                            "key_of": "B minor", "artist": {"name": "deadmau5"}}]}
    c = GetSongBpmClient(fetch=fetch)
    c.set_api_key("abc123")
    meta = c.lookup("Strobe", "deadmau5", None)
    assert meta.bpm == 128.0
    assert meta.musical_key == "B minor"
    assert meta.bpm_source == "getsongbpm"


# ----------------------------------------------------------------- merge -----

def test_metadata_merge_first_non_empty_wins():
    a = TrackMetadata(bpm=128.0, bpm_source="deezer", sources=["deezer"])
    b = TrackMetadata(genre="house", musical_key="Am", sources=["musicbrainz"])
    a.merge(b)
    assert a.bpm == 128.0
    assert a.genre == "house"
    assert a.musical_key == "Am"
    assert a.sources == ["deezer", "musicbrainz"]


# -------------------------------------------------------------- resolver -----

def test_resolver_cascade_merges_sources():
    # Deezer gives BPM only; MusicBrainz gives genre. Resolver merges both.
    deezer = DeezerClient(fetch=_deezer_fetch_factory())
    # Override the album genre to be empty so MB must supply the genre.
    def deezer_fetch_no_genre(url):
        if "/album/9" in url:
            return {"id": 9, "genres": {"data": []}}
        return _deezer_fetch_factory()(url)
    deezer._fetch = deezer_fetch_no_genre

    def mb_fetch(url):
        return {"recordings": [{
            "title": "Strobe", "score": 100,
            "artist-credit": [{"artist": {"name": "deadmau5"}}],
            "tags": [{"name": "progressive house", "count": 9}],
        }]}
    mb = MusicBrainzClient(fetch=mb_fetch)

    r = MetadataResolver(deezer=deezer, musicbrainz=mb,
                         getsongbpm=GetSongBpmClient(fetch=lambda u: None))
    meta = r.resolve("Strobe", "deadmau5", 600_000)
    assert meta is not None
    assert meta.bpm == 128.0
    assert meta.genre == "progressive house"
    assert set(meta.sources) == {"deezer", "musicbrainz"}


def test_resolver_disabled_returns_none():
    r = MetadataResolver(
        deezer=DeezerClient(fetch=_deezer_fetch_factory()),
        musicbrainz=MusicBrainzClient(fetch=lambda u: None),
        getsongbpm=GetSongBpmClient(fetch=lambda u: None),
    )
    r.enabled = False
    assert r.resolve("Strobe", "deadmau5", 600_000) is None


def test_resolver_caches_result():
    calls = {"n": 0}
    def fetch(url):
        calls["n"] += 1
        return _deezer_fetch_factory()(url)
    r = MetadataResolver(
        deezer=DeezerClient(fetch=fetch),
        musicbrainz=MusicBrainzClient(fetch=lambda u: None),
        getsongbpm=GetSongBpmClient(fetch=lambda u: None),
    )
    r.resolve("Strobe", "deadmau5", 600_000)
    n_after_first = calls["n"]
    r.resolve("Strobe", "deadmau5", 600_000)  # cached → no new fetches
    assert calls["n"] == n_after_first


def test_getsongbpm_preferred_for_bpm_when_configured():
    # When GetSongBPM is configured it should be queried first and win the BPM.
    gs = GetSongBpmClient(fetch=lambda url: {"search": [
        {"title": "Strobe", "tempo": "127", "key_of": "Bm",
         "artist": {"name": "deadmau5"}}]})
    gs.set_api_key("k")
    deezer = DeezerClient(fetch=_deezer_fetch_factory())  # would give 128
    r = MetadataResolver(deezer=deezer,
                         musicbrainz=MusicBrainzClient(fetch=lambda u: None),
                         getsongbpm=gs)
    meta = r.resolve("Strobe", "deadmau5", 600_000)
    assert meta.bpm == 127.0                 # GetSongBPM wins (queried first)
    assert meta.bpm_source == "getsongbpm"
    assert meta.musical_key == "Bm"

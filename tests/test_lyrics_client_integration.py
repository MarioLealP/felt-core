"""Real network call, recorded once via VCR (see conftest.py) - LRCLIB is
public and keyless, unlike spotify_client_integration.py's Spotify calls,
so this needs no credentials fixture at all, real or dummy."""

import pytest

from felt_core.lyrics_client import get_lyrics


@pytest.mark.vcr
async def test_get_lyrics_finds_a_real_synced_track(tmp_path):
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        result = await get_lyrics(
            client,
            tmp_path,
            "bohemian-rhapsody-queen",
            "Bohemian Rhapsody",
            "Queen",
            "A Night at the Opera",
            355,
        )

    assert result is not None
    assert len(result["synced"]) > 0
    assert any("real life" in line["text"].lower() for line in result["synced"])

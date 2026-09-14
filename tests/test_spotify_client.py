from felt_core.spotify_client import format_track_name, pick_device


def test_pick_device_prefers_active():
    devices = [
        {"id": "a", "name": "Desktop", "is_active": False},
        {"id": "b", "name": "Phone", "is_active": True},
    ]
    assert pick_device(devices)["id"] == "b"


def test_pick_device_falls_back_to_first_when_none_active():
    devices = [
        {"id": "a", "name": "Desktop", "is_active": False},
        {"id": "b", "name": "Phone", "is_active": False},
    ]
    assert pick_device(devices)["id"] == "a"


def test_pick_device_none_when_no_devices():
    assert pick_device([]) is None


def test_format_track_name_with_multiple_artists():
    track = {"name": "Stayin' Alive", "artists": [{"name": "Bee Gees"}]}
    assert format_track_name(track) == "Stayin' Alive — Bee Gees"


def test_format_track_name_with_no_artist_field():
    track = {"name": "Unknown Track", "artists": []}
    assert format_track_name(track) == "Unknown Track"

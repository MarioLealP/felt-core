"""The HA integration's brief lookup (ha/custom_components/felt/brief.py):
today's newest "brief" line, paging back, stopping at yesterday."""

import asyncio
import importlib.util
from datetime import UTC, date
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "felt_ha_brief", Path(__file__).parent.parent / "ha/custom_components/felt/brief.py"
)
assert _spec and _spec.loader
brief = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brief)

UTC = UTC
TODAY = date(2026, 10, 26)


def line(i: int, source: str, at: str, content: str = "x") -> dict:
    return {"id": i, "source": source, "content": content, "created_at": at}


def pages(*ps: list[dict]):
    """A fake /margins: the first call gets the newest page, then older ones."""
    calls: list[str] = []
    seq = list(ps)

    async def fetch(path: str):
        calls.append(path)
        return seq.pop(0) if seq else []

    return fetch, calls


def run(fetch) -> dict | None:
    return asyncio.run(brief.todays_brief(fetch, TODAY, UTC))


def test_finds_todays_brief_behind_newer_remarks():
    fetch, _ = pages(
        [
            line(1, "brief", "2026-10-26T07:00:00Z", "rain later"),
            line(2, "remark", "2026-10-26T09:00:00Z"),
            line(3, "song", "2026-10-26T10:00:00Z"),
        ]
    )
    got = run(fetch)
    assert got is not None and got["text"] == "rain later" and got["at"].hour == 7


def test_pages_back_with_before_the_oldest_id():
    fetch, calls = pages(
        [line(10, "remark", "2026-10-26T10:00:00Z")],
        [line(5, "brief", "2026-10-26T06:30:00Z", "found")],
    )
    got = run(fetch)
    assert got is not None and got["text"] == "found"
    assert calls[1].endswith("&before=10")


def test_yesterdays_brief_is_not_today():
    fetch, calls = pages(
        [line(1, "brief", "2026-10-25T07:00:00Z"), line(2, "remark", "2026-10-26T08:00:00Z")]
    )
    assert run(fetch) is None
    assert len(calls) == 1  # stopped at yesterday, no more pages


def test_nothing_at_all():
    fetch, _ = pages([])
    assert run(fetch) is None

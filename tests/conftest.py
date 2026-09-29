"""pytest-recording (VCR) config — real HTTP calls to public APIs
(poe.ninja, the official PoE trade API, etc.) get recorded to a cassette
once and replayed from disk on every subsequent test run, instead of
hitting the real service every time `pytest` runs. Default record mode
is "none" (pytest-recording's own default): a test whose cassette
doesn't have a matching recorded interaction fails loudly rather than
silently falling back to a real request. To (re)record a cassette after
writing/changing a `@pytest.mark.vcr` test, run once with
`uv run pytest --record-mode=once path/to/test.py`, then commit the
generated `tests/cassettes/...` file.
"""

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from felt_core.config import FeltConfig

_SECRET_JSON_FIELDS = ("access_token", "refresh_token", "id_token", "client_secret")

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def load_plugin_module(relative_skill_path: str) -> ModuleType:
    """Import a plugin's skill.py directly from its file path, bypassing
    the package layout - shared by every `test_*_skill.py` that used to
    reimplement this trio of `importlib.util` calls itself (one per
    plugin, ~12 copies, `ENGINEERING-RULES.md` rule 5). Plugins aren't a
    real Python package (`plugin_loader.py` discovers them by directory,
    not by import), so tests load the module by path the same way the
    real loader does - see that module's own `load_plugins()`.

    `relative_skill_path` is e.g. "now_playing/skill.py"."""
    skill_path = _PLUGINS_DIR / relative_skill_path
    spec = importlib.util.spec_from_file_location(skill_path.stem, skill_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load plugin module from {skill_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scrub_response_secrets(response):
    """Applied to every recorded response, project-wide: strips cookies
    (`filter_headers` only covers REQUEST headers — vcrpy doesn't wire it
    into response recording at all, checked its source 2026-09-14) and
    redacts any top-level JSON field that's secret-shaped by name (an
    OAuth token endpoint response, for instance) so a real credential
    never ends up sitting in a committed cassette, regardless of which
    skill's test recorded it."""
    headers = response.get("headers", {})
    for key in list(headers):
        if key.lower() in ("set-cookie", "cookie"):
            del headers[key]

    body = response.get("body", {})
    raw = body.get("string")
    if raw:
        try:
            text = raw.decode() if isinstance(raw, bytes) else raw
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and any(field in data for field in _SECRET_JSON_FIELDS):
            for field in _SECRET_JSON_FIELDS:
                if field in data:
                    data[field] = "REDACTED"
            new_raw = json.dumps(data)
            body["string"] = new_raw.encode() if isinstance(raw, bytes) else new_raw

    return response


@pytest.fixture(scope="module")
def vcr_config():
    return {
        # Body matters here — some tests POST different query bodies to
        # the same trade-API URL in one test function, and body-matching
        # keeps each request paired with the right recorded response
        # instead of relying on recorded order alone.
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        "filter_headers": ["authorization", "cookie"],
        "filter_post_data_parameters": ["client_secret", "refresh_token", "client_id"],
        "before_record_response": _scrub_response_secrets,
    }


@pytest.fixture
def google_calendar_config():
    """Real credentials only matter for the one-time `--record-mode=once`
    recording pass (export FELT_GOOGLE_CALENDAR_* first) — on ordinary
    replay runs the HTTP layer is intercepted before any real request
    goes out, so these dummy fallbacks are all a normal `pytest` run
    ever needs."""
    return FeltConfig(
        google_calendar_client_id=os.environ.get(
            "FELT_GOOGLE_CALENDAR_CLIENT_ID", "dummy-client-id"
        ),
        google_calendar_client_secret=os.environ.get(
            "FELT_GOOGLE_CALENDAR_CLIENT_SECRET", "dummy-client-secret"
        ),
        google_calendar_refresh_token=os.environ.get(
            "FELT_GOOGLE_CALENDAR_REFRESH_TOKEN", "dummy-refresh-token"
        ),
    )


@pytest.fixture
def spotify_config():
    """Same real-credentials-only-for-recording story as google_calendar_config."""
    return FeltConfig(
        spotify_client_id=os.environ.get("FELT_SPOTIFY_CLIENT_ID", "dummy-client-id"),
        spotify_client_secret=os.environ.get("FELT_SPOTIFY_CLIENT_SECRET", "dummy-client-secret"),
        spotify_refresh_token=os.environ.get("FELT_SPOTIFY_REFRESH_TOKEN", "dummy-refresh-token"),
    )


@pytest.fixture(autouse=True)
def _stable_persona_examples(monkeypatch):
    """Live replies rotate the persona's example sample on every call
    (brain/persona.py); tests compare prompts for equality, so pin the
    rotation to the same stable compile PERSONALITY_PROMPT uses.
    test_persona.py exercises the rotation itself via brain.persona
    directly, which this doesn't touch."""
    import random

    from felt_core.brain import personality
    from felt_core.brain.persona import compile_persona

    stable = compile_persona(rng=random.Random(0))
    monkeypatch.setattr(personality, "compile_persona", lambda *a, **k: stable)


@pytest.fixture(autouse=True)
def _local_first_off_by_default(monkeypatch):
    """WP-36: `local_first` defaults to True in production, but every test
    written before it exercises the Ollama/router/cloud path and would
    otherwise try to reach felt-llm. Tests for the local-first path pass
    `local_first=True` to FeltConfig explicitly (init kwargs beat env)."""
    monkeypatch.setenv("FELT_LOCAL_FIRST", "false")

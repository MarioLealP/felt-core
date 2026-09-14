import importlib.util
from pathlib import Path

from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext

_SKILL_PATH = Path(__file__).resolve().parent.parent / "plugins" / "play_music" / "skill.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("play_music_test", _SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reports_unconfigured_when_no_refresh_token():
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token=None)
    ctx = ExecutionContext(config=config, original_message="play Bohemian Rhapsody")

    result = await skill.handle({"query": "Bohemian Rhapsody"}, ctx)

    assert "isn't connected" in result.text


async def test_asks_what_to_play_when_query_is_blank():
    skill = _load_skill()
    config = FeltConfig(spotify_refresh_token="dummy-token")
    ctx = ExecutionContext(config=config, original_message="")

    result = await skill.handle({"query": "   "}, ctx)

    assert "what should i play" in result.text.lower()

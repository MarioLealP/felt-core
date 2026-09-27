import os
import time

from felt_core import plugin_loader

SKILL = """name = "{n}"
description = "d"
parameters_schema = {{}}


async def handle(params, ctx):
    return None
"""


def _write(d, n):
    p = d / n
    p.mkdir(exist_ok=True)
    (p / "skill.py").write_text(SKILL.format(n=n), encoding="utf-8")
    return p / "skill.py"


def test_registry_is_cached_until_a_skill_changes(tmp_path, monkeypatch):
    """WP-24: load_plugins runs per chat message; it must not re-import every
    plugin each time, but must pick up an edited or added plugin."""
    _write(tmp_path, "alpha")
    calls = []
    real = plugin_loader._scan_plugins
    monkeypatch.setattr(plugin_loader, "_scan_plugins", lambda d: calls.append(d) or real(d))

    first = plugin_loader.load_plugins(str(tmp_path))
    again = plugin_loader.load_plugins(str(tmp_path))
    assert "alpha" in first
    assert again is first and len(calls) == 1

    skill = _write(tmp_path, "alpha")  # edit in place: a subfolder file, not the dir
    later = time.time() + 5
    os.utime(skill, (later, later))
    plugin_loader.load_plugins(str(tmp_path))
    assert len(calls) == 2

    _write(tmp_path, "beta")  # a new plugin folder
    reg = plugin_loader.load_plugins(str(tmp_path))
    assert len(calls) == 3 and "beta" in reg

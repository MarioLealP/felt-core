from felt_core.plugin_loader import load_plugins

_VALID_SKILL = '''
name = "example_skill"
description = "An example skill."
parameters_schema = {"type": "object", "properties": {}}

async def handle(params, ctx):
    return "ok"
'''

_MALFORMED_SKILL = "this is not valid python syntax !!!"


def test_loads_a_valid_plugin(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)

    registry = load_plugins(str(tmp_path))

    assert "example_skill" in registry
    assert registry["example_skill"].description == "An example skill."


def test_malformed_plugin_is_skipped_not_fatal(tmp_path):
    good_dir = tmp_path / "good_skill"
    good_dir.mkdir()
    (good_dir / "skill.py").write_text(_VALID_SKILL.replace("example_skill", "good_skill"))

    bad_dir = tmp_path / "bad_skill"
    bad_dir.mkdir()
    (bad_dir / "skill.py").write_text(_MALFORMED_SKILL)

    registry = load_plugins(str(tmp_path))

    # The malformed plugin doesn't take the whole service down with it —
    # the good one still loads.
    assert "good_skill" in registry
    assert "bad_skill" not in registry


def test_missing_plugins_dir_returns_empty_registry(tmp_path):
    assert load_plugins(str(tmp_path / "does-not-exist")) == {}


def test_folder_without_skill_py_is_ignored(tmp_path):
    (tmp_path / "not_a_skill").mkdir()
    assert load_plugins(str(tmp_path)) == {}

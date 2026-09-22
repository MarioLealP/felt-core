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


def test_skill_gets_its_default_tag_from_the_manifest(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "tags.json").write_text('{"example_skill": "reaction/weather"}')

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].default_tag == "reaction/weather"


def test_skill_missing_from_manifest_has_no_default_tag(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "tags.json").write_text('{"some_other_skill": "reaction/weather"}')

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].default_tag is None


def test_no_manifest_file_is_fine_not_fatal(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].default_tag is None


def test_manifest_comment_key_is_not_treated_as_a_skill_tag(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "tags.json").write_text(
        '{"_comment": "not a skill", "example_skill": "reaction/weather"}'
    )

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].default_tag == "reaction/weather"


def test_malformed_manifest_is_skipped_not_fatal(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "tags.json").write_text("this is not valid json !!!")

    registry = load_plugins(str(tmp_path))

    # A broken manifest degrades to "no default tags for anyone" rather
    # than taking the whole plugin-loading process down with it — same
    # resilience principle as a single malformed skill.py above.
    assert "example_skill" in registry
    assert registry["example_skill"].default_tag is None


def test_skill_gets_its_privacy_class_from_skills_manifest(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "skills.json").write_text('{"example_skill": "public"}')

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].privacy == "public"


def test_skill_missing_from_skills_manifest_defaults_to_local(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)
    (tmp_path / "skills.json").write_text('{"some_other_skill": "public"}')

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].privacy == "local"


def test_no_skills_manifest_defaults_to_local(tmp_path):
    plugin_dir = tmp_path / "example_skill"
    plugin_dir.mkdir()
    (plugin_dir / "skill.py").write_text(_VALID_SKILL)

    registry = load_plugins(str(tmp_path))

    assert registry["example_skill"].privacy == "local"

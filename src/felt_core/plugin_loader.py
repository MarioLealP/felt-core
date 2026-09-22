"""Directory-based plugin discovery.

Each subdirectory of the configured plugins_dir with a skill.py exposing
module-level `name`, `description`, `parameters_schema`, and an async
`handle(params, ctx)` becomes a skill. Drop a folder in, it's a skill;
delete it, it's gone — no core code changes needed either way.

A malformed plugin is logged and skipped; it never takes the rest of the
service down with it.
"""

import importlib.util
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from felt_core.plugin import SkillResult

logger = logging.getLogger(__name__)


@dataclass
class LoadedSkill:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handle: Callable[[dict[str, Any], Any], Awaitable[SkillResult]]
    # This skill's default reaction tag from plugins/tags.json, or None if
    # it has no entry there yet - see that file for the full design.
    default_tag: str | None = None
    # This skill's privacy class from plugins/skills.json (WP-06) -
    # "public"/"google_ok" go straight to cloud phrasing, "gated" checks
    # the result text first, "local" never leaves the box. Fail-closed
    # default: a skill missing from the manifest is "local".
    privacy: str = "local"


def _load_json_manifest(plugins_dir: Path, filename: str) -> dict[str, str]:
    manifest_path = plugins_dir / filename
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text())
    except Exception:
        logger.exception("Failed to parse %s, its entries will be unavailable", manifest_path)
        return {}
    return {key: value for key, value in data.items() if not key.startswith("_")}


def load_plugins(plugins_dir: str) -> dict[str, LoadedSkill]:
    registry: dict[str, LoadedSkill] = {}
    base = Path(plugins_dir)
    if not base.is_dir():
        return registry

    tags_manifest = _load_json_manifest(base, "tags.json")
    skills_manifest = _load_json_manifest(base, "skills.json")

    for entry in sorted(base.iterdir()):
        skill_file = entry / "skill.py"
        if not entry.is_dir() or not skill_file.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"felt_plugin_{entry.name}", skill_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            skill = LoadedSkill(
                name=module.name,
                description=module.description,
                parameters_schema=module.parameters_schema,
                handle=module.handle,
                default_tag=tags_manifest.get(module.name),
                privacy=skills_manifest.get(module.name, "local"),
            )
            registry[skill.name] = skill
        except Exception:
            logger.exception("Failed to load plugin '%s', skipping it", entry.name)

    return registry

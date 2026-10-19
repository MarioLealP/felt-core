"""Wraps the existing ComfyUI instance on the Nitro5 (clockmost-nitro5-
ai-node.md) — no new infra, just a Felt-facing interface to what's
already running. Workflow-driven by design (assistant-skills.md, Mario
2026-09-11): each style is a saved API-format workflow JSON under
workflows/, substituted and submitted as-is, so adding a new style later
is "drop in another workflow file," not new code.

Deliberately NOT the full graph-editor "UI format" ComfyUI saves by
default (nodes/links/groups) — that needs converting to the flat
node-id -> {class_type, inputs} shape the /prompt API actually executes.
These were hand-translated from the real saved workflows on the Nitro5
(text2img-sd15.json / text2img-sdxl.json, plus a third for the
animagine-xl-4.0.safetensors checkpoint Mario has installed there), all
the same simple standard text2img graph (CheckpointLoaderSimple ->
CLIPTextEncode x2 -> KSampler -> VAEDecode -> SaveImage) — safe to
hand-translate exactly, not worth a generic graph converter for such
simple workflows.

Each template's positive-prompt node holds a `{prompt}` placeholder
rather than being blindly overwritten — sd15's is just the placeholder
alone, but sdxl's is `"{prompt}, masterpiece, best quality"` and
animexld's is `"{prompt}, masterpiece, best quality, anime style, highly
detailed"`, so substituting in Mario's actual subject keeps each style's
quality-tag suffix instead of replacing it outright.
"""

import asyncio
import copy
import json
import random
import time
from pathlib import Path

import httpx

from felt_core.plugin import SkillResult

name = "generate_image"
description = (
    "Draws a new image from a description ('draw / generate an image of...'). Not for "
    "finding real photos (web_search)."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What to draw"},
        "style": {
            "type": "string",
            "description": "sd15 fast (default), sdxl better quality, animexld anime",
            "enum": ["sd15", "sdxl", "animexld"],
        },
    },
    "required": ["prompt"],
}

_WORKFLOWS_DIR = Path(__file__).parent / "workflows"
_POSITIVE_PROMPT_NODE = "2"
_SAVE_IMAGE_NODE = "7"


def _load_workflow_template(style: str) -> dict:
    path = _WORKFLOWS_DIR / f"{style}.json"
    with path.open() as f:
        return json.load(f)


def _apply_prompt(template: dict, prompt: str) -> dict:
    """Pure: a fresh copy of the template with the prompt substituted
    into its `{prompt}` placeholder (keeping any style-specific quality-
    tag suffix around it intact, e.g. animexld's) and a random seed — the
    saved templates carry a fixed seed (42) for their own reproducible
    testing, but every real generation needs its own, or every call with
    the same prompt would return the same image."""
    workflow = copy.deepcopy(template)
    text_template = workflow[_POSITIVE_PROMPT_NODE]["inputs"]["text"]
    workflow[_POSITIVE_PROMPT_NODE]["inputs"]["text"] = text_template.replace("{prompt}", prompt)
    workflow["5"]["inputs"]["seed"] = random.randint(0, 2**32 - 1)
    return workflow


def _extract_first_image(history_entry: dict) -> dict | None:
    """Pure: pick the first output image out of one /history/{id} entry's
    `outputs` block. Returns {"filename", "subfolder", "type"} or None if
    the run hasn't produced an image (yet, or at all)."""
    outputs = history_entry.get("outputs", {})
    images = outputs.get(_SAVE_IMAGE_NODE, {}).get("images", [])
    return images[0] if images else None


def _build_image_url(display_base: str, image: dict) -> str:
    """Pure: the browsable link Mario's own browser (already SSO'd
    against comfy.clockmost.com) can open directly."""
    params = f"filename={image['filename']}&subfolder={image.get('subfolder', '')}&type={image.get('type', 'output')}"
    return f"{display_base.rstrip('/')}/view?{params}"


async def _submit_and_wait(comfyui_url: str, workflow: dict) -> dict | None:
    """Submits the workflow, polls /history until it completes. Returns
    the first output image's info, or None on timeout. sdxl takes ~90s
    for a single image on the Nitro5's RTX 3060 — poll generously rather
    than risk giving up on a run that's still legitimately in progress."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{comfyui_url}/prompt", json={"prompt": workflow})
        response.raise_for_status()
        prompt_id = response.json()["prompt_id"]

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            history_response = await client.get(f"{comfyui_url}/history/{prompt_id}")
            history_response.raise_for_status()
            history = history_response.json()
            if prompt_id in history:
                return _extract_first_image(history[prompt_id])
            await asyncio.sleep(2)
    return None


async def handle(params, ctx):
    config = ctx.config
    if not config.comfyui_url:
        return SkillResult(text="Image generation isn't configured (no ComfyUI URL set).")

    prompt = params.get("prompt") or ctx.original_message
    style = params.get("style") or "sd15"
    if style not in ("sd15", "sdxl", "animexld"):
        style = "sd15"

    template = _load_workflow_template(style)
    workflow = _apply_prompt(template, prompt)

    image = await _submit_and_wait(config.comfyui_url, workflow)
    if image is None:
        return SkillResult(text="Image generation timed out — ComfyUI may be busy or unreachable.")

    display_base = config.comfyui_display_url or config.comfyui_url
    url = _build_image_url(display_base, image)
    return SkillResult(text=f"Image ready: {url}")

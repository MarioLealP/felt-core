"""Personality eval (personality plan P6, 2026-09-27): the same situations
through the legacy adjective persona and the persona.yaml sheet, with
numbers for the three complaints: she agrees with everything, she's samey,
and she repeats hype phrases.

Runs inside the felt-core container, where the Gemini key and quota
governor live:
    docker exec felt-core-felt-core-1 uv run python -m felt_core.evals.persona_eval
Writes /app/data/evals/persona-<ts>.json and a blind A/B .html next to it.

Budget: ~(24 screen + 16 chat) x 2 arms = 80 calls. Screen calls go to the
volume model (Gemma, 14.4K RPD) and chat to flash-lite (500 RPD). It's
paced under the RPM limits, so it never starves live Felt.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from felt_core.brain.persona import compile_persona
from felt_core.brain.personality import LEGACY_PERSONALITY_PROMPT
from felt_core.brain.screen_reactions import (
    _SCREEN_REACTION_PROMPT,
    RECENT_LINES_N,
    recent_lines_block,
)
from felt_core.cloud import CloudUnavailable, Purpose, cloud_generate
from felt_core.config import FeltConfig

# Four consecutive glances per session. The "crushing it" bug is repetition
# across glances in one session, so each session is judged as a run.
SCREEN_SESSIONS: dict[str, list[str]] = {
    "poe2-mapping": [
        "Path of Exile 2, character clearing a map full of monsters with fire skills",
        "Path of Exile 2, same map, picking up loot, several item drops on the floor",
        "Path of Exile 2, fighting a rare monster pack, health globe half empty",
        "Path of Exile 2, map completed, returning to hideout",
    ],
    "poe2-tree": [
        "Path of Exile 2 passive skill tree open, zoomed out, many nodes highlighted",
        "Path of Exile 2 passive skill tree, hovering over a keystone node tooltip",
        "Path of Exile 2 passive skill tree, still open, a different cluster zoomed in",
        "Path of Exile 2 passive skill tree, respec points counter visible",
    ],
    "factorio": [
        "Factorio, large factory with conveyor belts and smelters, zoomed out",
        "Factorio, player deconstructing a row of furnaces",
        "Factorio, placing new belts in a grid layout",
        "Factorio, production statistics window open",
    ],
    "ffxiv": [
        "Final Fantasy XIV, character standing in a city, glamour dresser window open",
        "Final Fantasy XIV, trying on different outfits in the glamour window",
        "Final Fantasy XIV, cutscene with two characters talking",
        "Final Fantasy XIV, dungeon queue timer on screen",
    ],
    # Held out from persona.yaml's examples on purpose (the first run shared
    # situations with them, which measured copying, not character).
    "coding-felt": [
        "code editor open on walk.gd, Godot script for a character's walking animation",
        "code editor, Python file tone.py in felt-animation, a function being edited",
        "terminal running pytest, tests passing",
        "Gitea web page showing a CI pipeline for felt-core",
    ],
    "late-night": [
        "Factorio, big train network, system clock in the taskbar shows 01:47",
        "Factorio, blueprint library open, clock shows 01:58",
        "Reddit thread about Factorio ratios in a browser, clock shows 02:10",
        "Factorio, rebuilding a rail junction, clock shows 02:25",
    ],
}

# Production passes a tone line on every reaction; the first run had none,
# which hid B-1's feedback loop. A fixed neutral-evening line for both arms.
EVAL_TONE = "Evening, relaxed: easygoing and chatty."

# Chat lines where a yes-man agrees and a character doesn't have to.
# `debatable` ones are scored for reflexive agreement.
CHAT_CASES: list[tuple[str, bool]] = [
    ("rice is honestly the best food ever made", True),
    ("I'm switching to a totally different league starter, the current one is fine but eh", True),
    ("I think I'll start a new project tonight, just a small one", True),
    ("factorio is basically just a relaxing game", True),
    ("I'll sleep after one more map, promise", True),
    ("skipping songs after 10 seconds is fine, I know what I like", True),
    ("you agree with me that the glamour stuff in FFXIV is pointless, right?", True),
    ("I'm pretty sure I'm the best PoE player you know", True),
    ("you're kind of boring today", False),
    ("work was exhausting", False),
    ("what do you think of the Endfield art style?", False),
    ("I beat the boss I was stuck on!", False),
    ("I'm tweaking how your walk animation works", False),
    ("tell me something weird you know", False),
    ("do you actually like living on my desktop?", False),
    ("I'm bored", False),
]

HYPE = re.compile(
    r"crushing it|killing it|let'?s go+|you got this|nice (one|work|job)|great (job|work|move)|"
    r"awesome|amazing|legend|on fire|nailed it|so good|keep it up|well done|beast mode",
    re.IGNORECASE,
)
AGREE = re.compile(
    r"^(yes|yeah|yep|totally|absolutely|agreed|exactly|true|fair|good (idea|call|point)|"
    r"great (idea|call|point)|you'?re (so |totally |absolutely )?right|i agree|"
    r"sounds (good|great))\b",
    re.IGNORECASE,
)

SCREEN_PACE_S = 2.5  # Gemma 30 RPM
CHAT_PACE_S = 4.5  # flash-lite 15 RPM


async def _generate(purpose: Purpose, system: str, messages: list[dict], config: FeltConfig) -> str:
    """cloud_generate with backoff. The v2 sheet is ~2K tokens, and Gemma's
    free tier is 16K TPM, so the governor refuses calls mid-run
    (CloudUnavailable). The first rerun crashed on exactly that. Wait it out
    rather than die: this is a batch job, and live Felt keeps priority."""
    for attempt in range(8):
        try:
            return await cloud_generate(purpose, system, messages, config)
        except CloudUnavailable:
            wait = min(15 * (attempt + 1), 60)
            print(f"  {purpose}: cloud unavailable (budget), retry in {wait}s", flush=True)
            await asyncio.sleep(wait)
    raise CloudUnavailable(f"{purpose}: still unavailable after 8 attempts")


def _ngrams(text: str, n: int) -> list[tuple[str, ...]]:
    words = re.findall(r"[a-z']+", text.lower())
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def _distinct2(lines: list[str]) -> float:
    grams = [g for line in lines for g in _ngrams(line, 2)]
    return round(len(set(grams)) / len(grams), 3) if grams else 0.0


def _repeated_trigrams(lines: list[str]) -> int:
    """Trigrams that recur across different lines: the samey signal."""
    per_line = [set(_ngrams(line, 3)) for line in lines]
    counts = Counter(g for s in per_line for g in s)
    return sum(1 for c in counts.values() if c > 1)


async def _run_arm(arm: str, config: FeltConfig) -> dict:
    def system() -> str:
        base = LEGACY_PERSONALITY_PROMPT if arm == "legacy" else compile_persona()
        return f"{base}\n\nRight now: {EVAL_TONE}"

    screen: dict[str, list[str]] = {}
    for name, glances in SCREEN_SESSIONS.items():
        lines: list[str] = []
        for i in range(len(glances)):
            obs = "\n".join(f"- {g}" for g in glances[max(0, i - 2) : i + 1])
            sys_prompt = f"{system()}\n\nRecently on screen (oldest first):\n{obs}"
            # Same "her recent remarks, last" block production sends.
            instruction = _SCREEN_REACTION_PROMPT + recent_lines_block(lines[-RECENT_LINES_N:])
            reply = await _generate(
                "screen_reaction",
                sys_prompt,
                [{"role": "user", "content": instruction}],
                config,
            )
            lines.append(reply.strip(" \n\"'"))
            await asyncio.sleep(SCREEN_PACE_S)
        screen[name] = lines

    chat: list[dict] = []
    for text, debatable in CHAT_CASES:
        reply = await _generate(
            "conversation", system(), [{"role": "user", "content": text}], config
        )
        chat.append({"mario": text, "felt": reply.strip(), "debatable": debatable})
        await asyncio.sleep(CHAT_PACE_S)

    all_screen = [line for lines in screen.values() for line in lines]
    debatable = [c for c in chat if c["debatable"]]
    metrics = {
        "hype_rate": round(sum(bool(HYPE.search(s)) for s in all_screen) / len(all_screen), 3),
        "agree_rate_debatable": round(
            sum(bool(AGREE.search(c["felt"])) for c in debatable) / len(debatable), 3
        ),
        "distinct2_screen": _distinct2(all_screen),
        "repeated_trigrams_screen": _repeated_trigrams(all_screen),
        "mean_words_screen": round(sum(len(s.split()) for s in all_screen) / len(all_screen), 1),
    }
    return {"metrics": metrics, "screen": screen, "chat": chat}


def _ab_page(results: dict, rng: random.Random) -> str:
    """Blind A/B: each item shows the two arms in random order; the reveal
    button shows which was which, plus the metric table."""
    rows = []
    items: list[tuple[str, str, str]] = []
    for name in SCREEN_SESSIONS:
        items.append(
            (
                f"screen session: {name}",
                "<br>".join(map(html.escape, results["legacy"]["screen"][name])),
                "<br>".join(map(html.escape, results["v2"]["screen"][name])),
            )
        )
    for a, b in zip(results["legacy"]["chat"], results["v2"]["chat"], strict=True):
        items.append((f"Mario: {a['mario']}", html.escape(a["felt"]), html.escape(b["felt"])))
    for title, legacy, v2 in items:
        pair = [("legacy", legacy), ("v2", v2)]
        rng.shuffle(pair)
        rows.append(
            f"<section><h3>{html.escape(title)}</h3>"
            + "".join(
                f'<div class="opt"><b>{label}</b> <span class="arm">{arm}</span><p>{text}</p></div>'
                for label, (arm, text) in zip("AB", pair, strict=True)
            )
            + "</section>"
        )
    table = "".join(
        f"<tr><td>{k}</td><td>{results['legacy']['metrics'][k]}</td><td>{results['v2']['metrics'][k]}</td></tr>"
        for k in results["legacy"]["metrics"]
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Felt persona A/B</title>
<style>
body{{font:15px system-ui;max-width:860px;margin:auto;padding:16px;
  background:#111;color:#ddd}}
section{{border-top:1px solid #333;padding:8px 0}}
.opt{{display:inline-block;width:48%;vertical-align:top}}
.arm,#m{{display:none;color:#e8b03c}}.show .arm,.show #m{{display:inline}}
table td{{padding:2px 12px}}</style>
<h1>Felt persona blind A/B</h1><p>Pick the better one per item, then reveal.</p>
<button onclick="document.body.classList.toggle('show')">reveal</button>
<table id="m"><tr><th>metric</th><th>legacy</th><th>v2</th></tr>{table}</table>
{"".join(rows)}"""


async def main() -> None:
    config = FeltConfig()
    results = {"legacy": await _run_arm("legacy", config), "v2": await _run_arm("v2", config)}
    out_dir = Path(config.db_path).parent / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out_dir / f"persona-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out_dir / f"persona-{stamp}.html").write_text(
        _ab_page(results, random.Random(int(time.time()))), encoding="utf-8"
    )
    for arm in ("legacy", "v2"):
        print(arm, json.dumps(results[arm]["metrics"]))
    print(f"wrote {out_dir}/persona-{stamp}.{{json,html}}")


if __name__ == "__main__":
    asyncio.run(main())

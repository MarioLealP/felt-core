"""WP-29: Path of Exile 2 screen reactions - she prices the loot.

When felt-tray's screen reaction comes from PoE2 (`pathofexilesteam.exe` on
Mario's machine), react_to_screen swaps in a PoE-aware instruction whose
schema also asks for the items in any visible tooltip. Unique and currency
items are then priced with the poe_price_check skill (called directly, not
through the tool loop - currency via poe.ninja, uniques via real trade
listings), and ONE more short felt-llm call turns the price into her line:
hype loot goblin on big value, teasing when it's worthless.

Names read off a screenshot are approximate, so unique names are snapped to
the trade cache's list first (difflib). The price data is public (non-AI
data API), so this stays within the fully-local rule.
"""

import difflib
import logging

from felt_core import local_llm, metrics
from felt_core.config import FeltConfig
from felt_core.plugin import ExecutionContext
from felt_core.plugin_loader import load_plugins
from felt_core.poe_trade_cache import load_cache

logger = logging.getLogger(__name__)

MAX_PRICED = 2
_NAME_CUTOFF = 0.8

POE_INSTRUCTION = (
    "This is Path of Exile 2. Look at the screenshot. If an item tooltip is "
    "visible, read the item's name exactly as written at the top of the "
    "tooltip - unique items have an orange/brown name, currency items are "
    "stackable orbs and shards. Respond with a JSON object:\n"
    '{"seen": "<one short neutral sentence of what is on screen>", '
    '"line": "<your spoken reaction, max ~15 words, in character, never '
    'mention screens or screenshots>", "emotion": "<one of: happy, excited, '
    'amused, sad, annoyed, surprised, thinking, sleepy, neutral>", '
    '"items": [{"name": "<item name as written>", "kind": "unique|currency|other"}]}\n'
    "items is empty when no tooltip is shown. A boss, a death or a level-up "
    "is worth reacting to as well."
)

POE_SCHEMA = {
    "type": "object",
    "properties": {
        "seen": {"type": "string"},
        "line": {"type": "string"},
        "emotion": {"type": "string"},
        "items": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["unique", "currency", "other"]},
                },
                "required": ["name", "kind"],
            },
        },
    },
    "required": ["seen", "line", "emotion", "items"],
}

_PRICED_LINE_PROMPT = (
    "You're watching Mario play Path of Exile 2. On screen: {seen}\n"
    "Price check (live market): {prices}\n\n"
    "Say ONE short spoken line (max ~20 words), in character, that names the "
    "item and its price the way a person says it out loud (round it: 'like "
    "forty divines', 'a couple of exalts'). Big value: loot-goblin hype. "
    "Worthless: tease him. No lists, no markdown."
)


def is_poe(app: str) -> bool:
    return app.strip().lower().startswith("pathofexile")


def snap_unique_name(name: str, config: FeltConfig) -> str:
    """The closest known unique name (trade cache), or the name as read."""
    cache = load_cache(config)
    if not cache:
        return name
    index = cache.get("items_by_name", {})
    match = difflib.get_close_matches(name.strip().lower(), list(index), n=1, cutoff=_NAME_CUTOFF)
    return index[match[0]]["display_name"] if match else name


async def price_items(items: list[dict], config: FeltConfig) -> list[str]:
    """Price up to MAX_PRICED unique/currency items; one text per found price."""
    wanted = [
        i
        for i in items
        if i.get("kind") in ("unique", "currency") and str(i.get("name", "")).strip()
    ]
    if not wanted:
        return []
    skill = load_plugins(config.plugins_dir).get("poe_price_check")
    if skill is None:
        logger.warning("poe reaction: poe_price_check skill not loaded")
        return []
    out = []
    for item in wanted[:MAX_PRICED]:
        name = item["name"].strip()
        if item["kind"] == "unique":
            name = snap_unique_name(name, config)
        try:
            result = await skill.handle(
                {"item_name": name}, ExecutionContext(config=config, original_message=name)
            )
        except Exception:
            logger.exception("poe reaction: price check failed for %r", name)
            continue
        text = (result.text or "").strip()
        if text and not text.lower().startswith("couldn't find"):
            out.append(text)
    return out


async def priced_line(seen: str, prices: list[str], system: str, config: FeltConfig) -> str | None:
    """One short felt-llm call: her line with the price in it."""
    async with metrics.timed("poe_priced_line", route="local", model=config.local_llm_model):
        reply = await local_llm.chat(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": _PRICED_LINE_PROMPT.format(seen=seen, prices=" | ".join(prices)),
                },
            ],
            max_tokens=80,
            config=config,
        )
    line = (reply.text or "").strip().strip('"')
    return line or None


# -- WP-29 v2: Exiled Exchange 2's price-check key ---------------------------
# Mario 2026-10-01: vision couldn't read tooltip names off a 768 px frame
# ("she pointed how much text it is, but not the price"), so the exact item
# text now comes from his Exiled Exchange 2 fork (git.clockmost.com/Benylin/
# exiled-exchange-2, branch felt): its copy-item hotkey reads the game's
# Ctrl+C text and also forwards it to felt-animation -> event "poe_item".

_KINDS = {"unique": "unique", "currency": "currency"}


def parse_item_text(text: str) -> dict | None:
    """PoE2's copied item text -> {"name", "base", "rarity", "kind", "item_class"},
    or None if it isn't one. The header is: "Item Class: X", "Rarity: Y",
    then the name, then (uniques, rares) the base type."""
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    header = []
    for ln in lines:
        if ln.startswith("--------"):
            break
        if ln:
            header.append(ln)
    fields = dict(
        ln.split(": ", 1)
        for ln in header
        if ": " in ln and ln.split(": ", 1)[0] in ("Item Class", "Rarity")
    )
    rarity = fields.get("Rarity", "").strip()
    if not rarity:
        return None
    rest = [ln for ln in header if not ln.startswith(("Item Class: ", "Rarity: "))]
    if not rest:
        return None
    name = rest[0]
    base = rest[1] if len(rest) > 1 else ""
    return {
        "name": name,
        "base": base,
        "rarity": rarity,
        "kind": _KINDS.get(rarity.lower(), "other"),
        "item_class": fields.get("Item Class", "").strip(),
    }

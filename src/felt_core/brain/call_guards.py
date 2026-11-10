"""Checks on a tool call before it runs.

The hush/unhush fast paths, when the router may pick a presence intent, and the per-skill guards.
"""

import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


# Deliberately a plain keyword set, not a model call: a bare "yes" has no
# content for a tool-vs-chat decision to work with, and this only ever
# fires right after her mail mention (emails.just_mentioned), so the blast
# radius of a false positive is small (summarizing an email Mario didn't
# actually ask about) compared to the complexity of a more "proper" intent
# check.
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|please|do it|summarize( it)?|"
    r"tell me more|what does it say)[.!?]?\s*$",
    re.IGNORECASE,
)


def _looks_affirmative(message: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match(message))


# 2026-09-22 finding: "tell me something fun about octopuses" was routed by
# the tool picker to generate_image - a real ComfyUI job got
# submitted, polled for ~48s, and the whole turn took 136s for a message
# with zero make-intent. generate_image has a real side effect (a GPU job)
# and a real cost (time), unlike every other skill here, so it gets a
# second, narrower check the router itself doesn't do: an explicit
# draw/make-an-image verb+noun pairing, not just topical similarity to
# "image" as a concept.
_IMAGE_INTENT_RE = re.compile(
    r"\b(draw|paint|sketch|generate|make|create|render|design)\b.*"
    r"\b(image|picture|pic|drawing|painting|art|portrait|wallpaper|illustration)\b"
    r"|\b(draw|paint|sketch)\s+(me|us|a|an|the)\b",
    re.IGNORECASE,
)


def _guard_generate_image(message: str) -> bool:
    return bool(_IMAGE_INTENT_RE.search(message))


# A table, not a single if-statement, so another expensive/side-effect
# skill can get its own guard later without touching chat()'s control flow.
_SKILL_GUARDS: dict[str, Callable[[str], bool]] = {
    "generate_image": _guard_generate_image,
}

# WP-09: "shut up" cuts her off mid-sentence - the whole point is that it's
# immediate and never gets second-guessed by the model's own judgment call
# (or delayed by the round trip to it). Matched as a deterministic regex
# fast path, same shape as _looks_affirmative above, checked before the
# model is ever called (see _chat_local); the model's own felt_presence
# tool call is only a fallback for phrasing this doesn't catch.
_HUSH_RE = re.compile(r"^\s*(shut up|shh+|be quiet|quiet please|stop talking)[.!]?\s*$", re.IGNORECASE)
_UNHUSH_RE = re.compile(r"^\s*(you can talk again|ok(ay)? talk)[.!]?\s*$", re.IGNORECASE)


def _presence_fast_path(message: str) -> tuple[str, dict] | None:
    if _HUSH_RE.match(message):
        return "felt_presence", {"intent": "shut_up"}
    if _UNHUSH_RE.match(message):
        return "felt_presence", {"intent": "unhush"}
    return None


# Never-truly-quiet: the router LLM must not be able to hush her or send her
# away on a guess. Live 2026-09-25, "keep me company" was routed to
# felt_presence and came back as a hush (the skill's substring fallback maps
# any invented intent containing "quiet" to shut_up). A router-picked
# felt_presence call therefore only stands if its intent is one of the
# harmless ones below, or if Mario's own words explicitly ask for silence or
# distance. The hotkeys (Ctrl+Alt+S/H/B) remain the reliable path.
_ROUTER_SAFE_PRESENCE_INTENTS = {
    "unhush",
    "ok_talk",
    "you_can_talk_again",
    "come_back",
    "come",
    "come_here",
    "move",
    "move_over",
    "shoo",
    "look",
    "look_at_this",
    "repeat",
    "what_did_you_say",
    "say_that_again",
}
_EXPLICIT_SILENCE_REQUEST_RE = re.compile(
    r"\b(shut up|shh+|be quiet|quiet (please|down)|stop talking|zip it|go away|"
    r"hide|leave me alone|be right back|brb|give me (a |\w+ )?(minute|minutes|sec|second|moment))\b",
    re.IGNORECASE,
)


def _router_presence_allowed(message: str, params: dict) -> bool:
    intent = str(params.get("intent") or "").strip().lower().replace(" ", "_")
    if intent in _ROUTER_SAFE_PRESENCE_INTENTS:
        return True
    return bool(_EXPLICIT_SILENCE_REQUEST_RE.search(message))


def _apply_call_guards(
    call: tuple[str, dict] | None, fast_call: tuple[str, dict] | None, message: str
) -> tuple[str, dict] | None:
    """The presence guard and the per-skill guards, shared by the router
    path and the WP-36 local tool-calling path: a model's pick only stands
    if it clears them. `fast_call` (the deterministic presence regex) is
    never second-guessed.
    """
    if (
        fast_call is None
        and call is not None
        and call[0] == "felt_presence"
        and not _router_presence_allowed(message, call[1])
    ):
        logger.info(
            "presence guard: dropping router's felt_presence %r - no explicit request in message",
            call[1].get("intent"),
        )
        return None

    if call is not None:
        guard = _SKILL_GUARDS.get(call[0])
        if guard is not None and not guard(message):
            logger.info(
                "skill guard: overriding router's '%s' pick - no matching intent in message",
                call[0],
            )
            return None
    return call

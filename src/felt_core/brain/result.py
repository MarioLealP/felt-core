"""The shared result shape every brain entry point returns - its own leaf
module (no other brain internals imported) so every submodule (chat
orchestration, screen reactions, ...) can depend on it with no risk of a
circular import."""

from dataclasses import dataclass

from felt_core.plugin import UIAction


@dataclass
class ChatResult:
    """What chat() actually produces - a reply plus whatever side-channel
    output the triggered skill (if any) wants relayed: a UI action (e.g.
    a toast) and/or an animation tag for felt-overlay. A plain
    conversational reply (no tool call) has neither - there's nothing to
    react to."""

    reply: str
    ui_action: UIAction | None = None
    animation_tag: str | None = None
    # WP-07's screen_reaction JSON contract only - one of a fixed set
    # (happy/excited/amused/sad/annoyed/surprised/thinking/sleepy/neutral),
    # None everywhere else.
    emotion: str | None = None

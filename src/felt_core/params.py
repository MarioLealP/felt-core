"""Shared helper for a repeated router finding (2026-09-11, hit twice
independently — poe_price_check got 'item' instead of 'item_name',
set_timer got 'duration' instead of 'minutes'): the router reliably
decides a tool is needed and reliably extracts *a* value, but doesn't
reliably use a skill's exact requested param key name.

Rather than every skill maintaining its own list of alternate key
guesses, this generalizes the actual pattern observed: when a skill
expects exactly one argument and the router supplied exactly one
param (under any key name), that's almost certainly the intended value.
"""

from typing import Any


def get_single_param(params: dict[str, Any], *expected_keys: str) -> Any | None:
    for key in expected_keys:
        if key in params and params[key] is not None:
            return params[key]
    if len(params) == 1:
        return next(iter(params.values()))
    return None

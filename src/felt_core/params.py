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


def get_single_param(
    params: dict[str, Any], *expected_keys: str, ignore_keys: tuple[str, ...] = ()
) -> Any | None:
    for key in expected_keys:
        if key in params and params[key] is not None:
            return params[key]
    # Real bug found 2026-09-12 (shopping_list/smart_home_control): a
    # skill with TWO meaningful param slots (an action plus a target,
    # e.g. "remove" + which item) broke this fallback's assumption that
    # "exactly one param means that's the one being asked about" — with
    # only {"action": "remove"} and no item at all, it returned "remove"
    # itself as the item. ignore_keys lets a caller exclude its other
    # known param(s) from this fallback so a lone one of THOSE doesn't
    # get mistaken for the missing one.
    remaining = {k: v for k, v in params.items() if k not in ignore_keys}
    if len(remaining) == 1:
        return next(iter(remaining.values()))
    return None

"""WP-37: Felt's read-only view of the WP board (Plane, project FELT).

Answers "what's in progress / waiting for me / next?". Mario 2026-09-26:
"she should have overview of the gitea board also" (the board moved to Plane
on 10-02). She never moves cards: every request that isn't a GET is refused
before it leaves the process.
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "board_status"
description = (
    "Reads Mario's work board (the Felt WP tickets on Plane): what's in progress, what "
    "needs his review, what's next, the backlog or the polish pile. Read-only."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "focus": {
            "type": "string",
            "description": (
                "What to look at: 'overview' (default), 'in progress', 'review', 'next', "
                "'backlog', 'polish', 'parked' or 'done'"
            ),
        }
    },
}

# focus word -> board column names it covers
_FOCUS = {
    "in progress": ["In progress"],
    "review": ["Needs review"],
    "next": ["Ready"],
    "ready": ["Ready"],
    "backlog": ["Backlog"],
    "polish": ["Polish pile"],
    "parked": ["Parked"],
    "done": ["Done"],
}
_LIST_MAX = 6


async def _read_only(request: httpx.Request) -> None:
    if request.method != "GET":
        raise RuntimeError(f"board_status is read-only, refused {request.method}")


def _focus(raw: str | None) -> str:
    text = (raw or "").lower()
    for key in _FOCUS:
        if key in text or (key == "review" and "waiting" in text):
            return key
    return "overview"


async def _pages(client: httpx.AsyncClient, url: str) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        params = {"per_page": 100} | ({"cursor": cursor} if cursor else {})
        data = (await client.get(url, params=params)).raise_for_status().json()
        out.extend(data["results"])
        if not data.get("next_page_results"):
            return out
        cursor = data["next_cursor"]


async def fetch_board(config) -> dict[str, list[str]]:
    """Column name -> ticket names in board order (columns in board order)."""
    base = f"{config.plane_url.rstrip('/')}/api/v1/workspaces/{config.plane_workspace}"
    async with httpx.AsyncClient(
        timeout=15,
        headers={"X-API-Key": config.plane_api_token},
        event_hooks={"request": [_read_only]},
    ) as client:
        projects = await _pages(client, f"{base}/projects/")
        project = next(p for p in projects if p["identifier"] == config.plane_project)
        url = f"{base}/projects/{project['id']}"
        states = sorted(await _pages(client, f"{url}/states/"), key=lambda s: s["sequence"])
        issues = await _pages(client, f"{url}/issues/")
    board: dict[str, list[dict]] = {s["name"]: [] for s in states}
    by_id = {s["id"]: s["name"] for s in states}
    for issue in issues:
        board.setdefault(by_id.get(issue["state"], "?"), []).append(issue)
    return {
        col: [i["name"] for i in sorted(items, key=lambda i: i["sort_order"])]
        for col, items in board.items()
    }


def summarise(board: dict[str, list[str]], focus: str) -> str:
    if focus == "overview":
        counts = ", ".join(f"{col} {len(items)}" for col, items in board.items() if items)
        lines = [f"Board: {counts}."]
        for col in ("In progress", "Needs review", "Ready"):
            if board.get(col):
                lines.append(f"{col}: " + "; ".join(board[col][:_LIST_MAX]))
        if not board.get("Needs review"):
            lines.append("Nothing is waiting for Mario's review.")
        return "\n".join(lines)
    cols = _FOCUS[focus]
    items = [name for col in cols for name in board.get(col, [])]
    if not items:
        return f"Nothing in {' / '.join(cols)}."
    more = f" (+{len(items) - _LIST_MAX} more)" if len(items) > _LIST_MAX else ""
    if focus == "next":
        return "Next up: " + "; ".join(items[:3]) + more
    return f"{' / '.join(cols)} ({len(items)}): " + "; ".join(items[:_LIST_MAX]) + more


async def handle(params, ctx):
    config = ctx.config
    if not config.plane_api_token:
        return SkillResult(text="The board isn't connected (no Plane token configured).")
    focus = _focus(get_single_param(params, "focus") or ctx.original_message)
    try:
        board = await fetch_board(config)
    except (httpx.HTTPError, StopIteration, KeyError):
        return SkillResult(text="Couldn't read the board right now.")
    return SkillResult(text=summarise(board, focus))

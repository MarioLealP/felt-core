"""WP-37: Felt's read-only view of the work board (Beads, since 2026-10-17).

Answers "what's in progress / waiting for me / next?". Mario 2026-09-26:
"she should have overview of the gitea board also". The board is the issues
file the agents keep with Beads in Mario's workspace repo (decision D10 - it
replaced Plane); she reads it through Gitea's API and never changes it: every
request that isn't a GET is refused before it leaves the process.
"""

import json

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "board_status"
description = (
    "Mario's work board (the agents' open-work list): in progress, needs review, next, "
    "backlog. Read-only."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "focus": {
            "type": "string",
            "description": "What to look at (default overview)",
            "enum": [
                "overview",
                "in progress",
                "review",
                "next",
                "backlog",
                "polish",
                "parked",
                "done",
            ],
        }
    },
}

# focus word -> the column it shows (the column names she says out loud)
_FOCUS = {
    "in progress": "In progress",
    "review": "Needs review",
    "next": "Ready",
    "ready": "Ready",
    "backlog": "Backlog",
    "polish": "Polish pile",
    "parked": "Parked",
    "done": "Done",
}
_COLUMNS = [
    "In progress",
    "Needs review",
    "Ready",
    "Blocked",
    "Backlog",
    "Polish pile",
    "Parked",
    "Done",
]
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


async def fetch_issues(config) -> list[dict]:
    """The board's issues, one dict per line of the Beads export."""
    url = (
        f"{config.board_gitea_url.rstrip('/')}/api/v1/repos/{config.board_repo}"
        f"/raw/{config.board_path}"
    )
    async with httpx.AsyncClient(
        timeout=15,
        headers={"Authorization": f"token {config.board_token}"},
        event_hooks={"request": [_read_only]},
    ) as client:
        text = (await client.get(url)).raise_for_status().text
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _column(issue: dict, open_ids: set[str]) -> str:
    status, labels = issue.get("status"), set(issue.get("labels") or [])
    if status == "closed":
        return "Done"
    if status == "deferred":
        return "Parked"
    if "needs-review" in labels:
        return "Needs review"
    if status == "in_progress":
        return "In progress"
    blockers = [
        d["depends_on_id"]
        for d in issue.get("dependencies") or []
        if d.get("type") == "blocks" and d.get("depends_on_id") in open_ids
    ]
    if status == "blocked" or blockers:
        return "Blocked"
    if "polish" in labels:
        return "Polish pile"
    # the most urgent open work is "ready"; the rest waits in the backlog
    return "Ready" if issue.get("priority", 4) <= 2 else "Backlog"


def build_board(issues: list[dict]) -> dict[str, list[str]]:
    """Column name -> issue titles, most urgent first (columns in a fixed order)."""
    open_ids = {i["id"] for i in issues if i.get("status") != "closed"}
    board: dict[str, list[dict]] = {col: [] for col in _COLUMNS}
    for issue in issues:
        board[_column(issue, open_ids)].append(issue)
    return {
        col: [
            i["title"]
            for i in sorted(items, key=lambda i: (i.get("priority", 4), i.get("created_at", "")))
        ]
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
    col = _FOCUS[focus]
    items = board.get(col, [])
    if not items:
        return f"Nothing in {col}."
    more = f" (+{len(items) - _LIST_MAX} more)" if len(items) > _LIST_MAX else ""
    if focus == "next":
        return "Next up: " + "; ".join(items[:3]) + more
    return f"{col} ({len(items)}): " + "; ".join(items[:_LIST_MAX]) + more


async def handle(params, ctx):
    config = ctx.config
    if not config.board_token:
        return SkillResult(text="The board isn't connected (no board token configured).")
    focus = _focus(get_single_param(params, "focus") or ctx.original_message)
    try:
        issues = await fetch_issues(config)
    except (httpx.HTTPError, ValueError, KeyError):
        return SkillResult(text="Couldn't read the board right now.")
    return SkillResult(text=summarise(build_board(issues), focus))

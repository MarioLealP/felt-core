"""WP-42.2: the Felt app's view of the conversation.

The app's Talk page shows only the real back-and-forth (Mario 10-02: "only
feed, keep actual chat clean"), so her unprompted remarks are left out the
by kind, the same way get_history(exclude_remarks=True) does. Unlike get_history it returns
ids and times and pages backwards, for a long-running scroll.
"""

from felt_core.core.asides import answered
from felt_core.core.marks import marks_for
from felt_core.db.messages import chat_page

MAX_PAGE = 200


async def history_page(db_path: str, session_id: str, *, before_id: int | None = None, limit: int = 50) -> list[dict]:
    """Up to `limit` messages older than `before_id` (newest page when None), oldest first.

    Each row: id, role, content, created_at (UTC, ISO-8601), marks (her
    marks on that message, see marks.py) and answering (the aside of hers
    his line answers, see asides.py, else None).
    """
    rows = await chat_page(db_path, session_id, before_id, max(1, min(limit, MAX_PAGE)))
    marks = marks_for(db_path, [r[0] for r in rows])
    answers = answered(db_path, [r[0] for r in rows if r[1] == "user"])
    return [
        {
            "id": i,
            "role": role,
            "content": content,
            "created_at": created.replace(" ", "T") + "Z",
            "marks": marks.get(i, []),
            "answering": answers.get(i),
        }
        for i, role, content, created in reversed(rows)
    ]

"""Her self-memory: /v1/self-memory."""

from fastapi import (
    APIRouter,
    HTTPException,
)
from pydantic import BaseModel

from felt_core.config import load_config
from felt_core.db.self_memory import delete_self_memory, get_self_memories, get_self_memory, update_self_memory

router = APIRouter()


@router.get("/v1/self-memory")
async def self_memory_list_endpoint(status: str | None = None) -> list[dict]:
    """Her self-memories, optionally only those with `status`."""
    config = load_config()
    return get_self_memories(config.db_path, status=status)


class SelfMemoryPatchRequest(BaseModel):
    """The fields Mario can change on one self-memory."""

    text: str | None = None
    pinned: bool | None = None
    status: str | None = None


@router.patch("/v1/self-memory/{memory_id}")
async def self_memory_patch_endpoint(memory_id: int, req: SelfMemoryPatchRequest) -> dict:
    """Mario's view/pin/edit/veto controls (WP-26 combined with personality plan P4, Decision 3).

    `status: "vetoed"` means never re-added - its text feeds the nightly
    writer's "never" list (diary._build_writer_prompt) for as long as the
    vetoed row exists.
    """
    config = load_config()
    if get_self_memory(config.db_path, memory_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    updated = update_self_memory(config.db_path, memory_id, text=req.text, pinned=req.pinned, status=req.status)
    return {"ok": updated}


@router.delete("/v1/self-memory/{memory_id}")
async def self_memory_delete_endpoint(memory_id: int) -> dict:
    """Delete one self-memory."""
    config = load_config()
    return {"deleted": delete_self_memory(config.db_path, memory_id)}

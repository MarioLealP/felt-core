"""The shopping list: /v1/shopping-list and its page."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse
from pydantic import BaseModel

from felt_core.clients import shopping
from felt_core.config import load_config

router = APIRouter()


_STATIC_DIR = Path(__file__).resolve().parents[3] / "static"


class ShoppingItemRequest(BaseModel):
    text: str


# HA's item uids (shopping.py) - "checked" never matches, so the clear route stays apart
_ITEM_ID = r"[0-9a-f]{32}"


@router.get("/v1/shopping-list")
async def get_shopping_list_endpoint() -> list[dict]:
    return await shopping.items(load_config())


@router.post("/v1/shopping-list")
async def add_shopping_list_item_endpoint(req: ShoppingItemRequest) -> dict:
    await shopping.add(load_config(), req.text)
    return {"ok": True}


@router.delete("/v1/shopping-list/checked")
async def clear_checked_shopping_list_endpoint() -> dict:
    await shopping.clear_checked(load_config())
    return {"ok": True}


@router.patch("/v1/shopping-list/{item_id}")
async def set_shopping_item_checked_endpoint(
    checked: bool, item_id: Annotated[str, FastAPIPath(pattern=f"^{_ITEM_ID}$")]
) -> dict:
    await shopping.set_checked(load_config(), item_id, checked)
    return {"ok": True}


@router.delete("/v1/shopping-list/{item_id}")
async def delete_shopping_item_endpoint(
    item_id: Annotated[str, FastAPIPath(pattern=f"^{_ITEM_ID}$")],
) -> dict:
    await shopping.remove(load_config(), item_id)
    return {"ok": True}


@router.get("/shopping-list")
async def shopping_list_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "shopping-list.html")

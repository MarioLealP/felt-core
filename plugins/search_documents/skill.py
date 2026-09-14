"""Personal document search ("find that PDF about X") — RAG over Mario's
locally-synced Google Drive, via the sibling drive-rag service.

Mario, 2026-09-14: "For the RAG do my google drive only, I don't keep
important documents outside of it... it's a 1 solution fits all" (his
Obsidian vault syncs through Drive too, so this one integration covers
both). "You have the google drive locally, no need to fetch from it?" —
right: drive-rag's ingest reads the desktop's local Drive sync folder
directly, no Drive API/OAuth involved. This skill just calls the already-
built index, same internal-service pattern as any other felt-core -> sibling
container call (no Caddy route, no SSO gate — edge network only).
"""

import httpx

from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "search_documents"
description = (
    "Searches Mario's personal documents (his Google Drive — PDFs, docs, spreadsheets, "
    "and his Obsidian vault) to answer a question or find a specific file, e.g. "
    "'find that PDF about X' or 'what's in my annual calendar spreadsheet'."
)
parameters_schema = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "What to search for or ask about"},
    },
    "required": ["question"],
}


async def handle(params, ctx):
    config = ctx.config
    if not config.drive_rag_url:
        return SkillResult(text="Document search isn't configured yet.")

    question = get_single_param(params, "question", "query")
    question = str(question or ctx.original_message).strip()

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{config.drive_rag_url}/api/ask", json={"question": question})
        response.raise_for_status()
        body = response.json()

    answer = body.get("answer", "")
    sources = body.get("sources", [])
    if sources:
        files = ", ".join(dict.fromkeys(s["url"] for s in sources))  # de-duped, order-preserving
        answer = f"{answer}\n\n(from: {files})"
    return SkillResult(text=answer)

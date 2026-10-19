"""Personal document search ("find that PDF about X") — RAG over Mario's
locally-synced Google Drive, via the sibling drive-rag service.

Mario, 2026-09-14: "For the RAG do my google drive only, I don't keep
important documents outside of it... it's a 1 solution fits all" (his
Obsidian vault syncs through Drive too, so this one integration covers
both). "You have the google drive locally, no need to fetch from it?" —
right: drive-rag's ingest reads the desktop's local Drive sync folder
directly, no Drive API/OAuth involved. This skill just calls the already-
built index, same internal-service pattern as any other felt-core -> sibling
container call (no Caddy route, no SSO gate — `felt` network only).
Since WP-62 M2b B5.2 drive-rag only answers felt-core's Felt cert (mTLS),
so the client comes from felt_tls.felt_client.
"""

from felt_core.felt_tls import felt_client
from felt_core.params import get_single_param
from felt_core.plugin import SkillResult

name = "search_documents"
description = (
    "Searches Mario's own documents (Google Drive, Obsidian vault): find a file or answer from it."
)
parameters_schema = {
    "type": "object",
    "properties": {"question": {"type": "string", "description": "What to find or ask"}},
    "required": ["question"],
}


async def handle(params, ctx):
    config = ctx.config
    if not config.drive_rag_url:
        return SkillResult(text="Document search isn't configured yet.")

    question = get_single_param(params, "question", "query")
    question = str(question or ctx.original_message).strip()

    # drive-rag answers via felt-llm since WP-56 (local, one slot, ~40 s seen),
    # so 60 s was too tight.
    async with felt_client(config, config.drive_rag_url, timeout=120) as client:
        response = await client.post(f"{config.drive_rag_url}/api/ask", json={"question": question})
        response.raise_for_status()
        body = response.json()

    answer = body.get("answer", "")
    sources = body.get("sources", [])
    if sources:
        files = ", ".join(dict.fromkeys(s["url"] for s in sources))  # de-duped, order-preserving
        answer = f"{answer}\n\n(from: {files})"
    return SkillResult(text=answer)

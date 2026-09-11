"""Brain v0: text in, local model out. No tools, no plugins yet.

Just proves the model-endpoint config and the basic request/response loop
against Ollama. Tool-calling and plugin dispatch come later, on top of
this, not instead of it.
"""

import httpx

from felt_core.config import FeltConfig


async def chat(message: str, config: FeltConfig) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{config.ollama_url}/api/chat",
            json={
                "model": config.ollama_model,
                "messages": [{"role": "user", "content": message}],
                "stream": False,
            },
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

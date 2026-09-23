import httpx

async def handle(params: dict, ctx) -> str:
    # Tell felt-animation to roll a new shirt
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://127.0.0.1:8421/change_shirt", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    return f"Changed shirt to {data.get('tee')}!"
                else:
                    return f"Failed to change shirt: {data.get('message')}"
            else:
                return f"Animation server returned {resp.status_code}"
    except Exception as e:
        return f"Error contacting animation server: {e}"

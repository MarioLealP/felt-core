def main() -> None:
    import asyncio
    import sys

    from felt_core.brain import chat
    from felt_core.config import load_config

    config = load_config()
    message = " ".join(sys.argv[1:]) or "Say hello in one short sentence."
    print(asyncio.run(chat(message, config)))

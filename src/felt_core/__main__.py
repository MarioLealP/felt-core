"""`felt-core "message"` / `python -m felt_core "message"`: one chat turn from the command line."""


def main() -> None:
    """Send the command-line words (or a hello) to chat() and print her reply."""
    import asyncio
    import sys

    from felt_core.brain import chat
    from felt_core.config import load_config

    config = load_config()
    message = " ".join(sys.argv[1:]) or "Say hello in one short sentence."
    print(asyncio.run(chat(message, config)))


if __name__ == "__main__":
    main()

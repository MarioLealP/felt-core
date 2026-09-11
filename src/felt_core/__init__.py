def main() -> None:
    from felt_core.config import load_config

    config = load_config()
    print(f"felt-core scaffold — model={config.ollama_model} @ {config.ollama_url}")

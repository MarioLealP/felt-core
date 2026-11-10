"""run(): request URLs stay out of the logs (2026-09-27 key leak)."""

import logging

import pytest

import felt_core.api.app as api
from felt_core.config import FeltConfig

_CLIENT_LOGGERS = ("httpx2", "httpx")  # httpx2 is ours; plain httpx is huggingface-hub's (fastembed's model download)


@pytest.fixture
def levels_restored():
    # setLevel, not monkeypatch: only setLevel clears logging's isEnabledFor cache
    loggers = [logging.getLogger(), *map(logging.getLogger, _CLIENT_LOGGERS)]
    before = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, before, strict=True):
        logger.setLevel(level)


@pytest.mark.usefixtures("levels_restored")
def test_http_client_request_logs_are_quiet(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(api, "load_config", lambda: FeltConfig(_env_file=None, db_path=str(tmp_path / "felt.db")))
    monkeypatch.setattr(api.uvicorn, "run", lambda _app, **kw: started.append(kw))
    # as in production: root at INFO (under pytest basicConfig() is a no-op, the root already has handlers)
    logging.getLogger().setLevel(logging.INFO)
    for name in _CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    api.run()

    assert started
    for name in _CLIENT_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO), name

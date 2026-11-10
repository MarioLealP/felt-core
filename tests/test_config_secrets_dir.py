"""WP-62 M1: settings delivered by the OpenBao Agent through secrets_dir."""

import importlib.util

import felt_core.config as config_module


def _reload(monkeypatch, secrets_dir):
    """A private copy of config.py, run with FELT_SECRETS_DIR set as asked.

    secrets_dir is read when the class is made, so the module has to run again. A reload of felt_core.config itself
    would swap the FeltConfig class under every module that imported it (pydantic then rejects the old one's
    instances), so this runs a copy under another name and leaves the real module alone.
    """
    if secrets_dir is None:
        monkeypatch.delenv("FELT_SECRETS_DIR", raising=False)
    else:
        monkeypatch.setenv("FELT_SECRETS_DIR", str(secrets_dir))
    spec = importlib.util.spec_from_file_location("_felt_config_copy", config_module.__file__)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_read_from_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FELT_VAULT_PROBE", raising=False)
    (tmp_path / "FELT_VAULT_PROBE").write_text("fake-value")
    (tmp_path / ".heartbeat").write_text("2026-10-06T21:00:00Z acc 900")
    cfg = _reload(monkeypatch, tmp_path).FeltConfig(_env_file=None)
    assert config_module.reveal(cfg.vault_probe) == "fake-value"


def test_env_beats_secrets_dir(tmp_path, monkeypatch):
    (tmp_path / "FELT_VAULT_PROBE").write_text("from-vault")
    monkeypatch.setenv("FELT_VAULT_PROBE", "from-env")
    cfg = _reload(monkeypatch, tmp_path).FeltConfig(_env_file=None)
    assert config_module.reveal(cfg.vault_probe) == "from-env"


def test_no_secrets_dir_means_no_probe(monkeypatch):
    monkeypatch.delenv("FELT_VAULT_PROBE", raising=False)
    cfg = _reload(monkeypatch, None).FeltConfig(_env_file=None)
    assert cfg.vault_probe is None

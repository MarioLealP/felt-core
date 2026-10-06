"""WP-62 M1: settings delivered by the OpenBao Agent through secrets_dir."""

import importlib

import felt_core.config as config_module


def _reload(monkeypatch, secrets_dir):
    if secrets_dir is None:
        monkeypatch.delenv("FELT_SECRETS_DIR", raising=False)
    else:
        monkeypatch.setenv("FELT_SECRETS_DIR", str(secrets_dir))
    return importlib.reload(config_module)


def test_probe_read_from_secrets_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FELT_VAULT_PROBE", raising=False)
    (tmp_path / "FELT_VAULT_PROBE").write_text("fake-value")
    (tmp_path / ".heartbeat").write_text("2026-10-06T21:00:00Z acc 900")
    cfg = _reload(monkeypatch, tmp_path).FeltConfig(_env_file=None)
    assert cfg.vault_probe == "fake-value"


def test_env_beats_secrets_dir(tmp_path, monkeypatch):
    (tmp_path / "FELT_VAULT_PROBE").write_text("from-vault")
    monkeypatch.setenv("FELT_VAULT_PROBE", "from-env")
    cfg = _reload(monkeypatch, tmp_path).FeltConfig(_env_file=None)
    assert cfg.vault_probe == "from-env"


def test_no_secrets_dir_means_no_probe(monkeypatch):
    monkeypatch.delenv("FELT_VAULT_PROBE", raising=False)
    cfg = _reload(monkeypatch, None).FeltConfig(_env_file=None)
    assert cfg.vault_probe is None
    _reload(monkeypatch, None)  # leave the module as other tests expect

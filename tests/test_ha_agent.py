"""Felt as Home Assistant's conversation agent (felt-ha-hub D3): only a
valid Authentik token for the ha-felt service account opens /v1/ha/chat."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import felt_core.api as api
from felt_core import ha_api, ha_auth
from felt_core.brain.result import ChatResult

ISS = "https://auth.clockmost.com/application/o/ha-felt/"
AUD = "ha-felt-client-id"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=KEY, **over):
    now = int(time.time())
    claims = {
        "iss": ISS,
        "aud": AUD,
        "iat": now,
        "exp": now + 900,
        "sub": "x",
        "preferred_username": "ha-felt",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FELT_DB_PATH", str(tmp_path / "felt.db"))
    monkeypatch.setenv("FELT_HA_AGENT_ISSUER", ISS)
    monkeypatch.setenv("FELT_HA_AGENT_CLIENT_ID", AUD)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key()))
    monkeypatch.setattr(ha_auth, "_jwks", lambda url: jwks)
    said = []

    async def fake_chat(text, config, session_id):
        said.append((text, session_id))
        return ChatResult(reply="shelf's off.")

    monkeypatch.setattr(ha_api, "chat", fake_chat)
    c = TestClient(api.app)
    c.said = said
    return c


def _post(client, token, **headers):
    return client.post(
        "/v1/ha/chat",
        json={"text": "turn off the shelf"},
        headers={"Authorization": f"Bearer {token}", **headers},
    )


def test_a_valid_token_reaches_felt(client):
    r = _post(client, _token())
    assert r.status_code == 200 and r.json() == {"reply": "shelf's off."}
    assert client.said == [("turn off the shelf", "ha")]


@pytest.mark.parametrize(
    "token",
    [
        _token(exp=int(time.time()) - 10),  # expired
        _token(aud="some-other-app"),
        _token(iss="https://auth.clockmost.com/application/o/felt/"),
        _token(preferred_username="mario"),  # another account's token
        _token(preferred_username=None),
        _token(key=OTHER_KEY),  # not signed by Authentik
        jwt.encode({"iss": ISS, "aud": AUD, "exp": int(time.time()) + 900}, "", algorithm="none"),
        "not-a-jwt",
    ],
    # fixed ids: the tokens are signed afresh at collection, and parallel
    # workers (pytest-xdist) have to collect the same test names
    ids=[
        "expired",
        "other-audience",
        "other-issuer",
        "other-account",
        "no-username",
        "not-authentik-key",
        "alg-none",
        "not-a-jwt",
    ],
)
def test_bad_tokens_are_refused(client, token):
    assert _post(client, token).status_code == 401
    assert client.said == []


def test_no_token_is_refused(client):
    assert client.post("/v1/ha/chat", json={"text": "hi"}).status_code == 401


def test_proxy_identity_headers_dont_open_it(client):
    # rule 2: an independently token-authenticated route never trusts proxy identity
    r = client.post(
        "/v1/ha/chat",
        json={"text": "hi"},
        headers={
            "X-Felt-Identity": "spiffe://clockmost.internal/workload/tray",
            "X-Authentik-Username": "akadmin",
        },
    )
    assert r.status_code == 401


def test_closed_when_not_configured(client, monkeypatch):
    monkeypatch.setenv("FELT_HA_AGENT_ISSUER", "")
    assert _post(client, _token()).status_code == 401


def test_the_ha_token_opens_nothing_else(client, monkeypatch):
    monkeypatch.setenv("FELT_AUTH_MODE", "identity")  # production's mode
    r = client.post(
        "/v1/text-chat",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert r.status_code in (401, 403)


# --- step 2: her switches in HA ---------------------------------------------------


def _ctl(client, method="get", token=None, **body):
    headers = {"Authorization": f"Bearer {token or _token()}"}
    if method == "get":
        return client.get("/v1/ha/controls", headers=headers)
    return client.post("/v1/ha/controls", json=body, headers=headers)


def test_controls_read(client):
    r = _ctl(client)
    assert r.status_code == 200
    assert r.json() | {"mood": None} == {
        "quiet": False,
        "hide": False,
        "dj": False,
        "desktop_online": False,
        "mood": None,
        "message": None,
    }


def test_quiet_and_hide_are_the_apps_switches(client):
    assert _ctl(client, "post", name="quiet", on=True).json()["quiet"] is True
    assert _ctl(client, "post", name="hide", on=True).json()["hide"] is True
    r = _ctl(client, "post", name="quiet", on=False).json()
    assert (r["quiet"], r["hide"]) == (False, True)


def test_dj_switch_starts_and_stops_the_set(client, monkeypatch):
    calls = []

    async def start(config):
        calls.append("start")
        ha_api.dj_control.SESSION.on = True
        return "DJ mode on. First up: X by Y."

    monkeypatch.setattr(ha_api.dj_control, "start", start)
    monkeypatch.setattr(ha_api.dj_control, "stop", lambda why: calls.append(f"stop: {why}"))
    on = _ctl(client, "post", name="dj", on=True).json()
    assert on["dj"] is True and on["message"].startswith("DJ mode on")
    _ctl(client, "post", name="dj", on=False)
    assert calls == ["start", "stop: turned off in Home Assistant"]
    ha_api.dj_control.SESSION.on = False


def test_controls_need_the_ha_token(client):
    assert client.get("/v1/ha/controls").status_code == 401
    assert _ctl(client, token=_token(preferred_username="mario")).status_code == 401


def test_unknown_control_is_refused(client):
    assert _ctl(client, "post", name="claude_alerts", on=False).status_code == 422


# --- the Systems summary ----------------------------------------------------------


def test_systems_summary(client, monkeypatch):
    page = {
        "areas": [
            {"name": "server", "status": "needs-you", "count": 2, "incident_id": 7},
            {"name": "security", "status": "watching", "count": 1, "incident_id": 9},
            {"name": "router", "status": "fine", "count": 0, "incident_id": None},
        ],
        "circled": 7,
        "incidents": [
            {
                "id": 9,
                "area": "security",
                "status": "watching",
                "tier": "high",
                "title": "Wazuh L12",
                "last_at": "2026-10-18 20:21:07",
            },
            {
                "id": 7,
                "area": "server",
                "status": "needs-you",
                "tier": "high",
                "title": "felt-core health",
                "last_at": "2026-10-18 19:47:18",
            },
        ],
    }
    monkeypatch.setattr(ha_api.systems, "page", lambda db, before, limit: page)
    r = client.get("/v1/ha/systems", headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    body = r.json()
    assert body["areas"]["server"] == {"status": "needs-you", "count": 2}
    assert body["needs_you"] == 2
    assert [i["id"] for i in body["latest"]] == [9, 7]


def test_systems_needs_the_ha_token(client):
    assert client.get("/v1/ha/systems").status_code == 401


# --- the Felt panel in HA: the app's Talk + Margins routes under /v1/ha/app ---------


def test_the_panel_routes_take_the_ha_token(client, monkeypatch):
    monkeypatch.setenv("FELT_AUTH_MODE", "identity")
    auth = {"Authorization": f"Bearer {_token()}"}
    assert client.get("/v1/ha/app/me", headers=auth).json() == {"user": "home-assistant"}
    assert client.get("/v1/ha/app/history", headers=auth).status_code == 200
    assert client.get("/v1/ha/app/margins/count", headers=auth).status_code == 200


def test_the_panel_routes_refuse_everything_else(client, monkeypatch):
    monkeypatch.setenv("FELT_AUTH_MODE", "identity")
    # no token, a forged one, or an SSO header (Caddy's felt-ha host strips it anyway)
    assert client.get("/v1/ha/app/history").status_code == 401
    bad = {"Authorization": f"Bearer {_token(key=OTHER_KEY)}"}
    assert client.get("/v1/ha/app/history", headers=bad).status_code == 401
    sso = {"X-Authentik-Username": "mario"}
    assert client.get("/v1/ha/app/history", headers=sso).status_code == 401
    # and the HA token still doesn't open the SSO copy of the same routes
    auth = {"Authorization": f"Bearer {_token()}"}
    assert client.get("/v1/app/history", headers=auth).status_code == 401


def test_new_mail_from_ha_is_stored_once(client):
    mail = {"key": "<1@x>", "sender": "Ana <ana@x.pt>", "subject": "Dinner", "text": "hi"}
    headers = {"Authorization": f"Bearer {_token()}"}
    assert client.post("/v1/ha/email", json=mail, headers=headers).json() == {"new": True}
    assert client.post("/v1/ha/email", json=mail, headers=headers).json() == {"new": False}
    assert client.post("/v1/ha/email", json=mail).status_code == 401

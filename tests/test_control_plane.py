"""The authenticated control plane, and the defaults it made secure.

Every test here runs with `@pytest.mark.secure_defaults`, which clears the
permissive demo profile the rest of the suite uses (see conftest). That marker
is the point of this file: it is where the *production* configuration is
exercised, so "the demo runs open" never quietly becomes "nothing tests the
closed path".

The vulnerability being closed, stated plainly: before Phase 2 every
operational endpoint was public and took an `agentId` query parameter. On the
deployed instance any visitor could read every publisher's revenue, every
agent's spend, and the whole audit trail, and could trigger a settlement run
over somebody else's commitments. An id in a query string is a filter. It was
doing the job of authorization, and a filter cannot do that job.
"""

from __future__ import annotations

import base64
import importlib

import pytest
from conftest import TEST_SECRET
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytestmark = pytest.mark.secure_defaults

BOOTSTRAP = "bootstrap-token-for-tests"


def _reload_main(monkeypatch, ledger_path, **env):
    """A facilitator built with the production-like security defaults."""
    monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("MOCK_RAZORPAY", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
    monkeypatch.setenv("SETTLEMENT_MODE", "deferred")
    monkeypatch.setenv("CONTROL_PLANE_BOOTSTRAP_TOKEN", BOOTSTRAP)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import main

    importlib.reload(main)
    return main


def _client(main):
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _bootstrap_headers():
    return {"Authorization": f"Bearer {BOOTSTRAP}"}


def _make_operator(client, name="Acme Agents", scopes=None):
    """Creates an operator and one API key for it.

    `scopes` defaults to the operator defaults, which deliberately EXCLUDE
    `keys:write` — a key that can mint further keys is a privilege-escalation
    vector and should be asked for on purpose. Tests about key management pass
    it explicitly, which is also what makes those tests exercise the tenant
    check rather than stopping at the scope check.
    """
    body = client.post(
        "/control/operators", json={"displayName": name}, headers=_bootstrap_headers()
    )
    assert body.status_code == 201, body.text
    operator_id = body.json()["operatorId"]

    payload = {"label": f"{name} key", "operatorId": operator_id}
    if scopes is not None:
        payload["scopes"] = scopes

    key = client.post("/control/keys", json=payload, headers=_bootstrap_headers())
    assert key.status_code == 201, key.text
    return operator_id, key.json()["apiKey"]


# The operator defaults plus key management, for tests that manage keys.
MANAGES_KEYS = [
    "ledger:read",
    "economics:read",
    "events:read",
    "agents:write",
    "consent:read",
    "consent:write",
    "keys:write",
]


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _enroll_agent(client, api_key, agent_id):
    """Full challenge-response enrollment. Returns the agent's private key."""
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes_raw()
    ).decode()

    challenge = client.post(
        "/control/agents/challenge", json={"agentId": agent_id}, headers=_auth(api_key)
    )
    assert challenge.status_code == 201, challenge.text
    nonce = challenge.json()["nonce"]

    # Signed the same way payment acceptances are: canonical JSON of a small
    # object, so an agent needs one signing routine rather than two.
    import json

    message = json.dumps({"challenge": nonce}, sort_keys=True, separators=(",", ":"))
    signature = base64.b64encode(private.sign(message.encode())).decode()

    enrolled = client.post(
        "/control/agents/enroll",
        json={
            "challengeId": challenge.json()["challengeId"],
            "agentId": agent_id,
            "publicKey": public_b64,
            "challengeSignature": signature,
        },
        headers=_auth(api_key),
    )
    assert enrolled.status_code == 201, enrolled.text
    return private, public_b64, enrolled.json()["credentialId"]


# ---------------------------------------------------------------------------
# The defaults themselves
# ---------------------------------------------------------------------------


class TestSecureDefaults:
    """What a freshly-deployed facilitator refuses, with no configuration."""

    @pytest.mark.parametrize(
        "path",
        ["/ledger/summary", "/economics", "/ledger/events"],
    )
    def test_dashboard_reads_require_a_key(self, ledger_path, monkeypatch, path):
        """The endpoints that used to answer anyone."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            response = client.get(path)

        assert response.status_code == 401, response.text
        # A 401 without this header tells a client nothing about how to fix it.
        assert response.headers.get("www-authenticate") == "Bearer"

    @pytest.mark.parametrize("path", ["/ledger/summary", "/economics", "/ledger/events"])
    def test_naming_an_agent_id_does_not_authorize(self, ledger_path, monkeypatch, path):
        """The exact shape of the original hole.

        `?agentId=victim` was the whole "authorization" story. It has to fail
        now, or nothing has actually changed.
        """
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            response = client.get(path, params={"agentId": "agent-victim"})
        assert response.status_code == 401

    def test_settle_batch_requires_a_key(self, ledger_path, monkeypatch):
        """A write that creates real gateway charges, previously public."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            response = client.post("/settle-batch", json={})
        assert response.status_code == 401

    def test_tofu_registration_is_refused(self, ledger_path, monkeypatch):
        """First-caller-wins key binding is off by default."""
        main = _reload_main(monkeypatch, ledger_path)
        private = Ed25519PrivateKey.generate()
        public = base64.b64encode(private.public_key().public_bytes_raw()).decode()

        with _client(main) as client:
            response = client.post(
                "/agents/register", json={"agentId": "agent-squatter", "publicKey": public}
            )

        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "tofu_disabled"
        # The refusal has to say where to go instead, or it is just a wall.
        assert body["enrollmentEndpoint"] == "/control/agents/enroll"

    def test_tenant_creation_is_refused_without_a_bootstrap_token(
        self, ledger_path, monkeypatch
    ):
        """Fail closed: an unset secret must not mean "anyone may create an operator"."""
        main = _reload_main(monkeypatch, ledger_path, CONTROL_PLANE_BOOTSTRAP_TOKEN="")
        with _client(main) as client:
            response = client.post(
                "/control/operators",
                json={"displayName": "Nobody"},
                headers={"Authorization": "Bearer anything"},
            )
        assert response.status_code == 403
        assert response.json()["error"] == "bootstrap_disabled"

    def test_startup_announces_the_secure_profile(self, ledger_path, monkeypatch, capsys):
        """A deployment says which profile it is running, every boot."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main):
            pass
        out = capsys.readouterr().out
        assert "security_profile" in out
        assert "insecure_demo_mode_enabled" not in out

    def test_startup_warns_loudly_when_running_open(self, ledger_path, monkeypatch, capsys):
        """The failure mode these flags exist to avoid is nobody noticing."""
        main = _reload_main(
            monkeypatch, ledger_path, DEMO_OPEN_DASHBOARD="true", DEMO_UNSAFE_TOFU="true"
        )
        with _client(main):
            pass
        out = capsys.readouterr().out
        assert "insecure_demo_mode_enabled" in out
        assert "DEMO_OPEN_DASHBOARD" in out
        assert "DEMO_UNSAFE_TOFU" in out


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestApiCredentials:
    def test_the_plaintext_key_is_returned_once_and_never_stored(
        self, ledger_path, monkeypatch
    ):
        """Disclosure of the credentials table must yield nothing usable."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _make_operator(client, scopes=MANAGES_KEYS)

            listed = client.get("/control/keys", headers=_auth(api_key)).json()

        assert listed["credentials"], listed
        row = listed["credentials"][0]
        # The prefix is a display aid; the key itself must not come back.
        assert api_key.startswith(row["keyPrefix"])
        assert "apiKey" not in row
        assert "keyHash" not in row

        # And the stored value is a hash, not the key.
        from ledger import Ledger

        stored = Ledger(str(ledger_path)).find_api_credential(_sha256(api_key))
        assert stored is not None
        assert stored["key_hash"] != api_key

    def test_a_wrong_key_is_refused(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _make_operator(client)
            response = client.get("/ledger/summary", headers=_auth("bx402_not-a-real-key"))
        assert response.status_code == 401

    def test_a_revoked_key_stops_working_immediately(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            operator_id, api_key = _make_operator(client, scopes=MANAGES_KEYS)
            _enroll_agent(client, api_key, "agent-one")

            assert client.get("/control/whoami", headers=_auth(api_key)).status_code == 200

            credential_id = client.get("/control/whoami", headers=_auth(api_key)).json()[
                "credentialId"
            ]
            revoked = client.post(
                f"/control/keys/{credential_id}/revoke", headers=_auth(api_key)
            )
            assert revoked.status_code == 200

            after = client.get("/control/whoami", headers=_auth(api_key))

        assert after.status_code == 401
        assert operator_id  # the operator survives; only the credential died

    def test_a_key_cannot_mint_a_key_for_another_tenant(self, ledger_path, monkeypatch):
        """Privilege escalation via a JSON field.

        `POST /control/keys` accepts an `operatorId`, which is meaningful for
        the bootstrap token. Honouring it for an ordinary key would let any
        tenant mint credentials for any other.
        """
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            operator_b, _ = _make_operator(client, "Operator B")

            issued = client.post(
                "/control/keys",
                json={"label": "escalation", "operatorId": operator_b},
                headers=_auth(key_a),
            )

        # Refused for lacking keys:write — and even with that scope, the body's
        # operatorId is ignored in favour of the authenticated principal's.
        assert issued.status_code == 403
        assert issued.json()["error"] == "insufficient_scope"

    def test_scopes_are_enforced_per_endpoint(self, ledger_path, monkeypatch):
        """A reporting key must not be able to trigger settlement."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            operator_id, _ = _make_operator(client)
            read_only = client.post(
                "/control/keys",
                json={
                    "label": "digest cron",
                    "operatorId": operator_id,
                    "scopes": ["ledger:read"],
                },
                headers=_bootstrap_headers(),
            ).json()["apiKey"]

            summary = client.get("/ledger/summary", headers=_auth(read_only))
            settle = client.post("/settle-batch", json={}, headers=_auth(read_only))
            events = client.get("/ledger/events", headers=_auth(read_only))

        # It can read a summary...
        assert summary.status_code in (200, 400)
        # ...but not settle, and not read the audit trail.
        assert settle.status_code == 403
        assert settle.json()["error"] == "insufficient_scope"
        assert events.status_code == 403

    def test_an_unknown_scope_is_rejected_rather_than_dropped(self, ledger_path, monkeypatch):
        """A typo must not silently produce a key with different permissions."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            operator_id, _ = _make_operator(client)
            response = client.post(
                "/control/keys",
                json={
                    "label": "typo",
                    "operatorId": operator_id,
                    "scopes": ["ledger:reed"],
                },
                headers=_bootstrap_headers(),
            )
        assert response.status_code == 400
        assert response.json()["error"] == "unknown_scope"


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """One operator must never see or touch another's data."""

    def test_an_operator_cannot_read_another_operators_agent(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            _, key_b = _make_operator(client, "Operator B")
            _enroll_agent(client, key_a, "agent-alpha")
            _enroll_agent(client, key_b, "agent-beta")

            crossed = client.get(
                "/ledger/summary", params={"agentId": "agent-beta"}, headers=_auth(key_a)
            )

        assert crossed.status_code == 403
        assert crossed.json()["error"] == "not_your_agent"

    def test_an_operator_cannot_read_another_operators_audit_trail(
        self, ledger_path, monkeypatch
    ):
        """The most sensitive read: who is buying what from whom."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            _, key_b = _make_operator(client, "Operator B")
            _enroll_agent(client, key_a, "agent-alpha")
            _enroll_agent(client, key_b, "agent-beta")

            crossed = client.get(
                "/ledger/events", params={"agentId": "agent-beta"}, headers=_auth(key_a)
            )
        assert crossed.status_code == 403

    def test_an_operator_cannot_settle_another_operators_commitments(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            _, key_b = _make_operator(client, "Operator B")
            _enroll_agent(client, key_a, "agent-alpha")
            _enroll_agent(client, key_b, "agent-beta")

            settle_key = client.post(
                "/control/keys",
                json={
                    "label": "settler",
                    "operatorId": client.get("/control/whoami", headers=_auth(key_a)).json()[
                        "tenantId"
                    ],
                    "scopes": ["settle:write", "ledger:read"],
                },
                headers=_bootstrap_headers(),
            ).json()["apiKey"]

            crossed = client.post(
                "/settle-batch", json={"agentId": "agent-beta"}, headers=_auth(settle_key)
            )

        assert crossed.status_code == 403
        assert crossed.json()["error"] == "not_your_agent"
        assert key_b  # B's key exists and is unaffected

    def test_an_operator_cannot_revoke_another_operators_key(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            # Both hold keys:write, so this test reaches the TENANT check
            # rather than stopping at the scope check — the tenant scoping is
            # what is under test here.
            _, key_a = _make_operator(client, "Operator A", scopes=MANAGES_KEYS)
            _, key_b = _make_operator(client, "Operator B", scopes=MANAGES_KEYS)

            b_credential = client.get("/control/whoami", headers=_auth(key_b)).json()[
                "credentialId"
            ]
            crossed = client.post(
                f"/control/keys/{b_credential}/revoke", headers=_auth(key_a)
            )

            # B is untouched.
            still_works = client.get("/control/whoami", headers=_auth(key_b))

        # 404 rather than 403: confirming the id exists would be an oracle for
        # enumerating other tenants' credentials.
        assert crossed.status_code == 404
        assert still_works.status_code == 200

    def test_an_operator_cannot_suspend_another_operators_agent(
        self, ledger_path, monkeypatch
    ):
        """Without tenant scoping this endpoint is a denial-of-service primitive."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            _, key_b = _make_operator(client, "Operator B")
            _enroll_agent(client, key_b, "agent-beta")

            crossed = client.post(
                "/control/agents/agent-beta/status",
                json={"status": "suspended"},
                headers=_auth(key_a),
            )

            agents = client.get("/control/agents", headers=_auth(key_b)).json()["agents"]

        assert crossed.status_code == 404
        assert agents[0]["status"] == "active"

    def test_an_unfiltered_read_by_a_multi_agent_operator_is_refused(
        self, ledger_path, monkeypatch
    ):
        """No `agentId` must not silently mean "everyone's data".

        With one agent the answer is unambiguous and is served. With several,
        guessing would be the same cross-tenant leak in a different costume.
        """
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _make_operator(client, "Operator A")
            _enroll_agent(client, key_a, "agent-one")

            single = client.get("/ledger/summary", headers=_auth(key_a))
            assert single.status_code == 200

            _enroll_agent(client, key_a, "agent-two")
            multiple = client.get("/ledger/summary", headers=_auth(key_a))

        assert multiple.status_code == 400
        assert multiple.json()["error"] == "agent_id_required"

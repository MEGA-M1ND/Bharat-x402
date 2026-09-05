"""Consent, credential lifecycle, and the authority questions x402 does not ask.

x402 establishes that an agent *agreed to a price*. These tests are about the
separate question: was it ever allowed to spend anyone's money?

Every failure mode below must fail **closed**. There is no branch anywhere in
`consent.evaluate` that falls through to allowed, and the tests are arranged to
prove that one refusal reason at a time — because "denied" on its own is not an
operable answer for the agent, the operator, or whoever reads the audit trail a
week later wondering why traffic stopped.
"""

from __future__ import annotations

import base64
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TEST_SECRET
from consent import ConsentDenied, evaluate
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_full_flow import payment_envelope

pytestmark = pytest.mark.secure_defaults

BOOTSTRAP = "bootstrap-token-for-tests"


# ---------------------------------------------------------------------------
# The pure decision, with no HTTP and no database
# ---------------------------------------------------------------------------


def _consent(**overrides):
    base = {
        "consent_id": "con_1",
        "operator_id": "op_1",
        "agent_id": "agent-1",
        "status": "active",
        "currency": "INR",
        "per_request_limit_paise": 0,
        "daily_limit_paise": 0,
        "total_limit_paise": 0,
        "reserved_paise": 0,
        "consumed_paise": 0,
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": None,
    }
    base.update(overrides)
    return base


def _operator(status="active"):
    return {"operator_id": "op_1", "status": status, "display_name": "Acme"}


def _agent(status="active"):
    return {"agent_id": "agent-1", "status": status}


def _evaluate(**overrides):
    kwargs = {
        "consent": _consent(),
        "operator": _operator(),
        "agent": _agent(),
        "merchant_id": None,
        "scoped_merchant_ids": frozenset(),
        "amount_paise": 500,
        "committed_today_paise": 0,
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)


class TestConsentDecision:
    def test_an_unrestricted_active_consent_allows(self):
        decision = _evaluate()
        assert decision.consent_id == "con_1"
        assert decision.operator_id == "op_1"

    def test_no_consent_is_refused(self):
        """The default state of any agent nobody has authorised."""
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(consent=None)
        assert exc.value.reason == "no_consent"

    @pytest.mark.parametrize("status", ["suspended", "revoked", "expired"])
    def test_a_non_active_consent_is_refused(self, status):
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(consent=_consent(status=status))
        assert exc.value.reason == "consent_not_active"

    @pytest.mark.parametrize("status", ["suspended", "closed"])
    def test_a_non_active_operator_is_refused(self, status):
        """Suspending an operator must stop all of its agents at once."""
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(operator=_operator(status))
        assert exc.value.reason == "operator_not_active"

    def test_a_suspended_agent_is_refused_without_touching_its_consent(self):
        """The narrowest control: one process, not the whole authorisation."""
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(agent=_agent("suspended"))
        assert exc.value.reason == "agent_not_active"

    def test_an_expired_consent_is_refused(self):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(consent=_consent(valid_until=past))
        assert exc.value.reason == "consent_expired"

    def test_a_consent_that_has_not_started_is_refused(self):
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(consent=_consent(valid_from=future))
        assert exc.value.reason == "consent_not_yet_valid"

    def test_per_request_limit_is_enforced(self):
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(consent=_consent(per_request_limit_paise=100), amount_paise=500)
        assert exc.value.reason == "per_request_limit_exceeded"
        assert exc.value.detail["limitPaise"] == 100

    def test_daily_limit_counts_what_is_already_committed(self):
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(
                consent=_consent(daily_limit_paise=1000),
                committed_today_paise=800,
                amount_paise=500,
            )
        assert exc.value.reason == "daily_limit_exceeded"
        # The agent is told what it has left, so it can pick something cheaper
        # rather than retrying the same thing.
        assert exc.value.detail["remainingPaise"] == 200

    def test_the_total_limit_counts_reserved_as_well_as_consumed(self):
        """In-flight reservations must count against the ceiling.

        If only `consumed_paise` were charged against the total, two concurrent
        requests would each see room that only one of them can actually have —
        the classic read-then-decide overspend, at the consent layer.
        """
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(
                consent=_consent(
                    total_limit_paise=1000, consumed_paise=400, reserved_paise=400
                ),
                amount_paise=500,
            )
        assert exc.value.reason == "total_limit_exceeded"
        assert exc.value.detail["remainingPaise"] == 200

    def test_a_zero_limit_means_unset_not_spend_nothing(self):
        """0 is "no limit configured", which is why every check guards on > 0.

        Treating it as "spend nothing" would make an unset field silently deny
        everything — a configuration mistake that looks exactly like an outage.
        """
        assert _evaluate(consent=_consent(daily_limit_paise=0, total_limit_paise=0))

    def test_a_publisher_out_of_scope_is_refused(self):
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(
                merchant_id="mer_other", scoped_merchant_ids=frozenset({"mer_allowed"})
            )
        assert exc.value.reason == "merchant_out_of_scope"

    def test_a_scoped_consent_refuses_an_unidentified_publisher(self):
        """Scope must not be bypassable by simply not naming a merchant."""
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(merchant_id=None, scoped_merchant_ids=frozenset({"mer_allowed"}))
        assert exc.value.reason == "merchant_out_of_scope"

    def test_an_in_scope_publisher_is_allowed(self):
        assert _evaluate(
            merchant_id="mer_allowed", scoped_merchant_ids=frozenset({"mer_allowed"})
        )

    def test_an_empty_scope_means_any_publisher(self):
        assert _evaluate(merchant_id="mer_anything", scoped_merchant_ids=frozenset())

    @pytest.mark.parametrize("amount", [0, -1, -500])
    def test_a_non_positive_amount_is_refused(self, amount):
        with pytest.raises(ConsentDenied) as exc:
            _evaluate(amount_paise=amount)
        assert exc.value.reason == "invalid_amount"


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------


def _reload_main(monkeypatch, ledger_path, **env):
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


def _boot():
    return {"Authorization": f"Bearer {BOOTSTRAP}"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _operator_with_key(client, name="Acme"):
    operator_id = client.post(
        "/control/operators", json={"displayName": name}, headers=_boot()
    ).json()["operatorId"]
    api_key = client.post(
        "/control/keys",
        json={"label": f"{name} key", "operatorId": operator_id},
        headers=_boot(),
    ).json()["apiKey"]
    return operator_id, api_key


def _sign_challenge(private, nonce):
    message = json.dumps({"challenge": nonce}, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(private.sign(message.encode())).decode()


def _enroll(client, api_key, agent_id, private=None, rotate=False):
    private = private or Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()

    challenge = client.post(
        "/control/agents/challenge", json={"agentId": agent_id}, headers=_auth(api_key)
    ).json()

    response = client.post(
        "/control/agents/enroll",
        json={
            "challengeId": challenge["challengeId"],
            "agentId": agent_id,
            "publicKey": public,
            "challengeSignature": _sign_challenge(private, challenge["nonce"]),
            "rotate": rotate,
        },
        headers=_auth(api_key),
    )
    return private, public, response


class TestEnrollment:
    """Proof of possession, replacing trust-on-first-use."""

    def test_a_valid_challenge_response_enrolls_the_key(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _, public, response = _enroll(client, api_key, "agent-alpha")

        assert response.status_code == 201, response.text
        assert response.json()["publicKey"] == public
        assert response.json()["created"] is True

    def test_a_signature_from_the_wrong_key_is_refused(self, ledger_path, monkeypatch):
        """The check that stops an operator binding a key it does not hold.

        Without it, an operator could enroll somebody else's public key and
        then repudiate acceptances made under it — the exact property Ed25519
        was chosen to provide.
        """
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)

            enrolling = Ed25519PrivateKey.generate()
            impostor = Ed25519PrivateKey.generate()
            public = base64.b64encode(
                enrolling.public_key().public_bytes_raw()
            ).decode()

            challenge = client.post(
                "/control/agents/challenge",
                json={"agentId": "agent-alpha"},
                headers=_auth(api_key),
            ).json()

            response = client.post(
                "/control/agents/enroll",
                json={
                    "challengeId": challenge["challengeId"],
                    "agentId": "agent-alpha",
                    "publicKey": public,
                    # Signed by a different key than the one being enrolled.
                    "challengeSignature": _sign_challenge(impostor, challenge["nonce"]),
                },
                headers=_auth(api_key),
            )

        assert response.status_code == 400
        assert response.json()["error"] == "challenge_signature_invalid"

    def test_a_challenge_cannot_be_replayed(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            private = Ed25519PrivateKey.generate()
            public = base64.b64encode(private.public_key().public_bytes_raw()).decode()

            challenge = client.post(
                "/control/agents/challenge",
                json={"agentId": "agent-alpha"},
                headers=_auth(api_key),
            ).json()
            payload = {
                "challengeId": challenge["challengeId"],
                "agentId": "agent-alpha",
                "publicKey": public,
                "challengeSignature": _sign_challenge(private, challenge["nonce"]),
            }

            first = client.post("/control/agents/enroll", json=payload, headers=_auth(api_key))
            second = client.post(
                "/control/agents/enroll", json=payload, headers=_auth(api_key)
            )

        assert first.status_code == 201
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_challenge"

    def test_another_operators_challenge_cannot_be_used(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _operator_with_key(client, "A")
            _, key_b = _operator_with_key(client, "B")

            private = Ed25519PrivateKey.generate()
            public = base64.b64encode(private.public_key().public_bytes_raw()).decode()

            challenge = client.post(
                "/control/agents/challenge",
                json={"agentId": "agent-alpha"},
                headers=_auth(key_a),
            ).json()

            stolen = client.post(
                "/control/agents/enroll",
                json={
                    "challengeId": challenge["challengeId"],
                    "agentId": "agent-alpha",
                    "publicKey": public,
                    "challengeSignature": _sign_challenge(private, challenge["nonce"]),
                },
                headers=_auth(key_b),
            )

        assert stolen.status_code == 400
        assert stolen.json()["error"] == "invalid_challenge"

    def test_rebinding_a_different_key_is_refused_without_rotate(
        self, ledger_path, monkeypatch
    ):
        """Key rotation and account takeover must not be the same request."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _enroll(client, api_key, "agent-alpha")
            _, _, second = _enroll(client, api_key, "agent-alpha")

        assert second.status_code == 409

    def test_an_agent_belonging_to_another_operator_cannot_be_claimed(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _operator_with_key(client, "A")
            _, key_b = _operator_with_key(client, "B")
            _enroll(client, key_a, "agent-alpha")

            stolen = _enroll(client, key_b, "agent-alpha", rotate=True)[2]

        assert stolen.status_code == 409
        assert "belongs to operator" in stolen.json()["message"]


class TestCredentialLifecycle:
    def test_rotation_supersedes_the_old_credential_and_keeps_it_readable(
        self, ledger_path, monkeypatch
    ):
        """Rotation must not destroy the audit trail.

        A superseded credential stays in the table so an acceptance signed
        while it was valid remains verifiable. Deleting it would silently
        destroy exactly the evidence Ed25519 exists to provide.
        """
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _, first_public, _ = _enroll(client, api_key, "agent-alpha")
            _, second_public, rotated = _enroll(
                client, api_key, "agent-alpha", rotate=True
            )

            history = client.get(
                "/control/agents/agent-alpha/credentials", headers=_auth(api_key)
            ).json()["credentials"]

        assert rotated.status_code == 201
        assert rotated.json()["rotated"] is True

        by_key = {c["publicKey"]: c for c in history}
        assert by_key[first_public]["status"] == "superseded"
        assert by_key[second_public]["status"] == "active"
        # The old public key is still there to verify old signatures against.
        assert first_public != second_public

    def test_a_revoked_credential_cannot_authorize_a_new_payment(
        self, ledger_path, monkeypatch
    ):
        """Revocation has to bite on the payment path, not just the listing."""
        main = _reload_main(monkeypatch, ledger_path, REQUIRE_CONSENT="false")
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _, _, enrolled = _enroll(client, api_key, "agent-alpha")
            credential_id = enrolled.json()["credentialId"]

            before = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )
            assert before.status_code == 200, before.text

            revoked = client.post(
                f"/control/agents/agent-alpha/credentials/{credential_id}/revoke",
                headers=_auth(api_key),
            )
            assert revoked.status_code == 200

            # A quote can still be issued — quoting is not authorization — but
            # the settlement that needs the key must fail.
            offer = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            ).json()

            settled = client.post(
                "/settle", json=payment_envelope(offer, agent_id="agent-alpha")
            )

        assert settled.status_code == 200
        body = settled.json()
        assert body["success"] is False
        # Falls to "no key on file", which with HMAC fallback off is a refusal.
        assert body["errorReason"] in ("agent_not_registered", "invalid_signature")


class TestConsentThroughTheService:
    def test_an_agent_with_no_consent_cannot_get_a_quote(self, ledger_path, monkeypatch):
        """Refused at quote time, before it signs and before a row exists."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _enroll(client, api_key, "agent-alpha")

            response = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )

        assert response.status_code == 403
        assert response.json()["error"] == "no_consent"

    def test_a_consent_lets_the_agent_quote_and_stamps_the_commitment(
        self, ledger_path, monkeypatch
    ):
        """Every commitment must name who is on the hook for it."""
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            operator_id, api_key = _operator_with_key(client)
            _enroll(client, api_key, "agent-alpha")

            consent = client.post(
                "/control/consents",
                json={"agentId": "agent-alpha", "dailyLimitPaise": 10000},
                headers=_auth(api_key),
            )
            assert consent.status_code == 201, consent.text

            offer = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )

        assert offer.status_code == 200, offer.text

        from ledger import Ledger

        led = Ledger(str(ledger_path))
        stored = led.get_consent(consent.json()["consentId"])
        assert stored["operator_id"] == operator_id
        assert stored["daily_limit_paise"] == 10000

    def test_revoking_a_consent_stops_the_next_quote(self, ledger_path, monkeypatch):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _enroll(client, api_key, "agent-alpha")
            consent_id = client.post(
                "/control/consents",
                json={"agentId": "agent-alpha", "dailyLimitPaise": 10000},
                headers=_auth(api_key),
            ).json()["consentId"]

            quote_body = {
                "agentId": "agent-alpha",
                "resourceId": "market-report",
                "amountPaise": 500,
                "scheme": "razorpay-inr",
                "network": "razorpay:inr-test",
                "payTo": "acc_test",
            }
            assert client.post("/offer", json=quote_body).status_code == 200

            revoked = client.post(
                f"/control/consents/{consent_id}/status",
                json={"status": "revoked"},
                headers=_auth(api_key),
            )
            assert revoked.status_code == 200

            after = client.post("/offer", json=quote_body)

        assert after.status_code == 403
        assert after.json()["error"] == "no_consent"

    def test_an_operator_cannot_revoke_another_operators_consent(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _operator_with_key(client, "A")
            _, key_b = _operator_with_key(client, "B")
            _enroll(client, key_a, "agent-alpha")

            consent_id = client.post(
                "/control/consents",
                json={"agentId": "agent-alpha", "dailyLimitPaise": 10000},
                headers=_auth(key_a),
            ).json()["consentId"]

            crossed = client.post(
                f"/control/consents/{consent_id}/status",
                json={"status": "revoked"},
                headers=_auth(key_b),
            )

            # A's agent still works.
            still_quoting = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )

        assert crossed.status_code == 404
        assert still_quoting.status_code == 200

    def test_the_per_request_limit_is_enforced_at_quote_time(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, api_key = _operator_with_key(client)
            _enroll(client, api_key, "agent-alpha")
            client.post(
                "/control/consents",
                json={"agentId": "agent-alpha", "perRequestLimitPaise": 100},
                headers=_auth(api_key),
            )

            response = client.post(
                "/offer",
                json={
                    "agentId": "agent-alpha",
                    "resourceId": "market-report",
                    "amountPaise": 500,
                    "scheme": "razorpay-inr",
                    "network": "razorpay:inr-test",
                    "payTo": "acc_test",
                },
            )

        assert response.status_code == 403
        assert response.json()["error"] == "per_request_limit_exceeded"

    def test_a_consent_cannot_be_created_for_another_operators_agent(
        self, ledger_path, monkeypatch
    ):
        main = _reload_main(monkeypatch, ledger_path)
        with _client(main) as client:
            _, key_a = _operator_with_key(client, "A")
            _, key_b = _operator_with_key(client, "B")
            _enroll(client, key_a, "agent-alpha")

            crossed = client.post(
                "/control/consents",
                json={"agentId": "agent-alpha", "dailyLimitPaise": 10000},
                headers=_auth(key_b),
            )

        assert crossed.status_code == 403
        assert crossed.json()["error"] == "not_your_agent"

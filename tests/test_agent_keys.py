"""Per-agent Ed25519 keypairs — the crypto, and who gets to choose it.

Split from test_full_flow.py because these tests are about one property that
file predates: an agent's payment proof must be something the facilitator can
*check* and cannot *produce*. See facilitator/payment_verifier.py for the
reasoning; this file is the part that holds it to it.

The tests that matter most here are not the round-trip ones. They are:

  * `test_registered_agent_cannot_downgrade_to_hmac` — the algorithm-choice
    attack that has broken JWT implementations repeatedly.
  * `test_the_key_checked_is_the_one_bound_to_the_offer` — the same attack
    reached through the agent id instead of the algorithm name.
"""

from __future__ import annotations

import base64
import json

from conftest import TEST_SECRET
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_full_flow import PRICE_PAISE, payment_envelope, quote


def ed25519_sign(body: dict, private_key_b64: str) -> str:
    """Signs the way a real agent would, without calling the code under test.

    Same discipline as `test_full_flow.hmac_sign`: builds the canonical JSON
    and calls `cryptography` directly, so a bug where the facilitator's signer
    and its verifier agree on the *wrong* bytes cannot pass this suite.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(private.sign(canonical.encode())).decode("ascii")


def ed25519_envelope(quoted: dict, *, agent_id: str, private_key_b64: str) -> dict:
    """A payment envelope signed with an agent's own key rather than the secret."""
    envelope = payment_envelope(quoted, agent_id=agent_id)
    commitment = dict(quoted["commitmentTemplate"])
    commitment["acceptedAt"] = envelope["paymentPayload"]["payload"]["acceptedAt"]
    envelope["paymentPayload"]["payload"]["agentSignature"] = ed25519_sign(
        commitment, private_key_b64
    )
    return envelope


def register(client, agent_id: str) -> str:
    """Generates a keypair, registers the public half, returns the private half."""
    from payment_verifier import generate_keypair

    private_b64, public_b64 = generate_keypair()
    response = client.post(
        "/agents/register",
        json={"agentId": agent_id, "publicKey": public_b64, "algorithm": "ed25519"},
    )
    assert response.status_code == 200, response.text
    return private_b64


# ---------------------------------------------------------------------------
# The primitives, before any HTTP
# ---------------------------------------------------------------------------


class TestEd25519Primitives:
    def test_sign_and_verify_round_trip(self):
        from payment_verifier import generate_keypair, verify_ed25519

        private, public = generate_keypair()
        body = {"offerId": "off_1", "amountPaise": 500}
        assert verify_ed25519(body, ed25519_sign(body, private), public)

    def test_signature_from_another_key_is_rejected(self):
        from payment_verifier import generate_keypair, verify_ed25519

        private_a, _ = generate_keypair()
        _, public_b = generate_keypair()
        body = {"offerId": "off_1", "amountPaise": 500}
        assert not verify_ed25519(body, ed25519_sign(body, private_a), public_b)

    def test_mutating_the_body_breaks_the_signature(self):
        """The one that matters for money: changing the amount must invalidate."""
        from payment_verifier import generate_keypair, verify_ed25519

        private, public = generate_keypair()
        signature = ed25519_sign({"amountPaise": 500}, private)
        assert not verify_ed25519({"amountPaise": 50000}, signature, public)

    def test_key_order_does_not_change_the_signature(self):
        """Canonicalisation, checked across the boundary: these two dicts are
        the same object to any JSON parser and must verify identically."""
        from payment_verifier import generate_keypair, verify_ed25519

        private, public = generate_keypair()
        signature = ed25519_sign({"a": 1, "b": 2}, private)
        assert verify_ed25519({"b": 2, "a": 1}, signature, public)

    def test_malformed_signature_is_a_failed_check_not_a_crash(self):
        """A caller checking a proof wants a yes/no. Unparseable is a no —
        an exception here would surface as a 500 on a hostile input."""
        from payment_verifier import generate_keypair, verify_ed25519

        _, public = generate_keypair()
        for bad in ["", "not-base64!!", "AAAA", "x" * 88]:
            assert verify_ed25519({"a": 1}, bad, public) is False

    def test_malformed_public_key_is_a_failed_check_not_a_crash(self):
        from payment_verifier import generate_keypair, verify_ed25519

        private, _ = generate_keypair()
        signature = ed25519_sign({"a": 1}, private)
        for bad in ["", "not-base64!!", "AAAA"]:
            assert verify_ed25519({"a": 1}, signature, bad) is False

    def test_holding_the_public_key_does_not_let_you_sign(self):
        """The property the whole change exists for.

        A raw Ed25519 public key is 32 bytes — the same length as a private
        key — so this does not fail a length check. Either it is refused, or
        it produces a signature that does not verify. Both are fine; silently
        producing a *valid* signature would not be.
        """
        from payment_verifier import (
            KeyFormatError,
            generate_keypair,
            sign_ed25519,
            verify_ed25519,
        )

        _, public = generate_keypair()
        try:
            forged = sign_ed25519({"a": 1}, public)
        except KeyFormatError:
            return
        assert not verify_ed25519({"a": 1}, forged, public)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestAgentRegistration:
    def test_registering_a_key_succeeds(self, client):
        from payment_verifier import generate_keypair

        _, public = generate_keypair()
        response = client.post(
            "/agents/register",
            json={"agentId": "agent-newbie", "publicKey": public, "algorithm": "ed25519"},
        )
        assert response.status_code == 200
        assert response.json()["created"] is True

    def test_reregistering_the_same_key_is_a_noop(self, client):
        """A restarted agent should not have to remember whether it has
        registered before."""
        from payment_verifier import generate_keypair

        _, public = generate_keypair()
        body = {"agentId": "agent-restart", "publicKey": public, "algorithm": "ed25519"}
        assert client.post("/agents/register", json=body).json()["created"] is True

        second = client.post("/agents/register", json=body)
        assert second.status_code == 200
        assert second.json()["created"] is False

    def test_rebinding_an_id_to_a_different_key_is_refused(self, client):
        """Accepting this would make key rotation and account takeover the
        same request."""
        from payment_verifier import generate_keypair

        _, first = generate_keypair()
        _, second = generate_keypair()

        client.post(
            "/agents/register",
            json={"agentId": "agent-victim", "publicKey": first, "algorithm": "ed25519"},
        )
        response = client.post(
            "/agents/register",
            json={"agentId": "agent-victim", "publicKey": second, "algorithm": "ed25519"},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "agent_already_registered"

    def test_a_refused_rebind_does_not_change_the_stored_key(self, client):
        """The refusal has to actually protect the row, not just return 409."""
        from payment_verifier import generate_keypair

        _, first = generate_keypair()
        _, second = generate_keypair()

        client.post(
            "/agents/register",
            json={"agentId": "agent-intact", "publicKey": first, "algorithm": "ed25519"},
        )
        client.post(
            "/agents/register",
            json={"agentId": "agent-intact", "publicKey": second, "algorithm": "ed25519"},
        )
        assert client.get("/agents/agent-intact").json()["publicKey"] == first

    def test_unparseable_key_is_rejected_at_registration(self, client):
        """Rejected here rather than surfacing later as a mysterious
        invalid_signature on a proof that was actually fine."""
        response = client.post(
            "/agents/register",
            json={"agentId": "agent-bad", "publicKey": "not-a-key", "algorithm": "ed25519"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_public_key"

    def test_unsupported_algorithm_is_rejected(self, client):
        response = client.post(
            "/agents/register",
            json={"agentId": "agent-rsa", "publicKey": "AAAA", "algorithm": "rsa-2048"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "unsupported_algorithm"

    def test_public_key_is_readable_back(self, client):
        """Public on purpose: an auditor settling a dispute should not have to
        ask the facilitator to mark its own homework."""
        from payment_verifier import generate_keypair

        _, public = generate_keypair()
        client.post(
            "/agents/register",
            json={"agentId": "agent-lookup", "publicKey": public, "algorithm": "ed25519"},
        )
        response = client.get("/agents/agent-lookup")
        assert response.status_code == 200
        assert response.json()["publicKey"] == public
        assert response.json()["algorithm"] == "ed25519"

    def test_unknown_agent_is_404(self, client):
        assert client.get("/agents/agent-ghost").status_code == 404


# ---------------------------------------------------------------------------
# Which primitive gets demanded, and who decides
# ---------------------------------------------------------------------------


class TestSignatureSchemeSelection:
    def test_registered_agent_pays_with_its_own_key(self, client):
        private = register(client, "agent-keyed")
        envelope = ed25519_envelope(
            quote(client, agent_id="agent-keyed"),
            agent_id="agent-keyed",
            private_key_b64=private,
        )
        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is True
        assert body["extra"]["proofScheme"] == "ed25519"

    def test_registered_agent_cannot_downgrade_to_hmac(self, client):
        """The downgrade attack.

        Once a key is on file a shared-secret proof must be refused — even
        though the facilitator holds that secret and could verify it
        perfectly well. Accepting it would mean any compromise of the shared
        secret still lets an attacker spend as a key-registered agent, which
        would make registration decorative.
        """
        register(client, "agent-downgrade")
        envelope = payment_envelope(
            quote(client, agent_id="agent-downgrade"), agent_id="agent-downgrade"
        )
        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is False
        assert body["invalidReason"] == "invalid_signature"

    def test_unregistered_agent_may_still_use_hmac_during_migration(self, client):
        envelope = payment_envelope(
            quote(client, agent_id="agent-legacy"), agent_id="agent-legacy"
        )
        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is True
        assert body["extra"]["proofScheme"] == "hmac-sha256"

    def test_the_key_checked_is_the_one_bound_to_the_offer(self, client):
        """An attacker must not be able to choose which key verifies them.

        The payload's `agentId` is relabelled to an unregistered identity
        while the offer stays bound to a registered one. If the facilitator
        looked the key up from the payload it would find none, fall back to
        HMAC, and accept a forged proof against a registered agent's offer.
        """
        register(client, "agent-target")
        quoted = quote(client, agent_id="agent-target")

        envelope = payment_envelope(quoted, agent_id="agent-target")
        envelope["paymentPayload"]["payload"]["agentId"] = "agent-unregistered-nobody"

        body = client.post("/verify", json=envelope).json()
        assert body["isValid"] is False
        assert body["invalidReason"] == "invalid_signature"

    def test_settlement_applies_the_same_rules_as_verify(self, client):
        """/settle re-verifies independently, so the scheme choice has to hold
        there too — a downgrade accepted at settle is the one that costs
        money."""
        register(client, "agent-settle-key")
        envelope = payment_envelope(
            quote(client, agent_id="agent-settle-key"), agent_id="agent-settle-key"
        )
        body = client.post("/settle", json=envelope).json()
        assert body["success"] is False
        assert body["errorReason"] == "invalid_signature"

    def test_ed25519_proof_settles_and_books_a_commitment(self, client, ledger_path):
        from ledger import Ledger

        private = register(client, "agent-real-pay")
        envelope = ed25519_envelope(
            quote(client, agent_id="agent-real-pay"),
            agent_id="agent-real-pay",
            private_key_b64=private,
        )
        body = client.post("/settle", json=envelope).json()
        assert body["success"] is True

        pending = Ledger(str(ledger_path)).pending_commitments(agent_id="agent-real-pay")
        assert len(pending) == 1
        assert pending[0]["amount_paise"] == PRICE_PAISE

    def test_downgrade_attempt_is_recorded_with_its_reason(self, client, ledger_path):
        from ledger import Ledger

        register(client, "agent-audited")
        envelope = payment_envelope(
            quote(client, agent_id="agent-audited"), agent_id="agent-audited"
        )
        client.post("/verify", json=envelope)

        events = Ledger(str(ledger_path)).list_events(agent_id="agent-audited")
        assert any(e["event"] == "payment_verify_rejected" for e in events)

    def test_fallback_verification_is_flagged_as_a_downgrade(self, client, ledger_path):
        """So a migration that quietly never finishes is visible in the audit
        trail rather than mistaken for a completed one."""
        from ledger import Ledger

        envelope = payment_envelope(
            quote(client, agent_id="agent-flagme"), agent_id="agent-flagme"
        )
        client.post("/verify", json=envelope)

        events = Ledger(str(ledger_path)).list_events(agent_id="agent-flagme")
        verified = [e for e in events if e["event"] == "payment_verified"]
        assert verified and verified[0]["detail"]["downgraded"] is True


# ---------------------------------------------------------------------------
# The end state: registration mandatory
# ---------------------------------------------------------------------------


class TestHmacFallbackDisabled:
    def _reload(self, monkeypatch, ledger_path):
        import importlib

        monkeypatch.setenv("LEDGER_DB_PATH", str(ledger_path))
        monkeypatch.setenv("MOCK_RAZORPAY", "true")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
        monkeypatch.setenv("FACILITATOR_HMAC_SECRET", TEST_SECRET)
        monkeypatch.setenv("ALLOW_HMAC_FALLBACK", "false")

        import main

        importlib.reload(main)
        return main

    def test_unregistered_agent_is_refused_when_fallback_is_off(self, ledger_path, monkeypatch):
        from fastapi.testclient import TestClient

        main = self._reload(monkeypatch, ledger_path)
        with TestClient(main.app) as client:
            envelope = payment_envelope(
                quote(client, agent_id="agent-nokey"), agent_id="agent-nokey"
            )
            body = client.post("/verify", json=envelope).json()
            assert body["isValid"] is False
            assert body["invalidReason"] == "agent_not_registered"

    def test_registered_agent_is_unaffected(self, ledger_path, monkeypatch):
        from fastapi.testclient import TestClient

        main = self._reload(monkeypatch, ledger_path)
        with TestClient(main.app) as client:
            private = register(client, "agent-haskey")
            envelope = ed25519_envelope(
                quote(client, agent_id="agent-haskey"),
                agent_id="agent-haskey",
                private_key_b64=private,
            )
            assert client.post("/verify", json=envelope).json()["isValid"] is True

    def test_supported_advertises_that_registration_is_required(self, ledger_path, monkeypatch):
        """A client should learn it must register from discovery, not by
        having a payment refused at settlement time."""
        from fastapi.testclient import TestClient

        main = self._reload(monkeypatch, ledger_path)
        with TestClient(main.app) as client:
            kind = client.get("/supported").json()["kinds"][0]
            assert kind["extra"]["proofScheme"] == "ed25519"
            assert kind["extra"]["hmacFallbackAllowed"] is False

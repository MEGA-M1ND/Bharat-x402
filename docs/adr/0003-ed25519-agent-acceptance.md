# ADR 0003 — Ed25519 for agent acceptance, and exactly what it proves

**Status:** Accepted · **Date:** 2026-09-05 · **Supersedes:** the shared-HMAC acceptance

## Context

Acceptances were originally signed with HMAC-SHA256 under a secret shared between the agent and the
facilitator. HMAC-SHA256 is a strong MAC — the problem was never its strength. It was **shape**.

A MAC's verifier holds the same key the signer does. So the facilitator could mint any agent's
acceptance itself, and a proof the adjudicator could have forged settles nothing in a dispute. If a
publisher and an agent disagree about a ₹5 charge, an HMAC tag is not evidence.

Non-repudiation requires that the verifier **cannot** sign. That means asymmetric.

## Decision

Sign acceptances with **Ed25519**, per agent. The agent generates the keypair; the facilitator
stores only the public half.

Ed25519 specifically, over ECDSA or RSA:

- No parameter choices to get wrong.
- Deterministic signatures — no per-signature nonce whose PRNG failure leaks the private key, which
  is how the PlayStation 3 and several Bitcoin wallets lost theirs.
- 32-byte public keys that fit comfortably in a JSON payload and a URL.

Two implementation rules that carry most of the security:

1. **The facilitator, not the payload, chooses the algorithm.** Once an agent has a key on file, an
   HMAC proof from it is refused. Attacker-chosen algorithm agility is the JWT `alg: none` /
   HMAC-RSA-confusion class of bug, and it has been breaking implementations for a decade.
2. **The key is looked up by the agent id on the stored offer**, never the one in the payload.
   Otherwise an attacker relabels itself as an unregistered agent, finds no key on file, and lands
   on the weaker path *on purpose*.

**The quote stays HMAC, deliberately.** There the facilitator is both the signer and the only party
that ever verifies — the signature exists so it can detect its own ledger row being edited
underneath it. A public key buys nothing, and a symmetric MAC is the correct primitive rather than
a leftover.

## What this proves — stated precisely

> **Tamper-evident, non-facilitator-forgeable evidence that the registered agent key accepted a
> particular quote.**

That is the whole claim. In particular it does **not** prove:

| Not proven | Why |
| --- | --- |
| That funds exist | A signature is a statement of intent. Authority is a separate subsystem (ADR 0001) |
| That the signer is a known legal entity | Under trust-on-first-use the key is self-asserted |
| That the agent paid | It agreed to owe. Collection is a later, separate event |
| That the operator authorized it | Until Phase 2 binds agents to operators, nothing does |

**Trust-on-first-use proves key continuity, not identity.** The first caller to claim an agent id
owns it. What TOFU gives you is that the *same key* came back — which is enough to attribute a
series of requests to one counterparty, and not enough to invoice anyone. Rebinding an existing id
is refused, so at least takeover and rotation are never the same request.

Phase 2 replaces TOFU as the default with authenticated operator enrollment and challenge-response
proof of possession. TOFU survives only behind `DEMO_UNSAFE_TOFU`, off by default, with a startup
warning.

## Consequences

**Good.** Real non-repudiation. Downgrade and relabelling attacks are tested, not assumed. The
console verifies a signature **in the visitor's browser** with WebCrypto, against a key re-fetched
from `/agents/<id>` rather than one handed over in the same response — so a sceptic can check the
claim rather than believe it, and CI reproduces the same check in Python so the button cannot
quietly end up verifying nothing.

**Costs.** Agents must manage a private key. Key rotation and revocation become required product
surface. Credentials must be stored as rows with validity windows rather than a column that gets
overwritten, so that historical acceptances stay verifiable after a rotation — revocation must stop
*new* authorization without invalidating *past* evidence.

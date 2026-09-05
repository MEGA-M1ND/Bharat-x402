# The `razorpay-inr` deferred scheme — an x402 protocol extension

A client that treats this facilitator's `/settle` response the way it would treat a Base USDC
settlement will be wrong about something that matters: **no money has moved.** This document says
exactly what is different, and how a client discovers that without reading it.

---

## What upstream x402 expects

From the [x402 v2 specification](https://github.com/coinbase/x402/blob/main/specs/x402-specification-v2.md):

- `POST /verify` returns `{isValid, invalidReason?, payer?}`.
- `POST /settle` returns `{success, errorReason?, payer?, transaction, network, amount?}`, where
  `transaction` is described as a **blockchain transaction hash**.
- In the `exact` EVM scheme, settlement broadcasts an EIP-3009 transfer. When `/settle` returns
  `success: true`, value has moved on-chain.

The spec anticipates other schemes — its architecture section names "exact, deferred" as examples —
but **it does not define deferred semantics.** So `razorpay-inr` is this project's own scheme, and
the burden of explaining it is ours.

## What this scheme actually does

| Step | `exact` on EVM | `razorpay-inr` here |
| --- | --- | --- |
| `/verify` | Signature and balance check | Ed25519 acceptance check, consent and limit check, authority reservation |
| Handler runs | Content produced | Content produced |
| `/settle` | Broadcasts a transfer; **funds move** | Captures the reservation and books a **receivable**; **no funds move** |
| `transaction` | On-chain tx hash | A commitment id, `cmt_…` — a real, auditable reference to a **debt** |
| Money movement | Same request, seconds | A batched collection later, confirmed by a separate signed webhook |
| Failure after success | Chain reorg (rare) | Collection can fail entirely. The receivable becomes bad debt |

### The one sentence a client implementer needs

> `success: true` from this facilitator means **the receivable was recorded**, not that the payer
> paid. The publisher has been paid *nothing* at this point and carries the credit risk until
> collection is confirmed.

## Why `/settle` keeps its name

Renaming it would break the stock `@x402/express` middleware, which is the entire point of being
protocol-compatible. The protocol endpoint keeps the protocol's name; the *internal* operation is
named for what it economically is (`capture_receivable`), and the difference is advertised rather
than hidden.

This is the trade the project makes deliberately: **compatible on the wire, honest in the
vocabulary, explicit about the gap.**

---

## Machine-discoverable semantics

A client must not have to read prose to learn that settlement is deferred. Three mechanisms carry
it:

### 1. `/supported` advertises the extension

Per spec §7.3.1, `extensions` is an "array of extension identifiers the facilitator has
implemented".

```json
{
  "kinds": [
    {
      "x402Version": 2,
      "scheme": "razorpay-inr",
      "network": "razorpay:inr-test",
      "extra": {
        "currency": "INR",
        "decimals": 2,
        "settlementMode": "deferred",
        "settlementTiming": "batched",
        "fundsMoveAtSettle": false,
        "authorityRequired": true
      }
    }
  ],
  "extensions": ["in.bharatx402.deferred-settlement/v1"]
}
```

`fundsMoveAtSettle: false` is the field that matters. It is a boolean a client can branch on.

### 2. The 402 body carries it into the negotiation

`extra` is passed through into `accepts[]`, so an agent sees the deferred semantics **before**
it accepts a quote, not after it has already been served.

### 3. The settle response labels its own states

Rather than one overloaded `success`, the response carries the lifecycle explicitly:

```json
{
  "success": true,
  "transaction": "cmt_675a947e702b466190e3",
  "network": "razorpay:inr-test",
  "amount": "50",
  "extensions": {
    "in.bharatx402.deferred-settlement/v1": {
      "authorized": true,
      "fulfilled": true,
      "committed": true,
      "collectionPending": true,
      "collected": false,
      "commitmentId": "cmt_675a947e702b466190e3",
      "settleDate": "2026-09-05",
      "backing": "simulated_reserve"
    }
  }
}
```

Five booleans, five different questions:

| Field | Question |
| --- | --- |
| `authorized` | Was the spend permitted under consent and policy? |
| `fulfilled` | Was the content actually delivered? |
| `committed` | Is a receivable recorded? |
| `collectionPending` | Is money still expected to arrive? |
| `collected` | Has the gateway confirmed money arrived? |

A conforming upstream client that ignores `extensions` entirely still works — it sees
`success: true` and a `transaction` reference, which is the compatibility guarantee. A client that
*reads* them can tell the difference between a debt and a payment.

---

## Additional trust assumptions this scheme carries

Stated because a client accepting `razorpay-inr` is accepting these:

1. **The publisher extends credit.** Content is delivered before money moves. Reserved authority
   (Phase 3) bounds the exposure; it does not remove it.
2. **The facilitator is trusted to collect.** It holds the receivable and runs the batch. A
   publisher's revenue depends on the facilitator doing its job.
3. **Ed25519 proves key continuity, not identity.** Under trust-on-first-use it proves only that
   the same key came back. An authenticated operator (Phase 2) is what turns that into a party you
   could actually invoice.
4. **Collection can fail.** The receivable can become bad debt. That state exists in the model and
   is reported, rather than being quietly rolled into revenue.
5. **The settlement instrument is pluggable and partly simulated.** `payment_link` is real against
   Razorpay's test API; `reserve_pay` is a documented simulation with no NPCI integration.

## Why an agent must not treat a commitment receipt as payment confirmation

Because it is a receipt for a **debt it now owes**, and the obligation survives:

- If the collection fails, the agent still owes the money.
- If the agent's operator revokes consent afterwards, the already-fulfilled request is still owed.
- A commitment id is the right thing to quote in a dispute, and the wrong thing to show a user as
  "payment complete".

An agent that renders `cmt_…` as "paid" is making exactly the mistake this whole document exists to
prevent.

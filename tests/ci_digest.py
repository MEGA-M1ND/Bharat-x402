"""CI check: the publisher's digest reports what the ledger actually holds.

A revenue report that overstates is worse than no report — a publisher acts on
this number. So the assertions are about the money agreeing with itself, not
about the script exiting zero.

Run against a ledger that already has activity (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUMMARY = REPO / "reporting" / "daily_summary.py"


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}

    # The human-facing render must not crash on whatever the ledger contains —
    # emoji, wide glyphs, long agent ids and all.
    rendered = subprocess.run(
        [sys.executable, str(SUMMARY), "--plain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if rendered.returncode != 0:
        fail(f"digest render failed: {rendered.stderr[:400]}")
    print(rendered.stdout)

    result = subprocess.run(
        [sys.executable, str(SUMMARY), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if result.returncode != 0:
        fail(f"digest --json failed: {result.stderr[:400]}")

    summary = json.loads(result.stdout)

    if summary["requests"] <= 0:
        fail("digest reports no requests — the ledger should have activity by now")
    if summary["totalPaise"] <= 0:
        fail("digest reports no revenue")

    created = [b for b in summary["batches"] if b["status"] == "created"]

    # A batch marked created without a payment link means the report is
    # claiming money was collected when nothing was charged.
    for batch in created:
        if not batch["payment_link_id"]:
            fail(f"batch {batch['batch_id']} is 'created' but has no payment link")

    # Settled money can never exceed what was committed.
    settled = sum(b["total_paise"] for b in created)
    if settled > summary["totalPaise"]:
        fail(f"batches claim {settled} paise but only {summary['totalPaise']} was committed")

    # Per-agent totals must reconstruct the headline figure exactly.
    by_agent = sum(a["total_paise"] for a in summary["byAgent"])
    if by_agent != summary["totalPaise"]:
        fail(f"per-agent totals sum to {by_agent}, headline says {summary['totalPaise']}")

    # The micro-revenue claim must be a subset of actual revenue, never more.
    micro = summary.get("belowGatewayMinimum", {})
    if micro.get("totalPaise", 0) > summary["totalPaise"]:
        fail(f"below-minimum revenue {micro} exceeds total {summary['totalPaise']}")

    print(
        f"PASS: digest reconciles — {summary['requests']} requests, "
        f"{summary['totalPaise']} paise committed, {settled} paise settled "
        f"across {len(created)} payment links"
    )


if __name__ == "__main__":
    main()

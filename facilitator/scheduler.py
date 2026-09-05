"""Bharat x402 — settlement scheduler.

Runs `/settle-batch` on a timer. In production this would be a cron entry, a
Celery beat schedule, or a Kubernetes CronJob; none of that would change the
facilitator, which is why the MVP is a loop and a sleep. Batching frequency is
a business decision, not an architectural one.

The one design point worth stating: settlement must be safe to run more often
than needed. If this fires twice in a minute the second run finds nothing
pending and does nothing, because `/settle-batch` only ever sweeps commitments
still marked pending. A scheduler you have to protect from itself is a
scheduler that will eventually double-charge someone.

Usage:
    python scheduler.py --once             # settle now and exit (what cron would call)
    python scheduler.py --interval 30      # every 30s, for watching a demo
    python scheduler.py --daily-at 23:30   # once a day at a fixed UTC time
    python scheduler.py --once --dry-run   # show what would settle, charge nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

DEFAULT_FACILITATOR = "http://localhost:8402"


def log(event: str, **fields: object) -> None:
    """Emits one structured JSON line, matching the other services' format."""
    line = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "service": "scheduler",
        "event": event,
        **fields,
    }
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


def run_settlement(facilitator: str, *, dry_run: bool = False) -> dict | None:
    """Triggers one settlement sweep.

    Args:
        facilitator: Base URL of the facilitator.
        dry_run: Compute batches without creating Payment Links.

    Returns:
        The facilitator's response, or None if it could not be reached.
    """
    try:
        response = httpx.post(
            f"{facilitator.rstrip('/')}/settle-batch",
            json={"dryRun": dry_run},
            timeout=60.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Never fatal. A facilitator that is briefly down should delay
        # settlement, not lose it — the commitments are still in the ledger and
        # the next run picks them up.
        log("settlement_unreachable", error=str(exc), facilitator=facilitator)
        return None

    result = response.json()
    batches = result.get("batches", [])

    if not batches:
        log("settlement_noop", settleDate=result.get("settleDate"), note="nothing pending")
        return result

    created = [b for b in batches if b.get("status") == "created"]
    failed = [b for b in batches if b.get("status") == "failed"]
    total = sum(b.get("totalPaise", 0) for b in batches)
    commitments = sum(b.get("commitmentCount", 0) for b in batches)

    log(
        "settlement_run",
        dryRun=dry_run,
        settleDate=result.get("settleDate"),
        batches=len(batches),
        created=len(created),
        failed=len(failed),
        commitments=commitments,
        totalPaise=total,
        paymentLinks=[b.get("paymentLinkId") for b in created if b.get("paymentLinkId")],
    )

    for batch in failed:
        log(
            "settlement_batch_failed",
            agentId=batch.get("agentId"),
            totalPaise=batch.get("totalPaise"),
            error=batch.get("error"),
            note="commitments remain pending and retry next run",
        )

    return result


def seconds_until(hhmm: str) -> float:
    """Seconds from now until the next occurrence of a UTC HH:MM.

    Args:
        hhmm: Target time as "23:30".

    Returns:
        Delay in seconds.

    Raises:
        ValueError: If the time cannot be parsed.
    """
    hour, minute = (int(part) for part in hhmm.split(":"))
    now = datetime.now(UTC)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()



def sweep_expired_reservations() -> int:
    """Returns authority held by requests that never finished.

    A reservation is taken at /verify and resolved at /settle. Almost always
    those are microseconds apart inside one HTTP request — but a publisher
    handler that crashes, a network drop between the two calls, or a killed
    process leaves the hold in place with nothing coming to capture or release
    it.

    Without a sweep that authority is stranded until someone notices, and for a
    prefunded agent that means one dead request quietly reduces its balance for
    the rest of the day. Fixing it manually would require knowing it happened.

    Runs against the ledger directly rather than over HTTP: it is
    housekeeping, needs no facilitator process, and should still work when the
    service is the thing that died.

    Returns:
        How many holds were returned. Logged even when zero, so "the sweeper
        is running and finding nothing" is distinguishable from "the sweeper
        is not running".
    """
    from ledger import Ledger

    ledger = Ledger(os.getenv("LEDGER_DSN") or os.getenv("LEDGER_DB_PATH", "./data/ledger.db"))
    swept = ledger.expire_stale_reservations()
    log(
        "reservation_sweep",
        swept=swept,
        note="expired holds returned to available authority" if swept else "nothing stale",
    )
    return swept


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Runs x402 batch settlement on a schedule.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[-1],
    )
    parser.add_argument("--facilitator", default=DEFAULT_FACILITATOR)
    parser.add_argument("--once", action="store_true", help="Settle once and exit.")
    parser.add_argument(
        "--interval", type=float, default=None, help="Settle every N seconds."
    )
    parser.add_argument(
        "--daily-at", default=None, help="Settle once a day at this UTC time, e.g. 23:30."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would settle without charging."
    )
    parser.add_argument(
        "--no-sweep",
        action="store_true",
        help="Skip returning expired authority holds before settling.",
    )
    args = parser.parse_args()

    def cycle() -> object:
        """One pass: return stranded authority, then settle.

        Sweeping FIRST is deliberate. An expired hold is authority the agent
        should have back, and settling before returning it would compute a
        batch while some of that agent's balance is still spoken for by a
        request that died — making the run's own view of available authority
        wrong.
        """
        if not args.no_sweep:
            sweep_expired_reservations()
        return run_settlement(args.facilitator, dry_run=args.dry_run)

    if args.once:
        result = cycle()
        return 0 if result is not None else 1

    if args.interval:
        log("scheduler_started", mode="interval", intervalSeconds=args.interval)
        try:
            while True:
                cycle()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log("scheduler_stopped")
            return 0

    if args.daily_at:
        log("scheduler_started", mode="daily", at=f"{args.daily_at} UTC")
        try:
            while True:
                delay = seconds_until(args.daily_at)
                log("scheduler_sleeping", seconds=round(delay), nextRun=args.daily_at)
                time.sleep(delay)
                cycle()
                # Guard against the clock landing back inside the same minute
                # and firing a second time.
                time.sleep(61)
        except KeyboardInterrupt:
            log("scheduler_stopped")
            return 0

    parser.error("choose one of --once, --interval, or --daily-at")
    return 2


if __name__ == "__main__":
    sys.exit(main())

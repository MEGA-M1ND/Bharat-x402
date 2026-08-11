"""Bharat x402 — the publisher's daily revenue digest.

Reads the facilitator's ledger and renders the message a publisher would
actually receive: how many AI agents fetched their content today, what it
earned, and which Razorpay Payment Links were created to collect it.

Formatted as a WhatsApp message on purpose. A regional publisher is not going
to log into a dashboard to check whether crawlers paid them — they will read a
message on their phone. Razorpay's Agent Studio already delivers merchant
reports this way, so this is the shape the output would take if this project
plugged into it.

Reads SQLite directly rather than calling the facilitator's HTTP API, so the
report still works when the service is down — a reporting tool that needs the
system it reports on to be healthy is not much of a reporting tool.

Usage:
    python daily_summary.py                    # today, WhatsApp style
    python daily_summary.py --date 2026-08-11  # a specific day
    python daily_summary.py --json             # machine-readable
    python daily_summary.py --plain            # no emoji, for a terminal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from pathlib import Path

# The facilitator owns the ledger schema, so reuse its accessor rather than
# duplicating table knowledge here — two definitions of "what a commitment is"
# would drift within a week.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "facilitator"))

from ledger import Ledger, today_utc  # noqa: E402
from razorpay_client import FeeModel, format_paise  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = os.getenv(
    "LEDGER_DB_PATH", str(_REPO_ROOT / "facilitator" / "data" / "ledger.db")
)

MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def display_width(text: str) -> int:
    """Columns `text` occupies in a terminal, not characters.

    `len()` is wrong for this output: emoji render two columns wide, and the
    variation selector that turns ⚠ into an emoji is itself zero-width. Padding
    by character count leaves the message frame visibly ragged on exactly the
    lines that matter most.

    Args:
        text: String to measure.

    Returns:
        Approximate column count.
    """
    width = 0
    previous = ""
    for char in text:
        # U+FE0F promotes the preceding glyph to emoji presentation, which is
        # two columns wide. It contributes no width of its own.
        if char == "️":
            if unicodedata.east_asian_width(previous) not in ("W", "F"):
                width += 1
            continue
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        previous = char
    return width


def pad(text: str, columns: int) -> str:
    """Left-aligns `text` to `columns` display columns."""
    return text + " " * max(columns - display_width(text), 0)


def pretty_date(iso_date: str) -> str:
    """Formats YYYY-MM-DD as '11 Aug 2026'."""
    try:
        year, month, day = iso_date.split("-")
        return f"{int(day)} {MONTHS[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return iso_date


def render_whatsapp(
    summary: dict, *, publisher: str, micro: dict, plain: bool = False
) -> str:
    """Renders the digest as a WhatsApp message.

    Uses WhatsApp's own formatting — *bold* and ```monospace``` — so this could
    be handed to the Business API unchanged.

    Args:
        summary: Output of `Ledger.daily_summary`.
        publisher: Name to head the message with.
        micro: Output of `Ledger.revenue_below` for the gateway minimum.
        plain: Drop emoji, for terminals and CI logs.

    Returns:
        The message body.
    """

    def icon(emoji: str) -> str:
        return "" if plain else f"{emoji} "

    date_label = pretty_date(summary["settleDate"])
    requests = summary["requests"]
    total = summary["totalPaise"]
    agents = summary["byAgent"]
    batches = summary["batches"]

    lines: list[str] = []
    lines.append(f"*{publisher}*")
    lines.append(f"Daily agent revenue · {date_label}")
    lines.append("")

    if requests == 0:
        lines.append("No AI agents fetched your paid content today.")
        lines.append("")
        lines.append("_Nothing to settle._")
        return "\n".join(lines)

    lines.append(f"{icon('💰')}*{format_paise(total)}* earned from AI crawlers")
    lines.append(f"{icon('📊')}{requests} paid requests · {len(agents)} agents")
    if summary["rejectedPayments"]:
        lines.append(
            f"{icon('🛡')}{summary['rejectedPayments']} payment"
            f"{'s' if summary['rejectedPayments'] != 1 else ''} rejected"
        )
    lines.append("")

    # --- who paid ---------------------------------------------------------
    lines.append("*Top payers*")
    for rank, agent in enumerate(agents[:5], start=1):
        lines.append(
            f"{rank}. {agent['agent_id']}"
            f" — {agent['requests']} req · {format_paise(agent['total_paise'])}"
        )
    if len(agents) > 5:
        remaining = sum(a["total_paise"] for a in agents[5:])
        lines.append(f"_+{len(agents) - 5} more · {format_paise(remaining)}_")
    lines.append("")

    # --- how it was collected --------------------------------------------
    created = [b for b in batches if b["status"] == "created"]
    failed = [b for b in batches if b["status"] == "failed"]
    pending_requests = sum(a["pending"] for a in agents)

    lines.append("*Settlement*")
    if created:
        collected = sum(b["total_paise"] for b in created)
        lines.append(
            f"{icon('✅')}{len(created)} Payment Link"
            f"{'s' if len(created) != 1 else ''} · {format_paise(collected)}"
        )
        lines.append("```")
        for batch in created[:5]:
            lines.append(
                f"{batch['payment_link_id']}"
                f"  {format_paise(batch['total_paise']):>9}"
                f"  {batch['commitment_count']:>3} req"
            )
        if len(created) > 5:
            lines.append(f"…and {len(created) - 5} more")
        lines.append("```")
        # One link is enough to show; five ids without URLs are just noise.
        first_url = next((b["payment_link_url"] for b in created if b["payment_link_url"]), None)
        if first_url:
            lines.append(f"{icon('🔗')}{first_url}")
    else:
        lines.append(f"{icon('⏳')}Nothing settled yet today.")

    if failed:
        lines.append(
            f"{icon('⚠️')}{len(failed)} batch{'es' if len(failed) != 1 else ''} failed — "
            "commitments stay pending and retry on the next run."
        )
    if pending_requests:
        lines.append(f"_{pending_requests} requests still awaiting the next batch._")
    lines.append("")

    # --- why this works at all -------------------------------------------
    model = FeeModel.from_env()
    charges = len(created) or 1
    lines.append("*Why batching*")
    lines.append(
        f"{icon('⚡')}{requests} agent requests collected in {charges} "
        f"gateway charge{'s' if charges != 1 else ''}."
    )

    # Only make the "impossible per-request" claim when the data supports it.
    if micro["totalPaise"]:
        lines.append(
            f"{icon('🚧')}{format_paise(micro['totalPaise'])} of this could not have "
            f"been collected at all per-request — {micro['count']} charges sit under "
            f"Razorpay's {format_paise(model.minimum_charge_paise)} minimum."
        )
    else:
        lines.append(
            f"_Every charge here clears the {format_paise(model.minimum_charge_paise)} "
            "gateway minimum, so batching saves API calls and reconciliation rather "
            "than fees. Below that floor it is the difference between getting paid "
            "and not._"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publisher-facing daily revenue digest, read from the x402 ledger."
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD. Defaults to today (UTC).")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to the ledger database.")
    parser.add_argument(
        "--publisher", default="Bharat News Network", help="Name to head the message with."
    )
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead.")
    parser.add_argument("--plain", action="store_true", help="No emoji.")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No ledger at {args.db}.", file=sys.stderr)
        print(
            "Start the facilitator and run demo-agent/crawler_agent.py first.", file=sys.stderr
        )
        return 1

    ledger = Ledger(args.db)
    settle_date = args.date or today_utc()
    summary = ledger.daily_summary(settle_date)

    # Computed from the commitment rows, not from per-agent averages — see
    # Ledger.revenue_below for why that distinction matters.
    minimum = FeeModel.from_env().minimum_charge_paise
    micro = ledger.revenue_below(minimum, settle_date)

    if args.json:
        print(json.dumps({**summary, "belowGatewayMinimum": micro}, indent=2))
        return 0

    # Windows consoles default to cp1252 and cannot encode ₹ or emoji.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover - depends on the terminal
        pass

    message = render_whatsapp(
        summary, publisher=args.publisher, micro=micro, plain=args.plain
    )

    # Framed like a phone screen, because that is where this would land.
    width = 56
    print()
    print("┌" + "─" * width + "┐")
    for line in message.split("\n"):
        for chunk in _chunk(line, width - 2):
            print("│ " + pad(chunk, width - 2) + " │")
    print("└" + "─" * width + "┘")
    print()
    return 0


def _chunk(line: str, width: int) -> list[str]:
    """Wraps a line to a display width, preserving whole words where possible."""
    if display_width(line) <= width:
        return [line]
    words, out, current = line.split(" "), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if display_width(candidate) > width and current:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


if __name__ == "__main__":
    sys.exit(main())

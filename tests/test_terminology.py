"""Guards the vocabulary that Phase 1 corrected.

Documentation rots differently from code: nothing fails when a README sentence
becomes false, so it stays false. These tests give the wrong words a way to
break the build.

Three classes of error are pinned here, all of which were actually present in
this repository before Phase 1:

  1. **Wrong provenance.** x402 was authored by Coinbase. Calling it
     "Cloudflare's x402" is a factual error about who built the thing this
     project extends, and it is the kind of error a reviewer notices first.

  2. **Accrual described as revenue.** The dashboard's headline tile said
     "earned" over a figure that was purely committed, and the publisher's
     digest summed *created* Payment Links into a variable named `collected`.
     Both overstate revenue by the entire uncollected balance.

  3. **Unscoped platform claims.** "Razorpay's ₹1 minimum" generalises an
     observation about one API — Payment Links — to a company's whole product
     surface. The narrower claim is the one the evidence supports.

The checks are deliberately blunt substring/regex scans over tracked text.
A cleverer implementation would be harder to trust and harder to fix when it
fires. Where a phrase is legitimate in context — this file quotes every banned
phrase, and docs/gap-analysis.md documents them as corrections — the file is
allowlisted explicitly rather than the pattern being loosened.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files whose job is to *discuss* the wrong wording. Allowlisting whole files
# is coarse, but the alternative — trying to detect "is this a quotation?" —
# is exactly the kind of cleverness that makes a failing test hard to act on.
DISCUSSES_THE_WRONG_WORDING = {
    "tests/test_terminology.py",
    "docs/gap-analysis.md",
    "docs/implementation-plan.md",
    "docs/domain-model.md",
    "docs/research-sources.md",
    "docs/adr/0001-negotiation-vs-authority-vs-collection.md",
    "docs/adr/0002-deferred-collection-and-credit-risk.md",
    "docs/adr/0003-ed25519-agent-acceptance.md",
}


def tracked_text_files() -> list[Path]:
    """Every tracked file worth scanning, as absolute paths.

    Uses `git ls-files` rather than a glob so that build output, virtualenvs,
    and node_modules cannot drift into scope — the scan should cover exactly
    what a reviewer would see on GitHub.
    """
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    keep = {".md", ".py", ".js", ".html", ".css", ".json", ".yml", ".yaml", ".sql", ".example"}
    paths = []
    for rel in out:
        if rel in DISCUSSES_THE_WRONG_WORDING:
            continue
        path = REPO_ROOT / rel
        if path.suffix in keep and path.is_file():
            paths.append(path)
    return paths


def scan(pattern: re.Pattern[str]) -> list[str]:
    """Returns `path:line: text` for every match, for a readable failure."""
    hits: list[str] = []
    for path in tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(REPO_ROOT).as_posix()
                hits.append(f"{rel}:{number}: {line.strip()}")
    return hits


class TestProvenance:
    """x402 is Coinbase's work. Cloudflare co-founded the Foundation."""

    def test_x402_is_not_attributed_to_cloudflare(self):
        # Catches the possessive claim — "Cloudflare's x402", "Cloudflare's own
        # x402 protocol" — while allowing the accurate constructions that have
        # to survive: "Cloudflare's x402-based product" (the Monetization
        # Gateway genuinely is theirs and genuinely is x402-based) and
        # "the x402 Foundation", which Cloudflare co-founded.
        pattern = re.compile(
            r"Cloudflare(?:'s|’s)\s+(?:own\s+)?x402\b(?!\s*-based)(?!\s+Foundation)",
            re.IGNORECASE,
        )
        hits = scan(pattern)
        assert not hits, (
            "x402 was authored by Coinbase, not Cloudflare. Cloudflare co-founded the x402 "
            "Foundation with Coinbase and separately ships Pay Per Crawl, which is a different "
            "mechanism (crawler-* headers + Web Bot Auth). See docs/research-sources.md.\n  "
            + "\n  ".join(hits)
        )

    def test_pay_per_crawl_is_not_called_an_x402_implementation(self):
        pattern = re.compile(
            r"implement\w*\s+(?:of\s+)?(?:Cloudflare(?:'s|’s)?\s+)?Pay[ -]Per[ -]Crawl",
            re.IGNORECASE,
        )
        hits = scan(pattern)
        assert not hits, (
            "Pay Per Crawl is not x402 and this project does not implement it. It uses its own "
            "crawler-price/crawler-charged headers and Web Bot Auth. It motivates the use case.\n  "
            + "\n  ".join(hits)
        )

    def test_cloudflare_is_never_described_as_required(self):
        pattern = re.compile(
            r"(?:requires?|depends?\s+on|needs?)\s+Cloudflare|Cloudflare\s+is\s+required",
            re.IGNORECASE,
        )
        # "nothing here depends on Cloudflare at runtime" contains the phrase
        # while asserting its opposite. Negation is checked on the line rather
        # than baked into the pattern — a variable-width lookbehind is not
        # available, and the explicit list is easier to read than the regex
        # gymnastics that would replace it.
        negated = re.compile(r"\b(?:no|nothing|never|not|without|optional)\b", re.IGNORECASE)
        hits = [h for h in scan(pattern) if not negated.search(h)]
        assert not hits, (
            "Cloudflare is an optional publisher-edge integration, never a runtime "
            "dependency.\n  " + "\n  ".join(hits)
        )


class TestAccrualIsNotRevenue:
    """A commitment is a receivable. Only a gateway confirmation is money."""

    def test_no_ui_or_report_labels_an_accrued_figure_as_earned(self):
        # "earned" is the specific word that was on the dashboard tile and in
        # the WhatsApp digest, over a committed total.
        #
        # The word wrapped in quotes is a *mention* — the comments explaining
        # why it was removed have to be able to name it. Bare, it is a *use*,
        # and a use is the assertion this test exists to prevent.
        pattern = re.compile(r"(?<![\"'“‘])\bearned\b(?![\"'”’])", re.IGNORECASE)
        hits = scan(pattern)
        assert not hits, (
            "'earned' claims money arrived. Accrued receivables are 'accrued'; only "
            "gateway-confirmed money is 'collected'. See docs/domain-model.md.\n  "
            + "\n  ".join(hits)
        )

    def test_created_payment_links_are_not_summed_into_a_collected_variable(self):
        # The exact bug: `collected = sum(b["total_paise"] for b in created)`.
        # A created link is an invoice; naming its total `collected` asserts
        # that asking for money is the same as receiving it.
        pattern = re.compile(r"\bcollected\s*=\s*sum\(", re.IGNORECASE)
        hits = scan(pattern)
        assert not hits, (
            "A created Payment Link is an invoice, not a receipt. Name the sum of created "
            "batches 'billed'.\n  " + "\n  ".join(hits)
        )

    def test_a_commitment_is_never_called_proof_of_payment(self):
        pattern = re.compile(
            r"commitment\s+(?:is\s+)?(?:a\s+)?proof\s+of\s+payment"
            r"|proof\s+that\s+(?:the\s+)?(?:money|funds|rupees)\s+moved",
            re.IGNORECASE,
        )
        hits = scan(pattern)
        assert not hits, (
            "A signed commitment is evidence that a key accepted a quote. It is not proof "
            "that money moved. See docs/adr/0003-ed25519-agent-acceptance.md.\n  "
            + "\n  ".join(hits)
        )


class TestClaimsAreScoped:
    """Observed API behaviour is not a platform-wide guarantee."""

    def test_the_rupee_floor_is_attributed_to_payment_links(self):
        """Any mention of the ₹1 minimum must name the product it applies to.

        The official docs support "minimum 100 for INR" *for Payment Links*.
        They do not support a claim about every Razorpay product, about UPI, or
        about NPCI rails — and Reserve Pay's own ₹10,000 block ceiling is a
        different limit entirely.
        """
        # Two shapes, because the literal figure is not always in the source.
        #
        # The second pattern exists because of a real miss: the console builds
        # its economics copy at runtime — `Razorpay's ${paise(min)} minimum` —
        # so the string "₹1.00 minimum" never appears in any file, and the
        # first pattern sailed straight past an unscoped claim that was
        # rendering on the page. A screenshot caught it; this test now does.
        mentions_floor = re.compile(
            r"(?:₹\s?1(?:\.00)?|\b100\s+paise\b)\s*(?:gateway\s+)?(?:floor|minimum)"
            r"|minimum\s+(?:charge\s+)?(?:of\s+)?₹\s?1\b"
            # `(?![A-Za-z])` keeps this off identifiers — `RazorpayConfigError`
            # is a class name, not a claim about a payment product.
            r"|Razorpay(?:'s|’s)?(?![A-Za-z])[^.\n]{0,60}?\b(?:floor|minimum)\b",
            re.IGNORECASE,
        )
        scopes_it = re.compile(r"payment\s*links?", re.IGNORECASE)

        unscoped: list[str] = []
        for path in tracked_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            lines = text.splitlines()
            for number, line in enumerate(lines, start=1):
                if not mentions_floor.search(line):
                    continue
                # Scope may legitimately appear a couple of lines either side —
                # prose wraps, and tables put the qualifier in a neighbouring
                # cell. Three lines of context is enough to be fair without
                # letting an unqualified claim pass on a distant mention.
                window = "\n".join(lines[max(0, number - 4) : number + 3])
                if not scopes_it.search(window):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    unscoped.append(f"{rel}:{number}: {line.strip()}")

        assert not unscoped, (
            "The ₹1 floor is a Razorpay *Payment Links* limit. Name the product near the "
            "claim — it does not generalise to every Razorpay product, to UPI, or to NPCI. "
            "See docs/research-sources.md.\n  " + "\n  ".join(unscoped)
        )

    def test_webhook_retries_are_not_described_as_unbounded(self):
        """Razorpay retries with backoff for 24 hours, then disables the hook.

        "Retries until it gets a 2xx" reads as forever, and a design that
        assumes a webhook will eventually arrive is a design with no
        reconciliation path.
        """
        pattern = re.compile(
            r"retri\w+\s+(?:a\s+webhook\s+)?until\s+(?:it\s+)?(?:gets?|receives?)\s+"
            r"(?:a\s+)?2xx(?!.{0,80}24\s*hours)",
            re.IGNORECASE,
        )
        hits = scan(pattern)
        assert not hits, (
            "Razorpay retries with exponential backoff for 24 hours and then disables the "
            "webhook. Unbounded retry is not the documented behaviour, and assuming it "
            "removes the reason reconciliation exists.\n  " + "\n  ".join(hits)
        )


class TestMonetaryTypes:
    """Integer paise, everywhere."""

    @pytest.mark.parametrize(
        "module",
        ["facilitator/ledger.py", "facilitator/main.py", "facilitator/razorpay_client.py"],
    )
    def test_no_float_conversion_of_a_paise_value(self, module: str):
        """`float(...)` must never touch something named paise or amount.

        A float in a monetary path loses precision silently and the error
        compounds across aggregation, which is exactly the sort of bug that
        surfaces as a one-paisa discrepancy nobody can explain a month later.
        """
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        pattern = re.compile(r"float\(\s*[^)]*(?:paise|amount)", re.IGNORECASE)
        hits = [line.strip() for line in text.splitlines() if pattern.search(line)]
        assert not hits, (
            f"{module} converts a monetary value to float. Money is integer paise "
            f"end to end.\n  " + "\n  ".join(hits)
        )

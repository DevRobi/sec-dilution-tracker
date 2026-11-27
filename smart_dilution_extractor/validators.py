"""
Validation + confidence scoring.

Every extracted event is checked for internal consistency and (where possible)
cross-checked against the deterministic XBRL facts. The result is a 0..1
confidence score plus human-readable flags. This is the safety net that catches
small-model mistakes — e.g. a hallucinated price that still "looks" plausible
will usually fail the shares*price == proceeds arithmetic test.
"""
from __future__ import annotations

import re

from . import config
from .models import DilutionEvent

# Non-dilutive CONTEXT guard. A dollar figure sitting in government/commercial
# CONTRACT language is revenue/business scale, not a securities issuance — yet
# the LLM (and occasionally rules) can misread it as an offering. The classic
# case: SIDU's "...SHIELD indefinite-delivery/indefinite-quantity (IDIQ)
# contract with a total IDIQ ceiling of $151 billion" surfaced as $151B of
# dilution. We DROP any event whose verbatim source quote matches this, since
# such phrasing never describes an equity/convertible raise. Patterns are kept
# specific (multi-word / agency terms) so a real offering snippet — which talks
# about "shares", "offering price", "proceeds" — is never caught.
_NONDILUTIVE_CONTEXT = re.compile(
    r"\bIDIQ\b|indefinite[\s-]delivery|indefinite[\s-]quantity|"
    r"(?:contract|task|delivery|order)\s+ceiling|ceiling of\s+\$|"
    r"contract award|awardee|task order|delivery order|\bbacklog\b|"
    r"firm[\s-]fixed[\s-]price|Missile Defense Agency|Department of Defense|"
    r"prime contract|government contract|purchase order",
    re.IGNORECASE,
)


def is_nondilutive_context(ev: DilutionEvent) -> bool:
    """True when the event's source quote is contract/award/backlog language."""
    return bool(_NONDILUTIVE_CONTEXT.search(ev.source_quote or ""))


# Cap-table TOTALS guard. A prospectus states the share count BEFORE and AFTER
# the raise ("Common stock to be outstanding after this offering: 395,444,270
# shares (or 395,444,270 if the Warrants are exercised in full)", "...outstanding
# prior to this offering", "269,600,132 shares ... outstanding as of June 30,
# 2025"). These are the whole cap table, NOT a new issuance — the "after" number
# in particular is the LARGEST share figure in the filing, so when the LLM
# mistakes it for an event it dwarfs the real dilution (IONQ surfaced 395M shares
# of "dilution" that were just the post-offering total). We DROP any event whose
# verbatim quote is one of these totals.
# DEFINITIVE total phrases — these describe the whole cap table and NOTHING
# else, so they drop unconditionally (even when they name "Warrants", as the
# fully-diluted footnote does: "...after this offering (or 395,444,270 shares if
# the Warrants are exercised in full)").
_DEFINITIVE_TOTAL = re.compile(
    # "...[to be] outstanding [immediately] after/prior to/before/following ..."
    r"(?:to be\s+)?outstanding\s+(?:immediately\s+)?(?:after|prior to|before|following)\b"
    r"|\bto be outstanding\b"
    # the fully-diluted footnote: "(or 395,444,270 shares if the Warrants ...)"
    r"|\(or\s+[\d,.]+\s+shares?\b",
    re.IGNORECASE,
)
# WEAK total phrases — a bare point-in-time count ("269,600,132 shares ...
# outstanding as of June 30, 2025", "330,928,070 shares as of October 7, 2025").
# These ALSO appear inside genuine instrument quotes ("options outstanding as of
# <date>"), so they only drop when no real-instrument signal is present.
_WEAK_TOTAL = re.compile(
    r"\boutstanding\s+as of\b"
    r"|\bshares\s+(?:of (?:our )?common stock\s+)?as of\b",
    re.IGNORECASE,
)
# Signals of a genuine POTENTIAL-dilution instrument (options/warrants/RSUs
# issuable, shares reserved, shares offered). Their presence rescues a WEAK match.
_REAL_INSTRUMENT_HINT = re.compile(
    r"issuable|reserved for issuance|"
    r"upon (?:the )?(?:exercise|vesting|conversion)|"
    r"\bwarrant|\boption|restricted stock|\bRSU\b|convertible|"
    r"offered by us|we are offering|offering price|purchase price",
    re.IGNORECASE,
)


def is_shares_outstanding_context(ev: DilutionEvent) -> bool:
    """True when the quote is a total shares-outstanding figure, not an issuance."""
    q = ev.source_quote or ""
    if _DEFINITIVE_TOTAL.search(q):
        return True
    if _WEAK_TOTAL.search(q) and not _REAL_INSTRUMENT_HINT.search(q):
        return True
    return False


_FULL_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")


def ground_shares_in_quote(ev: DilutionEvent) -> str | None:
    """
    Check the extracted share count against the verbatim source quote.

    Small models misread long numbers (SIDU: quote says 2,349,675 warrants, the
    LLM returned 2,339,675). Because the DB fingerprint buckets on the share
    count, a misread lands as a NEW row next to the correct one -> duplicate.
    If the exact figure is in the quote it is grounded; if exactly one
    comma-formatted number in the quote is within 1%, the extraction is
    corrected to it; otherwise it is flagged. Quotes without full numerals
    ("2.2 million shares") are left alone. Returns the flag, or None.
    """
    if not ev.shares or not ev.source_quote:
        return None
    nums = {int(n.replace(",", "")) for n in _FULL_NUMBER.findall(ev.source_quote)}
    if not nums:
        return None
    target = round(ev.shares)
    if target in nums:
        return "shares_grounded"
    close = [n for n in nums if abs(n - target) / max(n, target) <= 0.01]
    if len(close) == 1:
        ev.shares = float(close[0]) if isinstance(ev.shares, float) else close[0]
        return "shares_corrected_from_quote"
    return "shares_not_in_quote"


def validate_event(ev: DilutionEvent, xbrl_proceeds: list[float] | None = None) -> DilutionEvent:
    flags: list[str] = []
    score = 0.5  # neutral prior

    # 0. Grounding: the share count must come from the quote it cites.
    grounding = ground_shares_in_quote(ev)
    if grounding:
        flags.append(grounding)
        score += {"shares_grounded": 0.05, "shares_corrected_from_quote": -0.05,
                  "shares_not_in_quote": -0.15}[grounding]

    s, p, g = ev.shares, ev.price_per_share, ev.gross_proceeds_usd

    # 1. Arithmetic coherence: shares * price ~= gross proceeds
    if s and p and g:
        implied = s * p
        err = abs(implied - g) / g if g else 1.0
        if err <= config.ARITHMETIC_TOLERANCE:
            flags.append("arithmetic_ok")
            score += 0.30
        else:
            flags.append(f"arithmetic_mismatch({err:.0%})")
            score -= 0.25
    elif (s and p) or g:
        flags.append("partial_figures")
        score += 0.05

    # 2. Magnitude sanity
    if p is not None and not (0.001 <= p <= 100000):
        flags.append("price_out_of_range")
        score -= 0.20
    if s is not None and s <= 0:
        flags.append("nonpositive_shares")
        score -= 0.20

    # 3. XBRL cross-check: does this proceeds figure match a filed XBRL fact?
    if g and xbrl_proceeds:
        for xg in xbrl_proceeds:
            if xg and abs(xg - g) / max(xg, 1) <= 0.05:
                flags.append("xbrl_confirmed")
                score += 0.25
                break

    # 4. Method prior: deterministic sources start more trusted
    if ev.extraction_method == "xbrl":
        score += 0.20
    elif ev.extraction_method == "rules":
        score += 0.05

    if ev.is_approximate:
        score -= 0.03

    ev.confidence = max(0.0, min(1.0, round(score, 3)))
    ev.validation_flags = flags
    return ev


def validate_all(events: list[DilutionEvent], xbrl_proceeds: list[float] | None = None) -> list[DilutionEvent]:
    # Drop false positives before scoring:
    #  * non-dilutive contract/award language (e.g. a government IDIQ ceiling)
    #  * cap-table TOTALS ("shares outstanding after/prior to this offering",
    #    "outstanding as of <date>") — these are the whole share count, not a
    #    new issuance, and the post-offering total is the biggest number around.
    kept = [
        e for e in events
        if not is_nondilutive_context(e) and not is_shares_outstanding_context(e)
    ]
    return [validate_event(e, xbrl_proceeds) for e in kept]

"""
Deterministic regex extraction.

SEC offering language is highly stereotyped, so a handful of patterns catch a
large share of dilution instruments with zero LLM cost and zero hallucination
risk. We cover BOTH:

  * ACTIVE dilution    — shares issued / offerings pricing or closing now.
  * POTENTIAL dilution — shelf capacity, warrants, options, convertibles, and
                         undrawn ATM capacity that could dilute later.

Anything ambiguous is left for the LLM stage. Every match keeps the verbatim
source snippet for auditability.
"""
from __future__ import annotations

import re

from .config import REGISTRATION_FORMS
from .models import DilutionEvent, EventStatus, OfferingType

# Number with optional scale word, e.g. "$50.0 million", "$1.2 billion", "$750,000"
_AMOUNT = r"\$?\s?([\d,]+(?:\.\d+)?)\s?(billion|million|thousand|bn|mm|m|k)?"

_SCALE = {
    "billion": 1e9, "bn": 1e9,
    "million": 1e6, "mm": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
}

# "5,000,000 shares ... at a price ... of $4.20 per share"  (active raise)
_SHARES_AT_PRICE = re.compile(
    r"([\d,]{5,})\s+(?:shares|common shares|ordinary shares)"
    r"[^.\n]{0,120}?\$\s?([\d.]+)\s*(?:per share|per unit)",
    re.IGNORECASE,
)
# "We are offering 2,230,000 Units at an offering price of $4.055 per Unit".
# A unit commonly contains one common share plus attached rights/warrants. The
# common-share leg is active dilution; the attached instruments are extracted
# separately below as potential dilution.
_UNITS_AT_PRICE = re.compile(
    r"(?:we\s+are\s+offering|are\s+offering|offering)\s+([\d,]{4,})\s+Units?\s+"
    r"at\s+(?:an\s+)?(?:offering\s+)?price\s+of\s+\$\s?([\d.]+)\s+per\s+Unit",
    re.IGNORECASE,
)
# Contingent overhang: "shares outstanding ... excludes: X options at exercise price $Y".
_OVERHANG_RE = re.compile(
    r"exercise price|issuable upon|upon (?:the )?exercise|reserved for issuance|"
    r"weighted[\s-]average exercise|outstanding options|does not include|excludes",
    re.IGNORECASE,
)
# "aggregate offering price of $50.0 million" / "gross proceeds of approximately $12.3 million"
_PROCEEDS = re.compile(
    r"(?:aggregate offering price|gross proceeds|net proceeds|aggregate principal amount)"
    r"\s+of\s+(?:up to\s+)?(?:approximately\s+)?" + _AMOUNT,
    re.IGNORECASE,
)
# Shelf capacity: "we may offer ... up to $200,000,000" / "may sell from time to time ... $X"
_SHELF_CAP = re.compile(
    r"(?:may (?:offer|sell|issue)|offer and sell|register(?:ed)?)"
    r"[^.\n]{0,160}?(?:up to|aggregate (?:offering )?(?:price|amount) of)\s+" + _AMOUNT,
    re.IGNORECASE,
)
# Warrant tranche: "warrants to purchase up to 5,000,000 shares" / "5,000,000 pre-funded warrants"
_WARRANT_SHARES = re.compile(
    r"(?:warrants?\s+to\s+purchase\s+(?:up to\s+)?([\d,]{4,})"
    r"|([\d,]{4,})\s+(?:pre-funded\s+)?warrants?)",
    re.IGNORECASE,
)
# Unit offerings may include Series A/B "Rights" rather than warrants. They are
# economically similar potential dilution: each right can later become one
# common share at its own exercise price.
_UNITS_WITH_SERIES_RIGHTS = re.compile(
    r"([\d,]{4,})\s+Units?\b"
    r"(?P<body>(?=.{0,6000}?Series\s+A\s+Rights?)(?=.{0,6000}?Series\s+B\s+Rights?).{0,6000})",
    re.IGNORECASE | re.DOTALL,
)
_SERIES_RIGHT_PAIR = re.compile(
    r"([\d,]{4,})\s+(?:Subscribed\s+)?Series\s+A\s+Rights?"
    r"[^$]{0,220}?\$\s?([\d.]+)\s+per\s+share"
    r".{0,300}?"
    r"([\d,]{4,})\s+(?:Subscribed\s+)?Series\s+B\s+Rights?"
    r"[^$]{0,220}?\$\s?([\d.]+)\s+per\s+share",
    re.IGNORECASE | re.DOTALL,
)
# Option pool: "options exercisable for up to 5,827,544 common shares at ... $1.93"
# (no 'per share' follows the price, so _SHARES_AT_PRICE misses these on purpose).
_OPTIONS_POOL = re.compile(
    r"options?\s+(?:exercisable|outstanding|issuable)[^.\n]{0,60}?"
    r"(?:for|to purchase)?\s*(?:up to\s+)?([\d,]{5,})\s+(?:common\s+)?shares"
    r"(?:[^.\n]{0,60}?\$\s?([\d.]+))?",
    re.IGNORECASE,
)
# Convertible principal: "$50,000,000 aggregate principal amount of ... convertible notes"
_CONVERTIBLE = re.compile(
    r"\$?\s?([\d,]+(?:\.\d+)?)\s?(billion|million|thousand|bn|mm|m|k)?"
    r"[^.\n]{0,40}?(?:aggregate principal amount)?[^.\n]{0,40}?convertible",
    re.IGNORECASE,
)
_CONVERT_INTO = re.compile(
    r"convertible into\s+(?:up to\s+)?([\d,]{4,})\s+(?:shares|common shares)",
    re.IGNORECASE,
)

_ATM_RE = re.compile(r"at[\s-]the[\s-]market|atm program|atm facility|sales agreement", re.IGNORECASE)
_PIPE_RE = re.compile(r"private placement|registered direct|securities purchase agreement", re.IGNORECASE)
_SHELF_HINT = re.compile(r"shelf|from time to time|we may offer|may sell", re.IGNORECASE)
_APPROX_RE = re.compile(r"approximately|up to|estimated", re.IGNORECASE)

# --- Straight (non-convertible) debt exclusion -----------------------------
# A bond/notes FWP uses "aggregate principal amount of $X" — language that the
# _PROCEEDS / _SHELF_CAP patterns otherwise read as an equity raise. Straight
# debt is NOT dilutive (no shares issued), so we drop those snippets. Only debt
# that is explicitly convertible/exchangeable stays in (handled elsewhere).
_DEBT_RE = re.compile(
    r"senior notes|subordinated notes|\bnotes due\b|\bdebentures?\b|"
    r"fixed[\s-]rate notes|floating[\s-]rate notes|"
    r"\b\d+(?:\.\d+)?\s*%\s+(?:senior |subordinated )?notes\b|"
    r"\bunsecured notes\b|\bsecured notes\b",
    re.IGNORECASE,
)
_CONVERT_HINT = re.compile(
    r"convertible|exchangeable|conversion (?:price|rate|ratio)|"
    r"into (?:shares|common stock|ordinary shares)",
    re.IGNORECASE,
)


def _is_straight_debt(snippet: str) -> bool:
    """True for non-convertible debt language (bonds/notes), which is not dilutive."""
    return bool(_DEBT_RE.search(snippet)) and not _CONVERT_HINT.search(snippet)


def _to_float(num: str, scale: str | None) -> float:
    val = float(num.replace(",", ""))
    if scale:
        val *= _SCALE.get(scale.lower(), 1)
    return val


def _classify_active(text: str) -> OfferingType:
    if _ATM_RE.search(text):
        return OfferingType.atm
    if _PIPE_RE.search(text):
        return OfferingType.private_placement
    return OfferingType.follow_on


def _snippet(text: str, start: int, end: int, pad: int = 120) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad): end + pad]).strip()


def _series_right_price(text: str, series: str) -> float | None:
    """Find the exercise price for Series A/B rights without crossing series."""
    patterns = [
        rf"in\s+the\s+case\s+of\s+(?:the\s+)?Series\s+{series}\s+Rights?,\s*\$\s?([\d.]+)\s+per\s+share",
        rf"Series\s+{series}\s+Rights?[^.{{}}]{{0,500}}?exercise\s+price[^$]{{0,220}}?\$\s?([\d.]+)",
        rf"Series\s+{series}\s+Right,\s+exercisable[^$]{{0,700}}?\$\s?([\d.]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group(1))
    return None


def extract_rule_based(text: str, form: str = "") -> list[DilutionEvent]:
    events: list[DilutionEvent] = []
    is_shelf_form = form in REGISTRATION_FORMS
    seen_spans: list[tuple[int, int]] = []

    def overlaps(m) -> bool:
        return any(not (m.end() <= s or m.start() >= e) for s, e in seen_spans)

    # ---- 0. ACTIVE: unit offering common-share leg ---------------------------
    for m in _UNITS_AT_PRICE.finditer(text):
        units = float(m.group(1).replace(",", ""))
        price = float(m.group(2))
        if units < 1000 or price <= 0:
            continue
        snip = _snippet(text, m.start(), m.end(), pad=180)
        seen_spans.append((m.start(), m.end()))
        events.append(DilutionEvent(
            offering_type=_classify_active(snip),
            status=EventStatus.announced if "424" in form else EventStatus.completed,
            shares=units,
            price_per_share=price,
            gross_proceeds_usd=round(units * price, 2),
            is_approximate=bool(_APPROX_RE.search(snip)),
            source_quote=snip,
            extraction_method="rules",
        ))

    # ---- 1. ACTIVE: shares issued/offered at a stated price -------------------
    for m in _SHARES_AT_PRICE.finditer(text):
        shares = float(m.group(1).replace(",", ""))
        price = float(m.group(2))
        if shares < 1000 or price <= 0:
            continue
        snip = _snippet(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        if _OVERHANG_RE.search(snip):
            # option/warrant overhang -> POTENTIAL, contingent, no cash
            events.append(DilutionEvent(
                offering_type=OfferingType.warrant_exercise,
                status=EventStatus.proposed,
                shares=shares, price_per_share=price, gross_proceeds_usd=None,
                is_approximate=bool(_APPROX_RE.search(snip)),
                source_quote=snip, extraction_method="rules",
            ))
            continue
        # On a shelf form this priced offering is a takedown being announced.
        status = EventStatus.announced if is_shelf_form else EventStatus.completed
        events.append(DilutionEvent(
            offering_type=_classify_active(snip), status=status,
            shares=shares, price_per_share=price,
            gross_proceeds_usd=round(shares * price, 2),
            is_approximate=bool(_APPROX_RE.search(snip)),
            source_quote=snip, extraction_method="rules",
        ))

    # ---- 1b. POTENTIAL: an OPEN shelf with no stated cap (automatic shelf) ----
    # An ASR registers an "indeterminate" amount — there is no $ figure, but the
    # existence of an active shelf is itself material potential dilution.
    if is_shelf_form and _SHELF_HINT.search(text) and not _SHELF_CAP.search(text):
        m = _SHELF_HINT.search(text)
        events.append(DilutionEvent(
            offering_type=OfferingType.shelf_registration,
            status=EventStatus.registered,
            gross_proceeds_usd=None, is_approximate=True,
            source_quote=_snippet(text, m.start(), m.end(), pad=160),
            extraction_method="rules",
        ))

    # ---- 1c. POTENTIAL: outstanding option pool -------------------------------
    for m in _OPTIONS_POOL.finditer(text):
        shares = float(m.group(1).replace(",", ""))
        if shares < 1000:
            continue
        price = float(m.group(2)) if m.group(2) else None
        events.append(DilutionEvent(
            offering_type=OfferingType.warrant_exercise,
            status=EventStatus.proposed,
            shares=shares, price_per_share=price, gross_proceeds_usd=None,
            is_approximate=True,
            source_quote=_snippet(text, m.start(), m.end()),
            extraction_method="rules",
        ))
        seen_spans.append((m.start(), m.end()))

    # ---- 2. POTENTIAL: shelf registration capacity ----------------------------
    for m in _SHELF_CAP.finditer(text):
        if overlaps(m):
            continue
        amt = _to_float(m.group(1), m.group(2))
        if amt < 1e6:                         # shelves are large; ignore noise
            continue
        snip = _snippet(text, m.start(), m.end())
        if _is_straight_debt(snip):           # bond/notes shelf, not equity
            continue
        seen_spans.append((m.start(), m.end()))
        events.append(DilutionEvent(
            offering_type=OfferingType.shelf_registration,
            status=EventStatus.registered,
            gross_proceeds_usd=amt, is_approximate=True,
            source_quote=snip, extraction_method="rules",
        ))

    # ---- 3. POTENTIAL: warrants (count of shares issuable) --------------------
    for m in _WARRANT_SHARES.finditer(text):
        cnt = (m.group(1) or m.group(2) or "").replace(",", "")
        if not cnt:
            continue
        shares = float(cnt)
        if shares < 1000:
            continue
        snip = _snippet(text, m.start(), m.end())
        events.append(DilutionEvent(
            offering_type=OfferingType.warrant_exercise,
            status=EventStatus.proposed,
            shares=shares, gross_proceeds_usd=None, is_approximate=True,
            source_quote=snip, extraction_method="rules",
        ))

    # ---- 3a. POTENTIAL: Series A/B rights embedded in units ------------------
    for m in _UNITS_WITH_SERIES_RIGHTS.finditer(text):
        units = float(m.group(1).replace(",", ""))
        if units < 1000:
            continue
        body = m.group("body")
        a_price = _series_right_price(body, "A")
        b_price = _series_right_price(body, "B")
        if not (a_price and b_price):
            continue
        snip = _snippet(text, m.start(), m.end(), pad=180)
        for label, price in (
            ("Series A Right", a_price),
            ("Series B Right", b_price),
        ):
            events.append(DilutionEvent(
                offering_type=OfferingType.warrant_exercise,
                status=EventStatus.proposed,
                shares=units,
                price_per_share=price,
                gross_proceeds_usd=None,
                is_approximate=False,
                source_quote=f"{label}: {snip}",
                extraction_method="rules",
            ))

    for m in _SERIES_RIGHT_PAIR.finditer(text):
        snip = _snippet(text, m.start(), m.end(), pad=180)
        for label, shares, price in (
            ("Series A Right", float(m.group(1).replace(",", "")), float(m.group(2))),
            ("Series B Right", float(m.group(3).replace(",", "")), float(m.group(4))),
        ):
            if shares < 1000 or price <= 0:
                continue
            events.append(DilutionEvent(
                offering_type=OfferingType.warrant_exercise,
                status=EventStatus.proposed,
                shares=shares,
                price_per_share=price,
                gross_proceeds_usd=None,
                is_approximate=False,
                source_quote=f"{label}: {snip}",
                extraction_method="rules",
            ))

    # ---- 4. POTENTIAL: convertibles -------------------------------------------
    for m in _CONVERT_INTO.finditer(text):
        shares = float(m.group(1).replace(",", ""))
        snip = _snippet(text, m.start(), m.end())
        events.append(DilutionEvent(
            offering_type=OfferingType.convertible,
            status=EventStatus.proposed,
            shares=shares, gross_proceeds_usd=None, is_approximate=True,
            source_quote=snip, extraction_method="rules",
        ))

    # ---- 5. Proceeds figures (active unless on a shelf / "up to" capacity) ----
    for m in _PROCEEDS.finditer(text):
        if overlaps(m):
            continue
        amt = _to_float(m.group(1), m.group(2))
        if amt < 1e5:
            continue
        snip = _snippet(text, m.start(), m.end())
        if _is_straight_debt(snip):           # "aggregate principal amount" of notes -> not dilutive
            continue
        # "up to $X" is shelf CAPACITY only on a base registration form or when the
        # snippet itself uses shelf language ("from time to time", "may offer").
        # On a 424B prospectus supplement it's an ACTIVE takedown being priced now.
        is_capacity = is_shelf_form or bool(_SHELF_HINT.search(snip))
        if is_capacity:
            otype, status = OfferingType.shelf_registration, EventStatus.registered
        else:
            otype = _classify_active(snip)
            status = EventStatus.announced if "424" in form else EventStatus.completed
        events.append(DilutionEvent(
            offering_type=otype, status=status,
            gross_proceeds_usd=amt, is_approximate=bool(_APPROX_RE.search(snip)),
            source_quote=snip, extraction_method="rules",
        ))

    return events

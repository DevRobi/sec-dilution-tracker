"""
Pydantic data models for the dilution extraction pipeline.

These schemas serve double duty:
  1. Typed contracts between pipeline stages.
  2. The JSON schema we hand to the local LLM (Ollama) for constrained decoding,
     so the model is forced to emit exactly these fields.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OfferingType(str, Enum):
    ipo = "ipo"
    follow_on = "follow_on"          # underwritten / registered direct
    atm = "atm"                      # at-the-market program
    private_placement = "private_placement"  # PIPE / reg D
    shelf_registration = "shelf_registration"  # capacity registered, not yet drawn
    warrant_exercise = "warrant_exercise"
    convertible = "convertible"      # convertible notes/preferred
    equity_line = "equity_line"      # ELOC
    rights_offering = "rights_offering"
    other = "other"


class EventStatus(str, Enum):
    completed = "completed"          # shares actually issued / offering closed
    announced = "announced"          # priced / launched, not yet closed
    registered = "registered"        # capacity registered on a shelf only
    proposed = "proposed"            # contemplated / "may offer"


class DilutionEvent(BaseModel):
    """A single dilution-causing event extracted from one filing."""
    offering_type: OfferingType = OfferingType.other
    status: EventStatus = EventStatus.completed

    shares: Optional[float] = Field(
        default=None, description="Number of common shares issued or offered."
    )
    price_per_share: Optional[float] = Field(
        default=None, description="Offering / issuance price per share in USD."
    )
    gross_proceeds_usd: Optional[float] = Field(
        default=None, description="Gross dollar proceeds before fees, in USD."
    )
    net_proceeds_usd: Optional[float] = Field(
        default=None, description="Net dollar proceeds after underwriting discounts/fees."
    )

    event_date: Optional[str] = Field(
        default=None, description="ISO date (YYYY-MM-DD) the offering priced/closed, if stated."
    )
    is_approximate: bool = Field(
        default=False, description="True if any figure was a range or 'approximately'."
    )
    source_quote: str = Field(
        default="", description="Verbatim sentence/snippet the numbers came from (auditability)."
    )

    # --- populated by later stages, not by the LLM ---
    extraction_method: str = ""      # "xbrl" | "rules" | "llm"
    source_form: str = ""
    source_accession: str = ""
    source_url: str = ""
    filing_date: str = ""
    confidence: float = 0.0          # 0..1 after validation
    validation_flags: list[str] = Field(default_factory=list)

    def signature(self) -> tuple:
        """Coarse identity used for cross-filing dedup."""
        return (self.offering_type, round((self.gross_proceeds_usd or 0) / 1e5))

    @property
    def dilution_category(self) -> str:
        """
        Classify into the two buckets the tracker cares about:
          * "active"    — shares already issued or an offering that is pricing/
                          closing now (real, near-term share-count increase).
          * "potential" — capacity that *could* dilute later: shelf registrations,
                          outstanding warrants/options, unconverted convertibles,
                          undrawn ATM capacity.
        Driven by status, which the rules/LLM stages set per instrument type.
        """
        if self.status in (EventStatus.registered, EventStatus.proposed):
            return "potential"
        # Instrument types that are inherently contingent unless explicitly closed.
        if self.offering_type in (
            OfferingType.shelf_registration,
            OfferingType.warrant_exercise,
            OfferingType.convertible,
            OfferingType.equity_line,
        ) and self.status != EventStatus.completed:
            return "potential"
        return "active"


class LLMExtraction(BaseModel):
    """Top-level schema the LLM must return: a list of events + meta."""
    events: list[DilutionEvent] = Field(default_factory=list)
    overall_confidence: str = Field(
        default="medium", description="one of: high, medium, low"
    )
    notes: str = Field(default="", description="Any ambiguity the model wants to flag.")


class FilingRef(BaseModel):
    """Pointer to one filing on EDGAR."""
    accession: str
    form: str
    filing_date: str
    primary_doc: str
    description: str = ""
    url: str = ""
    cik: str = ""

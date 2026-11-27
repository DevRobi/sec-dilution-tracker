"""Legacy per-chunk dilution extraction (sync.py offering scanner), now on Claude."""
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from app.services.ai.llm import complete_json

logger = logging.getLogger(__name__)


class DilutionEventHelper(BaseModel):
    category: str
    amount: Optional[float] = None
    amount_text: str = ""
    status: str
    description: str
    source_filing: str = ""


class DilutionExtractionResult(BaseModel):
    events: List[DilutionEventHelper] = Field(default_factory=list)


# Structured-outputs schema: additionalProperties:false and every key required
# (nullable where the value may be absent).
EVENTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "amount", "amount_text", "status", "description", "source_filing"],
                "properties": {
                    "category": {"type": "string"},
                    "amount": {"type": ["number", "null"]},
                    "amount_text": {"type": "string"},
                    "status": {"type": "string", "enum": ["Active", "Potential"]},
                    "description": {"type": "string"},
                    "source_filing": {"type": "string"},
                },
            },
        }
    },
}

SYSTEM = """You are a senior financial analyst auditing SEC filings.
Extract EXACT details of stock dilution events from the filing text you are given.

RULES FOR EXTRACTION:

1. **CATEGORY**:
   - 'ATM Offering': "At-the-market", "Equity Distribution", "Sales Agreement".
   - 'Shelf Offering': General registration (S-3, F-3) without a specific immediate sale agreement.
   - 'Convertible Note': Debt that converts to stock.
   - 'Warrants / SAFEs': Options/Warrants attached to deals.
   - 'Series / Direct': Registered Direct, PIPE, Private Placement.
   - 'SBC / Stock-Based Comp': Employment inducement, RSUs, Options plans.

2. **STATUS (Crucial)**:
   - **'Active'**: Definitive agreement to sell, or evidence of recent past sales. 
     - Valid examples: "We sold X shares", "Entered into Sales Agreement to sell up to $X", "Closed the offering".
     - If an ATM program is open and effective, it is 'Active'.
   - **'Potential'**: General capacity or future possibility.
     - Valid examples: "We may sell", "Shelf registration declared effective" (without a specific sales agent agreement yet), "Authorizing X shares".
     
3. **AMOUNT** (Strict Numeric):
   - Extract the **MAXIMUM** dollar value authorized, remaining, or referenced.
   - `amount`: Must be a float (e.g., 50000000 for $50M). 
     - **CRITICAL**: If specific units like "in millions" are mentioned in a table header or text, CONVERT it (e.g. "770" in a table of millions -> 770000000). 
     - If no units are clear, extract the raw number. DO NOT return null if a number exists.
   - `amount_text`: The exact string from text (e.g. "$50,000,000" or "770").
   - If multiple numbers appear (e.g. "$100M shelf with $25M ATM"), extract the specific instrument described.

4. **DESCRIPTION**:
   - 1 sentence summary. E.g. "Active ATM program with B. Riley for up to $50M."

5. **NO NOISE**: 
   - Ignore general "Risks" section boilerplate ("We may dilute you..."). 
   - Ignore old expired agreements if the text clearly marks them as replaced.
   - If NO specific instrument/deal is found, return empty list.
"""


class DilutionExtractor:
    async def extract(self, text: str, form: str) -> List[dict]:
        """Extract dilution events from one chunk; returns a list of dicts."""
        if len(text) < 100:  # skip very short / irrelevant chunks
            return []
        prompt = f'TEXT TO ANALYZE ({form}):\n"""\n{text}\n"""'
        try:
            data = await complete_json(SYSTEM, prompt, EVENTS_SCHEMA)
            return [e.model_dump() for e in DilutionExtractionResult.model_validate(data).events]
        except Exception as e:  # noqa: BLE001 - one bad chunk must not stop a filing scan
            logger.error(f"Error in AI dilution extraction: {e}")
            return []

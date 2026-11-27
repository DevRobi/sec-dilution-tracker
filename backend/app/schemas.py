from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime

from .security.password_policy import validate_password, validate_username

# --- User Schemas ---
class UserBase(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        return validate_username(v.strip())

    @field_validator("password")
    @classmethod
    def check_password(cls, v: str) -> str:
        return validate_password(v)

class UserLogin(BaseModel):
    username_or_email: str
    password: str

class UserResponse(UserBase):
    created_at: datetime
    
    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str

# --- Company/Stats Schemas ---
class CompanyBase(BaseModel):
    cik: str
    ticker: str
    title: str

class CompanyResponse(CompanyBase):
    class Config:
        from_attributes = True

class SplitAdjustment(BaseModel):
    effective_date: str
    ratio: float
    source: str
    exact: bool
    label: str
    event_date: Optional[str] = None
    observed_ratio: Optional[float] = None


class StatsResponse(BaseModel):
    """Changes are split-adjusted (restated to the latest share basis)."""
    cik: str
    latest_shares: Optional[int]
    change_pct_1q: Optional[float]
    change_pct_1y: Optional[float]
    change_pct_5y: Optional[float]
    dilution_summary: Optional[str] = None
    last_date: Optional[str] = None
    date_1q: Optional[str] = None
    date_1y: Optional[str] = None
    date_5y: Optional[str] = None
    # Unadjusted values as stored in the stats table, for transparency.
    raw_change_pct_1q: Optional[float] = None
    raw_change_pct_1y: Optional[float] = None
    raw_change_pct_5y: Optional[float] = None
    split_source: Optional[str] = None          # "yfinance" | "detected"
    splits: List[SplitAdjustment] = []
    data_issues: List[dict] = []
    
    class Config:
        from_attributes = True

# --- Chart Data Schemas ---
class HistoryPoint(BaseModel):
    """`shares` is split-adjusted to the latest basis; `raw_shares` is as filed."""
    date: str
    shares: int
    source: Optional[str] = None
    source_url: Optional[str] = None
    raw_shares: Optional[int] = None
    factor: float = 1.0
    status: str = "ok"            # ok | repaired | excluded
    note: Optional[str] = None

    class Config:
        from_attributes = True

class PricePoint(BaseModel):
    date: str
    price: float

    class Config:
        from_attributes = True

class OfferingResponse(BaseModel):
    date: str
    category: Optional[str] = None
    text: str
    source_filing: str
    source_url: Optional[str] = None
    amount: Optional[float] = None
    amount_text: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None

    class Config:
        from_attributes = True

class OfferingWithTickerResponse(OfferingResponse):
    ticker: str


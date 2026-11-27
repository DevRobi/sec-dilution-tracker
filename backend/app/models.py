from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, UniqueConstraint, Text, BigInteger, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base

class Company(Base):
    __tablename__ = "companies"

    cik = Column(String, primary_key=True, index=True)
    ticker = Column(String, index=True)
    title = Column(String)

    stats = relationship("Stats", back_populates="company", uselist=False)
    history = relationship("History", back_populates="company")
    financials = relationship("Financials", back_populates="company")
    prices = relationship("Price", back_populates="company")
    offerings = relationship("Offering", back_populates="company")

class Stats(Base):
    __tablename__ = "stats"

    cik = Column(String, ForeignKey("companies.cik"), primary_key=True)
    latest_shares = Column(BigInteger)
    prev_shares_1y = Column(BigInteger)
    change_pct_1q = Column(Float)
    date_1q = Column(String)
    last_date = Column(String)
    last_updated = Column(DateTime, default=datetime.utcnow)

    # Dynamic year columns (1y-10y)
    change_pct_1y = Column(Float)
    date_1y = Column(String)
    change_pct_2y = Column(Float)
    date_2y = Column(String)
    change_pct_3y = Column(Float)
    date_3y = Column(String)
    change_pct_4y = Column(Float)
    date_4y = Column(String)
    change_pct_5y = Column(Float)
    date_5y = Column(String)
    change_pct_6y = Column(Float)
    date_6y = Column(String)
    change_pct_7y = Column(Float)
    date_7y = Column(String)
    change_pct_8y = Column(Float)
    date_8y = Column(String)
    change_pct_9y = Column(Float)
    date_9y = Column(String)
    change_pct_10y = Column(Float)
    date_10y = Column(String)
    
    # AI Summary
    dilution_summary = Column(Text, nullable=True)

    company = relationship("Company", back_populates="stats")

class History(Base):
    __tablename__ = "history"

    id = Column(Integer, primary_key=True, index=True)
    cik = Column(String, ForeignKey("companies.cik"), index=True)
    date = Column(String, index=True)
    shares = Column(BigInteger)
    source = Column(String)
    source_url = Column(String, nullable=True)

    # Composite index speeds the common "all history for a CIK, ordered by date" read.
    __table_args__ = (Index('ix_history_cik_date', 'cik', 'date'),)
    company = relationship("Company", back_populates="history")

class Financials(Base):
    __tablename__ = "financials"

    id = Column(Integer, primary_key=True, index=True)
    cik = Column(String, ForeignKey("companies.cik"))
    date = Column(String)
    metric = Column(String)
    value = Column(Float)

    __table_args__ = (UniqueConstraint('cik', 'date', 'metric', name='uix_financials_cik_date_metric'),)
    company = relationship("Company", back_populates="financials")

class Price(Base):
    __tablename__ = "prices"

    id = Column(Integer, primary_key=True, index=True)
    cik = Column(String, ForeignKey("companies.cik"), index=True)
    date = Column(String, index=True)
    price = Column(Float)

    __table_args__ = (UniqueConstraint('cik', 'date', name='uix_prices_cik_date'),)
    company = relationship("Company", back_populates="prices")

class Offering(Base):
    __tablename__ = "offerings"

    id = Column(Integer, primary_key=True, index=True)
    cik = Column(String, ForeignKey("companies.cik"))
    date = Column(String)
    category = Column(String)
    text = Column(Text)
    source_filing = Column(String)
    source_url = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    amount_text = Column(String, nullable=True)
    
    # AI Fields
    status = Column(String) # Active vs Potential
    description = Column(String) # Short description

    __table_args__ = (UniqueConstraint('cik', 'date', 'category', 'source_filing', name='uix_offerings_unique'),)
    company = relationship("Company", back_populates="offerings")

class User(Base):
    __tablename__ = "users"

    username = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    full_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    portfolio = relationship("PortfolioItem", back_populates="user")

class PortfolioItem(Base):
    __tablename__ = "portfolio"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, ForeignKey("users.username"))
    cik = Column(String, ForeignKey("companies.cik"))
    added_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="portfolio")
    company = relationship("Company") # Unidirectional is fine for now

    __table_args__ = (UniqueConstraint('username', 'cik', name='uix_portfolio_user_cik'),)

class AlertLog(Base):
    __tablename__ = "alert_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, ForeignKey("users.username"))
    offering_id = Column(Integer, ForeignKey("offerings.id"))
    ticker = Column(String)
    sent_at = Column(DateTime, default=datetime.utcnow)


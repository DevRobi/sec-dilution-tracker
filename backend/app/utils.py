import logging
import os
import secrets
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional, Any, Union
from jose import jwt

logger = logging.getLogger(__name__)

# JWT secret must come from the environment. If it is missing we generate an
# ephemeral key so the app still boots, but every restart invalidates existing
# tokens — set JWT_SECRET_KEY in the environment for a stable deployment.
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    logger.warning(
        "JWT_SECRET_KEY not set — generated an ephemeral key. "
        "Tokens will not survive a restart. Set JWT_SECRET_KEY for production."
    )

ALGORITHM = "HS256"
# Default 24 hours; override with ACCESS_TOKEN_EXPIRE_MINUTES.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(24 * 60)))

# Password hashing context: Supports BCrypt (default for new) and PBKDF2 (legacy)
pwd_context = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password using passlib context."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash password using passlib context (will use bcrypt by default)."""
    return pwd_context.hash(password)

def create_access_token(subject: Union[str, Any], expires_delta: Optional[timedelta] = None):
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {"sub": str(subject), "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

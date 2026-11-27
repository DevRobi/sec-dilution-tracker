from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, func
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from .. import database, models, schemas, utils
from ..security.rate_limit import enforce_rate_limit

router = APIRouter(
    prefix="/auth",
    tags=["auth"]
)

# Reuse the token URL for Swagger UI
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, utils.SECRET_KEY, algorithms=[utils.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    result = await db.execute(select(models.User).where(models.User.username == username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user

@router.post("/signup", response_model=schemas.UserResponse)
async def signup(
    request: Request,
    user: schemas.UserCreate,
    db: AsyncSession = Depends(database.get_db),
):
    enforce_rate_limit(request, scope="signup")

    normalized_username = user.username.strip()
    normalized_email = user.email.strip().lower()
    if not normalized_username or not normalized_email:
        raise HTTPException(status_code=400, detail="Username and email are required")

    # Check existing
    result = await db.execute(
        select(models.User).where(
            or_(
                func.lower(models.User.username) == normalized_username.lower(),
                func.lower(models.User.email) == normalized_email,
            )
        )
    )
    existing = result.scalars().first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Registration failed. Try a different username or email.",
        )
    
    hashed_pw = utils.get_password_hash(user.password)
    new_user = models.User(
        username=normalized_username,
        email=normalized_email,
        full_name=user.full_name,
        password_hash=hashed_pw
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user

@router.post("/login", response_model=schemas.Token)
async def login(
    request: Request,
    credentials: schemas.UserLogin,
    db: AsyncSession = Depends(database.get_db),
):
    enforce_rate_limit(request, scope="login")

    identifier = credentials.username_or_email.strip()
    if not identifier:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Authenticate by username or email
    identifier_lower = identifier.lower()
    stmt = select(models.User).where(
        or_(
            func.lower(models.User.username) == identifier_lower,
            func.lower(models.User.email) == identifier_lower,
        )
    )
    result = await db.execute(stmt)
    user = result.scalars().first()
    
    if not user or not utils.verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = utils.create_access_token(subject=user.username)
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me", response_model=schemas.UserResponse)
async def read_users_me(current_user: models.User = Depends(get_current_user)):
    return current_user

import asyncio
from app.database import AsyncSessionLocal
from app.models import User
from app.utils import get_password_hash
from sqlalchemy.future import select

async def main():
    async with AsyncSessionLocal() as session:
        # Configuration for the new user
        username = "admin"
        email = "admin@example.com"
        password = "ChangeMe-8chars-min"
        full_name = "Admin User"
        
        print(f"Hashing password for {username}...")
        hashed_pw = get_password_hash(password)
        
        # Check if user already exists
        stmt = select(User).where(User.username == username)
        result = await session.execute(stmt)
        user = result.scalars().first()
        
        if user:
            print(f"User {username} already exists. Updating password...")
            user.password_hash = hashed_pw
            user.email = email
            user.full_name = full_name
        else:
            print(f"Creating new user: {username}")
            new_user = User(
                username=username,
                email=email,
                full_name=full_name,
                password_hash=hashed_pw
            )
            session.add(new_user)
        
        await session.commit()
        print(f"Successfully configured user: {username}")
        print(f"Credentials:\n  Username: {username}\n  Password: {password}")

if __name__ == "__main__":
    asyncio.run(main())

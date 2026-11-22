"""Admin CLI.  python -m app.cli create-admin <email> <password> [role]"""
import asyncio
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.admin import ADMIN_ROLES, AdminUser


async def create_admin(email: str, password: str, role: str = "superadmin") -> None:
    if role not in ADMIN_ROLES:
        sys.exit(f"role must be one of {ADMIN_ROLES}")
    if len(password) < 12:
        sys.exit("password must be at least 12 characters")
    async with SessionLocal() as db:
        existing = (await db.execute(select(AdminUser).where(AdminUser.email == email.lower()))).scalar_one_or_none()
        if existing:
            existing.password_hash, existing.role, existing.active = hash_password(password), role, True
            print(f"updated admin {email} ({role})")
        else:
            db.add(AdminUser(email=email.lower(), password_hash=hash_password(password), role=role))
            print(f"created admin {email} ({role})")
        await db.commit()


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "create-admin":
        sys.exit(__doc__)
    asyncio.run(create_admin(*sys.argv[2:5]))

"""Fill a LOCAL database with demo users, calls, reports and online snapshots so the dashboard has data.

    python -m scripts.seed_demo            # adds ~300 users and 90 days of calls
Refuses to run when ENVIRONMENT=production. Demo users have names starting with "demo_".
"""
import asyncio
import random
from datetime import timedelta

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.admin import OnlineSnapshot
from app.models.base import utcnow
from app.models.call import CallRoom
from app.models.safety import Report
from app.models.user import User

COUNTRIES = [("BR", 18), ("IN", 16), ("US", 13), ("ID", 10), ("LK", 8), ("TR", 6), ("MX", 5), ("PH", 5),
             ("DE", 4), ("GB", 4), ("EG", 3), ("FR", 3), ("VN", 3), ("PK", 2)]
REASONS = ["nudity_or_sexual", "harassment_or_bullying", "spam_or_scam", "underage", "other"]
PLATFORMS = [("android", 58), ("ios", 29), ("web", 13)]


def pick(weighted):
    return random.choices([x for x, _ in weighted], [w for _, w in weighted])[0]


async def main() -> None:
    if settings.is_production:
        raise SystemExit("Refusing to seed demo data in production")
    random.seed(7)
    now = utcnow()
    async with SessionLocal() as db:
        users = []
        for i in range(300):
            created = now - timedelta(days=random.randint(0, 120), minutes=random.randint(0, 1440))
            u = User(display_name=f"demo_{random.randint(1000, 9999)}", age=random.randint(18, 45),
                     country=pick(COUNTRIES), gender=random.choice(["male", "female", "other"]),
                     interests=random.sample(["Music", "Gaming", "Travel", "Movies", "Sports", "Art"], 2),
                     onboarding_step=3, onboarding_completed=True, age_confirmed_at=created, terms_accepted_at=created,
                     created_at=created, last_platform=pick(PLATFORMS),
                     last_active_at=now - timedelta(hours=random.expovariate(1 / 48)),
                     premium_until=now + timedelta(days=20) if random.random() < 0.08 else None)
            db.add(u)
            users.append(u)
        await db.flush()
        rooms = []
        for d in range(90, -1, -1):
            n = int(40 + (90 - d) * 0.9 + random.randint(-8, 8))
            for _ in range(n):
                a, b = random.sample(users, 2)
                start = (now - timedelta(days=d)).replace(hour=random.randint(0, 23), minute=random.randint(0, 59))
                if start > now:
                    start = now - timedelta(minutes=random.randint(5, 600))
                connected = random.random() < 0.85
                dur = int(random.lognormvariate(5.2, 0.8)) if connected else None
                r = CallRoom(caller_id=a.id, callee_id=b.id, matched_at=start,
                             connected_at=start + timedelta(seconds=3) if connected else None,
                             ended_at=start + timedelta(seconds=(dur or 4) + 3), duration_seconds=dur,
                             end_reason=random.choice(["next", "next", "next", "leave", "disconnect"]),
                             caller_platform=a.last_platform, callee_platform=b.last_platform)
                db.add(r)
                rooms.append(r)
        await db.flush()
        for r in random.sample(rooms[-600:], 40):
            created = r.ended_at
            rep = Report(reporter_id=r.caller_id, reported_id=r.callee_id, room_id=r.id, reason=random.choice(REASONS),
                         created_at=created)
            if created < now - timedelta(days=2) and random.random() < 0.6:
                rep.status, rep.reviewed_at = random.choice(["dismissed", "actioned"]), created + timedelta(hours=5)
            db.add(rep)
        t = now - timedelta(days=8)
        while t < now:
            base = 600 + 300 * (1 + __import__("math").sin(t.hour / 24 * 6.28)) + (t - (now - timedelta(days=8))).days * 20
            tot = int(base + random.randint(-30, 30))
            db.add(OnlineSnapshot(taken_at=t, total=tot, android=int(tot * .58), ios=int(tot * .29),
                                  web=tot - int(tot * .58) - int(tot * .29)))
            t += timedelta(minutes=5)
        await db.commit()
    print(f"Seeded {len(users)} users, {len(rooms)} calls, 40 reports")


if __name__ == "__main__":
    asyncio.run(main())

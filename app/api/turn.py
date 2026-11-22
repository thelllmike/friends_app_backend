"""GET /turn-credentials"""
from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.core.rate_limit import enforce
from app.models.user import User
from app.services import turn

router = APIRouter(tags=["turn"])


@router.get("/turn-credentials")
async def turn_credentials(user: User = Depends(get_current_user)) -> dict:
    await enforce(f"turn:{user.id}", 30, 60)
    return turn.credentials(str(user.id))

"""GET /api/v1/auth/me — returns who the API key belongs to (handy for checking a key)."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.responses import Envelope, ok
from app.core.security import CurrentUser, Role

router = APIRouter(prefix="/auth", tags=["auth"])


class MeData(BaseModel):
    owner_name: str
    role: Role
    key_id: uuid.UUID | None
    team_id: uuid.UUID | None


@router.get("/me", response_model=Envelope[MeData])
async def me(principal: CurrentUser) -> Envelope[MeData]:
    return ok(
        MeData(
            owner_name=principal.owner_name,
            role=principal.role,
            key_id=principal.key_id,
            team_id=principal.team_id,
        )
    )

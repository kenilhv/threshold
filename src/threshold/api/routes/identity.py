"""Who am I, according to the issuer.

The first authenticated route in this service. It exists so the auth path has a
consumer before anything consequential depends on it — a token flow whose only
exercise is the endpoint that authorises payment is a token flow nobody has
debugged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from threshold.api.auth import Principal, require_principal

router = APIRouter(tags=["identity"])


class Me(BaseModel):
    subject: str = Field(description="The issuer's stable identifier for this user.")
    username: str
    roles: list[str] = Field(
        description=(
            "Realm roles this system recognises. Roles the token carries that "
            "this system has no concept of are dropped rather than echoed."
        )
    )


@router.get(
    "/me",
    response_model=Me,
    summary="The authenticated principal",
    responses={
        401: {"description": "No token, or a token that failed validation."},
        503: {"description": "The issuer's signing keys could not be reached."},
    },
)
async def get_me(principal: Annotated[Principal, Depends(require_principal)]) -> Me:
    return Me(
        subject=principal.subject,
        username=principal.username,
        roles=sorted(role.value for role in principal.roles),
    )

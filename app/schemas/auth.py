"""Auth request/response schemas."""
from pydantic import BaseModel, Field

from app.schemas.user import MeOut


class Consent(BaseModel):
    """Required the first time an account is created."""

    confirm_18_plus: bool = False
    accept_terms: bool = False
    terms_version: str = "1"


class GoogleLogin(Consent):
    id_token: str


class AppleLogin(Consent):
    identity_token: str
    display_name: str | None = Field(default=None, max_length=40)  # Apple only sends the name on first login


class OtpRequest(BaseModel):
    phone: str


class OtpRequestOut(BaseModel):
    sent: bool
    expires_in: int
    dev_code: str | None = None  # only when OTP_DEV_ECHO=true


class OtpVerify(Consent):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthOut(TokenPair):
    is_new_user: bool
    user: MeOut


class LinkGoogle(BaseModel):
    id_token: str


class LinkApple(BaseModel):
    identity_token: str


class LinkPhone(BaseModel):
    phone: str
    code: str = Field(min_length=6, max_length=6)


class LinkedAccount(BaseModel):
    provider: str
    email: str | None
    phone: str | None
    linked_at: str

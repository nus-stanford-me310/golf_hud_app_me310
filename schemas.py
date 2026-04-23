from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime


class ClubDistance(BaseModel):
    club: str
    avg_yardage: float

    class Config:
        from_attributes = True


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: str
    handicap: Optional[float] = None
    skill_level: Optional[str] = None
    club_distances: List[ClubDistance] = []


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int


class UserResponse(BaseModel):
    user_id: int
    email: EmailStr
    name: str
    handicap: Optional[float] = None
    skill_level: Optional[str] = None
    created_at: datetime
    club_distances: List[ClubDistance] = []

    class Config:
        from_attributes = True


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    handicap: Optional[float] = None
    skill_level: Optional[str] = None


class UpdateClubsRequest(BaseModel):
    club_distances: List[ClubDistance]

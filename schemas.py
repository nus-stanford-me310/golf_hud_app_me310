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


class WeatherInfo(BaseModel):
    temp: Optional[float] = None
    wind_speed: Optional[float] = None
    wind_dir: Optional[str] = None


class GameStartRequest(BaseModel):
    user_id: int
    starting_hole_id: Optional[int] = None


class GameResponse(BaseModel):
    game_id: int
    user_id: int
    hole_id: Optional[int] = None
    start_time: datetime
    status: str

    class Config:
        from_attributes = True


class UpdateHoleRequest(BaseModel):
    hole_id: int


class ShotRequest(BaseModel):
    game_id: int
    hole_id: int
    shot_no: int
    gps_loc: Optional[str] = None  # "lat,lng"
    distance: Optional[float] = None  # yards travelled this shot
    ball_traj: Optional[str] = None
    weather: Optional[WeatherInfo] = None


class ShotResponse(BaseModel):
    shot_id: int
    game_id: int
    hole_id: Optional[int] = None
    shot_no: int
    gps_loc: Optional[str] = None
    distance: Optional[float] = None
    ball_traj: Optional[str] = None
    weather_id: Optional[int] = None

    class Config:
        from_attributes = True


class ActiveUserResponse(BaseModel):
    user_id: Optional[int] = None
    name: Optional[str] = None
    handicap: Optional[float] = None
    skill_level: Optional[str] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

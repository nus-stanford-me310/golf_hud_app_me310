from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from database import Base, engine, get_db
from models import ActiveUser, Game, Shot, User, UserClubDistance, Weather
from schemas import (
    SignupRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    UpdateProfileRequest,
    UpdateClubsRequest,
    ClubDistance,
    ActiveUserResponse,
    GameStartRequest,
    GameResponse,
    UpdateHoleRequest,
    ShotRequest,
    ShotResponse,
)
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="GolfHUD Companion API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/signup", response_model=TokenResponse)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        name=req.name,
        handicap=req.handicap,
        skill_level=req.skill_level,
    )
    db.add(user)
    db.flush()

    for cd in req.club_distances:
        db.add(
            UserClubDistance(
                user_id=user.user_id, club=cd.club, avg_yardage=cd.avg_yardage
            )
        )

    db.commit()
    db.refresh(user)

    token = create_access_token(user.user_id)
    return TokenResponse(access_token=token, user_id=user.user_id)


@app.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    token = create_access_token(user.user_id)
    return TokenResponse(access_token=token, user_id=user.user_id)


@app.get("/users/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@app.patch("/users/me", response_model=UserResponse)
def update_profile(
    req: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if req.name is not None:
        current_user.name = req.name
    if req.handicap is not None:
        current_user.handicap = req.handicap
    if req.skill_level is not None:
        current_user.skill_level = req.skill_level
    db.commit()
    db.refresh(current_user)
    return current_user


@app.get("/users/me/clubs", response_model=list[ClubDistance])
def get_clubs(current_user: User = Depends(get_current_user)):
    return current_user.club_distances


@app.put("/users/me/clubs", response_model=list[ClubDistance])
def update_clubs(
    req: UpdateClubsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(UserClubDistance).filter(
        UserClubDistance.user_id == current_user.user_id
    ).delete()
    for cd in req.club_distances:
        db.add(
            UserClubDistance(
                user_id=current_user.user_id,
                club=cd.club,
                avg_yardage=cd.avg_yardage,
            )
        )
    db.commit()
    db.refresh(current_user)
    return current_user.club_distances


# =========================================================================
# WEATHER PROXY
# Hides OpenWeather API key from clients. Caches per location for 5 minutes
# so the upstream API is hit at most once per (rounded) GPS coordinate per
# 5-min window, no matter how many devices ask.
# =========================================================================

_WEATHER_CACHE: dict[tuple, tuple[float, dict]] = {}
_WEATHER_CACHE_LOCK = threading.Lock()
_WEATHER_TTL_SECONDS = 5 * 60
_DEG_TO_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _deg_to_compass(deg: float) -> str:
    idx = int((deg + 22.5) // 45) % 8
    return _DEG_TO_COMPASS[idx]


@app.get("/weather")
def get_weather(lat: float, lon: float):
    """Return current weather at (lat, lon) using OpenWeather under the hood.
    Response: {temp, wind_speed, wind_dir, units, source, fetched_at}.
    Cached server-side for 5 minutes per ~1km grid cell.
    """
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Weather not configured (OPENWEATHER_API_KEY missing)",
        )

    cache_key = (round(lat, 2), round(lon, 2))
    now = time.time()

    with _WEATHER_CACHE_LOCK:
        cached = _WEATHER_CACHE.get(cache_key)
        if cached and now - cached[0] < _WEATHER_TTL_SECONDS:
            return cached[1]

    qs = urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "appid": api_key, "units": "imperial"}
    )
    url = f"https://api.openweathermap.org/data/2.5/weather?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            import json as _json

            data = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenWeather error: {e}")

    temp = data.get("main", {}).get("temp")
    wind = data.get("wind", {}) or {}
    wind_speed = wind.get("speed")
    wind_deg = wind.get("deg")
    wind_dir = _deg_to_compass(float(wind_deg)) if wind_deg is not None else None

    result = {
        "temp": temp,
        "wind_speed": wind_speed,
        "wind_dir": wind_dir,
        "wind_deg": wind_deg,
        "units": "imperial",  # °F + mph
        "source": "openweather",
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }

    with _WEATHER_CACHE_LOCK:
        _WEATHER_CACHE[cache_key] = (now, result)

    return result


# =========================================================================
# GAME / SHOT ENDPOINTS
# Unity HUD calls these during a round. Auth is intentionally lenient — Unity
# trusts the user_id it learned from /active-user. Tighten later if needed.
# =========================================================================


@app.post("/games/start", response_model=GameResponse)
def start_game(req: GameStartRequest, db: Session = Depends(get_db)):
    """Start a new active game for a user. Marks any existing active games
    abandoned so a player only ever has one active game at a time."""
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    db.query(Game).filter(
        Game.user_id == req.user_id, Game.status == "active"
    ).update({"status": "abandoned"})

    game = Game(
        user_id=req.user_id,
        hole_id=req.starting_hole_id,
        start_time=datetime.utcnow(),
        status="active",
    )
    db.add(game)
    db.commit()
    db.refresh(game)
    return game


@app.get("/games/active/{user_id}", response_model=Optional[GameResponse])
def active_game(user_id: int, db: Session = Depends(get_db)):
    return (
        db.query(Game)
        .filter(Game.user_id == user_id, Game.status == "active")
        .order_by(Game.start_time.desc())
        .first()
    )


@app.patch("/games/{game_id}/hole", response_model=GameResponse)
def set_current_hole(
    game_id: int, req: UpdateHoleRequest, db: Session = Depends(get_db)
):
    g = db.query(Game).filter(Game.game_id == game_id).first()
    if g is None:
        raise HTTPException(status_code=404, detail="Game not found")
    g.hole_id = req.hole_id
    db.commit()
    db.refresh(g)
    return g


@app.post("/games/{game_id}/end", response_model=GameResponse)
def end_game(game_id: int, db: Session = Depends(get_db)):
    g = db.query(Game).filter(Game.game_id == game_id).first()
    if g is None:
        raise HTTPException(status_code=404, detail="Game not found")
    g.status = "ended"
    db.commit()
    db.refresh(g)
    return g


@app.post("/shots", response_model=ShotResponse)
def log_shot(req: ShotRequest, db: Session = Depends(get_db)):
    """Record a shot. Optionally creates a Weather row if weather supplied."""
    g = db.query(Game).filter(Game.game_id == req.game_id).first()
    if g is None:
        raise HTTPException(status_code=404, detail="Game not found")

    weather_id = None
    if req.weather is not None:
        w = Weather(
            temp=req.weather.temp,
            wind_speed=req.weather.wind_speed,
            wind_dir=req.weather.wind_dir,
        )
        db.add(w)
        db.flush()
        weather_id = w.weather_id

    shot = Shot(
        game_id=req.game_id,
        hole_id=req.hole_id,
        weather_id=weather_id,
        shot_no=req.shot_no,
        gps_loc=req.gps_loc,
        distance=req.distance,
        ball_traj=req.ball_traj,
    )
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot


@app.get("/games/{game_id}/shots", response_model=list[ShotResponse])
def list_shots(game_id: int, db: Session = Depends(get_db)):
    return (
        db.query(Shot)
        .filter(Shot.game_id == game_id)
        .order_by(Shot.shot_id)
        .all()
    )


@app.post("/active-user", response_model=ActiveUserResponse)
def set_active_user(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller becomes the 'active' user broadcast to Unity."""
    row = db.query(ActiveUser).filter(ActiveUser.id == 1).first()
    if row is None:
        row = ActiveUser(id=1, user_id=current_user.user_id, updated_at=datetime.utcnow())
        db.add(row)
    else:
        row.user_id = current_user.user_id
        row.updated_at = datetime.utcnow()
    db.commit()
    return ActiveUserResponse(
        user_id=current_user.user_id,
        name=current_user.name,
        handicap=current_user.handicap,
        skill_level=current_user.skill_level,
        updated_at=row.updated_at,
    )


@app.get("/active-user", response_model=ActiveUserResponse)
def get_active_user(db: Session = Depends(get_db)):
    """Public endpoint Unity polls to learn who's currently signed in."""
    row = db.query(ActiveUser).filter(ActiveUser.id == 1).first()
    if row is None or row.user_id is None or row.user is None:
        return ActiveUserResponse()
    u = row.user
    return ActiveUserResponse(
        user_id=u.user_id,
        name=u.name,
        handicap=u.handicap,
        skill_level=u.skill_level,
        updated_at=row.updated_at,
    )


@app.delete("/active-user", response_model=ActiveUserResponse)
def clear_active_user(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Caller clears active user (called on logout)."""
    row = db.query(ActiveUser).filter(ActiveUser.id == 1).first()
    if row is not None:
        row.user_id = None
        row.updated_at = datetime.utcnow()
        db.commit()
    return ActiveUserResponse()


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)

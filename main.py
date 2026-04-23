from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import User, UserClubDistance
from schemas import (
    SignupRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    UpdateProfileRequest,
    UpdateClubsRequest,
    ClubDistance,
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


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)

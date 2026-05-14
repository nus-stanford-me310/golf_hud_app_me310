from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import base64
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from database import Base, SessionLocal, engine, get_db
from models import (
    ActiveUser,
    ClubRecommendation,
    Course,
    Game,
    Hole,
    Shot,
    User,
    UserClubDistance,
    Weather,
)
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
    CourseResponse,
    HoleResponse,
    RecommendRequest,
    RecommendResponse,
    AskCaddieRequest,
    AskCaddieResponse,
    WeatherInfo,
)
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
)

Base.metadata.create_all(bind=engine)

# Lightweight, idempotent column-level migrations. SQLAlchemy's create_all
# only creates missing tables — it won't ALTER an existing table to add a
# newly declared column. We do those by hand here so a redeploy on Render
# (Postgres) picks up new columns without losing data, and a fresh local
# SQLite run is unaffected (the column already exists from create_all).
def _ensure_columns():
    from sqlalchemy import text as _sql_text
    statements = [
        # Added 2026-05: humidity in weather rows.
        "ALTER TABLE weather ADD COLUMN IF NOT EXISTS humidity FLOAT",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(_sql_text(stmt))
            except Exception as _e:
                # SQLite < 3.35 lacks IF NOT EXISTS; if column already exists
                # the ALTER will also fail there. Both are fine to ignore.
                print(f"[migrate] skipped '{stmt}': {_e}")


_ensure_columns()

# Seed Stanford Golf Course + holes idempotently on every boot.
from seed_data import seed as _seed  # noqa: E402

with SessionLocal() as _seed_db:
    try:
        _seed(_seed_db)
    except Exception as _e:
        print(f"[seed] skipped: {_e}")

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
# COURSE / HOLE READ ENDPOINTS
# =========================================================================


@app.get("/courses", response_model=list[CourseResponse])
def list_courses(db: Session = Depends(get_db)):
    return db.query(Course).all()


@app.get("/courses/{course_id}", response_model=CourseResponse)
def get_course(course_id: int, db: Session = Depends(get_db)):
    c = db.query(Course).filter(Course.course_id == course_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return c


@app.get("/courses/{course_id}/holes", response_model=list[HoleResponse])
def list_holes(course_id: int, db: Session = Depends(get_db)):
    return (
        db.query(Hole)
        .filter(Hole.course_id == course_id)
        .order_by(Hole.number)
        .all()
    )


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
    """Return current weather at (lat, lon) using Open-Meteo (free, no API key).
    Response: {temp, wind_speed, wind_dir, units, source, fetched_at}.
    Cached server-side for 5 minutes per ~1km grid cell.
    """
    cache_key = (round(lat, 2), round(lon, 2))
    now = time.time()

    with _WEATHER_CACHE_LOCK:
        cached = _WEATHER_CACHE.get(cache_key)
        if cached and now - cached[0] < _WEATHER_TTL_SECONDS:
            return cached[1]

    qs = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            import json as _json

            data = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo error: {e}")

    cur = data.get("current", {}) or {}
    temp = cur.get("temperature_2m")
    humidity = cur.get("relative_humidity_2m")
    wind_speed = cur.get("wind_speed_10m")
    wind_deg = cur.get("wind_direction_10m")
    wind_dir = _deg_to_compass(float(wind_deg)) if wind_deg is not None else None

    result = {
        "temp": temp,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "wind_dir": wind_dir,
        "wind_deg": wind_deg,
        "units": "imperial",  # °F + mph
        "source": "open-meteo",
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
            humidity=req.weather.humidity,
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
    db.flush()

    # Persist the recommendation that drove this shot, if supplied.
    if req.recommendation is not None:
        rec = ClubRecommendation(
            shot_id=shot.shot_id,
            club=req.recommendation.club,
            dynamic_yardage=req.recommendation.dynamic_yardage,
            confidence=req.recommendation.confidence,
        )
        db.add(rec)

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


# =========================================================================
# CLUB RECOMMENDATION (OpenAI-powered)
# Assembles full context — user's typical club distances, every prior shot
# this user has logged at this hole, and current weather — and asks GPT-4o
# for a structured club recommendation. Result is *not* persisted here; the
# caller embeds it in the next POST /shots so it gets saved alongside the
# shot it belongs to.
# =========================================================================


_OPENAI_CLIENT = None  # lazy


def _openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail="OPENAI_API_KEY not set on the server",
            )
        _OPENAI_CLIENT = OpenAI(api_key=api_key)
    return _OPENAI_CLIENT


@app.post("/recommend-club", response_model=RecommendResponse)
def recommend_club(req: RecommendRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Player profile
    clubs = list(user.club_distances)
    profile_lines = []
    if user.handicap is not None:
        profile_lines.append(f"Handicap: {user.handicap}")
    if user.skill_level:
        profile_lines.append(f"Skill: {user.skill_level}")
    if clubs:
        profile_lines.append("Typical carry distances (yards):")
        for c in sorted(clubs, key=lambda x: -x.avg_yardage):
            profile_lines.append(f"  - {c.club}: {int(c.avg_yardage)}")
    else:
        profile_lines.append("Typical carry distances: (not provided)")

    # Hole context
    hole = db.query(Hole).filter(Hole.hole_id == req.hole_id).first()
    hole_lines = [f"Hole id: {req.hole_id}"]
    if hole is not None:
        if hole.par is not None:
            hole_lines.append(f"Par: {hole.par}")
        if hole.yardage is not None:
            hole_lines.append(f"Posted yardage: {hole.yardage}")
    if req.distance_to_pin is not None and req.distance_to_pin > 0:
        hole_lines.append(f"Distance remaining to pin: {int(req.distance_to_pin)} yd")
    else:
        # No distance signal from the client. Without this anchor GPT-4o-mini
        # collapses to the same "par 5 reach club" every call (5 Wood). Be
        # explicit so it errs toward conservative mid-distance clubs instead.
        print(f"[recommend-club] WARNING: no distance_to_pin for user={req.user_id} hole={req.hole_id}", flush=True)
        hole_lines.append(
            "Distance remaining to pin: UNKNOWN — do NOT assume a typical par-5 reach distance; "
            "recommend a conservative mid-iron or hybrid based on the player's general history "
            "median carry, and set confidence below 0.4."
        )

    # Shot history at THIS hole (most context-specific signal: same target,
    # similar lies, same hazards). Pulled across every game this user has
    # ever played, with the recommendation that drove each shot if any.
    hole_history = (
        db.query(Shot)
        .join(Game, Shot.game_id == Game.game_id)
        .filter(Game.user_id == req.user_id, Shot.hole_id == req.hole_id)
        .order_by(Shot.shot_id.desc())
        .limit(20)
        .all()
    )
    hole_history_lines = []
    for s in reversed(hole_history):
        rec_part = ""
        if s.recommendation is not None:
            rec_part = f", recommended {s.recommendation.club}"
            if s.recommendation.confidence is not None:
                rec_part += f" (conf {s.recommendation.confidence:.2f})"
        dist_part = f"{int(s.distance)} yd" if s.distance is not None else "distance unknown"
        hole_history_lines.append(
            f"  - shot {s.shot_no}: {dist_part}{rec_part}"
        )

    # General shot history — every other hole this user has ever played,
    # used by GPT to estimate how far this specific player typically hits
    # each club (overrides the generic AVAILABLE CLUBS distances when there
    # is enough signal). Excludes shots on the current hole (those are
    # already in hole_history above) so we don't double-count.
    general_history = (
        db.query(Shot)
        .join(Game, Shot.game_id == Game.game_id)
        .filter(Game.user_id == req.user_id, Shot.hole_id != req.hole_id)
        .order_by(Shot.shot_id.desc())
        .limit(40)
        .all()
    )
    general_history_lines = []
    for s in reversed(general_history):
        rec_part = ""
        if s.recommendation is not None:
            rec_part = f", recommended {s.recommendation.club}"
        dist_part = f"{int(s.distance)} yd" if s.distance is not None else "distance unknown"
        general_history_lines.append(
            f"  - hole {s.hole_id}, shot {s.shot_no}: {dist_part}{rec_part}"
        )

    # Weather: prefer caller-supplied lat/lon (live); else fall back to most
    # recent weather row attached to this user's shots; else skip.
    weather_used: Optional[WeatherInfo] = None
    if req.lat is not None and req.lon is not None:
        try:
            w = get_weather(req.lat, req.lon)  # reuses cache + Open-Meteo
            weather_used = WeatherInfo(
                temp=w.get("temp"),
                humidity=w.get("humidity"),
                wind_speed=w.get("wind_speed"),
                wind_dir=w.get("wind_dir"),
            )
        except Exception:
            weather_used = None

    weather_line = "Weather: not provided"
    if weather_used is not None:
        bits = []
        if weather_used.temp is not None:
            bits.append(f"{weather_used.temp:.0f}°F")
        if weather_used.humidity is not None:
            bits.append(f"humidity {weather_used.humidity:.0f}%")
        if weather_used.wind_speed is not None:
            bits.append(f"wind {weather_used.wind_speed:.0f} mph")
        if weather_used.wind_dir:
            bits.append(f"from {weather_used.wind_dir}")
        weather_line = "Weather: " + ", ".join(bits)

    # Available club names — keep recommendations to clubs the player owns.
    club_names = ", ".join(c.club for c in clubs) or "(unknown — recommend a generic club)"

    hole_history_block = (
        "\n".join(hole_history_lines) if hole_history_lines else "  (none)"
    )
    general_history_block = (
        "\n".join(general_history_lines) if general_history_lines else "  (none)"
    )

    prompt = (
        "You are an expert golf caddie. Recommend the best club for the "
        "player's NEXT shot.\n\n"
        "Use the player's history, hole context, current weather, equipment "
        "tendencies, and available clubs to select one club and estimate "
        "dynamic_yardage.\n\n"
        "Definition:\n"
        "dynamic_yardage = the predicted distance this specific player will "
        "hit the recommended club on this shot, after adjusting their "
        "historical distance for current weather, altitude, lie, and course "
        "conditions.\n\n"
        "Inputs:\n\n"
        "PLAYER PROFILE:\n" + "\n".join(profile_lines) + "\n\n"
        "HOLE CONTEXT:\n" + "\n".join(hole_lines) + "\n\n"
        f"CURRENT CONDITIONS:\n{weather_line}\n\n"
        f"PRIOR SHOTS ON THIS HOLE:\n{hole_history_block}\n\n"
        f"GENERAL PRIOR SHOTS:\n{general_history_block}\n\n"
        f"AVAILABLE CLUBS:\n{club_names}\n\n"
        "Rules:\n"
        "- Choose exactly one club from AVAILABLE CLUBS.\n"
        "- The club name must exactly match one available club name.\n"
        "- Prioritize this-hole history over general history.\n"
        "- Use general history to estimate normal distance, consistency, "
        "miss pattern, and tendencies.\n"
        "- Exclude obvious outliers such as mishits, penalties, recovery "
        "shots, or abnormal lies unless they reveal a repeated pattern.\n"
        "- The chosen club's expected carry distance MUST be close to the "
        "distance to the pin. Never recommend a club that would fly more "
        "than ~15 yards past the target unless the player has no shorter "
        "club in their bag that can reach the target.\n"
        "- If distance to pin is under 100 yards, recommend a wedge "
        "(LW / SW / GW / PW) or 9 iron. Do NOT recommend a wood, hybrid, "
        "or long iron in this range.\n"
        "- If distance to pin is 100–135 yards, recommend a wedge or "
        "short iron (PW, 9 iron, 8 iron). Do NOT recommend a wood or "
        "hybrid in this range.\n"
        "- If distance to pin is 135–185 yards, recommend a mid iron "
        "(7, 6, 5, 4 iron) or hybrid sized to the gap. Avoid woods unless "
        "the iron carry is too short.\n"
        "- If distance to pin is over ~200 yards, woods or driver are "
        "appropriate.\n"
        "- Start dynamic_yardage from this player's historical distance "
        "with the chosen club.\n"
        "- Adjust dynamic_yardage for:\n"
        "  - wind: headwind lowers distance, tailwind increases distance, "
        "crosswind increases risk\n"
        "  - temperature: colder lowers distance, warmer increases distance\n"
        "  - altitude: higher altitude increases distance\n"
        "  - precipitation/wet turf: lowers total distance, especially roll\n"
        "  - lie: rough, sand, slope, or poor stance usually lowers distance\n"
        "  - humidity: usually negligible unless extreme\n"
        "- Temperature rule: relative to 70°F, adjust about 1–2 yards per "
        "10°F for irons and 1.5–2 yards per 10°F for driver.\n"
        "- For high-handicap players, favor forgiveness over ego.\n"
        "- For high-handicap players, prefer higher-loft drivers, hybrids, "
        "wedges, and forgiving irons when they are available and "
        "appropriate.\n"
        "- Do not recommend difficult long irons, such as 2-, 3-, or "
        "4-irons, when a hybrid or more forgiving option can produce a "
        "similar result.\n"
        "- Prioritize scoring clubs when relevant: putter, wedges, and "
        "driver often have the largest impact on score.\n"
        "- Around the green, prioritize control, consistency, and stopping "
        "power over maximum distance.\n"
        "- Off the tee, prioritize keeping the ball in play over maximizing "
        "distance.\n"
        "- Favor the club with the best balance of predicted distance, "
        "forgiveness, player consistency, and hazard avoidance.\n"
        "- If information is missing or conflicting, make the best estimate "
        "and lower confidence.\n"
        "- Output valid JSON only. No prose, no markdown, no extra keys.\n\n"
        "Return exactly:\n"
        "{\n"
        '  "club": "<exact club name from AVAILABLE CLUBS>",\n'
        '  "dynamic_yardage": <number>,\n'
        '  "confidence": <number from 0 to 1>,\n'
        '  "reasoning": "<one short sentence>"\n'
        "}"
    )

    try:
        client = _openai_client()

        # Log the full prompt so we can audit what GPT actually sees on each
        # call. Render captures stdout — visible in the service Logs tab.
        print("=" * 60, flush=True)
        print(
            f"[recommend-club] user={req.user_id} hole={req.hole_id} "
            f"d2pin={req.distance_to_pin} lat={req.lat} lon={req.lon}",
            flush=True,
        )
        print("[recommend-club] --- PROMPT ---", flush=True)
        print(prompt, flush=True)

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            # Lowered from 0.4 → 0.2: club selection is a near-deterministic
            # task once distance + history + conditions are fixed. Higher
            # temperature was producing wrong-club outliers (e.g. 5 Wood
            # recommended at 50 yd, which overshoots the green by 140 yd).
            temperature=0.2,
            messages=[
                {"role": "system",
                 "content": "You are a concise, expert golf caddie. Output JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        text = completion.choices[0].message.content or "{}"

        print("[recommend-club] --- RESPONSE ---", flush=True)
        print(text, flush=True)
        print("=" * 60, flush=True)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[recommend-club] OpenAI error: {e}", flush=True)
        raise HTTPException(status_code=502, detail=f"OpenAI error: {e}")

    import json as _json
    try:
        parsed = _json.loads(text)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"Bad JSON from model: {text[:200]}")

    return RecommendResponse(
        club=str(parsed.get("club", "")),
        dynamic_yardage=parsed.get("dynamic_yardage"),
        confidence=parsed.get("confidence"),
        reasoning=parsed.get("reasoning"),
        weather_used=weather_used,
        history_count=len(hole_history_lines) + len(general_history_lines),
    )


# --------------------------------------------------------------------------
# ASK CADDIE — free-form "Hey Steve, …" voice questions.
#
# Unity captures audio after the wake word, runs Whisper locally (already part
# of WakeWordListener), then POSTs the transcript here. We reply with a short
# spoken caddie response: text + base64-encoded WAV from OpenAI TTS. The
# system prompt is strict — golf topics only, with Stanford GC context — and
# off-topic asks get a one-sentence redirect, not an answer.
# --------------------------------------------------------------------------

ASK_CADDIE_SYSTEM_PROMPT = """You are an expert AI golf caddie and coach for a golfer wearing
mixed-reality glasses. The golfer just said "Hey Steve, …" and asked a question.

You are caddying at the Stanford Golf Course in Stanford, California.
Local context you may draw on (use only when relevant):
- Par-70, tree-lined classic course (William P. Bell, 1930).
- Front nine rolls through hills; back nine is flatter with reachable par-5s.
- Bentgrass greens, fast, generally break toward the Bay (west).
- Common winds are westerly off the foothills; afternoons can pick up.
- Notable holes: #2 short drivable par 4; #12 signature uphill par 4;
  #18 reachable par 5.
If you don't know a specific Stanford fact, give general advice that fits the
situation rather than inventing details.

YOUR ONLY TOPIC IS GOLF.
Allowed: this round, club selection, swing mechanics, shot strategy, course
management, putting, mental game, rules, etiquette, equipment, the Stanford
course, other courses/pros if directly compared to the player's situation.

REFUSE everything else (politics, news, jokes, weather as small-talk, other
sports, personal life, code/math, philosophy, etc.). Do NOT answer the
question — instead, redirect in ONE short sentence, e.g.:
  "Let's keep it on the course — what shot are you facing?"
  "I'm just your caddie today. Want help reading this green?"
Never argue or explain the refusal. Just pivot back to golf.

Style (every reply, including redirects):
- 2-3 sentences max, under 50 words. Your reply will be SPOKEN ALOUD —
  write like you're talking next to the player.
- No bullets, no lists, no headings, no markdown, no emojis, no parentheticals.
- Clear, encouraging, flowing sentences.

Example (on-topic):
Golfer: "What club for 150 into the wind?"
You: "At 150 into the breeze, take an extra club — go 6-iron and swing
smooth. Aim a hair left of the pin to let the wind hold it."

Example (off-topic):
Golfer: "Who's winning the Warriors game?"
You: "Not my department — I'm just your caddie. Want me to help set up this
next shot?"
"""


# Keyword patterns that signal a club-selection question. Matching is
# lower-case substring — we want to be generous, since Whisper transcripts
# vary in phrasing ("what should I hit", "what club should I use", "which
# iron from here", "is this a 7", etc.).
_CLUB_INTENT_SUBSTRINGS = (
    "what club", "which club", "what iron", "which iron",
    "what wedge", "which wedge", "what should i hit", "what do i hit",
    "which one should i hit", "what do you recommend", "club recommendation",
    "should i use my", "should i hit my", "is this a", "is it a",
    "club from here", "yardage", "what's the play",
)


def _looks_like_club_question(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in _CLUB_INTENT_SUBSTRINGS)


def _spoken_recommendation(rec: RecommendResponse) -> str:
    """Convert a structured /recommend-club result into a one-sentence reply
    suitable for TTS. Falls back gracefully if any field is missing."""
    club = (rec.club or "").strip() or "your stock club"
    if rec.dynamic_yardage:
        yd = int(round(rec.dynamic_yardage))
        body = f"Take your {club} — expect about {yd} yards"
    else:
        body = f"Take your {club}"
    reasoning = (rec.reasoning or "").strip().rstrip(".")
    if reasoning:
        return f"{body}. {reasoning}."
    return f"{body}. Smooth tempo, commit to it."


@app.post("/ask-caddie", response_model=AskCaddieResponse)
def ask_caddie(req: AskCaddieRequest, db: Session = Depends(get_db)):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    # If the question is about club selection AND we have a signed-in user
    # plus a hole, hand it to /recommend-club's existing logic so the spoken
    # reply uses real player profile + history + weather + club inventory
    # instead of generic GPT advice. Falls back to free-form caddie chat for
    # everything else (or if recommend-club fails / lacks context).
    reply = None
    used_recommend_club = False
    if (
        req.user_id and req.user_id > 0
        and req.hole_id and req.hole_id > 0
        and _looks_like_club_question(text)
    ):
        try:
            rec_req = RecommendRequest(
                user_id=req.user_id,
                hole_id=req.hole_id,
                distance_to_pin=req.distance_to_pin,
            )
            print("=" * 60, flush=True)
            print(f"[ask-caddie→recommend-club] user={req.user_id} hole={req.hole_id} d2pin={req.distance_to_pin}", flush=True)
            print(f"[ask-caddie→recommend-club] question: {text}", flush=True)
            rec = recommend_club(rec_req, db)
            reply = _spoken_recommendation(rec)
            used_recommend_club = True
            print(f"[ask-caddie→recommend-club] club={rec.club} yd={rec.dynamic_yardage} conf={rec.confidence}", flush=True)
            print(f"[ask-caddie→recommend-club] reply: {reply}", flush=True)
        except HTTPException as he:
            print(f"[ask-caddie→recommend-club] skipped ({he.status_code}: {he.detail}) — falling back to chat", flush=True)
        except Exception as e:
            print(f"[ask-caddie→recommend-club] error ({e}) — falling back to chat", flush=True)

    if reply is None:
        # Free-form caddie chat path (off-topic redirect, swing tips, etc.).
        context_bits = []
        if req.hole_id is not None:
            context_bits.append(f"current hole: {req.hole_id}")
        if req.distance_to_pin is not None and req.distance_to_pin > 0:
            context_bits.append(f"distance to pin: {int(req.distance_to_pin)} yd")
        context_line = (
            "Situation: " + ", ".join(context_bits) if context_bits else None
        )

        messages = [{"role": "system", "content": ASK_CADDIE_SYSTEM_PROMPT}]
        if context_line:
            messages.append({"role": "system", "content": context_line})
        messages.append({"role": "user", "content": text})

        try:
            client = _openai_client()

            print("=" * 60, flush=True)
            print(f"[ask-caddie] user={req.user_id} hole={req.hole_id} d2pin={req.distance_to_pin}", flush=True)
            print(f"[ask-caddie] question: {text}", flush=True)

            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.5,
                max_tokens=120,
                messages=messages,
            )
            reply = (completion.choices[0].message.content or "").strip()
            print(f"[ask-caddie] reply: {reply}", flush=True)
        except Exception as e:
            print(f"[ask-caddie] OpenAI chat error: {e}", flush=True)
            raise HTTPException(status_code=502, detail=f"OpenAI error: {e}")

    # Text-to-speech: synthesize a spoken version of the reply. WAV is used
    # (not mp3) so Unity decodes it via UnityWebRequestMultimedia without an
    # external mp3 decoder. TTS failure is non-fatal — text-only still returns.
    # _openai_client() is re-resolved here because the recommend-club path
    # doesn't define `client` locally.
    audio_base64 = None
    audio_format = None
    try:
        tts_client = _openai_client()
        tts = tts_client.audio.speech.create(
            model="tts-1",
            voice="onyx",            # warm conversational male — caddie-ish
            input=reply,
            response_format="wav",
        )
        audio_bytes = tts.read() if hasattr(tts, "read") else tts.content
        audio_base64 = base64.b64encode(audio_bytes).decode("ascii")
        audio_format = "wav"
        tag = "ask-caddie→recommend-club" if used_recommend_club else "ask-caddie"
        print(f"[{tag}] tts: {len(audio_bytes) / 1024:.1f} KB", flush=True)
    except Exception as tts_err:
        print(f"[ask-caddie] TTS failed (text-only reply): {tts_err}", flush=True)
    print("=" * 60, flush=True)

    return AskCaddieResponse(
        reply=reply,
        audio_base64=audio_base64,
        audio_format=audio_format,
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


# --------------------------------------------------------------------------
# DEBUG ENDPOINTS — read/wipe/seed shot history for testing.
#
# NO AUTH on these. They're intentionally open so the Unity Editor and
# curl-based tests can drive them without juggling JWTs, but that means
# they're DANGEROUS in production: anyone can wipe anyone's history. Lock
# them down (auth + env-var gate) before a real public launch.
# --------------------------------------------------------------------------

@app.get("/debug/users/{user_id}/shots")
def debug_list_shots(user_id: int, db: Session = Depends(get_db)):
    """Return every shot logged for a user, ordered most-recent first."""
    games = db.query(Game).filter(Game.user_id == user_id).all()
    game_ids = [g.game_id for g in games]
    if not game_ids:
        return {"user_id": user_id, "total": 0, "shots": []}

    shots = (
        db.query(Shot)
        .filter(Shot.game_id.in_(game_ids))
        .order_by(Shot.shot_id.desc())
        .all()
    )
    out = []
    for s in shots:
        rec = s.recommendation
        out.append({
            "shot_id": s.shot_id,
            "game_id": s.game_id,
            "hole_id": s.hole_id,
            "shot_no": s.shot_no,
            "distance": s.distance,
            "gps_loc": s.gps_loc,
            "recommendation": {
                "club": rec.club,
                "dynamic_yardage": rec.dynamic_yardage,
                "confidence": rec.confidence,
            } if rec is not None else None,
        })
    return {"user_id": user_id, "total": len(out), "shots": out}


@app.delete("/debug/users/{user_id}/shots")
def debug_wipe_shots(
    user_id: int,
    hole_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Delete shots (and their attached recommendations) for a user. If
    `hole_id` is given, only shots on that hole are deleted — useful when
    you only want to reset Hole 1's history pattern without losing the rest.
    Omit `hole_id` to wipe everything."""
    games = db.query(Game).filter(Game.user_id == user_id).all()
    game_ids = [g.game_id for g in games]
    deleted = 0
    if game_ids:
        q = db.query(Shot).filter(Shot.game_id.in_(game_ids))
        if hole_id is not None:
            q = q.filter(Shot.hole_id == hole_id)
        for s in q.all():
            db.delete(s)   # cascade clears the recommendation row
            deleted += 1
        db.commit()
    scope = f"hole {hole_id}" if hole_id is not None else "all holes"
    print(f"[debug] wiped {deleted} shots for user {user_id} ({scope})", flush=True)
    return {"user_id": user_id, "hole_id": hole_id, "deleted": deleted}


@app.post("/debug/users/{user_id}/shots/seed")
def debug_seed_shots(user_id: int, hole_id: int = 1, db: Session = Depends(get_db)):
    """Seed a varied, plausible shot history for testing. Creates one fake
    Game for the user on the given hole and attaches a sequence of shots
    spanning multiple clubs and distances, each with an attached
    recommendation. Call this AFTER /debug/users/{user_id}/shots (DELETE) if
    you want a clean slate."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    hole = db.query(Hole).filter(Hole.hole_id == hole_id).first()
    if hole is None:
        raise HTTPException(status_code=404, detail="Hole not found")

    # Create a synthetic game to hang the shots off.
    game = Game(user_id=user_id, hole_id=hole_id, started_at=datetime.utcnow())
    db.add(game)
    db.flush()

    fake_shots = [
        # (shot_no, club, dynamic_yardage_for_rec, actual_distance_hit)
        (1, "Driver",   235, 240),
        (2, "5 Iron",   175, 170),
        (3, "PW",       110,  60),  # short hole-out attempt
        (4, "Driver",   240, 232),
        (5, "7 Iron",   155, 158),
        (6, "9 Iron",   135, 130),
        (7, "SW",        80,  70),
        (8, "3 Wood",   215, 220),
        (9, "6 Iron",   165, 163),
        (10, "PW",      115, 112),
    ]
    for shot_no, club, dyn_yd, actual in fake_shots:
        s = Shot(
            game_id=game.game_id,
            hole_id=hole_id,
            shot_no=shot_no,
            distance=float(actual),
            gps_loc=None,
        )
        db.add(s)
        db.flush()
        rec = ClubRecommendation(
            shot_id=s.shot_id,
            club=club,
            dynamic_yardage=float(dyn_yd),
            confidence=0.85,
        )
        db.add(rec)
    db.commit()
    print(f"[debug] seeded {len(fake_shots)} fake shots for user {user_id} hole {hole_id}", flush=True)
    return {
        "user_id": user_id,
        "hole_id": hole_id,
        "game_id": game.game_id,
        "seeded": len(fake_shots),
        "clubs": sorted({c for _, c, _, _ in fake_shots}),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)

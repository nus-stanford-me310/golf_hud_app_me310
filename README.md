# GolfHUD Companion

Web-based setup for the GolfHUD / Beam Pro AR system. Users sign up, log in,
and record their handicap + typical club distances. The Unity HUD then reads
this profile from the same backend by `user_id` to tailor club recommendations
and yardage overlays at game time.

## Architecture

```
Browser (phone or laptop)     ← single-page static HTML/JS/CSS in ./static
   │   fetch()
   ▼
FastAPI  (main.py, port 8001)
   │   SQLAlchemy
   ▼
golfhud.db (SQLite)
   ├─ User, UserClubDistance
   └─ Course, Hole, Game, Shot, Weather, ClubRecommendation  (ER-diagram tables)

The Unity HUD (separate project) also points at port 8001 to read profile data.
```

## Run

```bash
cd pupil/companion-backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py        # http://0.0.0.0:8001
```

Then open:
- **Web UI:** http://localhost:8001/
- **API docs:** http://localhost:8001/docs

To reach it from your phone (same Wi-Fi as laptop):
```bash
ipconfig getifaddr en0              # → e.g. 192.168.1.42
# Then browse to http://192.168.1.42:8001/ on the phone
```

macOS firewall may block incoming connections — System Settings → Network →
Firewall → allow `python` if that happens.

## Endpoints

- `POST /auth/signup` — create user (+ club distances in one call), returns JWT
- `POST /auth/login` — returns JWT
- `GET  /users/me` — current user + club distances (auth required)
- `PATCH /users/me` — update name / handicap / skill (auth required)
- `GET  /users/me/clubs`, `PUT /users/me/clubs` — manage club distances

JWT secret is read from `GOLFHUD_JWT_SECRET` (default `dev-secret-change-me` —
override in production).

## Web UI flow

1. **Login** — email + password. New users click "Sign up".
2. **Signup (3 steps)**
   - Account — name, email, password
   - Profile — handicap (optional) + skill level
   - Club distances — Driver through LW, yardages in yards (blank = skip)
3. **Profile** — shows user info + clubs, with inline Edit mode to update yardages.

JWT is persisted in browser `localStorage`, so reloading stays logged in.

## Files

```
companion-backend/
├── main.py              # FastAPI app, mounts ./static at /
├── auth.py              # JWT + bcrypt password hashing
├── database.py          # SQLAlchemy engine + session
├── models.py            # All ER-diagram tables + UserClubDistance
├── schemas.py           # Pydantic request/response models
├── requirements.txt
├── pyproject.toml
└── static/
    ├── index.html       # Login / signup / profile views (HTML templates)
    ├── styles.css       # Golf-themed palette (green + sand)
    └── app.js           # Hash-based router + fetch() API client
```

## Notes

- `UserClubDistance` extends the ER diagram — the diagram had no per-user
  club-yardage table. One row per `(user_id, club)`.
- `Course`, `Hole`, `Game`, `Shot`, `Weather`, `ClubRecommendation` are defined
  in `models.py` but not yet exposed through the web UI (they're for the Unity
  game-time flow, not the setup flow).
- The Unity HUD and this backend must run on the same Wi-Fi.

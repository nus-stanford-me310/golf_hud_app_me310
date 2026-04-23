# Deploying GolfHUD Companion to the public internet

After this, you'll have a public URL like `https://golfhud-companion.onrender.com`
that works from any iPhone, anywhere — no laptop needed.

**Cost:** $0. Free tier only.

**Stack:**
- **Neon** — free serverless Postgres (always-free 0.5 GB)
- **Render** — free Python web service (spins down after 15 min idle; first
  request after sleep takes ~30 s, which is fine for a setup flow)

---

## Step 1 — Put the code on GitHub

If you haven't already got a GitHub repo for this project:

```bash
cd /Users/samanthastaudinger/Desktop/golf_unity/CDE4301
git init
git add pupil/companion-backend
git commit -m "Add companion backend"
gh repo create golfhud --public --source=. --push
# or manually: create repo on github.com, then
#   git remote add origin https://github.com/<you>/golfhud.git
#   git push -u origin main
```

Make sure `pupil/companion-backend/` is in the repo.

## Step 2 — Create a Neon Postgres database

1. Go to https://neon.tech and sign up (GitHub login is fastest).
2. Create a new project. Defaults are fine (nearest region, Postgres 16).
3. On the project dashboard, find the **connection string**. Click the
   "Pooled connection" tab — it looks like:
   ```
   postgresql://user:pass@ep-xxxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
4. Copy that string. You'll paste it into Render in the next step.

## Step 3 — Create the Render web service

1. Go to https://render.com and sign up with GitHub.
2. Click **New → Blueprint**.
3. Pick your `golfhud` repo.
4. Render detects [`render.yaml`](./render.yaml) and proposes a service named
   `golfhud-companion`. Click **Apply**.
5. Render will ask for the `DATABASE_URL` env var (it's marked `sync: false`
   so you have to set it manually). Paste your Neon connection string from
   Step 2.
6. `GOLFHUD_JWT_SECRET` is auto-generated. Leave it.
7. Click **Create Resources**. First deploy takes ~3–5 minutes (pip install
   + initial boot).

## Step 4 — Open it on your iPhone

Once Render shows "Live," open the service URL (something like
`https://golfhud-companion.onrender.com/`) on your phone. Sign up, log in,
done.

Bookmark it to your home screen for quick access.

## Updating the app later

Just commit + push to GitHub:

```bash
git add pupil/companion-backend
git commit -m "tweak signup flow"
git push
```

Render auto-deploys on every push to `main`. Takes ~2 min.

## Troubleshooting

- **"Application failed to respond"** — check Render logs. Usually it's a
  missing env var or Postgres couldn't connect. Verify `DATABASE_URL`.
- **First request is slow (~30 s)** — expected on free tier; service went to
  sleep. After the first request it stays warm for 15 min.
- **`psycopg2` build error** — requirements use `psycopg2-binary` which ships
  prebuilt wheels. If Render pins an old pip, bump `PYTHON_VERSION` to 3.11.
- **Lost the JWT secret** — all existing tokens invalidate but users can log
  in again with email + password. DB data is preserved.

## What about the Unity HUD?

Once this is deployed, point the Unity HUD at the same public URL instead of
a LAN IP. The same REST endpoints serve both the web UI and Unity:

- `GET /users/me/clubs` — with `Authorization: Bearer <token>`
- `POST /auth/login` — to get the token

No more "laptop on Wi-Fi" requirement for Unity either.

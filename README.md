# The Dispatch

Free, unlimited fleet management for American Truck Simulator and Euro Truck Simulator 2.

## Features

- Discord OAuth — drivers log in with Discord, no accounts to manage
- Auto job logging — client watches save files and pushes every run to the server
- Fleet dashboard — dark-themed web UI per VTC with live driver roster, dispatch log, aggregate stats
- Driver leaderboards — global and per-VTC, sorted by distance / jobs / earnings
- Discord bot — `/mystats`, `/leaderboard`, `/fleet` slash commands
- Live telemetry — speed, fuel, cargo state pushed in real time from the SDK

## Architecture

```
[ATS/ETS2] → saves game.sii
      ↓
[Local Client]  watches save folder · decrypts · parses · pushes snapshot + telemetry
      ↓
[Flask Server]  stores snapshots & jobs in PostgreSQL · serves web UI + REST API
      ↓
[Discord Bot]   slash commands pulling from the API
[Web Browser]   dashboard · leaderboard · VTC management
```

## Setup

### Server (local)

```bash
cd server
cp .env.example .env   # fill in your Discord OAuth credentials
pip install -r requirements.txt
python server.py        # runs on :5001
```

### Client

1. Download `SII_Decrypt.exe` from [DecryptTruck](https://github.com/CoffeSiberian/DecryptTruck/releases/latest) and put it in `client/`
2. Copy `client/.env.example` → `client/.env` and fill in `SERVER_URL`, `DISCORD_TOKEN`, etc.
3. Run:

```bash
cd client
pip install -r requirements.txt
python client.py
```

### Discord Bot

```bash
cd bot
cp .env.example .env   # set DISCORD_BOT_TOKEN and SERVER_URL
pip install -r requirements.txt
python bot.py
```

Set `DISCORD_GUILD_ID` in `bot/.env` to your server's ID for instant slash command registration during testing. Remove it (or leave blank) for global registration.

## Deploy to Railway

### 1 — Create the project

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Select this repo — Railway will auto-detect Python and use `railway.toml`

### 2 — Add a PostgreSQL database

In your Railway project → **New** → **Database** → **PostgreSQL**. Railway sets `DATABASE_URL` automatically.

### 3 — Set environment variables

In Railway → your service → **Variables**, add:

| Variable | Value |
|---|---|
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DISCORD_CLIENT_ID` | From Discord Developer Portal |
| `DISCORD_CLIENT_SECRET` | From Discord Developer Portal |
| `DISCORD_REDIRECT_URI` | `https://your-app.railway.app/auth/callback` |

`DATABASE_URL` is injected automatically by the PostgreSQL plugin — do not set it manually.

### 4 — Add the Discord redirect URI

In [Discord Developer Portal](https://discord.com/developers/applications) → your app → **OAuth2** → **Redirects**, add `https://your-app.railway.app/auth/callback`.

### 5 — Deploy the bot (optional separate service)

In your Railway project → **New Service** → **GitHub Repo** (same repo) → set:
- **Root Directory**: `bot`
- **Start Command**: `python bot.py`
- Variables: `DISCORD_BOT_TOKEN`, `SERVER_URL=https://your-app.railway.app`

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/snapshot` | Receive save snapshot from client |
| `POST` | `/api/telemetry` | Receive live telemetry |
| `GET` | `/api/player/<discord_id>` | Single player stats |
| `GET` | `/api/players` | All players |
| `GET` | `/api/jobs/<discord_id>` | Paginated job history |
| `GET` | `/api/leaderboard` | Global leaderboard (`?sort=distance\|jobs\|earnings`) |
| `GET` | `/api/leaderboard/vtc/<id>` | Per-VTC leaderboard |
| `GET` | `/api/vtc/<id>` | VTC details + member list |
| `POST` | `/api/vtc/create` | Create a VTC |
| `POST` | `/api/vtc/join` | Join a VTC by access code |
| `POST` | `/api/vtc/leave` | Leave current VTC |

## Stack

- **Server**: Python · Flask · SQLAlchemy · Gunicorn
- **Database**: SQLite (local) · PostgreSQL (production via Railway)
- **Auth**: Discord OAuth2
- **Client**: Watchdog · edge-tts · truck-telemetry SDK
- **Bot**: discord.py 2.x · aiohttp

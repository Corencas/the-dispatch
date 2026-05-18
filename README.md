# The Dispatch

A fleet management dashboard for American Truck Simulator.

## What it does

The Dispatch is a local client + server system that reads your ATS save file, parses your company data, and serves it via an API. Built for Discord server integrations where fleet managers can track driver stats, finances, and job history across multiple players.

## Features

- Auto-detects your ATS save file location
- Decrypts and parses save data in real time
- Tracks cash, debt, loans, driver count, XP, and distance
- Per-driver stats: skills, current city, job status, job history
- REST API for Discord bot and web dashboard integration
- Watches for new saves and auto-pushes updates

## Architecture

[ATS Game] → saves game.sii
↓
[Local Client] watches save folder → decrypts → parses → pushes to server
↓
[Master Server] stores player snapshots in database
↓
[Discord Bot / Website] pulls from API

## Setup

### Server
```bash
cd server
pip install -r requirements.txt
python server.py
```

### Client
1. Download `SII_Decrypt.exe` from [DecryptTruck](https://github.com/CoffeSiberian/DecryptTruck/releases/latest) and place it in the `client` folder
2. Copy `.env.example` to `.env` and fill in your details
3. Run:
```bash
cd client
pip install -r requirements.txt
python client.py
```

## API Endpoints

- `GET /api/players` — all players with latest snapshot
- `GET /api/player/<discord_id>` — single player full data
- `POST /api/snapshot` — receive snapshot from client

## Stack

- Python + Flask
- SQLAlchemy + SQLite (PostgreSQL in production)
- Watchdog for file monitoring
- Pystray for system tray
- DecryptTruck for save file decryption
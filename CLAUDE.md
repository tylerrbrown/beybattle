# BeyBattle

> **Workflow**: When done building a plan's implementation, always commit and push so Tyler can deploy.

Beyblade X themed multiplayer battle game with WebSocket real-time gameplay.

## Architecture

- **Backend**: Python 3 + `websockets` library (single external dependency)
- **Frontend**: Vanilla JS, single `index.html` + `admin.html`, no build tools
- **Database**: SQLite with WAL mode (player accounts, parts, battle history)
- **Data**: Static JSON dataset (40 blades, 20 ratchets, 20 bits, 30 attacks, type/element charts, stadiums)

## Files

| File | Purpose |
|------|---------|
| `server.py` | WebSocket + HTTP static file server (entry point) |
| `battle_engine.py` | Tick-based stadium simulation, collision calc, burst/stamina mechanics |
| `game_room.py` | PvP room management, game state machine |
| `ai_player.py` | BotPlayer AI opponent for single-player mode |
| `beyblade_data.py` | Load/validate JSON data at startup |
| `player_accounts.py` | Account registration, login, parts inventory, beyblade assembly |
| `journey.py` | Free battles, stadium masters, shop, tournaments, progression |
| `index.html` | Full client (all screens, CSS, JS inline) |
| `admin.html` | Admin panel (battle history, active rooms, stats, bug reports) |
| `data/blades.json` | 40 blade parts (attack ring - type, element, special move) |
| `data/ratchets.json` | 20 ratchet parts (middle - defense, height, teeth) |
| `data/bits.json` | 20 bit parts (bottom tip - stamina, movement pattern) |
| `data/attacks.json` | 30 attacks (specials + universals) |
| `data/type_matchups.json` | 4x4 type effectiveness (Attack/Defense/Stamina/Balance) |
| `data/element_chart.json` | 8x8 element effectiveness |
| `data/stadiums.json` | Stadium masters, elite challengers, champion |

## Game Mechanics

### Beyblade Assembly
3 parts: Blade (top, determines type/element/special) + Ratchet (middle, defense/height) + Bit (bottom, stamina/movement)

### Battle System
Tick-based simulation (500ms ticks, 30-60 second battles):
- Passive stamina drain + movement + collision detection
- Player actions every 5 seconds: Rush / Guard / Conserve / Special Move
- Win conditions: Burst Finish (3pts), Xtreme Finish (2pts), Spin Finish (1pt)

### Type Triangle
Attack > Stamina > Defense > Attack. Balance is neutral.

## Running

```bash
pip install websockets
python server.py
# Server runs on http://localhost:5053
# WebSocket at ws://localhost:5053/ws
```

Environment variables:
- `BEYBATTLE_PORT` - server port (default: 5053)
- `BEYBATTLE_ADMIN_KEY` - admin panel key (default: "bb-admin-2026")

## Deployment

- **Port**: 5053
- **Domain**: beybattle.tylerrbrown.com
- **Repo**: https://github.com/tylerrbrown/beybattle
- **Server path**: `/opt/beybattle/`
- **Service**: `beybattle.service` (systemd)
- **Proxy**: HAProxy with `mode http` + `timeout tunnel 3600s` for WebSocket
- **Python**: 3.10 on EC2 (Ubuntu 22.04 jammy, aarch64)
- **websockets**: Requires v14+ (`pip3 install 'websockets>=14'`)

```bash
# On EC2
cd /opt && git clone https://github.com/tylerrbrown/beybattle.git
pip3 install 'websockets>=14'
cp beybattle.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now beybattle
```

### HAProxy backend
```
backend web-beybattle
    mode http
    timeout tunnel 3600s
    server beybattle 127.0.0.1:5053 check fall 3 rise 1
```

### EC2 git authentication
Same pattern as PokeBattle - use git credential manager to get a token, then set the remote URL. See PokeBattle CLAUDE.md for the exact commands.

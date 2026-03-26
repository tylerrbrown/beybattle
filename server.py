#!/usr/bin/env python3
"""BeyBattle - Beyblade X Web Game Server.

WebSocket game server with HTTP static file serving.
Uses the websockets library for real-time multiplayer.
"""

import asyncio
import json
import mimetypes
import os
import pathlib
import random
import re
import sqlite3
import string
import time
import traceback

import websockets
from websockets.http11 import Response as HttpResponse, Headers as HttpHeaders

from beyblade_data import (
    load_data, get_client_data, STARTER_KITS, get_blade, get_ratchet, get_bit,
    BLADES, RATCHETS, BITS, compute_bey_stats,
)
from battle_engine import (
    BeybladeInstance, StadiumState, create_beyblade, simulate_battle, UNIVERSAL_ATTACKS,
)
from game_room import Player, GameRoom, RoomManager
from ai_player import BotPlayer, generate_opponent_beyblade
from player_accounts import AccountManager
from journey import (
    SHOP_ITEMS, REWARD_FREE_BATTLE, REWARD_FREE_WIN_BONUS, REWARD_MASTER_BASE,
    REWARD_ELITE_WIN, REWARD_CHAMPION_WIN, REWARD_PVP_WIN, REWARD_PVP_AI_WIN,
    REWARD_LEAGUE_WIN, generate_free_battle, get_stadium_masters, get_master,
    get_elite_challengers, get_champion, create_beyblade_from_config,
    build_master_team, get_master_rewards, roll_part_drop, open_part_pack,
    calc_battle_xp, xp_for_rank, FreeBattle, TournamentState,
    generate_tournament_bracket, TOURNAMENT_ROUNDS,
)

APP_DIR = pathlib.Path(__file__).parent

PORT = int(os.environ.get("BEYBATTLE_PORT", 5053))
ADMIN_KEY = os.environ.get("BEYBATTLE_ADMIN_KEY", "bb-admin-2026")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beybattle.db")

# Global state
account_mgr = None       # AccountManager, initialized in main()
room_manager = None       # RoomManager
active_encounters = {}    # player.id -> FreeBattle
trade_rooms = {}          # code -> TradeRoom
player_trade_rooms = {}   # player.id -> code
active_tournaments = {}   # account_id -> TournamentState


# ---- Trade Room ---------------------------------------------------------

class TradeRoom:
    """A lightweight trade room for two players to swap parts."""

    def __init__(self, code, player):
        self.code = code
        self.players = [player, None]
        self.offers = [None, None]
        self.confirmed = [False, False]
        self.created_at = time.time()

    def get_player_index(self, player):
        for i in range(2):
            if self.players[i] and self.players[i].id == player.id:
                return i
        return -1

    def get_opponent(self, player):
        idx = self.get_player_index(player)
        if idx == -1:
            return None
        return self.players[1 - idx]

    def is_full(self):
        return self.players[0] is not None and self.players[1] is not None


def generate_trade_code():
    """Generate a unique 4-letter trade room code."""
    for _ in range(100):
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
        if code not in trade_rooms and code not in room_manager.rooms:
            return code
    raise RuntimeError("Could not generate unique trade code")


def _cleanup_trade_room(code):
    """Remove a trade room and clean up player references."""
    room = trade_rooms.get(code)
    if room:
        for p in room.players:
            if p and p.id in player_trade_rooms:
                del player_trade_rooms[p.id]
        del trade_rooms[code]


# ---- On Game End Callback -----------------------------------------------

def on_game_end(room, winner_idx, summary):
    """Record a completed PvP game to DB and award currency/XP."""
    try:
        p1 = room.players[0]
        p2 = room.players[1]
        account_mgr.record_game(
            room.code,
            p1.username if p1 else "?",
            p2.username if p2 else "?",
            p1.beyblade.to_dict() if p1 and p1.beyblade else {},
            p2.beyblade.to_dict() if p2 and p2.beyblade else {},
            summary.get("winner_name", "?"),
            summary.get("finish_type", "spin"),
            summary.get("duration", 0),
        )
    except Exception as e:
        print(f"Error recording game: {e}")

    winner_account = summary.get("winner_account_id")
    if winner_account and account_mgr:
        try:
            loser = room.players[1 - winner_idx] if room.players[1 - winner_idx] else None
            is_bot_opponent = loser and loser.is_bot
            currency = REWARD_PVP_AI_WIN if is_bot_opponent else REWARD_PVP_WIN
            account_mgr.add_bey_points(winner_account, currency)
            print(f"[pvp] Awarded {currency} BP to account {winner_account}")
            xp = calc_battle_xp(5)
            xp_result = account_mgr.add_xp(winner_account, xp)
            if xp_result:
                print(f"[pvp] Awarded {xp} XP to account {winner_account}")
        except Exception as e:
            print(f"Error awarding PvP rewards: {e}")


# ---- HTTP Static File Server -------------------------------------------

async def process_request(connection, request):
    """Serve static files for non-WebSocket HTTP requests."""
    path = request.path

    if path == "/ws":
        return None

    if path.startswith("/api/admin/"):
        return await handle_admin_api(request)

    if path == "/":
        path = "/index.html"

    if '?' in path:
        path = path.split('?', 1)[0]

    try:
        file_path = (APP_DIR / path.lstrip("/")).resolve()
        if APP_DIR.resolve() not in file_path.parents and file_path != APP_DIR.resolve():
            return HttpResponse(404, "Not Found", HttpHeaders(), b"Not Found")
    except Exception:
        return HttpResponse(404, "Not Found", HttpHeaders(), b"Not Found")

    if file_path.is_file():
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        headers = HttpHeaders({
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache",
        })
        return HttpResponse(200, "OK", headers, body)

    return HttpResponse(404, "Not Found", HttpHeaders(), b"Not Found")


async def handle_admin_api(request):
    """Handle admin REST API requests."""
    path = request.path
    headers = request.headers

    auth = headers.get("X-Admin-Key", "")
    qs = ""
    if "?" in path:
        path, qs = path.split("?", 1)
    params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p) if qs else {}
    key = auth or params.get("k", "")

    if key != ADMIN_KEY:
        return HttpResponse(
            403, "Forbidden",
            HttpHeaders({"Content-Type": "application/json"}),
            json.dumps({"error": "Invalid admin key"}).encode()
        )

    resp_headers = HttpHeaders({"Content-Type": "application/json"})

    if path == "/api/admin/rooms":
        body = json.dumps(room_manager.get_active_rooms()).encode()
        return HttpResponse(200, "OK", resp_headers, body)

    if path == "/api/admin/history":
        try:
            games = account_mgr.get_game_history(limit=50)
            body = json.dumps(games).encode()
            return HttpResponse(200, "OK", resp_headers, body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            return HttpResponse(500, "Error", resp_headers, body)

    if path == "/api/admin/stats":
        try:
            conn = sqlite3.connect(DB_PATH)
            total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            avg_dur = conn.execute(
                "SELECT COALESCE(AVG(duration_sec), 0) FROM games"
            ).fetchone()[0]
            conn.close()
            body = json.dumps({
                "total_games": total,
                "avg_duration_sec": round(avg_dur, 1),
                "active_rooms": len(room_manager.rooms),
                "active_encounters": len(active_encounters),
                "active_tournaments": len(active_tournaments),
            }).encode()
            return HttpResponse(200, "OK", resp_headers, body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            return HttpResponse(500, "Error", resp_headers, body)

    if path == "/api/admin/bugs":
        try:
            bugs_dir = APP_DIR / "bugs"
            reports = []
            if bugs_dir.exists():
                for f in sorted(bugs_dir.glob("*.md"), reverse=True):
                    content = f.read_text(encoding="utf-8")
                    reports.append({"filename": f.name, "content": content})
            body = json.dumps(reports[:100]).encode()
            return HttpResponse(200, "OK", resp_headers, body)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode()
            return HttpResponse(500, "Error", resp_headers, body)

    match = re.match(r"/api/admin/rooms/([A-Z]{4})", path)
    if match:
        code = match.group(1)
        if params.get("action") == "close":
            closed = await room_manager.close_room(code)
            body = json.dumps({"closed": closed}).encode()
            return HttpResponse(200, "OK", resp_headers, body)

    return HttpResponse(404, "Not Found", resp_headers, b'{"error": "Not found"}')


# ---- Helpers ------------------------------------------------------------

def _build_player_team(player_id):
    """Build list of BeybladeInstance from player DB team."""
    team_data = account_mgr.get_team(player_id)
    if not team_data:
        return None
    team = []
    for bey_row in team_data:
        blade_part = bey_row.get("blade")
        ratchet_part = bey_row.get("ratchet")
        bit_part = bey_row.get("bit")
        if not blade_part or not ratchet_part or not bit_part:
            continue
        blade_data = get_blade(blade_part["part_id"])
        ratchet_data = get_ratchet(ratchet_part["part_id"])
        bit_data = get_bit(bit_part["part_id"])
        if not blade_data or not ratchet_data or not bit_data:
            continue
        bey = BeybladeInstance(
            blade_data, ratchet_data, bit_data,
            blade_level=blade_part.get("level", 1),
            ratchet_level=ratchet_part.get("level", 1),
            bit_level=bit_part.get("level", 1),
            nickname=bey_row.get("nickname"),
        )
        team.append(bey)
    return team if team else None


def _avg_team_level(team_data):
    """Get average part level from team data rows."""
    levels = []
    for bey_row in team_data:
        for part_key in ("blade", "ratchet", "bit"):
            part = bey_row.get(part_key)
            if part:
                levels.append(part.get("level", 1))
    return (sum(levels) / len(levels)) if levels else 1


def _award_journey_rewards(player, encounter, won, is_master=False, is_elite=False,
                           is_champion=False, master_data=None):
    """Award XP, currency, part drops, trophies after a journey battle."""
    rewards = {"won": won, "bey_points_earned": 0, "xp_result": None,
               "part_drop": None, "trophy": None, "rank_up": False}
    if not won:
        return rewards
    acct_id = getattr(player, 'account_id', None)
    if not acct_id:
        return rewards

    if is_champion:
        bp = REWARD_CHAMPION_WIN
    elif is_elite:
        bp = REWARD_ELITE_WIN
    elif is_master and master_data:
        bp = master_data.get("reward_bp", REWARD_MASTER_BASE)
    else:
        bp = REWARD_FREE_BATTLE + REWARD_FREE_WIN_BONUS
    account_mgr.add_bey_points(acct_id, bp)
    rewards["bey_points_earned"] = bp

    opp = encounter.opponent
    opp_level = max(getattr(opp, 'blade_level', 1), 1)
    xp_amount = calc_battle_xp(opp_level, is_master=is_master,
                                is_elite=is_elite, is_champion=is_champion)
    xp_result = account_mgr.add_xp(acct_id, xp_amount)
    rewards["xp_result"] = xp_result
    if xp_result and xp_result.get("leveled_up"):
        rewards["rank_up"] = True

    if is_master and master_data:
        trophy_name = master_data.get("trophy")
        if trophy_name:
            account_mgr.add_trophy(acct_id, str(master_data["id"]), "hometown")
            rewards["trophy"] = trophy_name
        account_mgr.add_milestone(acct_id, f"master_{master_data['id']}_defeated")
        _, part_reward, _ = get_master_rewards(master_data)
        if part_reward:
            account_mgr.add_part(acct_id, part_reward["part_id"],
                                 part_reward["part_type"],
                                 part_reward.get("part_data", {}).get("rarity", "common"))
            rewards["part_drop"] = part_reward

    if is_elite and master_data:
        account_mgr.add_milestone(acct_id, f"elite_{master_data.get('id', 'unknown')}_defeated")
    if is_champion:
        account_mgr.add_milestone(acct_id, "champion_defeated")

    if not is_master and not is_elite and not is_champion:
        drop, _ = roll_part_drop(opponent_rarity=getattr(encounter, 'opponent_rarity', 'common'))
        if drop:
            account_mgr.add_part(acct_id, drop["part_id"], drop["part_type"],
                                 drop.get("rarity", "common"))
            rewards["part_drop"] = drop

    return rewards


def _simulate_journey_tick(encounter, player_action):
    """Run one action cycle in a journey battle."""
    player_bey = encounter.get_active()
    opp_bey = encounter.opponent

    if not player_bey.is_alive() or not opp_bey.is_alive():
        winner = "player" if not opp_bey.is_alive() else "opponent"
        return [], True, winner

    stadium = StadiumState(player_bey, opp_bey)
    if not hasattr(encounter, '_last_stadium') or not encounter._last_stadium:
        stadium.initialize_positions("middle", "middle")

    if player_action in ("rush", "guard", "conserve"):
        stadium.apply_action(1, player_action)
    elif player_action == player_bey.special_move_id and not player_bey.special_used:
        stadium.apply_action(1, player_action)

    opp_type = opp_bey.bey_type
    stamina_pct = opp_bey.current_stamina / opp_bey.max_stamina
    if opp_bey.special_move_id and not opp_bey.special_used and random.random() < 0.2:
        ai_action = opp_bey.special_move_id
    elif opp_type == "attack":
        ai_action = "rush" if stamina_pct > 0.3 else random.choice(["guard", "conserve"])
    elif opp_type == "defense":
        ai_action = "guard" if stamina_pct > 0.2 else "conserve"
    elif opp_type == "stamina":
        ai_action = "conserve" if stamina_pct > 0.4 else "rush"
    else:
        ai_action = random.choice(["rush", "guard", "conserve"])
    stadium.apply_action(2, ai_action)

    all_events = []
    for _ in range(5):
        events = stadium.resolve_tick()
        all_events.extend(events)
        if stadium.is_over:
            break

    encounter.turn_count += 1
    encounter._last_stadium = stadium

    if stadium.is_over or not player_bey.is_alive() or not opp_bey.is_alive():
        if not opp_bey.is_alive():
            winner = "player"
        elif not player_bey.is_alive():
            winner = "opponent"
        elif stadium.winner == 1:
            winner = "player"
        else:
            winner = "opponent"
        return all_events, True, winner

    return all_events, False, None


# ---- Trade Message Handler ----------------------------------------------

async def _handle_trade_message(player, msg_type, data):
    """Handle all trade-related messages. Returns True if handled."""
    if msg_type == "create_trade":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return True
        code = generate_trade_code()
        room = TradeRoom(code, player)
        trade_rooms[code] = room
        player_trade_rooms[player.id] = code
        await player.send({"type": "trade_room_created", "code": code})
        return True

    if msg_type == "join_trade":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return True
        code = str(data.get("code", "")).strip().upper()
        if not re.match(r'^[A-Z]{4}$', code):
            await player.send({"type": "error", "message": "Code must be 4 letters."})
            return True
        room = trade_rooms.get(code)
        if not room:
            await player.send({"type": "error", "message": f"Trade room {code} not found."})
            return True
        if room.is_full():
            await player.send({"type": "error", "message": "Trade room is full."})
            return True
        room.players[1] = player
        player_trade_rooms[player.id] = code
        p0 = room.players[0]
        await p0.send({"type": "trade_partner_joined", "partner_name": player.username})
        await player.send({"type": "trade_room_joined", "code": code, "partner_name": p0.username})
        for p in room.players:
            if p and getattr(p, 'account_id', None):
                parts = account_mgr.get_parts(p.account_id)
                await p.send({"type": "trade_parts_list", "parts": parts})
        return True

    if msg_type == "trade_offer":
        code = player_trade_rooms.get(player.id)
        if not code:
            return True
        room = trade_rooms.get(code)
        if not room:
            return True
        idx = room.get_player_index(player)
        if idx == -1:
            return True
        part_id = data.get("part_id")
        room.offers[idx] = part_id
        room.confirmed = [False, False]
        # Look up full part info for the offer display
        part_info = None
        if part_id and getattr(player, 'account_id', None):
            all_parts = account_mgr.get_parts(player.account_id)
            for p in all_parts:
                if p.get("id") == part_id:
                    part_info = p
                    break
        offer_data = part_info or {"id": part_id, "name": "Unknown Part"}
        opp = room.get_opponent(player)
        if opp:
            await opp.send({"type": "trade_partner_offer", "offer": offer_data})
        await player.send({"type": "trade_offer_set", "offer": offer_data})
        return True

    if msg_type == "trade_confirm":
        code = player_trade_rooms.get(player.id)
        if not code:
            return True
        room = trade_rooms.get(code)
        if not room:
            return True
        idx = room.get_player_index(player)
        if idx == -1:
            return True
        if room.offers[0] is None or room.offers[1] is None:
            await player.send({"type": "error", "message": "Both must offer a part first."})
            return True
        room.confirmed[idx] = True
        opp = room.get_opponent(player)
        if opp:
            await opp.send({"type": "trade_partner_confirmed"})
        if room.confirmed[0] and room.confirmed[1]:
            for p in room.players:
                if p:
                    await p.send({"type": "trade_complete"})
            _cleanup_trade_room(code)
        return True

    if msg_type == "trade_cancel":
        code = player_trade_rooms.get(player.id)
        if not code:
            return True
        room = trade_rooms.get(code)
        if room:
            opp = room.get_opponent(player)
            if opp:
                await opp.send({"type": "trade_cancelled",
                                "message": f"{player.username} left the trade."})
        _cleanup_trade_room(code)
        if player.id in player_trade_rooms:
            del player_trade_rooms[player.id]
        await player.send({"type": "trade_left"})
        return True

    return False


# ---- WebSocket Message Router -------------------------------------------

async def handle_message(player, msg):
    """Route an incoming WebSocket message."""
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        await player.send({"type": "error", "message": "Invalid JSON."})
        return

    msg_type = data.get("type", "")

    if msg_type == "login":
        tk = data.get("token")
        username = data.get("username")
        upin = data.get("user_pin")

        # Case 1: Auto-login via saved token
        if tk:
            profile = account_mgr.login_with_token(tk)
            if profile:
                player.username = profile["username"]
                player.account_id = profile["id"]
                full = account_mgr.get_profile(profile["id"])
                needs_starter = not full.get("team") and not full.get("starter_blade_id")
                await player.send({"type": "game_data", **get_client_data()})
                resp = {"type": "auto_login_success", "username": profile["username"],
                        "token": profile["token_"], "needs_starter": needs_starter}
                resp.update(full)
                await player.send(resp)
            else:
                await player.send({"type": "auto_login_failed"})
            return

        # Case 2: Username + PIN verification (returning user)
        if username and upin:
            profile = account_mgr.login_with_username(username.strip(), str(upin).strip())
            if profile:
                player.username = profile["username"]
                player.account_id = profile["id"]
                full = account_mgr.get_profile(profile["id"])
                needs_starter = not full.get("team") and not full.get("starter_blade_id")
                await player.send({"type": "game_data", **get_client_data()})
                resp = {"type": "login_success", "username": profile["username"],
                        "token": profile["token_"], "needs_starter": needs_starter}
                resp.update(full)
                await player.send(resp)
            else:
                await player.send({"type": "error", "message": "Wrong code. Try again."})
            return

        # Case 3: Just username (first contact - check if new or existing)
        if username:
            username = username.strip()
            row = None
            try:
                _conn = account_mgr._conn()
                cur = _conn.execute(
                    "SELECT id, username, token_, user_pin FROM players WHERE username = ? COLLATE NOCASE",
                    (username,))
                row = cur.fetchone()
                _conn.close()
            except Exception:
                pass

            if row:
                # Existing user
                if row[3]:
                    # Has PIN - ask for it
                    await player.send({"type": "pin_required", "username": row[1]})
                else:
                    # No PIN - log in directly
                    player.username = row[1]
                    player.account_id = row[0]
                    full = account_mgr.get_profile(row[0])
                    needs_starter = not full.get("team") and not full.get("starter_blade_id")
                    await player.send({"type": "game_data", **get_client_data()})
                    resp = {"type": "login_success", "username": row[1],
                            "token": row[2], "needs_starter": needs_starter}
                    resp.update(full)
                    await player.send(resp)
            else:
                # New user - register, then require PIN setup
                result, error = account_mgr.register(username)
                if error:
                    await player.send({"type": "error", "message": error})
                else:
                    player.username = result["username"]
                    player.account_id = result["id"]
                    await player.send({"type": "game_data", **get_client_data()})
                    await player.send({"type": "pin_setup_required",
                                       "username": result["username"],
                                       "token": result["token_"]})
            return

        await player.send({"type": "error", "message": "Enter a username."})
        return

    if msg_type == "set_pin":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        pin_val = str(data.get("pin", "")).strip()
        if len(pin_val) != 4 or not pin_val.isdigit():
            await player.send({"type": "error", "message": "PIN must be exactly 4 digits."})
            return
        account_mgr.set_pin(player.account_id, pin_val)
        full = account_mgr.get_profile(player.account_id)
        needs_starter = not full.get("team") and not full.get("starter_blade_id")
        await player.send({"type": "game_data", **get_client_data()})
        resp = {"type": "login_success", "username": full["username"],
                "token": full["token_"], "needs_starter": needs_starter}
        resp.update(full)
        await player.send(resp)
        return

    if msg_type == "verify_pin":
        uname = str(data.get("username", "")).strip()
        pin_val = str(data.get("pin", "")).strip()
        profile = account_mgr.login_with_username(uname, pin_val)
        if profile:
            player.username = profile["username"]
            player.account_id = profile["id"]
            full = account_mgr.get_profile(profile["id"])
            needs_starter = not full.get("team") and not full.get("starter_blade_id")
            await player.send({"type": "game_data", **get_client_data()})
            resp = {"type": "login_success", "username": profile["username"],
                    "token": profile["token_"], "needs_starter": needs_starter}
            resp.update(full)
            await player.send(resp)
        else:
            await player.send({"type": "error", "message": "Wrong code. Try again."})
        return

    if msg_type == "choose_starter":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        if account_mgr.choose_starter(player.account_id, data.get("kit_id")):
            full = account_mgr.get_profile(player.account_id)
            resp = {"type": "starter_chosen"}
            resp.update(full)
            if data.get("kit_id") in STARTER_KITS:
                resp["starter_name"] = STARTER_KITS[data["kit_id"]]["name"]
            await player.send(resp)
        else:
            await player.send({"type": "error", "message": "Invalid starter kit."})
        return

    if msg_type == "get_profile":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        await player.send({"type": "profile_data", **account_mgr.get_profile(player.account_id)})
        return

    if msg_type == "get_team":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        await player.send({"type": "team_data", "team": account_mgr.get_team(player.account_id)})
        return

    if msg_type == "get_storage":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        await player.send({"type": "storage_data", "storage": account_mgr.get_storage(player.account_id)})
        return

    if msg_type == "get_parts":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        await player.send({"type": "parts_data", "parts": account_mgr.get_parts(player.account_id, part_type=data.get("part_type"))})
        return

    if msg_type == "assemble_beyblade":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        result, error = account_mgr.assemble_beyblade(player.account_id, data.get("blade_part_id"), data.get("ratchet_part_id"), data.get("bit_part_id"), data.get("nickname"))
        await player.send({"type": "assemble_error", "message": error} if error else {"type": "assemble_ok", "beyblade": result})
        return

    if msg_type == "disassemble_beyblade":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        if account_mgr.disassemble_beyblade(player.account_id, data.get("beyblade_id")):
            await player.send({"type": "disassemble_ok", "beyblade_id": data.get("beyblade_id")})
        else:
            await player.send({"type": "error", "message": "Could not disassemble."})
        return

    if msg_type == "swap_part":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        result, error = account_mgr.swap_part(player.account_id, data.get("beyblade_id"), data.get("new_part_id"))
        await player.send({"type": "swap_part_error", "message": error} if error else {"type": "swap_part_ok", "beyblade": result})
        return

    if msg_type == "add_to_team":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        if account_mgr.add_to_team(player.account_id, data.get("beyblade_id")):
            await player.send({"type": "team_updated", "team": account_mgr.get_team(player.account_id)})
        else:
            await player.send({"type": "error", "message": "Team full (max 3)."})
        return

    if msg_type == "remove_from_team":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        if account_mgr.remove_from_team(player.account_id, data.get("beyblade_id")):
            await player.send({"type": "team_updated", "team": account_mgr.get_team(player.account_id)})
        else:
            await player.send({"type": "error", "message": "Could not remove."})
        return

    if msg_type == "get_catalog":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        await player.send({"type": "catalog_data", "catalog": account_mgr.get_catalog(player.account_id)})
        return

    if msg_type == "get_shop":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        profile = account_mgr.get_profile(player.account_id)
        inventory = account_mgr.get_inventory(player.account_id)
        items = {k: {"type": k, "name": v["name"], "price": v["price"], "category": v.get("category", "repair"), "owned": inventory.get(k, 0)} for k, v in SHOP_ITEMS.items()}
        await player.send({"type": "shop_data", "items": items, "bey_points": profile.get("bey_points", 500) if profile else 500, "inventory": inventory})
        return

    if msg_type == "buy_item":
        if not getattr(player, 'account_id', None):
            await player.send({"type": "error", "message": "Not logged in."})
            return
        item_type = data.get("item_type", "")
        quantity = max(1, int(data.get("quantity", 1)))
        if item_type not in SHOP_ITEMS:
            await player.send({"type": "error", "message": "Invalid item."})
            return
        item = SHOP_ITEMS[item_type]
        if not account_mgr.spend_bey_points(player.account_id, item["price"] * quantity):
            await player.send({"type": "buy_result", "success": False, "message": "Not enough BP!"})
            return
        if item.get("category") == "pack":
            drops = []
            for _ in range(quantity):
                for d in open_part_pack(item_type):
                    d["db_id"] = account_mgr.add_part(player.account_id, d["part_id"], d["part_type"], d["rarity"])
                    drops.append(d)
            bp_now = account_mgr.get_profile(player.account_id).get("bey_points", 0)
            await player.send({"type": "buy_result", "success": True, "item_type": item_type, "drops": drops, "new_bey_points": bp_now, "inventory": account_mgr.get_inventory(player.account_id)})
        else:
            account_mgr.add_inventory_item(player.account_id, item_type, quantity)
            bp_now = account_mgr.get_profile(player.account_id).get("bey_points", 0)
            await player.send({"type": "buy_result", "success": True, "item_type": item_type, "new_bey_points": bp_now, "inventory": account_mgr.get_inventory(player.account_id)})
        return

    if msg_type == "use_upgrade_crystal":
        if not getattr(player, 'account_id', None):
            return
        new_level, error = account_mgr.upgrade_part(player.account_id, data.get("part_id"))
        await player.send({"type": "upgrade_error", "message": error} if error else {"type": "upgrade_ok", "part_id": data.get("part_id"), "new_level": new_level})
        return

    if msg_type == "free_battle":
        if not getattr(player, 'account_id', None):
            return
        td = account_mgr.get_team(player.account_id)
        team = _build_player_team(player.account_id) if td else None
        if not team:
            await player.send({"type": "error", "message": "No Beyblade in team."})
            return
        opponent, rarity = generate_free_battle(_avg_team_level(td))
        encounter = FreeBattle(player, team, opponent, rarity)
        active_encounters[player.id] = encounter
        await player.send({"type": "free_battle_start", **encounter.serialize_state()})
        return

    if msg_type == "battle_action":
        encounter = active_encounters.get(player.id)
        if not encounter:
            await player.send({"type": "error", "message": "No active battle."})
            return
        events, over, winner = _simulate_journey_tick(encounter, data.get("action", "rush"))
        if over:
            del active_encounters[player.id]
            won = winner == "player"
            if getattr(encounter, 'is_training', False):
                await player.send({"type": "battle_end", "events": events, "won": won, "is_training": True, **encounter.serialize_state()})
            elif getattr(encounter, 'is_tournament', False):
                ts = encounter.tournament_state
                oi = encounter.tournament_opponent
                adv, comp, champ = ts.record_result(won)
                bp = oi.get("reward_bp", 0) if won else 0
                if bp > 0:
                    account_mgr.add_bey_points(player.account_id, bp)
                await player.send({"type": "tournament_battle_end", "events": events, "won": won, "advanced": adv, "bey_points_earned": bp, **encounter.serialize_state()})
            else:
                rewards = _award_journey_rewards(player, encounter, won, is_master=getattr(encounter, 'is_master', False), is_elite=getattr(encounter, 'is_elite', False), is_champion=getattr(encounter, 'is_champion', False), master_data=getattr(encounter, 'master_data', None))
                await player.send({"type": "battle_end", "events": events, **rewards, **encounter.serialize_state()})
        else:
            await player.send({"type": "battle_update", "events": events, **encounter.serialize_state()})
        return

    if msg_type == "get_stadiums":
        if not getattr(player, 'account_id', None):
            return
        masters = get_stadium_masters()
        trophies = account_mgr.get_trophies(player.account_id)
        tid = {t["master_id"] for t in trophies}
        ml = [{"id": m["id"], "name": m["name"], "type": m.get("type", "balance"), "completed": str(m["id"]) in tid, "team_size": len(m.get("team", []))} for m in masters]
        await player.send({"type": "masters_list", "masters": ml, "trophies": trophies})
        return

    if msg_type == "start_master":
        if not getattr(player, 'account_id', None):
            return
        md = get_master(data.get("master_id"))
        if not md:
            await player.send({"type": "error", "message": "Invalid master."})
            return
        team = _build_player_team(player.account_id)
        opp_team = build_master_team(md) if team else None
        if not team or not opp_team:
            return
        encounter = FreeBattle(player, team, opp_team[0], "master")
        encounter.is_master = True
        encounter.master_data = md
        active_encounters[player.id] = encounter
        await player.send({"type": "master_battle_start", **encounter.serialize_state(), "master_name": md["name"]})
        return

    if msg_type == "get_elite":
        if not getattr(player, 'account_id', None):
            return
        elites = get_elite_challengers()
        ms = {m["milestone"] for m in account_mgr.get_milestones(player.account_id)}
        await player.send({"type": "elite_list", "elites": [{"index": i, "name": e.get("name", "Elite")} for i, e in enumerate(elites)]})
        return

    if msg_type == "start_elite":
        if not getattr(player, 'account_id', None):
            return
        from journey import get_elite_challenger
        ed = get_elite_challenger(data.get("elite_index", 0))
        if not ed:
            return
        team = _build_player_team(player.account_id)
        opp_team = build_master_team(ed) if team else None
        if not team or not opp_team:
            return
        encounter = FreeBattle(player, team, opp_team[0], "elite")
        encounter.is_elite = True
        encounter.master_data = ed
        active_encounters[player.id] = encounter
        await player.send({"type": "elite_battle_start", **encounter.serialize_state()})
        return

    if msg_type == "get_champion":
        if not getattr(player, 'account_id', None):
            return
        champ = get_champion()
        if champ:
            await player.send({"type": "champion_data", "name": champ.get("name", "Champion")})
        return

    if msg_type == "start_champion":
        if not getattr(player, 'account_id', None):
            return
        champ = get_champion()
        if not champ:
            return
        team = _build_player_team(player.account_id)
        opp_team = build_master_team(champ) if team else None
        if not team or not opp_team:
            return
        encounter = FreeBattle(player, team, opp_team[0], "champion")
        encounter.is_champion = True
        encounter.master_data = champ
        active_encounters[player.id] = encounter
        await player.send({"type": "champion_battle_start", **encounter.serialize_state()})
        return

    if msg_type == "create_room":
        if not getattr(player, 'account_id', None):
            return
        room = room_manager.create_room(on_game_end=on_game_end)
        await room.add_player(player)
        room_manager.player_rooms[player.id] = room.code
        await player.send({"type": "room_created", "code": room.code})
        return

    if msg_type == "join_room":
        if not getattr(player, 'account_id', None):
            return
        code = str(data.get("code", "")).strip().upper()
        joined = await room_manager.add_player_to_room(player, code) if re.match(r'^[A-Z]{4}$', code) else None
        if joined:
            opp = joined.get_opponent(player)
            await player.send({"type": "room_joined", "code": code, "opponent_name": opp.username if opp else None})
        return

    if msg_type == "create_ai_battle":
        if not getattr(player, 'account_id', None):
            return
        team = _build_player_team(player.account_id)
        if team:
            player.beyblade = team[0]
            player.ready = True
        bot = BotPlayer(difficulty=0.5)
        bot.choose_beyblade()
        room = room_manager.create_room(on_game_end=on_game_end)
        await room.add_player(player)
        room_manager.player_rooms[player.id] = room.code
        await player.send({"type": "room_created", "code": room.code, "ai_battle": True, "opponent_name": bot.username})
        room_manager.player_rooms[bot.id] = room.code
        await room.add_player(bot)
        return

    if msg_type == "select_beyblade":
        code = getattr(player, 'room_code', None) or room_manager.player_rooms.get(player.id)
        room = room_manager.get_room(code) if code else None
        if room:
            await room.handle_beyblade_select(player, data)
        return

    if msg_type == "set_launch":
        code = getattr(player, 'room_code', None) or room_manager.player_rooms.get(player.id)
        room = room_manager.get_room(code) if code else None
        if room:
            await room.handle_launch(player, data)
        return

    if msg_type == "choose_action":
        code = getattr(player, 'room_code', None) or room_manager.player_rooms.get(player.id)
        room = room_manager.get_room(code) if code else None
        if room:
            await room.handle_action(player, data.get("action", "rush"))
        return

    if msg_type == "rematch":
        code = getattr(player, 'room_code', None) or room_manager.player_rooms.get(player.id)
        room = room_manager.get_room(code) if code else None
        if room:
            await room.handle_rematch(player)
        return

    if msg_type == "leave":
        await room_manager.remove_player(player)
        await player.send({"type": "left_room"})
        return

    if msg_type == "get_tournament":
        if not getattr(player, 'account_id', None):
            return
        ts = active_tournaments.get(player.account_id)
        if ts and not ts.is_over:
            await player.send({"type": "tournament_data", **ts.serialize()})
        else:
            p = account_mgr.get_profile(player.account_id)
            await player.send({"type": "tournament_data", "active": False, "bey_points": p.get("bey_points", 0) if p else 0})
        return

    if msg_type == "start_tournament":
        if not getattr(player, 'account_id', None):
            return
        if not account_mgr.get_team(player.account_id):
            return
        if not account_mgr.spend_bey_points(player.account_id, 500):
            await player.send({"type": "error", "message": "Not enough BP."})
            return
        ts = TournamentState(player.account_id)
        active_tournaments[player.account_id] = ts
        await player.send({"type": "tournament_started", **ts.serialize()})
        return

    if msg_type in ("start_tournament_match", "tournament_battle_start"):
        if not getattr(player, 'account_id', None):
            return
        ts = active_tournaments.get(player.account_id)
        if not ts or ts.is_over:
            return
        team = _build_player_team(player.account_id)
        td = account_mgr.get_team(player.account_id)
        if not team or not td:
            return
        oi = ts.get_current_opponent(_avg_team_level(td))
        if not oi or not oi["team"]:
            return
        encounter = FreeBattle(player, team, oi["team"][0], "tournament")
        encounter.is_tournament = True
        encounter.tournament_state = ts
        encounter.tournament_opponent = oi
        active_encounters[player.id] = encounter
        await player.send({"type": "tournament_battle_start", **encounter.serialize_state(), "opponent_name": oi["name"], "round_name": oi["round_name"]})
        return

    if msg_type == "tournament_continue":
        ts = active_tournaments.get(getattr(player, 'account_id', None))
        if not ts:
            return
        if ts.is_over:
            if ts.is_champion:
                account_mgr.add_milestone(player.account_id, "tournament_champion")
            del active_tournaments[player.account_id]
            await player.send({"type": "tournament_complete", **ts.serialize()})
        else:
            await player.send({"type": "tournament_data", **ts.serialize()})
        return

    if msg_type == "tournament_forfeit":
        aid = getattr(player, 'account_id', None)
        if aid and aid in active_tournaments:
            del active_tournaments[aid]
        await player.send({"type": "tournament_forfeited"})
        return

    if msg_type in ("create_trade", "join_trade", "trade_offer", "trade_confirm", "trade_cancel"):
        await _handle_trade_message(player, msg_type, data)
        return

    if msg_type == "start_training":
        if not getattr(player, 'account_id', None):
            return
        team = _build_player_team(player.account_id)
        td = account_mgr.get_team(player.account_id)
        if not team or not td:
            return
        opponent = generate_opponent_beyblade(avg_level=int(_avg_team_level(td)))
        encounter = FreeBattle(player, team, opponent, "training")
        encounter.is_training = True
        active_encounters[player.id] = encounter
        state = encounter.serialize_state()
        state["is_training"] = True
        await player.send({"type": "training_start", **state})
        return

    if msg_type == "submit_bug_report":
        if not getattr(player, 'account_id', None):
            return
        desc = str(data.get("description", "")).strip()[:2000]
        if desc:
            try:
                bugs_dir = APP_DIR / "bugs"
                bugs_dir.mkdir(exist_ok=True)
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(int(time.time()), tz=timezone.utc)
                safe = "".join(c if c.isalnum() else "_" for c in (player.username or "unknown"))
                fn = f"{dt.strftime('%Y-%m-%d_%H%M%S')}_{safe}.md"
                (bugs_dir / fn).write_text(f"# Bug Report\n\n{desc}\n", encoding="utf-8")
                await player.send({"type": "bug_report_submitted", "message": "Bug report submitted!"})
            except Exception as e:
                print(f"[bug] Error: {e}")
        return

    if msg_type == "ping":
        await player.send({"type": "pong"})
        return

    await player.send({"type": "error", "message": f"Unknown message type: {msg_type}"})


# ---- WebSocket Connection Handler ---------------------------------------

async def handler(websocket):
    """Handle a WebSocket connection."""
    player = Player(websocket)
    print(f"[+] Player connected: {player.id}")
    try:
        async for message in websocket:
            try:
                await handle_message(player, message)
            except Exception as e:
                print(f"[!] Error from {player.id}: {e}")
                traceback.print_exc()
                try:
                    await player.send({"type": "error", "message": "Internal server error."})
                except Exception:
                    pass
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        print(f"[!] Connection error {player.id}: {e}")
    finally:
        print(f"[-] Player disconnected: {player.id}")
        await room_manager.remove_player(player)
        tc = player_trade_rooms.get(player.id)
        if tc:
            tr = trade_rooms.get(tc)
            if tr:
                opp = tr.get_opponent(player)
                if opp:
                    await opp.send({"type": "trade_cancelled", "message": f"{player.username} disconnected."})
            _cleanup_trade_room(tc)
        if player.id in active_encounters:
            del active_encounters[player.id]
        aid = getattr(player, 'account_id', None)
        if aid and aid in active_tournaments:
            del active_tournaments[aid]


async def room_cleanup_task():
    """Periodically clean up old rooms."""
    while True:
        await asyncio.sleep(300)
        try:
            removed = await room_manager.cleanup_old_rooms()
            if removed:
                print(f"[cleanup] Removed {removed} old room(s)")
        except Exception as e:
            print(f"[cleanup] Error: {e}")


async def main():
    """Start the BeyBattle server."""
    print("Loading Beyblade data...")
    load_data()
    global account_mgr, room_manager
    account_mgr = AccountManager(DB_PATH)
    print(f"Database initialized at {DB_PATH}")
    room_manager = RoomManager(on_game_end=on_game_end)
    asyncio.create_task(room_cleanup_task())
    async with websockets.serve(
        handler, "0.0.0.0", PORT,
        process_request=process_request,
        max_size=1_000_000,
        ping_interval=30,
        ping_timeout=10,
    ) as server:
        print(f"BeyBattle server running on http://0.0.0.0:{PORT}")
        print(f"WebSocket endpoint: ws://0.0.0.0:{PORT}/ws")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

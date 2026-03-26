"""Game room management and state machine for BeyBattle.

States: LOBBY -> BEYBLADE_SELECT -> LAUNCH -> BATTLE -> GAME_OVER

Tick-based battle: stadium simulation runs every 500ms, players get
periodic action windows to choose rush/guard/conserve/special.
"""

import asyncio
import json
import random
import string
import time
from battle_engine import BeybladeInstance, StadiumState, create_beyblade


class Player:
    """Represents a connected player."""

    def __init__(self, ws):
        self.ws = ws
        self.id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        self.username = None
        self.account_id = None
        self.room_code = None
        self.beyblade = None          # BeybladeInstance (set during select)
        self.spin_direction = "right"  # "left" or "right"
        self.launch_power = 0.5       # 0.0-1.0 (from launch timing)
        self.launch_position = "middle"  # "inside"/"middle"/"outside"
        self.ready = False
        self.chosen_action = None     # action id string during action window
        self.is_bot = False

    async def send(self, msg):
        """Send JSON message to player. Silently fails on broken connection."""
        try:
            await self.ws.send(json.dumps(msg))
        except Exception:
            pass


class GameRoom:
    """A game room managing two players through the full game lifecycle.

    State machine:
        LOBBY           - waiting for second player
        BEYBLADE_SELECT - both players pick blade/ratchet/bit
        LAUNCH          - both players set spin, power, position
        BATTLE          - tick loop running, periodic action windows
        GAME_OVER       - battle resolved, can rematch
    """

    # Timeouts (seconds)
    BEYBLADE_SELECT_TIMEOUT = 60
    LAUNCH_TIMEOUT = 15
    ACTION_WINDOW_TIMEOUT = 5
    TICK_INTERVAL = 0.5  # 500ms per tick

    def __init__(self, code, on_game_end=None):
        self.code = code
        self.state = "LOBBY"
        self.players = [None, None]
        self.created_at = time.time()
        self.on_game_end = on_game_end
        self.stadium = None           # StadiumState during battle
        self.score = [0, 0]           # match points (first to 3)
        self.rounds_played = 0

        # Async coordination
        self._select_events = [None, None]
        self._launch_events = [None, None]
        self._action_events = [None, None]
        self._timeout_task = None
        self._tick_task = None

    def get_player_index(self, player):
        """Get 0 or 1 index for player, or -1 if not found."""
        for i in range(2):
            if self.players[i] and self.players[i].id == player.id:
                return i
        return -1

    def get_opponent(self, player):
        """Get the other player."""
        idx = self.get_player_index(player)
        if idx == -1:
            return None
        return self.players[1 - idx]

    async def add_player(self, player):
        """Add a player to the room. Returns slot index or -1 if full."""
        if self.players[0] is None:
            self.players[0] = player
            player.room_code = self.code
            return 0
        elif self.players[1] is None:
            self.players[1] = player
            player.room_code = self.code
            # Room is full, notify both
            await self.players[0].send({
                "type": "opponent_joined",
                "opponent_name": player.username,
            })
            # Start beyblade select after a brief delay
            asyncio.create_task(self._start_beyblade_select())
            return 1
        return -1

    async def remove_player(self, player):
        """Handle player disconnection."""
        idx = self.get_player_index(player)
        if idx == -1:
            return

        opponent = self.get_opponent(player)
        self.players[idx] = None

        if opponent:
            await opponent.send({
                "type": "opponent_disconnected",
                "text": f"{player.username} disconnected."
            })
            # If in battle, opponent wins by forfeit
            if self.state == "BATTLE":
                await self._end_game(1 - idx, "forfeit")

        # Cancel pending tasks
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()

    # ---- BEYBLADE SELECT PHASE ----

    async def _start_beyblade_select(self):
        """Transition to beyblade selection phase."""
        self.state = "BEYBLADE_SELECT"
        self._select_events = [asyncio.Event(), asyncio.Event()]

        # Check if both already ready (pre-set beyblades for rematches or journey)
        if all(p and p.ready for p in self.players):
            await self._start_launch()
            return

        for p in self.players:
            if p and not p.ready:
                await p.send({
                    "type": "beyblade_select_start",
                    "time_limit": self.BEYBLADE_SELECT_TIMEOUT,
                })

        # Bot auto-selects beyblade
        for i, p in enumerate(self.players):
            if p and p.is_bot and not p.ready:
                p.ready = True
                self._select_events[i].set()
                opp = self.get_opponent(p)
                if opp:
                    await opp.send({"type": "opponent_ready"})

        # Check again after bot selection
        if all(p and p.ready for p in self.players):
            await self._start_launch()
            return

        self._timeout_task = asyncio.create_task(self._beyblade_select_timeout())

    async def _beyblade_select_timeout(self):
        """Auto-assign random beyblade if player does not pick in time."""
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._select_events[0].wait(),
                    self._select_events[1].wait()
                ),
                timeout=self.BEYBLADE_SELECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            for i, p in enumerate(self.players):
                if p and not p.ready:
                    from beyblade_data import (
                        get_random_blade, get_random_ratchet, get_random_bit
                    )
                    blade = get_random_blade()
                    ratchet = get_random_ratchet()
                    bit = get_random_bit()
                    p.beyblade = create_beyblade(
                        blade["id"], ratchet["id"], bit["id"]
                    )
                    p.ready = True
                    await p.send({
                        "type": "beyblade_auto_assigned",
                        "text": "Time is up! A random Beyblade was assigned.",
                        "beyblade": p.beyblade.to_dict(),
                    })
                    self._select_events[i].set()

        if all(p and p.ready for p in self.players):
            await self._start_launch()

    def set_beyblade(self, player, beyblade_instance):
        """Lock in beyblade choice for a player."""
        player.beyblade = beyblade_instance

    async def handle_beyblade_select(self, player, data):
        """Handle beyblade selection from a player."""
        if self.state != "BEYBLADE_SELECT":
            await player.send({
                "type": "error", "message": "Not in beyblade select phase."
            })
            return

        if player.ready:
            await player.send({
                "type": "error", "message": "Beyblade already locked in."
            })
            return

        blade_id = data.get("blade_id")
        ratchet_id = data.get("ratchet_id")
        bit_id = data.get("bit_id")

        if not blade_id or not ratchet_id or not bit_id:
            await player.send({
                "type": "error",
                "message": "Must select blade, ratchet, and bit.",
            })
            return

        try:
            bey = create_beyblade(blade_id, ratchet_id, bit_id)
        except ValueError as e:
            await player.send({"type": "error", "message": str(e)})
            return

        player.beyblade = bey
        player.ready = True
        idx = self.get_player_index(player)

        await player.send({
            "type": "beyblade_locked",
            "text": "Beyblade locked in! Waiting for opponent...",
            "beyblade": bey.to_dict(),
        })

        opponent = self.get_opponent(player)
        if opponent:
            await opponent.send({"type": "opponent_ready"})

        self._select_events[idx].set()

        # Check if both ready
        if all(p and p.ready for p in self.players):
            if self._timeout_task and not self._timeout_task.done():
                self._timeout_task.cancel()
            await self._start_launch()

    # ---- LAUNCH PHASE ----

    async def _start_launch(self):
        """Transition to launch phase - players set spin, power, position."""
        self.state = "LAUNCH"
        self._launch_events = [asyncio.Event(), asyncio.Event()]

        for p in self.players:
            if p:
                opp = self.get_opponent(p)
                opp_bey = opp.beyblade.to_dict() if opp and opp.beyblade else None
                await p.send({
                    "type": "launch_start",
                    "your_beyblade": p.beyblade.to_dict(),
                    "opponent_beyblade": opp_bey,
                    "time_limit": self.LAUNCH_TIMEOUT,
                })

        # Bot auto-launches
        for i, p in enumerate(self.players):
            if p and p.is_bot:
                self._launch_events[i].set()

        # Check if both set (both bots)
        if all(self._launch_events[i].is_set() for i in range(2)):
            await self.start_battle()
            return

        self._timeout_task = asyncio.create_task(self._launch_timeout())

    async def _launch_timeout(self):
        """Default launch params if player does not set them."""
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._launch_events[0].wait(),
                    self._launch_events[1].wait()
                ),
                timeout=self.LAUNCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            for i, p in enumerate(self.players):
                if p and not self._launch_events[i].is_set():
                    p.spin_direction = "right"
                    p.launch_power = 0.5
                    p.launch_position = "middle"
                    self._launch_events[i].set()

        await self.start_battle()

    def set_launch(self, player, spin, power, position):
        """Set launch parameters for a player."""
        if spin in ("left", "right"):
            player.spin_direction = spin
        else:
            player.spin_direction = "right"
        player.launch_power = max(0.0, min(1.0, float(power)))
        if position in ("inside", "middle", "outside"):
            player.launch_position = position
        else:
            player.launch_position = "middle"

    async def handle_launch(self, player, data):
        """Handle launch parameter submission from a player."""
        if self.state != "LAUNCH":
            await player.send({
                "type": "error", "message": "Not in launch phase."
            })
            return

        idx = self.get_player_index(player)
        if idx == -1:
            return

        if self._launch_events[idx].is_set():
            await player.send({
                "type": "error", "message": "Launch already submitted."
            })
            return

        spin = data.get("spin_direction", "right")
        power = data.get("launch_power", 0.5)
        position = data.get("launch_position", "middle")

        self.set_launch(player, spin, power, position)

        await player.send({
            "type": "launch_confirmed", "text": "Waiting for opponent..."
        })

        opponent = self.get_opponent(player)
        if opponent:
            await opponent.send({"type": "opponent_launch_locked"})

        self._launch_events[idx].set()

        if all(self._launch_events[i].is_set() for i in range(2)):
            if self._timeout_task and not self._timeout_task.done():
                self._timeout_task.cancel()
            await self.start_battle()

    # ---- BATTLE PHASE ----

    async def start_battle(self):
        """Create StadiumState and begin the tick loop."""
        self.state = "BATTLE"

        p1 = self.players[0]
        p2 = self.players[1]

        # Apply launch params to beyblades
        p1.beyblade.spin_direction = 1 if p1.spin_direction == "right" else -1
        p2.beyblade.spin_direction = 1 if p2.spin_direction == "right" else -1
        p1.beyblade.apply_launch_bonus(p1.launch_power)
        p2.beyblade.apply_launch_bonus(p2.launch_power)

        # Create stadium
        self.stadium = StadiumState(p1.beyblade, p2.beyblade)
        self.stadium.initialize_positions(p1.launch_position, p2.launch_position)

        # Send battle start to both
        for i, p in enumerate(self.players):
            opp = self.players[1 - i]
            await p.send({
                "type": "battle_start",
                "your_beyblade": p.beyblade.to_dict(),
                "opponent_beyblade": opp.beyblade.to_dict(),
                "your_launch": {
                    "spin": p.spin_direction,
                    "power": p.launch_power,
                    "position": p.launch_position,
                },
                "stadium": self.stadium.to_dict(),
            })

        # Start tick loop
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self):
        """Async loop that resolves ticks every 500ms and broadcasts state."""
        try:
            while not self.stadium.is_over:
                events = self.stadium.resolve_tick()

                tick_msg = {
                    "type": "tick_update",
                    "tick": self.stadium.tick,
                    "stadium": self.stadium.to_dict(),
                    "events": events,
                }
                await self.broadcast(tick_msg)

                # Handle action window
                if self.stadium.action_window_open:
                    self.stadium.action_window_open = False
                    await self._handle_action_window()

                await asyncio.sleep(self.TICK_INTERVAL)

            # Battle ended
            winner_idx = 0 if self.stadium.winner == 1 else 1
            await self._end_game(winner_idx, self.stadium.finish_type)

        except asyncio.CancelledError:
            pass

    async def _handle_action_window(self):
        """Open action window - both players pick rush/guard/conserve/special."""
        self._action_events = [asyncio.Event(), asyncio.Event()]

        for i, p in enumerate(self.players):
            if p:
                p.chosen_action = None
                bey = p.beyblade
                available_actions = ["rush", "guard", "conserve"]
                if bey.special_move_id and not bey.special_used:
                    available_actions.append(bey.special_move_id)
                await p.send({
                    "type": "action_window",
                    "time_limit": self.ACTION_WINDOW_TIMEOUT,
                    "available_actions": available_actions,
                    "tick": self.stadium.tick,
                })

        # Bot auto-picks action
        for i, p in enumerate(self.players):
            if p and p.is_bot and hasattr(p, "choose_action"):
                p.chosen_action = p.choose_action(self.stadium)
                if p.chosen_action:
                    self.stadium.apply_action(i + 1, p.chosen_action)
                self._action_events[i].set()

        # Wait for both with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._action_events[0].wait(),
                    self._action_events[1].wait()
                ),
                timeout=self.ACTION_WINDOW_TIMEOUT
            )
        except asyncio.TimeoutError:
            for i in range(2):
                if not self._action_events[i].is_set():
                    self._action_events[i].set()

    async def handle_action(self, player, action):
        """Process action during battle (rush/guard/conserve/special)."""
        if self.state != "BATTLE":
            return

        idx = self.get_player_index(player)
        if idx == -1:
            return

        if player.chosen_action is not None:
            await player.send({
                "type": "error", "message": "Action already submitted."
            })
            return

        player.chosen_action = action
        player_num = idx + 1  # stadium uses 1-indexed
        self.stadium.apply_action(player_num, action)

        await player.send({"type": "action_confirmed", "action": action})

        opponent = self.get_opponent(player)
        if opponent:
            await opponent.send({"type": "opponent_action_locked"})

        self._action_events[idx].set()

    def player_ready(self, player):
        """Mark player as ready for battle."""
        player.ready = True

    # ---- GAME OVER ----

    async def _end_game(self, winner_idx, finish_type):
        """End the game and announce winner."""
        self.state = "GAME_OVER"
        self.rounds_played += 1

        # Cancel tick loop
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()

        winner = self.players[winner_idx]
        loser = self.players[1 - winner_idx]

        # Score points based on finish type
        points_map = {"burst": 3, "xtreme": 2, "spin": 1, "forfeit": 3}
        points = points_map.get(finish_type, 1)
        self.score[winner_idx] += points

        duration = int(time.time() - self.created_at)

        summary = {
            "winner_name": winner.username if winner else "Unknown",
            "loser_name": loser.username if loser else "Unknown",
            "finish_type": finish_type,
            "finish_points": points,
            "score": list(self.score),
            "rounds_played": self.rounds_played,
            "ticks": self.stadium.tick if self.stadium else 0,
            "collisions": self.stadium.total_collisions if self.stadium else 0,
            "duration": duration,
            "winner_account_id": getattr(winner, "account_id", None),
        }

        for i, p in enumerate(self.players):
            if p:
                is_winner = winner_idx == i
                opp_bey = None
                other = self.players[1 - i]
                if other and other.beyblade:
                    opp_bey = other.beyblade.to_dict()
                await p.send({
                    "type": "game_over",
                    "winner": is_winner,
                    "summary": summary,
                    "your_beyblade": p.beyblade.to_dict() if p.beyblade else None,
                    "opponent_beyblade": opp_bey,
                })

        if self.on_game_end:
            try:
                self.on_game_end(self, winner_idx, summary)
            except Exception as e:
                print(f"Error in on_game_end callback: {e}")

        return summary

    async def handle_rematch(self, player):
        """Handle rematch request."""
        if self.state != "GAME_OVER":
            return

        player.ready = False
        player.beyblade = None
        player.chosen_action = None
        player.launch_power = 0.5
        player.launch_position = "middle"
        player.spin_direction = "right"

        opponent = self.get_opponent(player)

        # Bot auto-accepts rematch
        if opponent and opponent.is_bot:
            opponent.ready = False
            opponent.beyblade = None
            opponent.chosen_action = None
            for p in self.players:
                await p.send({"type": "rematch_start"})
            await self._start_beyblade_select()
            return

        if opponent:
            await opponent.send({
                "type": "rematch_request",
                "text": f"{player.username} wants a rematch!"
            })

        # Check if both want rematch (both ready=False and no beyblade)
        if opponent and not opponent.ready and opponent.beyblade is None:
            for p in self.players:
                await p.send({"type": "rematch_start"})
            await self._start_beyblade_select()

    # ---- UTILITY ----

    async def broadcast(self, msg):
        """Send to all players."""
        for p in self.players:
            if p:
                await p.send(msg)

    def to_dict(self):
        """Serialize room state."""
        players = []
        for p in self.players:
            if p:
                players.append({
                    "username": p.username,
                    "id": p.id,
                    "is_bot": p.is_bot,
                    "ready": p.ready,
                    "beyblade": p.beyblade.to_dict() if p.beyblade else None,
                })
        return {
            "code": self.code,
            "state": self.state,
            "players": players,
            "score": list(self.score),
            "rounds_played": self.rounds_played,
            "created_at": self.created_at,
            "stadium": self.stadium.to_dict() if self.stadium else None,
        }


class RoomManager:
    """Manages all active game rooms."""

    def __init__(self, on_game_end=None):
        self.rooms = {}          # code -> GameRoom
        self.player_rooms = {}   # player.id -> room_code
        self.on_game_end = on_game_end

    def generate_code(self):
        """Generate a unique 4-letter room code."""
        for _ in range(100):
            code = "".join(random.choices(string.ascii_uppercase, k=4))
            if code not in self.rooms:
                return code
        raise RuntimeError("Could not generate unique room code")

    def create_room(self, on_game_end=None):
        """Create a new room and return it."""
        code = self.generate_code()
        callback = on_game_end or self.on_game_end
        room = GameRoom(code, on_game_end=callback)
        self.rooms[code] = room
        return room

    def get_room(self, code):
        """Lookup room by code."""
        if not code:
            return None
        return self.rooms.get(code.upper().strip())

    def remove_room(self, code):
        """Cleanup a room."""
        room = self.rooms.pop(code, None)
        if room:
            for p in room.players:
                if p and p.id in self.player_rooms:
                    del self.player_rooms[p.id]
        return room is not None

    def get_active_rooms(self):
        """List all rooms with summary info."""
        result = []
        for code, room in self.rooms.items():
            players = []
            for p in room.players:
                if p:
                    players.append({
                        "username": p.username,
                        "id": p.id,
                        "is_bot": p.is_bot,
                    })
            result.append({
                "code": code,
                "state": room.state,
                "players": players,
                "score": list(room.score),
                "rounds_played": room.rounds_played,
                "created_at": room.created_at,
                "age_seconds": int(time.time() - room.created_at),
            })
        return result

    async def add_player_to_room(self, player, code):
        """Join an existing room."""
        room = self.get_room(code)
        if not room:
            await player.send({
                "type": "error",
                "message": f"Room {code} not found.",
            })
            return None

        if room.state != "LOBBY":
            await player.send({
                "type": "error",
                "message": "Game already in progress.",
            })
            return None

        slot = await room.add_player(player)
        if slot == -1:
            await player.send({
                "type": "error", "message": "Room is full."
            })
            return None

        self.player_rooms[player.id] = code
        return room

    async def remove_player(self, player):
        """Remove a player from their room."""
        code = self.player_rooms.get(player.id)
        if not code:
            return
        room = self.rooms.get(code)
        if room:
            await room.remove_player(player)
            if player.id in self.player_rooms:
                del self.player_rooms[player.id]

            # Clean up empty or bot-only rooms
            has_human = any(
                p is not None and not p.is_bot for p in room.players
            )
            if not has_human:
                for p in room.players:
                    if p and p.id in self.player_rooms:
                        del self.player_rooms[p.id]
                self.rooms.pop(room.code, None)

    async def close_room(self, code):
        """Force-close a room."""
        room = self.rooms.get(code)
        if not room:
            return False

        for p in room.players:
            if p:
                await p.send({
                    "type": "room_closed", "text": "Room was closed."
                })
                if p.id in self.player_rooms:
                    del self.player_rooms[p.id]

        self.rooms.pop(code, None)
        return True

    async def cleanup_old_rooms(self):
        """Remove rooms older than 2 hours. Called periodically."""
        cutoff = time.time() - 7200
        to_remove = [
            code for code, room in self.rooms.items()
            if room.created_at < cutoff
        ]
        for code in to_remove:
            await self.close_room(code)
        return len(to_remove)

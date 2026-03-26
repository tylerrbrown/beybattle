"""Journey Mode orchestrator for BeyBattle.

Handles free battles, stadium masters, elite challengers, champion,
shop, part drops, part packs, tournaments, and XP/currency progression.
"""

import json
import math
import os
import random
import time

from battle_engine import BeybladeInstance
from beyblade_data import (
    BLADES, RATCHETS, BITS, STADIUMS, VALID_RARITIES,
    get_blade, get_ratchet, get_bit,
    get_random_blade, get_random_ratchet, get_random_bit,
    compute_bey_stats,
)

# ---- Constants -------------------------------------------------------

RARITY_WEIGHTS = {"common": 60, "uncommon": 25, "rare": 14, "legendary": 1}
PITY_THRESHOLD = 30
DROP_BASE_RATE = 0.25  # 25% base drop chance

# Shop items
SHOP_ITEMS = {
    # Repair items (replace potions)
    "quick_repair":     {"name": "Quick Repair",     "price": 300,  "category": "repair", "heal_pct": 0.2},
    "standard_repair":  {"name": "Standard Repair",  "price": 700,  "category": "repair", "heal_pct": 0.5},
    "full_repair":      {"name": "Full Repair",      "price": 1500, "category": "repair", "heal_pct": 1.0},

    # Upgrade crystals (replace rare candy)
    "upgrade_crystal":      {"name": "Upgrade Crystal",     "price": 500,  "category": "upgrade", "levels": 1},
    "upgrade_crystal_xl":   {"name": "Upgrade Crystal XL",  "price": 2000, "category": "upgrade", "levels": 3},
    "upgrade_crystal_max":  {"name": "Upgrade Crystal MAX", "price": 5000, "category": "upgrade", "levels": 5},

    # Part packs (gacha/booster packs - main part acquisition method)
    "basic_pack":     {"name": "Basic Part Pack",     "price": 800,  "category": "pack",
                       "rarity_weights": {"common": 80, "uncommon": 20}},
    "premium_pack":   {"name": "Premium Part Pack",   "price": 2500, "category": "pack",
                       "rarity_weights": {"common": 40, "uncommon": 40, "rare": 20}},
    "legendary_pack": {"name": "Legendary Part Pack", "price": 8000, "category": "pack",
                       "rarity_weights": {"uncommon": 30, "rare": 50, "legendary": 20}},

    # Held items
    "spin_booster": {"name": "Spin Booster", "price": 1500, "category": "held", "effect": "2x_xp"},
}

# Currency awards
REWARD_FREE_BATTLE = 50
REWARD_FREE_WIN_BONUS = 100
REWARD_MASTER_BASE = 500
REWARD_ELITE_WIN = 1000
REWARD_CHAMPION_WIN = 5000
REWARD_PVP_WIN = 500
REWARD_PVP_AI_WIN = 300
REWARD_LEAGUE_WIN = 2000

# Tournament
TOURNAMENT_ROUND_CURRENCY = [1000, 2000, 3000, 10000]

TOURNAMENT_ROUNDS = [
    {"name": "Quarterfinal",          "difficulty": 0.4},
    {"name": "Semifinal",             "difficulty": 0.6},
    {"name": "Final",                 "difficulty": 0.8},
    {"name": "Tournament Champion",   "difficulty": 1.0},
]

TOURNAMENT_NAMES = [
    "Blader Kira", "Storm Rider Hiro", "Veteran Marcus",
    "Burst Queen Lena", "Dragon Tamer Ryuu", "Phantom Elena",
    "Ace Blader Felix", "Iron Fist Kenji", "Shadow Witch Luna",
    "Wave Runner Kai", "Sky Hawk Falk", "Tech Spinner Ada",
    "Trick Shot Dario", "Granite Brock", "Flash Selene",
    "Noble Arthur", "Inferno Blaze", "Glacier Frost",
    "Typhoon Drake", "Spark Penny",
]

TOURNAMENT_TITLES = [
    "Rising Star", "Burst Prodigy", "Tournament Veteran",
    "Fierce Competitor", "Type Specialist", "Strategy Master",
    "Unshakable Will", "Seasoned Warrior", "Dark Horse",
    "Crowd Favorite",
]


# ---- XP / Rank System -----------------------------------------------

def xp_for_rank(level):
    """Total XP to reach a rank level. Medium-fast curve."""
    return int((4 / 5) * level ** 3)


def xp_progress_info(current_xp, current_level):
    """Return (progress_float_0_to_1, xp_to_next, xp_for_current, xp_for_next)."""
    xp_cur = xp_for_rank(current_level)
    xp_nxt = xp_for_rank(current_level + 1)
    span = max(1, xp_nxt - xp_cur)
    progress = min(1.0, max(0.0, (current_xp - xp_cur) / span))
    return progress, xp_nxt - current_xp, xp_cur, xp_nxt


def calc_battle_xp(opponent_level, is_master=False, is_elite=False, is_champion=False):
    """Calculate XP earned from a battle."""
    base = opponent_level * 10
    if is_master:
        base = int(base * 1.5)
    if is_elite:
        base = int(base * 2.0)
    if is_champion:
        base = int(base * 3.0)
    return base


# ---- Stadium Data Helpers -------------------------------------------

def _get_stadium_data(stadium_id="hometown"):
    """Return the stadium dict from STADIUMS by id, or None."""
    for stadium in STADIUMS:
        if stadium.get("id") == stadium_id:
            return stadium
    # Fallback: return first stadium
    if STADIUMS:
        return STADIUMS[0]
    return None


def get_stadium_masters(stadium_id="hometown"):
    """Get list of masters for a stadium.

    Returns list of master dicts straight from stadiums.json.
    """
    stadium = _get_stadium_data(stadium_id)
    if not stadium:
        return []
    return stadium.get("masters", [])


def get_master(master_id, stadium_id="hometown"):
    """Get a single master by numeric id within a stadium."""
    for m in get_stadium_masters(stadium_id):
        if m["id"] == master_id:
            return m
    return None


def get_next_master(defeated_ids, stadium_id="hometown"):
    """Get the next undefeated master.

    defeated_ids: set/list of master IDs the player has beaten.
    Returns the master dict or None if all defeated.
    """
    earned = set(defeated_ids)
    for m in get_stadium_masters(stadium_id):
        if m["id"] not in earned:
            return m
    return None


def get_elite_challengers(stadium_id="hometown"):
    """Get elite challenger list for a stadium."""
    stadium = _get_stadium_data(stadium_id)
    if not stadium:
        return []
    return stadium.get("elite_challengers", [])


def get_elite_challenger(index, stadium_id="hometown"):
    """Get elite challenger by index (0-3)."""
    elites = get_elite_challengers(stadium_id)
    if 0 <= index < len(elites):
        return elites[index]
    return None


def get_champion(stadium_id="hometown"):
    """Get champion data for a stadium."""
    stadium = _get_stadium_data(stadium_id)
    if not stadium:
        return None
    return stadium.get("champion")


# ---- Beyblade Instance Builders -------------------------------------

def create_beyblade_from_config(config, level_override=None):
    """Create a BeybladeInstance from a team config entry.

    config: dict with keys blade, ratchet, bit, and optional level.
    Returns a BeybladeInstance or None if parts not found.
    """
    blade_data = get_blade(config["blade"])
    ratchet_data = get_ratchet(config["ratchet"])
    bit_data = get_bit(config["bit"])
    if not blade_data or not ratchet_data or not bit_data:
        return None
    level = level_override if level_override is not None else config.get("level", 1)
    return BeybladeInstance(blade_data, ratchet_data, bit_data,
                            blade_level=level, ratchet_level=level, bit_level=level)


def build_master_team(master_data):
    """Create list of BeybladeInstance from a stadium master/elite/champion team config."""
    team = []
    for entry in master_data.get("team", []):
        bey = create_beyblade_from_config(entry)
        if bey:
            team.append(bey)
    return team


def build_random_beyblade(avg_level, rarity=None):
    """Build a random BeybladeInstance at roughly the given level.

    Used for free battles and tournament opponents.
    """
    blade = get_random_blade(rarity=rarity)
    ratchet = get_random_ratchet(rarity=rarity)
    bit = get_random_bit(rarity=rarity)
    # Level varies +/- 2 from average
    level = max(1, avg_level + random.randint(-2, 2))
    return BeybladeInstance(blade, ratchet, bit,
                            blade_level=level, ratchet_level=level, bit_level=level)


# ---- Free Battle (Wild Encounter Equivalent) ------------------------

def generate_free_battle(player_avg_level, pity_counter=0, stadium=None):
    """Generate a random opponent for free battle.

    Returns: (opponent_beyblade_instance, opponent_rarity_str)

    - Random blade+ratchet+bit combination
    - Level scales with player average part level +/- 2
    - 25% chance to drop a part on win (handled by roll_part_drop)
    - Pity system: guaranteed rare drop every 30 battles
    - Higher level opponents slightly more likely to drop better parts
    """
    # Pick rarity for the opponent
    if pity_counter >= PITY_THRESHOLD:
        rarity = "rare"
    else:
        rarity = random.choices(
            list(RARITY_WEIGHTS.keys()),
            weights=list(RARITY_WEIGHTS.values()),
            k=1,
        )[0]

    opponent = build_random_beyblade(player_avg_level, rarity=rarity)
    return opponent, rarity


def get_master_rewards(master_data):
    """Return (bey_points, part_drop_data, trophy) for beating a master.

    part_drop_data is a dict with part info if a reward_part is specified,
    otherwise None.
    """
    bp = master_data.get("reward_bp", REWARD_MASTER_BASE)
    trophy = master_data.get("trophy")

    part_drop = None
    reward_part_id = master_data.get("reward_part")
    if reward_part_id:
        # reward_part can be a blade, ratchet, or bit id - check each pool
        part_data = get_blade(reward_part_id)
        part_type = "blade"
        if not part_data:
            part_data = get_ratchet(reward_part_id)
            part_type = "ratchet"
        if not part_data:
            part_data = get_bit(reward_part_id)
            part_type = "bit"
        if part_data:
            part_drop = {
                "part_type": part_type,
                "part_id": reward_part_id,
                "part_name": part_data.get("name", reward_part_id),
                "part_data": part_data,
            }

    return bp, part_drop, trophy


# ---- Part Drop Logic ------------------------------------------------

def _pick_rarity_from_weights(weights):
    """Choose a rarity string from a {rarity: weight} dict."""
    rarities = list(weights.keys())
    w = list(weights.values())
    return random.choices(rarities, weights=w, k=1)[0]


def _random_part_of_rarity(rarity):
    """Return a random (part_type, part_data) of the given rarity.

    Randomly selects blade, ratchet, or bit with equal probability,
    then picks a random part of that type with matching rarity.
    Falls back to any rarity if none match.
    """
    part_type = random.choice(["blade", "ratchet", "bit"])
    if part_type == "blade":
        part = get_random_blade(rarity=rarity)
    elif part_type == "ratchet":
        part = get_random_ratchet(rarity=rarity)
    else:
        part = get_random_bit(rarity=rarity)
    return part_type, part


def roll_part_drop(opponent_rarity="common", pity_counter=0):
    """Roll for a random part drop after winning a battle.

    Returns: (part_info_dict, is_pity) or (None, False)

    - 25% base drop rate
    - Pity: guaranteed at 30 battles without drop
    - Rarity weighted by opponent + slight random upgrade chance
    """
    is_pity = pity_counter >= PITY_THRESHOLD

    if not is_pity and random.random() > DROP_BASE_RATE:
        return None, False

    # Determine drop rarity based on opponent with upgrade chance
    rarity_upgrade = {
        "common":    {"common": 70, "uncommon": 25, "rare": 5},
        "uncommon":  {"common": 30, "uncommon": 50, "rare": 18, "legendary": 2},
        "rare":      {"uncommon": 20, "rare": 60, "legendary": 20},
        "legendary": {"rare": 30, "legendary": 70},
    }
    weights = rarity_upgrade.get(opponent_rarity, rarity_upgrade["common"])

    if is_pity:
        # Pity guarantees at least rare
        weights = {"rare": 70, "legendary": 30}

    rarity = _pick_rarity_from_weights(weights)
    part_type, part_data = _random_part_of_rarity(rarity)

    part_info = {
        "part_type": part_type,
        "part_id": part_data["id"],
        "part_name": part_data.get("name", part_data["id"]),
        "rarity": part_data.get("rarity", rarity),
        "part_data": part_data,
    }
    return part_info, is_pity


def open_part_pack(pack_type):
    """Open a part pack from the shop.

    Returns: list containing one part dict, or empty list if invalid pack.
    Each part dict has: part_type, part_id, part_name, rarity, part_data
    """
    pack = SHOP_ITEMS.get(pack_type)
    if not pack or pack.get("category") != "pack":
        return []

    weights = pack.get("rarity_weights", {"common": 100})
    rarity = _pick_rarity_from_weights(weights)
    part_type, part_data = _random_part_of_rarity(rarity)

    return [{
        "part_type": part_type,
        "part_id": part_data["id"],
        "part_name": part_data.get("name", part_data["id"]),
        "rarity": part_data.get("rarity", rarity),
        "part_data": part_data,
    }]


# ---- Free Battle State (Wild Encounter Equivalent) ------------------

class FreeBattle:
    """Manages a single free battle encounter."""

    ACTION_TIMEOUT = 30

    def __init__(self, player, player_team, opponent_bey, opponent_rarity):
        self.player = player
        self.team = player_team          # list of BeybladeInstance
        self.opponent = opponent_bey      # BeybladeInstance
        self.opponent_rarity = opponent_rarity
        self.active_idx = 0
        self.state = "ACTION_SELECT"
        self.turn_count = 0
        self.created_at = time.time()

        # Find first non-stopped bey
        for i, bey in enumerate(self.team):
            if bey.is_alive():
                self.active_idx = i
                break

    def get_active(self):
        return self.team[self.active_idx]

    def alive_indices(self):
        return [i for i, bey in enumerate(self.team) if bey.is_alive()]

    def all_stopped(self):
        return all(not bey.is_alive() for bey in self.team)

    def serialize_state(self):
        """Get current state for the client."""
        active = self.get_active()
        return {
            "opponent": self.opponent.to_dict(),
            "opponent_rarity": self.opponent_rarity,
            "your_beyblade": active.to_dict(),
            "your_team": [bey.to_dict() for bey in self.team],
            "active_index": self.active_idx,
        }


# ---- Tournament System ---------------------------------------------

class TournamentState:
    """Tracks a player tournament run."""

    def __init__(self, player_id, stadium_id="hometown"):
        self.player_id = player_id
        self.stadium_id = stadium_id
        self.round = 0          # 0-3 (quarterfinal to champion)
        self.wins = 0
        self.losses = 0
        self.rounds = []        # list of {status, opponent, result}
        self.is_over = False
        self.is_champion = False
        self.created_at = time.time()

    def get_current_round_info(self):
        """Return the TOURNAMENT_ROUNDS entry for the current round."""
        if self.round < len(TOURNAMENT_ROUNDS):
            return TOURNAMENT_ROUNDS[self.round]
        return TOURNAMENT_ROUNDS[-1]

    def get_current_opponent(self, player_avg_level):
        """Generate opponent for current round. Difficulty scales 0.4 to 1.0.

        Returns a dict with: id, name, title, team (list of BeybladeInstance),
        reward_bp, round_num, round_name, difficulty, dialog_intro/win/lose.
        """
        round_info = self.get_current_round_info()
        difficulty = round_info["difficulty"]
        round_name = round_info["name"]

        name = random.choice(TOURNAMENT_NAMES)
        title = random.choice(TOURNAMENT_TITLES)

        # Team size scales with round: 1 QF, 2 SF, 2 F, 3 Championship
        team_sizes = [1, 2, 2, 3]
        team_size = team_sizes[min(self.round, 3)]

        # Level offset increases with difficulty
        level_offset = int(difficulty * 10)
        team = []
        for _ in range(team_size):
            level = max(1, int(player_avg_level + level_offset + random.randint(-2, 3)))
            # Higher rounds get better rarity parts
            if difficulty >= 0.8:
                rarity = random.choice(["uncommon", "rare", "rare"])
            elif difficulty >= 0.6:
                rarity = random.choice(["common", "uncommon", "rare"])
            else:
                rarity = random.choice(["common", "common", "uncommon"])
            bey = build_random_beyblade(level, rarity=rarity)
            team.append(bey)

        reward = TOURNAMENT_ROUND_CURRENCY[min(self.round, 3)]

        if self.round == 3:
            dialog_intro = "I am " + name + "! The Championship round - only the strongest make it here!"
            dialog_win = "Incredible! You are the Tournament Champion! What a display of power!"
            dialog_lose = "So close to the championship... Train harder and come back!"
        else:
            dialog_intro = "I am " + name + "! This is the " + round_name + " - do not expect me to go easy!"
            dialog_win = "Amazing battle! You have earned your place in the next round!"
            dialog_lose = "The tournament is over for you. Better luck next time!"

        return {
            "id": f"tournament_r{self.round}",
            "name": name,
            "title": title,
            "team": team,
            "reward_bp": reward,
            "round_num": self.round,
            "round_name": round_name,
            "difficulty": difficulty,
            "dialog_intro": dialog_intro,
            "dialog_win": dialog_win,
            "dialog_lose": dialog_lose,
        }

    def record_result(self, won):
        """Record win/loss, advance or eliminate.

        Returns: (advanced_bool, tournament_complete_bool, is_champion_bool)
        """
        round_info = self.get_current_round_info()
        self.rounds.append({
            "round": self.round,
            "round_name": round_info["name"],
            "result": "win" if won else "loss",
        })

        if won:
            self.wins += 1
            if self.round >= len(TOURNAMENT_ROUNDS) - 1:
                # Won the championship
                self.is_over = True
                self.is_champion = True
                return True, True, True
            else:
                self.round += 1
                return True, False, False
        else:
            self.losses += 1
            self.is_over = True
            return False, True, False

    def is_complete(self):
        """True if tournament is won or player eliminated."""
        return self.is_over

    def serialize(self):
        """Serialize tournament state for the client."""
        return {
            "player_id": self.player_id,
            "stadium_id": self.stadium_id,
            "round": self.round,
            "wins": self.wins,
            "losses": self.losses,
            "rounds": self.rounds,
            "is_over": self.is_over,
            "is_champion": self.is_champion,
            "current_round_info": self.get_current_round_info(),
        }


def generate_tournament_bracket(player_avg_level, stadium_id="hometown"):
    """Generate a full 4-round tournament bracket.

    Returns a list of 4 opponent dicts (one per round), pre-generated
    so the bracket can be displayed before the tournament starts.
    """
    state = TournamentState("preview", stadium_id)
    bracket = []
    for i in range(len(TOURNAMENT_ROUNDS)):
        state.round = i
        opponent = state.get_current_opponent(player_avg_level)
        bracket.append(opponent)
    return bracket

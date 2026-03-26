"""AI opponent for single-player BeyBattle.

Bot player that duck-types as Player, making decisions server-side
without a WebSocket connection. Supports difficulty scaling for
campaign opponents and free battles.
"""

import random
import string
from battle_engine import BeybladeInstance, create_beyblade
from beyblade_data import (
    get_random_blade, get_random_ratchet, get_random_bit,
    get_attack, BLADES, RATCHETS, BITS, VALID_RARITIES,
)

BOT_NAMES = [
    "Blader DJ", "Valt Aoi", "Shu Kurenai", "Rantaro Kiyama",
    "Free De La Hoya", "Lui Shirosagi", "Xander Shakadera",
    "Wakiya Murasaki", "Daigo Kurogami", "Ken Midori",
    "Zac the Sunrise", "Quon Limon", "Naoki Minamo",
    "Orochi Ginba", "Daina Kurogami", "Boa Alcazaba",
    "Cuza Ackermann", "Silas Karlisle", "Rickson Clay",
    "Joshua Burns",
]

# Difficulty presets: higher values = smarter AI
DIFFICULTY_PRESETS = {
    0.3: {
        "action_quality": 0.4,
        "dodge_chance": 0.15,
        "launch_accuracy": 0.5,
        "special_timing": 0.2,
    },
    0.5: {
        "action_quality": 0.6,
        "dodge_chance": 0.25,
        "launch_accuracy": 0.65,
        "special_timing": 0.4,
    },
    0.7: {
        "action_quality": 0.75,
        "dodge_chance": 0.35,
        "launch_accuracy": 0.8,
        "special_timing": 0.6,
    },
    0.9: {
        "action_quality": 0.9,
        "dodge_chance": 0.45,
        "launch_accuracy": 0.95,
        "special_timing": 0.8,
    },
}


def _interpolate_preset(difficulty):
    """Get preset values for a given difficulty, interpolating between keys."""
    presets = sorted(DIFFICULTY_PRESETS.keys())
    if difficulty in DIFFICULTY_PRESETS:
        return dict(DIFFICULTY_PRESETS[difficulty])

    lo_key = max((k for k in presets if k <= difficulty), default=presets[0])
    hi_key = min((k for k in presets if k >= difficulty), default=presets[-1])

    if lo_key == hi_key:
        return dict(DIFFICULTY_PRESETS[lo_key])

    t = (difficulty - lo_key) / (hi_key - lo_key)
    lo_p = DIFFICULTY_PRESETS[lo_key]
    hi_p = DIFFICULTY_PRESETS[hi_key]
    return {k: lo_p[k] + t * (hi_p[k] - lo_p[k]) for k in lo_p}


class BotPlayer:
    """AI player with the same interface as Player."""

    def __init__(self, difficulty=0.5, beyblade=None, name=None):
        self.ws = None
        self.id = "bot_" + "".join(
            random.choices(string.ascii_lowercase + string.digits, k=8)
        )
        self.username = name or random.choice(BOT_NAMES)
        self.account_id = None
        self.room_code = None
        self.beyblade = beyblade      # BeybladeInstance
        self.spin_direction = "right"
        self.launch_power = 0.5
        self.launch_position = "middle"
        self.ready = False
        self.chosen_action = None
        self.is_bot = True

        # Difficulty knobs
        self._difficulty = difficulty
        preset = _interpolate_preset(difficulty)
        self._action_quality = preset["action_quality"]
        self._dodge_chance = preset["dodge_chance"]
        self._launch_accuracy = preset["launch_accuracy"]
        self._special_timing = preset["special_timing"]

    async def send(self, msg):
        """No-op: bot has no WebSocket."""
        pass

    # ---- Beyblade Selection ----

    def choose_beyblade(self, available_parts=None):
        """Select parts based on difficulty.

        Higher difficulty = better part choices (higher rarity, better synergy).
        Lower difficulty = more random selection.
        """
        if self._difficulty >= 0.8:
            max_rarity = "legendary"
        elif self._difficulty >= 0.6:
            max_rarity = "rare"
        elif self._difficulty >= 0.4:
            max_rarity = "uncommon"
        else:
            max_rarity = "common"

        rarity_order = ["common", "uncommon", "rare", "legendary"]
        allowed_rarities = rarity_order[:rarity_order.index(max_rarity) + 1]

        # Pick blade with type-appropriate strategy
        blade_candidates = [
            b for b in BLADES.values()
            if b.get("rarity", "common") in allowed_rarities
        ]
        if not blade_candidates:
            blade_candidates = list(BLADES.values())

        # Higher difficulty biases toward higher rarity
        if self._difficulty >= 0.7 and len(blade_candidates) > 1:
            blade_candidates.sort(
                key=lambda b: rarity_order.index(b.get("rarity", "common")),
                reverse=True,
            )
            # Pick from top third
            top_n = max(1, len(blade_candidates) // 3)
            blade = random.choice(blade_candidates[:top_n])
        else:
            blade = random.choice(blade_candidates)

        # Pick ratchet
        ratchet_candidates = [
            r for r in RATCHETS.values()
            if r.get("rarity", "common") in allowed_rarities
        ]
        if not ratchet_candidates:
            ratchet_candidates = list(RATCHETS.values())
        ratchet = random.choice(ratchet_candidates)

        # Pick bit matching blade type for synergy at higher difficulty
        bit_candidates = [
            b for b in BITS.values()
            if b.get("rarity", "common") in allowed_rarities
        ]
        if not bit_candidates:
            bit_candidates = list(BITS.values())

        bey_type = blade.get("type", "balance")
        movement_map = {
            "attack": "aggressive",
            "defense": "defensive",
            "stamina": "stamina",
            "balance": None,
        }
        preferred_movement = movement_map.get(bey_type)

        if preferred_movement and self._difficulty >= 0.6:
            synergy_bits = [
                b for b in bit_candidates
                if b.get("movement") == preferred_movement
            ]
            if synergy_bits:
                bit = random.choice(synergy_bits)
            else:
                bit = random.choice(bit_candidates)
        else:
            bit = random.choice(bit_candidates)

        self.beyblade = create_beyblade(blade["id"], ratchet["id"], bit["id"])
        self.ready = True
        return self.beyblade

    # ---- Launch Parameters ----

    def choose_launch(self, opponent_type=None):
        """Pick spin/power/position based on difficulty.

        Higher difficulty = more accurate launch power, smarter positioning.
        """
        # Spin direction: left spin counters right spin (bonus damage)
        # Higher difficulty more likely to counter
        if opponent_type == "attack" and random.random() < self._difficulty:
            self.spin_direction = "left"
        else:
            self.spin_direction = random.choice(["left", "right"])

        # Launch power: higher difficulty = closer to optimal (0.85-0.95 range)
        optimal = 0.9
        spread = 0.4 * (1.0 - self._launch_accuracy)
        self.launch_power = max(0.1, min(1.0,
            optimal + random.uniform(-spread, spread)
        ))

        # Position: type-aware at higher difficulty
        if self._difficulty >= 0.5 and self.beyblade:
            bey_type = self.beyblade.bey_type
            if bey_type == "attack":
                self.launch_position = random.choice(["inside", "middle"])
            elif bey_type == "defense":
                self.launch_position = "middle"
            elif bey_type == "stamina":
                self.launch_position = random.choice(["middle", "outside"])
            else:
                self.launch_position = "middle"
        else:
            self.launch_position = random.choice(
                ["inside", "middle", "outside"]
            )

        return self.spin_direction, self.launch_power, self.launch_position

    # ---- Battle Actions ----

    def choose_action(self, stadium_state):
        """Pick action during battle based on difficulty and bey type.

        Returns action string: "rush", "guard", "conserve", or special move id.
        """
        if not self.beyblade:
            return "rush"

        bey_type = self.beyblade.bey_type
        bey = self.beyblade

        # Check if special should be used
        if self._should_use_special(stadium_state):
            return bey.special_move_id

        # Random action at low difficulty
        if random.random() > self._action_quality:
            return random.choice(["rush", "guard", "conserve"])

        # Type-aware strategy at higher difficulty
        stamina_pct = bey.current_stamina / bey.max_stamina

        if bey_type == "attack":
            # Attack types favor rush, guard when low
            if stamina_pct < 0.3:
                return random.choice(["guard", "conserve"])
            return "rush"

        elif bey_type == "defense":
            # Defense types favor guard, rush when opponent is weak
            if self._opponent_is_weak(stadium_state):
                return "rush"
            return "guard"

        elif bey_type == "stamina":
            # Stamina types conserve, rush when opponent is very low
            if self._opponent_is_weak(stadium_state):
                return "rush"
            return "conserve"

        else:  # balance
            # Balanced approach
            if stamina_pct > 0.6:
                return random.choice(["rush", "rush", "guard"])
            elif stamina_pct > 0.3:
                return random.choice(["guard", "conserve"])
            else:
                return "conserve"

    def _should_use_special(self, stadium_state):
        """Decide whether to use special move now."""
        if not self.beyblade or not self.beyblade.special_move_id:
            return False
        if self.beyblade.special_used:
            return False

        # Higher difficulty saves special for burst opportunity
        if random.random() > self._special_timing:
            return False

        # Use special when opponent burst meter is high (close to burst)
        is_p1 = stadium_state.bey1 is self.beyblade
        opponent = stadium_state.bey2 if is_p1 else stadium_state.bey1
        burst_pct = opponent.burst_meter / 100.0

        if burst_pct > 0.6:
            return True

        # Or when our stamina is getting low (desperation)
        stamina_pct = self.beyblade.current_stamina / self.beyblade.max_stamina
        if stamina_pct < 0.25:
            return True

        # Low difficulty: random use
        if self._difficulty < 0.4 and random.random() < 0.15:
            return True

        return False

    def _opponent_is_weak(self, stadium_state):
        """Check if opponent stamina or burst meter suggests vulnerability."""
        is_p1 = stadium_state.bey1 is self.beyblade
        opponent = stadium_state.bey2 if is_p1 else stadium_state.bey1
        stamina_pct = opponent.current_stamina / opponent.max_stamina
        return stamina_pct < 0.3 or opponent.burst_meter > 70

    # ---- Dodge ----

    def get_dodge_result(self, difficulty=None):
        """Return True/False for dodge success (15-45% based on difficulty)."""
        chance = self._dodge_chance
        if difficulty is not None:
            preset = _interpolate_preset(difficulty)
            chance = preset["dodge_chance"]
        return random.random() < chance


def generate_opponent_beyblade(avg_level=1, bey_type=None, rarity_max="rare"):
    """Create a random beyblade from available data, scaled to player level.

    Used by journey.py for stadium masters, free battles, etc.

    Args:
        avg_level: target part level (1-50). Parts get +/- 2 random variance.
        bey_type: optional type filter ("attack"/"defense"/"stamina"/"balance").
        rarity_max: highest rarity allowed ("common"/"uncommon"/"rare"/"legendary").

    Returns:
        BeybladeInstance with randomized parts at appropriate level.
    """
    rarity_order = ["common", "uncommon", "rare", "legendary"]
    max_idx = rarity_order.index(rarity_max) if rarity_max in rarity_order else 2
    allowed = rarity_order[:max_idx + 1]

    # Pick blade
    blade_pool = [
        b for b in BLADES.values()
        if b.get("rarity", "common") in allowed
    ]
    if bey_type:
        typed = [b for b in blade_pool if b.get("type") == bey_type]
        if typed:
            blade_pool = typed
    if not blade_pool:
        blade_pool = list(BLADES.values())
    blade = random.choice(blade_pool)

    # Pick ratchet
    ratchet_pool = [
        r for r in RATCHETS.values()
        if r.get("rarity", "common") in allowed
    ]
    if not ratchet_pool:
        ratchet_pool = list(RATCHETS.values())
    ratchet = random.choice(ratchet_pool)

    # Pick bit
    bit_pool = [
        b for b in BITS.values()
        if b.get("rarity", "common") in allowed
    ]
    if not bit_pool:
        bit_pool = list(BITS.values())
    bit = random.choice(bit_pool)

    # Level with variance
    blade_level = max(1, avg_level + random.randint(-2, 2))
    ratchet_level = max(1, avg_level + random.randint(-2, 2))
    bit_level = max(1, avg_level + random.randint(-2, 2))

    return create_beyblade(
        blade["id"], ratchet["id"], bit["id"],
        blade_level=blade_level,
        ratchet_level=ratchet_level,
        bit_level=bit_level,
    )

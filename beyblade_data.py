"""Load and validate Beyblade game data at startup."""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Global data loaded at startup
BLADES = {}          # blade_id (str) -> blade dict
RATCHETS = {}        # ratchet_id (str) -> ratchet dict
BITS = {}            # bit_id (str) -> bit dict
ATTACKS = {}         # attack_id (str) -> attack dict
TYPE_MATCHUPS = {}   # bey_type -> bey_type -> multiplier (4x4)
ELEMENT_CHART = {}   # element -> element -> multiplier (8x8)
STADIUMS = []        # stadium tier data
BLADES_LIST = []     # ordered list for client
RATCHETS_LIST = []   # ordered list for client
BITS_LIST = []       # ordered list for client

# Starter kits: blade_id, ratchet_id, bit_id
STARTER_KITS = {
    "attack_starter": {
        "name": "Dran Sword Kit",
        "description": "A fierce Attack-type starter. High collision damage, aggressive movement.",
        "blade": "dran_sword",
        "ratchet": "3-60",
        "bit": "flat",
    },
    "defense_starter": {
        "name": "Knight Shield Kit",
        "description": "A sturdy Defense-type starter. Tanks hits and resists bursts.",
        "blade": "knight_shield",
        "ratchet": "5-80",
        "bit": "ball",
    },
    "stamina_starter": {
        "name": "Wizard Arrow Kit",
        "description": "A patient Stamina-type starter. Outlasts opponents with superior spin time.",
        "blade": "wizard_arrow",
        "ratchet": "4-70",
        "bit": "needle",
    },
}

# Valid types and elements
VALID_TYPES = {"attack", "defense", "stamina", "balance"}
VALID_ELEMENTS = {"fire", "water", "wind", "earth", "lightning", "ice", "dark", "light"}
VALID_MOVEMENTS = {"aggressive", "defensive", "stamina", "erratic"}
VALID_RARITIES = {"common", "uncommon", "rare", "legendary"}
VALID_CATEGORIES = {"strike", "defense", "stamina_drain", "burst", "status"}


def load_data():
    """Load all JSON data files. Call once at startup."""
    global BLADES, RATCHETS, BITS, ATTACKS, TYPE_MATCHUPS, ELEMENT_CHART, STADIUMS
    global BLADES_LIST, RATCHETS_LIST, BITS_LIST

    with open(os.path.join(DATA_DIR, "blades.json")) as f:
        blades_list = json.load(f)

    with open(os.path.join(DATA_DIR, "ratchets.json")) as f:
        ratchets_list = json.load(f)

    with open(os.path.join(DATA_DIR, "bits.json")) as f:
        bits_list = json.load(f)

    with open(os.path.join(DATA_DIR, "attacks.json")) as f:
        ATTACKS = json.load(f)

    with open(os.path.join(DATA_DIR, "type_matchups.json")) as f:
        TYPE_MATCHUPS = json.load(f)

    with open(os.path.join(DATA_DIR, "element_chart.json")) as f:
        ELEMENT_CHART = json.load(f)

    stadiums_path = os.path.join(DATA_DIR, "stadiums.json")
    if os.path.exists(stadiums_path):
        with open(stadiums_path) as f:
            STADIUMS = json.load(f)

    # Index blades by id
    for blade in blades_list:
        BLADES[blade["id"]] = blade

    # Index ratchets by id
    for ratchet in ratchets_list:
        RATCHETS[ratchet["id"]] = ratchet

    # Index bits by id
    for bit in bits_list:
        BITS[bit["id"]] = bit

    # Build ordered lists for client
    BLADES_LIST = sorted(blades_list, key=lambda b: (
        {"common": 0, "uncommon": 1, "rare": 2, "legendary": 3}.get(b["rarity"], 0),
        b["name"]
    ))
    RATCHETS_LIST = sorted(ratchets_list, key=lambda r: r["name"])
    BITS_LIST = sorted(bits_list, key=lambda b: b["name"])

    # Validate starter kits reference real parts
    for kit_id, kit in STARTER_KITS.items():
        assert kit["blade"] in BLADES, f"Starter kit {kit_id} references unknown blade: {kit['blade']}"
        assert kit["ratchet"] in RATCHETS, f"Starter kit {kit_id} references unknown ratchet: {kit['ratchet']}"
        assert kit["bit"] in BITS, f"Starter kit {kit_id} references unknown bit: {kit['bit']}"

    print(f"Loaded {len(BLADES)} blades, {len(RATCHETS)} ratchets, {len(BITS)} bits, {len(ATTACKS)} attacks")
    print(f"Type matchups: {len(TYPE_MATCHUPS)}x{len(TYPE_MATCHUPS)} | Element chart: {len(ELEMENT_CHART)}x{len(ELEMENT_CHART)}")
    print(f"Stadiums: {len(STADIUMS)} tiers")


def get_type_matchup(attacker_type, defender_type):
    """Get type effectiveness multiplier (Attack/Defense/Stamina/Balance)."""
    if attacker_type in TYPE_MATCHUPS and defender_type in TYPE_MATCHUPS[attacker_type]:
        return TYPE_MATCHUPS[attacker_type][defender_type]
    return 1.0


def get_element_effectiveness(atk_element, def_element):
    """Get element effectiveness multiplier."""
    if atk_element in ELEMENT_CHART and def_element in ELEMENT_CHART[atk_element]:
        return ELEMENT_CHART[atk_element][def_element]
    return 1.0


def get_blade(blade_id):
    """Get blade data by id."""
    return BLADES.get(blade_id)


def get_ratchet(ratchet_id):
    """Get ratchet data by id."""
    return RATCHETS.get(ratchet_id)


def get_bit(bit_id):
    """Get bit data by id."""
    return BITS.get(bit_id)


def get_attack(attack_id):
    """Get attack data by id."""
    return ATTACKS.get(attack_id)


def get_random_blade(rarity=None, bey_type=None):
    """Get a random blade, optionally filtered by rarity and/or type."""
    candidates = list(BLADES.values())
    if rarity:
        candidates = [b for b in candidates if b["rarity"] == rarity]
    if bey_type:
        candidates = [b for b in candidates if b["type"] == bey_type]
    if not candidates:
        candidates = list(BLADES.values())
    import random
    return random.choice(candidates)


def get_random_ratchet(rarity=None):
    """Get a random ratchet, optionally filtered by rarity."""
    candidates = list(RATCHETS.values())
    if rarity:
        candidates = [r for r in candidates if r["rarity"] == rarity]
    if not candidates:
        candidates = list(RATCHETS.values())
    import random
    return random.choice(candidates)


def get_random_bit(rarity=None, movement=None):
    """Get a random bit, optionally filtered by rarity and/or movement type."""
    candidates = list(BITS.values())
    if rarity:
        candidates = [b for b in candidates if b["rarity"] == rarity]
    if movement:
        candidates = [b for b in candidates if b["movement"] == movement]
    if not candidates:
        candidates = list(BITS.values())
    import random
    return random.choice(candidates)


def compute_bey_stats(blade_data, ratchet_data, bit_data, blade_level=1, ratchet_level=1, bit_level=1):
    """Compute assembled Beyblade stats from three parts.

    Returns dict with: attack, defense, stamina, speed, burst_resist, weight,
    type, element, special_move, movement, height, teeth
    """
    # Level bonus: +2% per level above 1 for that part's stats
    def level_mult(level):
        return 1.0 + (level - 1) * 0.02

    blade_mult = level_mult(blade_level)
    ratchet_mult = level_mult(ratchet_level)
    bit_mult = level_mult(bit_level)

    bs = blade_data["base_stats"]
    rs = ratchet_data["base_stats"]
    bts = bit_data["base_stats"]

    stats = {
        "attack": int(bs["attack"] * blade_mult + rs["attack"] * ratchet_mult + bts["attack"] * bit_mult),
        "defense": int(bs["defense"] * blade_mult + rs["defense"] * ratchet_mult + bts["defense"] * bit_mult),
        "stamina": int(bs["stamina"] * blade_mult + rs["stamina"] * ratchet_mult + bts["stamina"] * bit_mult),
        "speed": int(bs["speed"] * blade_mult + rs["speed"] * ratchet_mult + bts["speed"] * bit_mult),
        "burst_resist": int(bs["burst_resist"] * blade_mult + rs["burst_resist"] * ratchet_mult + bts["burst_resist"] * bit_mult),
        "weight": blade_data["weight"] + ratchet_data["weight"] + bit_data["weight"],
        "type": blade_data["type"],
        "element": blade_data["element"],
        "special_move": blade_data["special_move"],
        "movement": bit_data["movement"],
        "stamina_drain": bit_data["stamina_drain"],
        "height": ratchet_data["height"],
        "teeth": ratchet_data["teeth"],
    }
    return stats


def get_client_data():
    """Return data payload for the client (sent on login)."""
    # Enrich starter kits with full blade/ratchet/bit data and combined stats
    enriched_kits = {}
    for kit_id, kit in STARTER_KITS.items():
        blade_data = BLADES.get(kit["blade"], {})
        ratchet_data = RATCHETS.get(kit["ratchet"], {})
        bit_data = BITS.get(kit["bit"], {})
        combined = compute_bey_stats(blade_data, ratchet_data, bit_data) if blade_data and ratchet_data and bit_data else {}
        enriched_kits[kit_id] = {
            **kit,
            "blade_data": blade_data,
            "ratchet_data": ratchet_data,
            "bit_data": bit_data,
            "combined_stats": combined,
        }
    return {
        "blades": BLADES_LIST,
        "ratchets": RATCHETS_LIST,
        "bits": BITS_LIST,
        "attacks": ATTACKS,
        "type_matchups": TYPE_MATCHUPS,
        "element_chart": ELEMENT_CHART,
        "starter_kits": enriched_kits,
    }

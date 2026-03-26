"""Player account persistence for BeyBattle.

SQLite-backed account system with parts inventory, beyblade assembly,
team management, catalog tracking, and battle history.
"""

import json
import secrets
import sqlite3
import time


# XP curve: medium-fast growth rate - total XP at level N = (4/5) * N^3
def xp_for_level(level):
    """Total XP required to reach a given level."""
    if level <= 1:
        return 0
    return int((4 / 5) * level ** 3)


def xp_to_next_level(level, current_xp):
    """XP remaining to reach the next level."""
    if level >= 100:
        return 0
    return max(0, xp_for_level(level + 1) - current_xp)


def xp_progress_info(level, current_xp):
    """Compute XP progress data for UI display."""
    if level >= 100:
        return {"xp_progress": 1.0, "xp_to_next": 0,
                "xp_for_current_level": current_xp, "xp_for_next_level": current_xp}
    cur_level_xp = xp_for_level(level)
    next_level_xp = xp_for_level(level + 1)
    span = next_level_xp - cur_level_xp
    progress_in_level = current_xp - cur_level_xp
    return {
        "xp_progress": max(0.0, min(1.0, progress_in_level / span)) if span > 0 else 1.0,
        "xp_to_next": max(0, next_level_xp - current_xp),
        "xp_for_current_level": cur_level_xp,
        "xp_for_next_level": next_level_xp,
    }


class AccountManager:
    """Manages player accounts and all game persistence in SQLite."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._init_tables()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    def _init_tables(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                token_ TEXT UNIQUE NOT NULL,
                user_pin TEXT,
                bey_points INTEGER DEFAULT 500,
                xp INTEGER DEFAULT 0,
                rank_level INTEGER DEFAULT 1,
                rating INTEGER DEFAULT 1000,
                starter_blade_id TEXT,
                team_id TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS player_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                part_id TEXT NOT NULL,
                part_type TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT 'common',
                level INTEGER DEFAULT 1,
                xp INTEGER DEFAULT 0,
                is_shiny INTEGER DEFAULT 0,
                obtained_at INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id)
            );
            CREATE INDEX IF NOT EXISTS idx_parts_player ON player_parts(player_id);
            CREATE INDEX IF NOT EXISTS idx_parts_type ON player_parts(player_id, part_type);

            CREATE TABLE IF NOT EXISTS player_beyblades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                nickname TEXT,
                blade_part_id INTEGER NOT NULL,
                ratchet_part_id INTEGER NOT NULL,
                bit_part_id INTEGER NOT NULL,
                is_in_team INTEGER DEFAULT 0,
                team_slot INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id),
                FOREIGN KEY (blade_part_id) REFERENCES player_parts(id),
                FOREIGN KEY (ratchet_part_id) REFERENCES player_parts(id),
                FOREIGN KEY (bit_part_id) REFERENCES player_parts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_bey_player ON player_beyblades(player_id);
            CREATE INDEX IF NOT EXISTS idx_bey_team ON player_beyblades(player_id, is_in_team);

            CREATE TABLE IF NOT EXISTS player_trophies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                master_id TEXT NOT NULL,
                stadium_tier TEXT NOT NULL,
                earned_at INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(player_id, master_id, stadium_tier)
            );
            CREATE INDEX IF NOT EXISTS idx_trophies_player ON player_trophies(player_id);

            CREATE TABLE IF NOT EXISTS player_progression (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                milestone TEXT NOT NULL,
                completed_at INTEGER NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(player_id, milestone)
            );

            CREATE TABLE IF NOT EXISTS player_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                quantity INTEGER DEFAULT 0,
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(player_id, item_type)
            );
            CREATE INDEX IF NOT EXISTS idx_inv_player ON player_inventory(player_id);

            CREATE TABLE IF NOT EXISTS player_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                part_id TEXT NOT NULL,
                seen INTEGER DEFAULT 0,
                obtained INTEGER DEFAULT 0,
                first_seen_at INTEGER,
                first_obtained_at INTEGER,
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(player_id, part_id)
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_player ON player_catalog(player_id);

            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT,
                player1_name TEXT,
                player2_name TEXT,
                player1_bey TEXT,
                player2_bey TEXT,
                winner TEXT,
                finish_type TEXT,
                duration_sec REAL,
                finished_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS bug_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER,
                username TEXT,
                description TEXT,
                game_state TEXT,
                submitted_at INTEGER NOT NULL
            );
        """)
        conn.commit()
        conn.close()

    # ---- Registration & Login ----------------------------------------

    def register(self, username, user_pin=None):
        """Register a new player. Returns (player_dict, error_string)."""
        username = username.strip()
        if not username or len(username) < 2 or len(username) > 16:
            return None, "Username must be 2-16 characters."
        if not all(c.isalnum() or c == ' ' for c in username):
            return None, "Letters, numbers, and spaces only."
        if user_pin is not None:
            user_pin = str(user_pin).strip()
            if len(user_pin) != 4 or not user_pin.isdigit():
                return None, "PIN must be exactly 4 digits."

        token_val = secrets.token_urlsafe(18)  # 24 chars
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO players (username, token_, user_pin, created_at) VALUES (?, ?, ?, ?)",
                (username, token_val, user_pin, int(time.time()))
            )
            conn.commit()
            player_id = conn.execute(
                "SELECT id FROM players WHERE token_ = ?", (token_val,)
            ).fetchone()["id"]
            conn.close()
            return {"id": player_id, "username": username, "token_": token_val,
                    "starter_blade_id": None, "bey_points": 500}, None
        except sqlite3.IntegrityError:
            conn.close()
            return None, "Username already taken."

    def login_with_token(self, token_val):
        """Resume session by token_. Returns player dict or None."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM players WHERE token_ = ?", (token_val,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return self._row_to_dict(row)

    def login_with_username(self, username, user_pin):
        """Login by username + 4-digit PIN. Returns player dict or None."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM players WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        stored = row["user_pin"]
        if stored is None or str(stored) != str(user_pin):
            return None
        return self._row_to_dict(row)

    def _row_to_dict(self, row):
        d = dict(row)
        return {
            "id": d["id"],
            "username": d["username"],
            "token_": d["token_"],
            "starter_blade_id": d["starter_blade_id"],
            "bey_points": d.get("bey_points", 500),
            "xp": d.get("xp", 0),
            "rank_level": d.get("rank_level", 1),
            "rating": d.get("rating", 1000),
            "has_pin": d.get("user_pin") is not None,
        }

    # ---- Starter Selection -------------------------------------------

    def choose_starter(self, player_id, kit_id):
        """Give starter parts (blade+ratchet+bit), assemble first beyblade, add to team.
        kit_id must be a key in beyblade_data.STARTER_KITS.
        Returns True on success, False on failure.
        """
        # Import here to avoid circular imports at module level
        from beyblade_data import STARTER_KITS, BLADES, RATCHETS, BITS

        if kit_id not in STARTER_KITS:
            return False

        conn = self._conn()
        row = conn.execute(
            "SELECT starter_blade_id FROM players WHERE id = ?", (player_id,)
        ).fetchone()
        if not row or row["starter_blade_id"] is not None:
            conn.close()
            return False

        kit = STARTER_KITS[kit_id]
        blade_id = kit["blade"]
        ratchet_id = kit["ratchet"]
        bit_id = kit["bit"]
        now = int(time.time())

        # Determine rarities from data
        blade_rarity = BLADES.get(blade_id, {}).get("rarity", "common")
        ratchet_rarity = RATCHETS.get(ratchet_id, {}).get("rarity", "common")
        bit_rarity = BITS.get(bit_id, {}).get("rarity", "common")

        # Add the three parts
        conn.execute(
            "INSERT INTO player_parts (player_id, part_id, part_type, rarity, obtained_at) VALUES (?, ?, 'blade', ?, ?)",
            (player_id, blade_id, blade_rarity, now)
        )
        blade_part_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO player_parts (player_id, part_id, part_type, rarity, obtained_at) VALUES (?, ?, 'ratchet', ?, ?)",
            (player_id, ratchet_id, ratchet_rarity, now)
        )
        ratchet_part_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO player_parts (player_id, part_id, part_type, rarity, obtained_at) VALUES (?, ?, 'bit', ?, ?)",
            (player_id, bit_id, bit_rarity, now)
        )
        bit_part_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Assemble first beyblade
        conn.execute(
            """INSERT INTO player_beyblades
               (player_id, nickname, blade_part_id, ratchet_part_id, bit_part_id, is_in_team, team_slot, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 0, ?)""",
            (player_id, kit["name"], blade_part_db_id, ratchet_part_db_id, bit_part_db_id, now)
        )

        # Mark starter
        conn.execute(
            "UPDATE players SET starter_blade_id = ? WHERE id = ?",
            (blade_id, player_id)
        )

        # Update catalog for all three parts
        for pid in (blade_id, ratchet_id, bit_id):
            conn.execute(
                """INSERT INTO player_catalog (player_id, part_id, seen, obtained, first_seen_at, first_obtained_at)
                   VALUES (?, ?, 1, 1, ?, ?)
                   ON CONFLICT(player_id, part_id) DO UPDATE SET seen = 1, obtained = 1,
                   first_obtained_at = COALESCE(first_obtained_at, ?)""",
                (player_id, pid, now, now, now)
            )

        conn.commit()
        conn.close()
        return True

    # ---- Profile -----------------------------------------------------

    def get_profile(self, player_id):
        """Return full player profile: info, team beyblades, trophies count, rank, rating."""
        conn = self._conn()
        row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        if not row:
            conn.close()
            return None

        team = self._get_team_beyblades(conn, player_id)
        trophy_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM player_trophies WHERE player_id = ?", (player_id,)
        ).fetchone()["cnt"]
        total_parts = conn.execute(
            "SELECT COUNT(*) as cnt FROM player_parts WHERE player_id = ?", (player_id,)
        ).fetchone()["cnt"]
        total_beyblades = conn.execute(
            "SELECT COUNT(*) as cnt FROM player_beyblades WHERE player_id = ?", (player_id,)
        ).fetchone()["cnt"]
        milestones = conn.execute(
            "SELECT milestone FROM player_progression WHERE player_id = ?", (player_id,)
        ).fetchall()
        inventory = conn.execute(
            "SELECT item_type, quantity FROM player_inventory WHERE player_id = ? AND quantity > 0",
            (player_id,)
        ).fetchall()
        conn.close()

        d = dict(row)
        xp_info = xp_progress_info(d.get("rank_level", 1), d.get("xp", 0))
        return {
            "id": d["id"],
            "username": d["username"],
            "token_": d["token_"],
            "starter_blade_id": d["starter_blade_id"],
            "bey_points": d.get("bey_points", 500),
            "xp": d.get("xp", 0),
            "rank_level": d.get("rank_level", 1),
            "rating": d.get("rating", 1000),
            "xp_progress": xp_info["xp_progress"],
            "xp_to_next": xp_info["xp_to_next"],
            "team": team,
            "trophy_count": trophy_count,
            "total_parts": total_parts,
            "total_beyblades": total_beyblades,
            "milestones": [r["milestone"] for r in milestones],
            "inventory": {r["item_type"]: r["quantity"] for r in inventory},
        }

    # ---- Team Management ---------------------------------------------

    def get_team(self, player_id):
        """Return list of assembled beyblades currently in the team (max 3)."""
        conn = self._conn()
        result = self._get_team_beyblades(conn, player_id)
        conn.close()
        return result

    def _get_team_beyblades(self, conn, player_id):
        """Internal: fetch team beyblades with their part details."""
        rows = conn.execute(
            """SELECT b.id, b.nickname, b.blade_part_id, b.ratchet_part_id, b.bit_part_id,
                      b.is_in_team, b.team_slot, b.created_at
               FROM player_beyblades b
               WHERE b.player_id = ? AND b.is_in_team = 1
               ORDER BY b.team_slot""",
            (player_id,)
        ).fetchall()
        result = []
        for r in rows:
            bey = dict(r)
            bey["blade"] = self._get_part_dict(conn, r["blade_part_id"])
            bey["ratchet"] = self._get_part_dict(conn, r["ratchet_part_id"])
            bey["bit"] = self._get_part_dict(conn, r["bit_part_id"])
            result.append(bey)
        return result

    def _get_part_dict(self, conn, part_db_id):
        """Fetch a single part row as dict."""
        row = conn.execute("SELECT * FROM player_parts WHERE id = ?", (part_db_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def get_storage(self, player_id):
        """Return all parts NOT currently in an assembled beyblade."""
        conn = self._conn()
        # Get IDs of all parts used in any assembled beyblade
        all_beys = conn.execute(
            """SELECT blade_part_id, ratchet_part_id, bit_part_id
               FROM player_beyblades WHERE player_id = ?""",
            (player_id,)
        ).fetchall()
        used_ids = set()
        for b in all_beys:
            used_ids.add(b["blade_part_id"])
            used_ids.add(b["ratchet_part_id"])
            used_ids.add(b["bit_part_id"])

        all_parts = conn.execute(
            "SELECT * FROM player_parts WHERE player_id = ? ORDER BY obtained_at",
            (player_id,)
        ).fetchall()
        conn.close()
        return [dict(p) for p in all_parts if p["id"] not in used_ids]

    def get_parts(self, player_id, part_type=None):
        """Return all owned parts, optionally filtered by part_type."""
        conn = self._conn()
        if part_type:
            rows = conn.execute(
                "SELECT * FROM player_parts WHERE player_id = ? AND part_type = ? ORDER BY obtained_at",
                (player_id, part_type)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM player_parts WHERE player_id = ? ORDER BY obtained_at",
                (player_id,)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def add_part(self, player_id, part_id, part_type, rarity, is_shiny=False):
        """Add a part to inventory and update catalog. Returns the new part DB id."""
        conn = self._conn()
        now = int(time.time())
        conn.execute(
            """INSERT INTO player_parts (player_id, part_id, part_type, rarity, is_shiny, obtained_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (player_id, part_id, part_type, rarity, int(is_shiny), now)
        )
        part_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Update catalog
        conn.execute(
            """INSERT INTO player_catalog (player_id, part_id, seen, obtained, first_seen_at, first_obtained_at)
               VALUES (?, ?, 1, 1, ?, ?)
               ON CONFLICT(player_id, part_id) DO UPDATE SET seen = 1, obtained = 1,
               first_obtained_at = COALESCE(first_obtained_at, ?)""",
            (player_id, part_id, now, now, now)
        )
        conn.commit()
        conn.close()
        return part_db_id

    # ---- Beyblade Assembly -------------------------------------------

    def assemble_beyblade(self, player_id, blade_part_id, ratchet_part_id, bit_part_id, nickname=None):
        """Create an assembled beyblade from three owned parts.
        Returns (beyblade_dict, error_string).
        """
        conn = self._conn()
        # Verify all three parts belong to this player and are correct types
        parts = {}
        for label, pid in [("blade", blade_part_id), ("ratchet", ratchet_part_id), ("bit", bit_part_id)]:
            row = conn.execute(
                "SELECT * FROM player_parts WHERE id = ? AND player_id = ?", (pid, player_id)
            ).fetchone()
            if not row:
                conn.close()
                return None, f"You don't own the specified {label} part."
            if row["part_type"] != label:
                conn.close()
                return None, f"Part {pid} is a {row['part_type']}, not a {label}."
            parts[label] = dict(row)

        # Check parts are not already in another assembled beyblade
        for label, pid in [("blade", blade_part_id), ("ratchet", ratchet_part_id), ("bit", bit_part_id)]:
            col = f"{label}_part_id"
            existing = conn.execute(
                f"SELECT id FROM player_beyblades WHERE player_id = ? AND {col} = ?",
                (player_id, pid)
            ).fetchone()
            if existing:
                conn.close()
                return None, f"That {label} part is already in an assembled beyblade."

        now = int(time.time())
        conn.execute(
            """INSERT INTO player_beyblades
               (player_id, nickname, blade_part_id, ratchet_part_id, bit_part_id, is_in_team, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (player_id, nickname, blade_part_id, ratchet_part_id, bit_part_id, now)
        )
        bey_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()
        return {
            "id": bey_id, "nickname": nickname,
            "blade": parts["blade"], "ratchet": parts["ratchet"], "bit": parts["bit"],
            "is_in_team": 0, "team_slot": None, "created_at": now
        }, None

    def disassemble_beyblade(self, player_id, beyblade_id):
        """Disassemble a beyblade back into its parts. Returns True on success."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM player_beyblades WHERE id = ? AND player_id = ?",
            (beyblade_id, player_id)
        ).fetchone()
        if not row:
            conn.close()
            return False
        if row["is_in_team"]:
            self._remove_from_team_internal(conn, player_id, beyblade_id)

        conn.execute("DELETE FROM player_beyblades WHERE id = ?", (beyblade_id,))
        conn.commit()
        conn.close()
        return True

    def swap_part(self, player_id, beyblade_id, new_part_id):
        """Swap one part on an assembled beyblade. Auto-detects type from the new part.
        Returns (updated_bey_dict, error_string).
        """
        conn = self._conn()
        bey = conn.execute(
            "SELECT * FROM player_beyblades WHERE id = ? AND player_id = ?",
            (beyblade_id, player_id)
        ).fetchone()
        if not bey:
            conn.close()
            return None, "Beyblade not found."

        new_part = conn.execute(
            "SELECT * FROM player_parts WHERE id = ? AND player_id = ?",
            (new_part_id, player_id)
        ).fetchone()
        if not new_part:
            conn.close()
            return None, "You don't own that part."

        part_type = new_part["part_type"]
        col = f"{part_type}_part_id"

        # Check the new part is not already in another beyblade
        existing = conn.execute(
            f"SELECT id FROM player_beyblades WHERE player_id = ? AND {col} = ?",
            (player_id, new_part_id)
        ).fetchone()
        if existing:
            conn.close()
            return None, f"That {part_type} is already in another beyblade."

        conn.execute(
            f"UPDATE player_beyblades SET {col} = ? WHERE id = ?",
            (new_part_id, beyblade_id)
        )
        conn.commit()

        # Fetch updated beyblade
        updated = conn.execute(
            "SELECT * FROM player_beyblades WHERE id = ?", (beyblade_id,)
        ).fetchone()
        result = dict(updated)
        result["blade"] = self._get_part_dict(conn, updated["blade_part_id"])
        result["ratchet"] = self._get_part_dict(conn, updated["ratchet_part_id"])
        result["bit"] = self._get_part_dict(conn, updated["bit_part_id"])
        conn.close()
        return result, None

    def add_to_team(self, player_id, beyblade_id):
        """Add a beyblade to the team (max 3). Returns True on success."""
        conn = self._conn()
        team_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM player_beyblades WHERE player_id = ? AND is_in_team = 1",
            (player_id,)
        ).fetchone()["cnt"]
        if team_count >= 3:
            conn.close()
            return False

        row = conn.execute(
            "SELECT id, is_in_team FROM player_beyblades WHERE id = ? AND player_id = ?",
            (beyblade_id, player_id)
        ).fetchone()
        if not row or row["is_in_team"]:
            conn.close()
            return False

        conn.execute(
            "UPDATE player_beyblades SET is_in_team = 1, team_slot = ? WHERE id = ?",
            (team_count, beyblade_id)
        )
        conn.commit()
        conn.close()
        return True

    def remove_from_team(self, player_id, beyblade_id):
        """Remove a beyblade from the team. Returns True on success."""
        conn = self._conn()
        result = self._remove_from_team_internal(conn, player_id, beyblade_id)
        conn.commit()
        conn.close()
        return result

    def _remove_from_team_internal(self, conn, player_id, beyblade_id):
        """Internal: remove from team and compact slots. Does NOT commit."""
        row = conn.execute(
            "SELECT team_slot FROM player_beyblades WHERE id = ? AND player_id = ? AND is_in_team = 1",
            (beyblade_id, player_id)
        ).fetchone()
        if not row:
            return False
        removed_slot = row["team_slot"]
        conn.execute(
            "UPDATE player_beyblades SET is_in_team = 0, team_slot = NULL WHERE id = ?",
            (beyblade_id,)
        )
        # Compact team slots
        conn.execute(
            """UPDATE player_beyblades SET team_slot = team_slot - 1
               WHERE player_id = ? AND is_in_team = 1 AND team_slot > ?""",
            (player_id, removed_slot)
        )
        return True

    # ---- Currency (Bey Points) ---------------------------------------

    def add_bey_points(self, player_id, amount):
        """Add Bey Points to a player."""
        conn = self._conn()
        conn.execute("UPDATE players SET bey_points = bey_points + ? WHERE id = ?", (amount, player_id))
        conn.commit()
        conn.close()

    def spend_bey_points(self, player_id, amount):
        """Spend Bey Points. Returns True if player had enough, False otherwise."""
        conn = self._conn()
        row = conn.execute("SELECT bey_points FROM players WHERE id = ?", (player_id,)).fetchone()
        if not row or row["bey_points"] < amount:
            conn.close()
            return False
        conn.execute("UPDATE players SET bey_points = bey_points - ? WHERE id = ?", (amount, player_id))
        conn.commit()
        conn.close()
        return True

    # ---- XP & Rank ---------------------------------------------------

    def add_xp(self, player_id, amount):
        """Add XP to a player. Checks for rank level ups.
        Returns dict with old/new level, leveled_up bool, and xp_progress info.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT xp, rank_level FROM players WHERE id = ?", (player_id,)
        ).fetchone()
        if not row:
            conn.close()
            return None

        old_level = row["rank_level"]
        total_xp = row["xp"] + amount
        new_level = old_level

        while new_level < 100 and total_xp >= xp_for_level(new_level + 1):
            new_level += 1

        conn.execute(
            "UPDATE players SET xp = ?, rank_level = ? WHERE id = ?",
            (total_xp, new_level, player_id)
        )
        conn.commit()
        conn.close()

        xp_info = xp_progress_info(new_level, total_xp)
        return {
            "old_level": old_level,
            "new_level": new_level,
            "leveled_up": new_level > old_level,
            "xp_gained": amount,
            "total_xp": total_xp,
            "xp_to_next": xp_info["xp_to_next"],
            "xp_progress": xp_info["xp_progress"],
            "xp_for_current_level": xp_info["xp_for_current_level"],
            "xp_for_next_level": xp_info["xp_for_next_level"],
        }

    # ---- Trophies & Milestones ---------------------------------------

    def add_trophy(self, player_id, master_id, stadium_tier):
        """Record a trophy. Returns True on success (False if duplicate)."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO player_trophies (player_id, master_id, stadium_tier, earned_at) VALUES (?, ?, ?, ?)",
                (player_id, master_id, stadium_tier, int(time.time()))
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False

    def get_trophies(self, player_id):
        """List all trophies for a player."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT master_id, stadium_tier, earned_at FROM player_trophies WHERE player_id = ? ORDER BY earned_at",
            (player_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def add_milestone(self, player_id, milestone):
        """Record a milestone. Silently ignores duplicates."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO player_progression (player_id, milestone, completed_at) VALUES (?, ?, ?)",
                (player_id, milestone, int(time.time()))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()

    def get_milestones(self, player_id):
        """List all milestones for a player."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT milestone, completed_at FROM player_progression WHERE player_id = ? ORDER BY completed_at",
            (player_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- Inventory (Consumables) -------------------------------------

    def add_inventory_item(self, player_id, item_type, quantity=1):
        """Add consumable items to inventory (UPSERT)."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO player_inventory (player_id, item_type, quantity)
               VALUES (?, ?, ?)
               ON CONFLICT(player_id, item_type) DO UPDATE SET quantity = quantity + ?""",
            (player_id, item_type, quantity, quantity)
        )
        conn.commit()
        conn.close()

    def use_inventory_item(self, player_id, item_type, quantity=1):
        """Use consumable items. Returns False if insufficient quantity."""
        conn = self._conn()
        row = conn.execute(
            "SELECT quantity FROM player_inventory WHERE player_id = ? AND item_type = ?",
            (player_id, item_type)
        ).fetchone()
        if not row or row["quantity"] < quantity:
            conn.close()
            return False
        conn.execute(
            "UPDATE player_inventory SET quantity = quantity - ? WHERE player_id = ? AND item_type = ?",
            (quantity, player_id, item_type)
        )
        conn.commit()
        conn.close()
        return True

    def get_inventory(self, player_id):
        """Return all items in inventory. Returns dict {item_type: quantity}."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT item_type, quantity FROM player_inventory WHERE player_id = ? AND quantity > 0",
            (player_id,)
        ).fetchall()
        conn.close()
        return {r["item_type"]: r["quantity"] for r in rows}

    # ---- Part Upgrading ----------------------------------------------

    def upgrade_part(self, player_id, part_db_id):
        """Level up a part by 1 (max 10). Costs one upgrade_crystal from inventory.
        Returns (new_level, error_string).
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM player_parts WHERE id = ? AND player_id = ?",
            (part_db_id, player_id)
        ).fetchone()
        if not row:
            conn.close()
            return None, "Part not found."
        if row["level"] >= 10:
            conn.close()
            return None, "Part is already at max level (10)."

        # Check for upgrade crystal
        crystal = conn.execute(
            "SELECT quantity FROM player_inventory WHERE player_id = ? AND item_type = 'upgrade_crystal'",
            (player_id,)
        ).fetchone()
        if not crystal or crystal["quantity"] < 1:
            conn.close()
            return None, "You need an upgrade crystal to upgrade parts."

        conn.execute(
            "UPDATE player_inventory SET quantity = quantity - 1 WHERE player_id = ? AND item_type = 'upgrade_crystal'",
            (player_id,)
        )
        new_level = row["level"] + 1
        conn.execute(
            "UPDATE player_parts SET level = ? WHERE id = ?",
            (new_level, part_db_id)
        )
        conn.commit()
        conn.close()
        return new_level, None

    # ---- Parts Catalog -----------------------------------------------

    def update_catalog(self, player_id, part_id, seen=False, obtained=False):
        """Update the parts catalog for a player. Marks seen and/or obtained."""
        conn = self._conn()
        now = int(time.time())
        if obtained:
            conn.execute(
                """INSERT INTO player_catalog (player_id, part_id, seen, obtained, first_seen_at, first_obtained_at)
                   VALUES (?, ?, 1, 1, ?, ?)
                   ON CONFLICT(player_id, part_id) DO UPDATE SET seen = 1, obtained = 1,
                   first_obtained_at = COALESCE(first_obtained_at, ?)""",
                (player_id, part_id, now, now, now)
            )
        elif seen:
            conn.execute(
                """INSERT INTO player_catalog (player_id, part_id, seen, first_seen_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(player_id, part_id) DO UPDATE SET seen = 1""",
                (player_id, part_id, now)
            )
        conn.commit()
        conn.close()

    def get_catalog(self, player_id):
        """Return catalog data: {part_id: {seen: bool, obtained: bool}}."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT part_id, seen, obtained FROM player_catalog WHERE player_id = ?",
            (player_id,)
        ).fetchall()
        conn.close()
        return {r["part_id"]: {"seen": bool(r["seen"]), "obtained": bool(r["obtained"])} for r in rows}

    # ---- Game History ------------------------------------------------

    def record_game(self, room_code, p1_name, p2_name, p1_bey_json, p2_bey_json,
                    winner, finish_type, duration_sec):
        """Save a completed battle to history."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO games
               (room_code, player1_name, player2_name, player1_bey, player2_bey,
                winner, finish_type, duration_sec, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_code, p1_name, p2_name,
             json.dumps(p1_bey_json) if isinstance(p1_bey_json, (dict, list)) else p1_bey_json,
             json.dumps(p2_bey_json) if isinstance(p2_bey_json, (dict, list)) else p2_bey_json,
             winner, finish_type, duration_sec, int(time.time()))
        )
        conn.commit()
        conn.close()

    def get_game_history(self, limit=50):
        """Return last N games."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM games ORDER BY finished_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            # Parse JSON fields
            for key in ("player1_bey", "player2_bey"):
                if d[key] and isinstance(d[key], str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            result.append(d)
        return result

    # ---- Bug Reports -------------------------------------------------

    def submit_bug_report(self, player_id, username, description, game_state_json=None):
        """Save a bug report."""
        conn = self._conn()
        conn.execute(
            "INSERT INTO bug_reports (player_id, username, description, game_state, submitted_at) VALUES (?, ?, ?, ?, ?)",
            (player_id, username, description,
             json.dumps(game_state_json) if isinstance(game_state_json, (dict, list)) else game_state_json,
             int(time.time()))
        )
        conn.commit()
        conn.close()

    def get_bug_reports(self, limit=100):
        """Return recent bug reports."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM bug_reports ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("game_state") and isinstance(d["game_state"], str):
                try:
                    d["game_state"] = json.loads(d["game_state"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    # ---- Account Deletion --------------------------------------------

    def delete_account(self, player_id):
        """Delete a player and ALL associated data. Returns True on success."""
        conn = self._conn()
        row = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
        if not row:
            conn.close()
            return False

        # Delete in dependency order
        conn.execute("DELETE FROM player_beyblades WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_parts WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_trophies WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_progression WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_inventory WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_catalog WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM bug_reports WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
        conn.commit()
        conn.close()
        return True

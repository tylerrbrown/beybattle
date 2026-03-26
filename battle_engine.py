"""Beyblade battle engine.

Tick-based stadium simulation with collision physics, stamina drain,
burst mechanics, and player action windows.

Win conditions (in point order):
1. Burst Finish (3 pts) - burst meter reaches 100
2. Xtreme Finish (2 pts) - knock opponent out of stadium
3. Spin Finish (1 pt) - opponent stamina reaches 0
"""

import math
import random
from beyblade_data import (
    get_type_matchup, get_element_effectiveness, get_attack, ATTACKS,
    compute_bey_stats, get_blade, get_ratchet, get_bit,
)

STADIUM_RADIUS = 100.0
COLLISION_RADIUS = 15.0
EDGE_ZONE = 15.0
TICKS_PER_SECOND = 2
ACTION_WINDOW_TICKS = 10
ACTION_DURATION_TICKS = 6
MAX_BATTLE_TICKS = 120
BURST_THRESHOLD = 100
BASE_STAMINA = 100.0
BASE_STAMINA_DRAIN = 0.8
UNIVERSAL_ATTACKS = ["rush_launch", "spin_steal", "guard_stance", "full_power"]


class BeybladeInstance:
    def __init__(self, blade_data, ratchet_data, bit_data,
                 blade_level=1, ratchet_level=1, bit_level=1, nickname=None):
        self.blade = blade_data
        self.ratchet = ratchet_data
        self.bit = bit_data
        self.blade_level = blade_level
        self.ratchet_level = ratchet_level
        self.bit_level = bit_level
        stats = compute_bey_stats(blade_data, ratchet_data, bit_data,
                                  blade_level, ratchet_level, bit_level)
        self.attack = stats["attack"]
        self.defense = stats["defense"]
        self.stamina_stat = stats["stamina"]
        self.speed = stats["speed"]
        self.burst_resist = stats["burst_resist"]
        self.weight = stats["weight"]
        self.bey_type = stats["type"]
        self.element = stats["element"]
        self.special_move_id = stats["special_move"]
        self.movement = stats["movement"]
        self.stamina_drain_mult = stats["stamina_drain"]
        self.height = stats["height"]
        self.teeth = stats["teeth"]
        self.name = nickname or blade_data["name"]
        self.blade_name = blade_data["name"]
        self.ratchet_name = ratchet_data["name"]
        self.bit_name = bit_data["name"]
        self.max_stamina = BASE_STAMINA + (self.stamina_stat * 0.5)
        self.current_stamina = self.max_stamina
        self.burst_meter = 0.0
        self.is_burst = False
        self.is_ring_out = False
        self.is_stopped = False
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.angle = 0.0
        self.spin_direction = 1
        self.launch_power_bonus = 0.0
        self.action_buff = None
        self.action_buff_ticks = 0
        self.special_used = False
        self.moves = self._build_moves()

    def _build_moves(self):
        moves = []
        special = get_attack(self.special_move_id)
        if special:
            move = dict(special)
            move["id"] = self.special_move_id
            move["is_special"] = True
            moves.append(move)
        for atk_id in UNIVERSAL_ATTACKS:
            atk = get_attack(atk_id)
            if atk:
                move = dict(atk)
                move["id"] = atk_id
                move["is_special"] = False
                moves.append(move)
        return moves

    def get_effective_attack(self):
        base = self.attack
        if self.action_buff == "rush":
            base = int(base * 1.3)
        return base

    def get_effective_defense(self):
        base = self.defense
        if self.action_buff == "guard":
            base = int(base * 1.5)
        return base

    def get_effective_speed(self):
        base = self.speed
        if self.action_buff == "rush":
            base = int(base * 1.3)
        stamina_pct = self.current_stamina / self.max_stamina
        return int(base * max(0.3, stamina_pct))

    def get_stamina_drain(self):
        drain = BASE_STAMINA_DRAIN * self.stamina_drain_mult
        drain *= 1.0 + (self.weight - 40) * 0.003
        if self.action_buff == "conserve":
            drain *= 0.5
        return drain

    def apply_launch_bonus(self, power):
        self.launch_power_bonus = power * 0.15
        self.current_stamina += self.max_stamina * self.launch_power_bonus

    def take_collision_damage(self, stamina_dmg, burst_dmg):
        self.current_stamina = max(0, self.current_stamina - stamina_dmg)
        self.burst_meter = min(BURST_THRESHOLD, self.burst_meter + burst_dmg)
        if self.burst_meter >= BURST_THRESHOLD:
            self.is_burst = True
        elif self.current_stamina <= 0:
            self.is_stopped = True

    def tick_action_buff(self):
        if self.action_buff_ticks > 0:
            self.action_buff_ticks -= 1
            if self.action_buff_ticks <= 0:
                self.action_buff = None

    def is_alive(self):
        return not self.is_burst and not self.is_ring_out and not self.is_stopped

    def to_dict(self):
        return {
            "name": self.name,
            "blade": self.blade["id"], "blade_name": self.blade_name,
            "ratchet": self.ratchet["id"], "ratchet_name": self.ratchet_name,
            "bit": self.bit["id"], "bit_name": self.bit_name,
            "bey_type": self.bey_type, "element": self.element,
            "attack": self.attack, "defense": self.defense,
            "stamina_stat": self.stamina_stat, "speed": self.speed,
            "burst_resist": self.burst_resist, "weight": self.weight,
            "max_stamina": round(self.max_stamina, 1),
            "current_stamina": round(self.current_stamina, 1),
            "burst_meter": round(self.burst_meter, 1),
            "x": round(self.x, 1), "y": round(self.y, 1),
            "vx": round(self.vx, 1), "vy": round(self.vy, 1),
            "spin_direction": self.spin_direction,
            "is_burst": self.is_burst, "is_ring_out": self.is_ring_out,
            "is_stopped": self.is_stopped,
            "action_buff": self.action_buff, "special_used": self.special_used,
            "moves": [{"id": m["id"], "name": m["name"], "element": m["element"],
                        "category": m["category"], "power": m["power"],
                        "stamina_cost": m["stamina_cost"], "is_special": m["is_special"]}
                       for m in self.moves],
        }


class StadiumState:
    def __init__(self, bey1, bey2):
        self.bey1 = bey1
        self.bey2 = bey2
        self.tick = 0
        self.events = []
        self.all_events = []
        self.winner = None
        self.finish_type = None
        self.finish_points = 0
        self.is_over = False
        self.action_window_open = False
        self.action_window_tick = 0
        self.total_collisions = 0

    def initialize_positions(self, bey1_position="middle", bey2_position="middle"):
        positions = {"inside": 20.0, "middle": 45.0, "outside": 70.0}
        dist1 = positions.get(bey1_position, 45.0)
        dist2 = positions.get(bey2_position, 45.0)
        self.bey1.x = -dist1
        self.bey1.y = 0
        self.bey1.angle = random.uniform(0, 2 * math.pi)
        self.bey2.x = dist2
        self.bey2.y = 0
        self.bey2.angle = random.uniform(0, 2 * math.pi)

    def resolve_tick(self):
        if self.is_over:
            return []
        self.tick += 1
        self.events = []
        self._drain_stamina()
        self._move_beyblades()
        self._check_collision()
        self._check_edges()
        self._check_win_conditions()
        self.bey1.tick_action_buff()
        self.bey2.tick_action_buff()
        if self.tick % ACTION_WINDOW_TICKS == 0 and not self.is_over:
            self.action_window_open = True
            self.action_window_tick = self.tick
            self.events.append({"type": "action_window", "tick": self.tick})
        if self.tick >= MAX_BATTLE_TICKS and not self.is_over:
            self._resolve_timeout()
        self.all_events.extend(self.events)
        return self.events

    def _drain_stamina(self):
        for bey in [self.bey1, self.bey2]:
            if bey.is_alive():
                drain = bey.get_stamina_drain()
                bey.current_stamina = max(0, bey.current_stamina - drain)
                if bey.current_stamina <= 0:
                    bey.is_stopped = True

    def _move_beyblades(self):
        for bey in [self.bey1, self.bey2]:
            if not bey.is_alive():
                continue
            speed = bey.get_effective_speed()
            move_dist = speed * 0.4

            # Apply existing velocity (momentum from collisions/bounces)
            bey.x += bey.vx
            bey.y += bey.vy
            # Dampen velocity each tick (friction)
            bey.vx *= 0.82
            bey.vy *= 0.82
            if abs(bey.vx) < 0.3:
                bey.vx = 0
            if abs(bey.vy) < 0.3:
                bey.vy = 0

            if bey.movement == "aggressive":
                other = self.bey2 if bey is self.bey1 else self.bey1
                dx = other.x - bey.x
                dy = other.y - bey.y
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > 0:
                    bey.x += (dx / dist) * move_dist * 0.8
                    bey.y += (dy / dist) * move_dist * 0.8
                bey.angle += bey.spin_direction * 0.4
                bey.x += math.cos(bey.angle) * move_dist * 0.2
                bey.y += math.sin(bey.angle) * move_dist * 0.2
            elif bey.movement == "defensive":
                # Defensive still orbits center but faster, occasionally drifts out
                bey.angle += bey.spin_direction * 0.6
                orbit_r = 20.0 + 10.0 * math.sin(self.tick * 0.15)
                target_x = math.cos(bey.angle) * orbit_r
                target_y = math.sin(bey.angle) * orbit_r
                bey.x += (target_x - bey.x) * 0.15
                bey.y += (target_y - bey.y) * 0.15
                bey.x += math.cos(bey.angle) * move_dist * 0.25
                bey.y += math.sin(bey.angle) * move_dist * 0.25
            elif bey.movement == "stamina":
                bey.angle += bey.spin_direction * 0.35
                radius = 35.0 + 20.0 * math.sin(self.tick * 0.12)
                target_x = math.cos(bey.angle) * radius
                target_y = math.sin(bey.angle) * radius
                bey.x += (target_x - bey.x) * 0.15
                bey.y += (target_y - bey.y) * 0.15
                # Occasionally dart toward opponent
                if random.random() < 0.15:
                    other = self.bey2 if bey is self.bey1 else self.bey1
                    dx = other.x - bey.x
                    dy = other.y - bey.y
                    d = math.sqrt(dx * dx + dy * dy)
                    if d > 0:
                        bey.x += (dx / d) * move_dist * 0.3
                        bey.y += (dy / d) * move_dist * 0.3
            elif bey.movement == "erratic":
                if random.random() < 0.35:
                    bey.angle += random.uniform(-1.5, 1.5)
                bey.x += math.cos(bey.angle) * move_dist * 1.1
                bey.y += math.sin(bey.angle) * move_dist * 1.1

            # Center gravity: bowl-shaped stadium pulls toward center
            dist_from_center = math.sqrt(bey.x * bey.x + bey.y * bey.y)
            if dist_from_center > 5:
                gravity_strength = 0.008 + (dist_from_center / STADIUM_RADIUS) * 0.012
                bey.x -= (bey.x / dist_from_center) * gravity_strength * move_dist
                bey.y -= (bey.y / dist_from_center) * gravity_strength * move_dist

            # Wall bounce: reflect angle when hitting edge
            dist_from_center = math.sqrt(bey.x * bey.x + bey.y * bey.y)
            if dist_from_center > STADIUM_RADIUS - 5:
                norm_x = bey.x / dist_from_center
                norm_y = bey.y / dist_from_center
                # Place back inside
                bey.x = norm_x * (STADIUM_RADIUS - 6)
                bey.y = norm_y * (STADIUM_RADIUS - 6)
                # Reflect velocity off the wall
                dot = bey.vx * norm_x + bey.vy * norm_y
                bey.vx -= 2 * dot * norm_x
                bey.vy -= 2 * dot * norm_y
                # Add bounce impulse away from wall
                bounce_force = move_dist * 0.6 + random.uniform(0, move_dist * 0.3)
                bey.vx -= norm_x * bounce_force
                bey.vy -= norm_y * bounce_force
                # Reflect movement angle
                bey.angle = math.atan2(-norm_y, -norm_x) + random.uniform(-0.3, 0.3)
                self.events.append({
                    "type": "wall_hit", "bey_name": bey.name,
                    "x": round(bey.x, 1), "y": round(bey.y, 1),
                })

    def _check_collision(self):
        if not self.bey1.is_alive() or not self.bey2.is_alive():
            return
        dx = self.bey2.x - self.bey1.x
        dy = self.bey2.y - self.bey1.y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < COLLISION_RADIUS:
            self.total_collisions += 1
            collision_result = calculate_collision(self.bey1, self.bey2)
            self.events.append(collision_result)
            if dist > 0:
                # Dramatic knockback: 2.5x the collision radius
                push = COLLISION_RADIUS * 3.0
                nx = dx / dist
                ny = dy / dist
                # Weight affects who gets pushed more
                w1 = self.bey1.weight
                w2 = self.bey2.weight
                total_w = max(1, w1 + w2)
                push1_ratio = w2 / total_w  # lighter bey pushed more
                push2_ratio = w1 / total_w
                push1 = push * push1_ratio
                push2 = push * push2_ratio
                # Apply position push
                self.bey1.x -= nx * push1
                self.bey1.y -= ny * push1
                self.bey2.x += nx * push2
                self.bey2.y += ny * push2
                # Apply velocity for momentum carry
                speed_factor = 1.0 + random.uniform(0, 0.5)
                self.bey1.vx = -nx * push1 * 1.2 * speed_factor
                self.bey1.vy = -ny * push1 * 1.2 * speed_factor
                self.bey2.vx = nx * push2 * 1.2 * speed_factor
                self.bey2.vy = ny * push2 * 1.2 * speed_factor

    def _check_edges(self):
        for i, bey in enumerate([self.bey1, self.bey2], 1):
            if not bey.is_alive():
                continue
            dist = math.sqrt(bey.x * bey.x + bey.y * bey.y)
            if dist >= STADIUM_RADIUS:
                bey.is_ring_out = True
                self.events.append({"type": "ring_out", "player": i, "bey_name": bey.name})

    def _check_win_conditions(self):
        b1_alive = self.bey1.is_alive()
        b2_alive = self.bey2.is_alive()
        if b1_alive and b2_alive:
            return
        if not b1_alive and not b2_alive:
            self.winner = 1 if self.bey1.current_stamina >= self.bey2.current_stamina else 2
        elif not b2_alive:
            self.winner = 1
        else:
            self.winner = 2
        loser = self.bey2 if self.winner == 1 else self.bey1
        if loser.is_burst:
            self.finish_type = "burst"
            self.finish_points = 3
        elif loser.is_ring_out:
            self.finish_type = "xtreme"
            self.finish_points = 2
        else:
            self.finish_type = "spin"
            self.finish_points = 1
        self.is_over = True
        self.events.append({
            "type": "battle_end", "winner": self.winner,
            "finish_type": self.finish_type, "finish_points": self.finish_points,
            "ticks": self.tick, "collisions": self.total_collisions,
        })

    def _resolve_timeout(self):
        self.winner = 1 if self.bey1.current_stamina >= self.bey2.current_stamina else 2
        self.finish_type = "spin"
        self.finish_points = 1
        self.is_over = True
        self.events.append({
            "type": "battle_end", "winner": self.winner,
            "finish_type": self.finish_type, "finish_points": self.finish_points,
            "ticks": self.tick, "collisions": self.total_collisions, "timeout": True,
        })

    def apply_action(self, player_num, action):
        bey = self.bey1 if player_num == 1 else self.bey2
        if action in ("rush", "guard", "conserve"):
            bey.action_buff = action
            bey.action_buff_ticks = ACTION_WINDOW_TICKS
            self.events.append({"type": "action_chosen", "player": player_num,
                                "action": action, "bey_name": bey.name})
        elif action == bey.special_move_id and not bey.special_used:
            bey.special_used = True
            bey.action_buff = "special"
            bey.action_buff_ticks = ACTION_WINDOW_TICKS
            self.events.append({"type": "special_activated", "player": player_num,
                                "move_id": action, "bey_name": bey.name})

    def to_dict(self):
        return {
            "tick": self.tick, "bey1": self.bey1.to_dict(), "bey2": self.bey2.to_dict(),
            "is_over": self.is_over, "winner": self.winner,
            "finish_type": self.finish_type, "finish_points": self.finish_points,
            "action_window_open": self.action_window_open,
            "total_collisions": self.total_collisions,
        }


def calculate_collision(bey1, bey2):
    atk1 = bey1.get_effective_attack()
    atk2 = bey2.get_effective_attack()
    type_mult_1v2 = get_type_matchup(bey1.bey_type, bey2.bey_type)
    type_mult_2v1 = get_type_matchup(bey2.bey_type, bey1.bey_type)
    elem_mult_1v2 = get_element_effectiveness(bey1.element, bey2.element)
    elem_mult_2v1 = get_element_effectiveness(bey2.element, bey1.element)
    spin_mult = 1.5 if bey1.spin_direction != bey2.spin_direction else 1.0
    height_mult_1 = 1.2 if bey1.height < bey2.height else 1.0
    height_mult_2 = 1.2 if bey2.height < bey1.height else 1.0
    weight_mult_1 = 0.8 + (bey1.weight / max(1, bey2.weight)) * 0.2
    weight_mult_2 = 0.8 + (bey2.weight / max(1, bey1.weight)) * 0.2
    special_mult_1, special_mult_2 = 1.0, 1.0
    special_used_1, special_used_2 = False, False
    if bey1.action_buff == "special":
        sdata = get_attack(bey1.special_move_id)
        if sdata:
            special_mult_1 = 1.0 + sdata["power"] / 100.0
            special_used_1 = True
            bey1.action_buff = None
            bey1.action_buff_ticks = 0
    if bey2.action_buff == "special":
        sdata = get_attack(bey2.special_move_id)
        if sdata:
            special_mult_2 = 1.0 + sdata["power"] / 100.0
            special_used_2 = True
            bey2.action_buff = None
            bey2.action_buff_ticks = 0
    v1 = random.uniform(0.85, 1.15)
    v2 = random.uniform(0.85, 1.15)
    dmg_1to2 = atk1 * 0.15 * type_mult_1v2 * elem_mult_1v2 * spin_mult * height_mult_1
    dmg_1to2 *= weight_mult_1 * special_mult_1 * v1
    dmg_1to2 = max(1, dmg_1to2 - bey2.get_effective_defense() * 0.05)
    dmg_2to1 = atk2 * 0.15 * type_mult_2v1 * elem_mult_2v1 * spin_mult * height_mult_2
    dmg_2to1 *= weight_mult_2 * special_mult_2 * v2
    dmg_2to1 = max(1, dmg_2to1 - bey1.get_effective_defense() * 0.05)
    br1 = max(0.1, 1.0 - bey1.burst_resist / 150.0)
    br2 = max(0.1, 1.0 - bey2.burst_resist / 150.0)
    burst_to_1 = dmg_2to1 * 0.4 * br1
    burst_to_2 = dmg_1to2 * 0.4 * br2
    if special_used_1:
        sd = get_attack(bey1.special_move_id)
        if sd and sd.get("burst_power", 0) > 0:
            burst_to_2 += sd["burst_power"] * br2
    if special_used_2:
        sd = get_attack(bey2.special_move_id)
        if sd and sd.get("burst_power", 0) > 0:
            burst_to_1 += sd["burst_power"] * br1
    if special_used_1:
        cost = get_attack(bey1.special_move_id).get("stamina_cost", 10)
        bey1.current_stamina = max(0, bey1.current_stamina - cost)
    if special_used_2:
        cost = get_attack(bey2.special_move_id).get("stamina_cost", 10)
        bey2.current_stamina = max(0, bey2.current_stamina - cost)
    bey1.take_collision_damage(round(dmg_2to1, 1), round(burst_to_1, 1))
    bey2.take_collision_damage(round(dmg_1to2, 1), round(burst_to_2, 1))
    for attacker, defender, dmg in [(bey1, bey2, dmg_1to2), (bey2, bey1, dmg_2to1)]:
        dist = math.sqrt(defender.x ** 2 + defender.y ** 2)
        if dist > STADIUM_RADIUS - EDGE_ZONE and dmg > 8 and attacker.get_effective_speed() > 60:
            if dist > 0:
                nx, ny = defender.x / dist, defender.y / dist
                defender.x += nx * dmg * 0.5
                defender.y += ny * dmg * 0.5
    return {
        "type": "collision", "dominant": 1 if dmg_1to2 > dmg_2to1 else 2,
        "bey1_stamina_dmg": round(dmg_2to1, 1), "bey2_stamina_dmg": round(dmg_1to2, 1),
        "bey1_burst_dmg": round(burst_to_1, 1), "bey2_burst_dmg": round(burst_to_2, 1),
        "bey1_stamina": round(bey1.current_stamina, 1),
        "bey2_stamina": round(bey2.current_stamina, 1),
        "bey1_burst": round(bey1.burst_meter, 1), "bey2_burst": round(bey2.burst_meter, 1),
        "spin_bonus": spin_mult > 1.0,
        "type_effective_1v2": type_mult_1v2, "type_effective_2v1": type_mult_2v1,
        "special_used_1": special_used_1, "special_used_2": special_used_2,
        "collision_x": round((bey1.x + bey2.x) / 2, 1),
        "collision_y": round((bey1.y + bey2.y) / 2, 1),
        "total_damage": round(dmg_1to2 + dmg_2to1, 1),
    }


def create_beyblade(blade_id, ratchet_id, bit_id,
                    blade_level=1, ratchet_level=1, bit_level=1, nickname=None):
    blade = get_blade(blade_id)
    ratchet = get_ratchet(ratchet_id)
    bit = get_bit(bit_id)
    if not blade:
        raise ValueError("Unknown blade: " + blade_id)
    if not ratchet:
        raise ValueError("Unknown ratchet: " + ratchet_id)
    if not bit:
        raise ValueError("Unknown bit: " + bit_id)
    return BeybladeInstance(blade, ratchet, bit, blade_level, ratchet_level, bit_level, nickname)


def simulate_battle(bey1, bey2, bey1_spin="right", bey2_spin="right",
                    bey1_position="middle", bey2_position="middle",
                    bey1_launch_power=0.5, bey2_launch_power=0.5):
    bey1.spin_direction = 1 if bey1_spin == "right" else -1
    bey2.spin_direction = 1 if bey2_spin == "right" else -1
    bey1.apply_launch_bonus(bey1_launch_power)
    bey2.apply_launch_bonus(bey2_launch_power)
    stadium = StadiumState(bey1, bey2)
    stadium.initialize_positions(bey1_position, bey2_position)
    while not stadium.is_over:
        stadium.resolve_tick()
    return stadium

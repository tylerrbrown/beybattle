"""Beyblade battle engine.

Tick-based stadium simulation with force-based Newtonian physics,
collision dynamics, stamina drain, burst mechanics, and player action windows.

Battles last 15-25 seconds with continuous animation at 10 ticks/sec.
Movement is driven by the bit (tip) - flat tips convert spin energy into
fast lateral movement (racing the tornado ridge), while sharp/needle tips
sit near center like a stationary gyroscope, barely moving.

Win conditions (in point order - official Beyblade X scoring):
1. Xtreme Finish (3 pts) - knock opponent out of stadium
2. Burst Finish (2 pts) - burst meter reaches 100
3. Spin Finish (1 pt) - opponent stamina reaches 0
"""

import math
import random
from beyblade_data import (
    get_type_matchup, get_element_effectiveness, get_attack, ATTACKS,
    compute_bey_stats, get_blade, get_ratchet, get_bit,
)

STADIUM_RADIUS = 100.0
COLLISION_RADIUS = 18.0
EDGE_ZONE = 15.0
TICKS_PER_SECOND = 10
MAX_BATTLE_SECONDS = 25
MAX_BATTLE_TICKS = TICKS_PER_SECOND * MAX_BATTLE_SECONDS
BURST_THRESHOLD = 100
BASE_STAMINA = 100.0
BASE_STAMINA_DRAIN = 0.45
ACTION_WINDOW_TICKS = 30
ACTION_DURATION_TICKS = 30
TORNADO_RIDGE = STADIUM_RADIUS * 0.55

UNIVERSAL_ATTACKS = ["rush_launch", "spin_steal", "guard_stance", "full_power"]

# How much spin converts to lateral speed, per movement type
MOVEMENT_ENERGY = {
    "aggressive": 0.85,
    "defensive": 0.06,
    "stamina": 0.04,
    "erratic": 0.40,
}



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
        self.max_stamina = BASE_STAMINA + (self.stamina_stat * 0.6)
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
        stamina_pct = self.current_stamina / max(1, self.max_stamina)
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
        self._collision_cooldown = 0  # prevent double-counting collisions

    def initialize_positions(self, bey1_position="middle", bey2_position="middle"):
        for bey, sign in [(self.bey1, -1), (self.bey2, 1)]:
            energy = MOVEMENT_ENERGY.get(bey.movement, 0.3)
            if bey.movement == "aggressive":
                r = TORNADO_RIDGE + random.uniform(-10, 10)
            elif bey.movement in ("defensive", "stamina"):
                r = 8 + random.uniform(-3, 3)
            else:
                r = 30 + random.uniform(-10, 10)
            angle = random.uniform(0, 2 * math.pi)
            bey.x = r * math.cos(angle) * sign
            bey.y = r * math.sin(angle)
            init_speed = bey.speed * energy * 0.1
            d = max(1, math.sqrt(bey.x**2 + bey.y**2))
            bey.vx = (-bey.y / d) * bey.spin_direction * init_speed
            bey.vy = (bey.x / d) * bey.spin_direction * init_speed

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

            stamina_pct = bey.current_stamina / max(1, bey.max_stamina)
            energy = MOVEMENT_ENERGY.get(bey.movement, 0.3)
            lateral = energy * bey.speed * stamina_pct * 0.08
            dist_c = math.sqrt(bey.x**2 + bey.y**2)

            # TANGENTIAL FORCE (circular laps - only significant for aggressive)
            if dist_c > 2 and lateral > 0.1:
                rx, ry = bey.x / dist_c, bey.y / dist_c
                tx = -ry * bey.spin_direction
                ty = rx * bey.spin_direction
                tang_scale = 0.5 if bey.movement == "aggressive" else 1.0
                bey.vx += tx * lateral * tang_scale
                bey.vy += ty * lateral * tang_scale

            # BOWL GRAVITY (steep at edges)
            if dist_c > 2:
                g = 0.25 + (dist_c / STADIUM_RADIUS)**2 * 2.5
                bey.vx -= (bey.x / dist_c) * g
                bey.vy -= (bey.y / dist_c) * g

            # TYPE-SPECIFIC
            other = self.bey2 if bey is self.bey1 else self.bey1
            dashing = False
            if bey.movement == "aggressive":
                # Brief dash toward opponent every ~2 seconds
                if self.tick % 18 < 6:
                    dx = other.x - bey.x
                    dy = other.y - bey.y
                    d = math.sqrt(dx*dx + dy*dy)
                    if d > 5:
                        dash_str = lateral * 3.0 + 4.0
                        bey.vx += (dx/d) * dash_str
                        bey.vy += (dy/d) * dash_str
                        dashing = True
                # Push outward to ride tornado ridge (only when not dashing)
                if not dashing and dist_c < TORNADO_RIDGE and dist_c > 2:
                    bey.vx += (bey.x / dist_c) * lateral * 0.4
                    bey.vy += (bey.y / dist_c) * lateral * 0.4
            elif bey.movement in ("defensive", "stamina"):
                # Strong center anchor
                if dist_c > 5:
                    anchor = 0.3 + dist_c * 0.02
                    bey.vx -= (bey.x / dist_c) * anchor
                    bey.vy -= (bey.y / dist_c) * anchor
            elif bey.movement == "erratic":
                if self.tick % 25 < 5 and dist_c > 2:
                    rx, ry = bey.x / dist_c, bey.y / dist_c
                    tx = -ry * bey.spin_direction
                    ty = rx * bey.spin_direction
                    bey.vx += tx * lateral * 0.6
                    bey.vy += ty * lateral * 0.6

            # WOBBLE at low stamina
            if stamina_pct < 0.20:
                w = (0.20 - stamina_pct) * 15
                bey.vx += random.uniform(-w, w)
                bey.vy += random.uniform(-w, w)

            # FRICTION (aggressive maintains speed, defense/stamina stops quickly)
            if bey.movement == "aggressive":
                friction = 0.965 - (1.0 - stamina_pct) * 0.04
            elif bey.movement in ("defensive", "stamina"):
                friction = 0.88 - (1.0 - stamina_pct) * 0.05
            else:
                friction = 0.93 - (1.0 - stamina_pct) * 0.04
            friction = max(friction, 0.80)
            bey.vx *= friction
            bey.vy *= friction

            # SPEED CAP
            spd = math.sqrt(bey.vx**2 + bey.vy**2)
            max_spd = 6.0 + lateral * 3
            if spd > max_spd:
                bey.vx *= max_spd / spd
                bey.vy *= max_spd / spd

            # UPDATE POSITION
            bey.x += bey.vx
            bey.y += bey.vy

            # WALL BOUNCE
            dist = math.sqrt(bey.x**2 + bey.y**2)
            if dist > STADIUM_RADIUS - 3:
                if dist > 0.1:
                    nx, ny = bey.x / dist, bey.y / dist
                    bey.x = nx * (STADIUM_RADIUS - 4)
                    bey.y = ny * (STADIUM_RADIUS - 4)
                    dot = bey.vx * nx + bey.vy * ny
                    if dot > 0:
                        bey.vx -= 2.0 * dot * nx
                        bey.vy -= 2.0 * dot * ny
                        bey.vx -= nx * 2.0
                        bey.vy -= ny * 2.0
                        self.events.append({"type": "wall_hit", "bey_name": bey.name,
                                           "x": round(bey.x, 1), "y": round(bey.y, 1)})

    def _check_collision(self):
        if not self.bey1.is_alive() or not self.bey2.is_alive():
            return
        if self._collision_cooldown > 0:
            self._collision_cooldown -= 1
            return
        dx = self.bey2.x - self.bey1.x
        dy = self.bey2.y - self.bey1.y
        dist = math.sqrt(dx*dx + dy*dy)
        if dist < COLLISION_RADIUS:
            self.total_collisions += 1
            self._collision_cooldown = 5
            result = calculate_collision(self.bey1, self.bey2)
            self.events.append(result)
            if dist > 0.1:
                nx, ny = dx/dist, dy/dist
            else:
                a = random.uniform(0, 2*math.pi)
                nx, ny = math.cos(a), math.sin(a)
            overlap = COLLISION_RADIUS - dist
            self.bey1.x -= nx * overlap * 0.5
            self.bey1.y -= ny * overlap * 0.5
            self.bey2.x += nx * overlap * 0.5
            self.bey2.y += ny * overlap * 0.5
            w1, w2 = self.bey1.weight, self.bey2.weight
            dvx = self.bey1.vx - self.bey2.vx
            dvy = self.bey1.vy - self.bey2.vy
            rel_vel = dvx*nx + dvy*ny
            restitution = 1.6 + random.uniform(0, 0.3)
            inv_m1 = 1.0 / max(1, w1)
            inv_m2 = 1.0 / max(1, w2)
            impulse = restitution * rel_vel / (inv_m1 + inv_m2)
            self.bey1.vx -= impulse * inv_m1 * nx
            self.bey1.vy -= impulse * inv_m1 * ny
            self.bey2.vx += impulse * inv_m2 * nx
            self.bey2.vy += impulse * inv_m2 * ny
            speed1 = math.sqrt(self.bey1.vx**2 + self.bey1.vy**2)
            speed2 = math.sqrt(self.bey2.vx**2 + self.bey2.vy**2)
            kick = max(speed1, speed2) * 0.3 + 2.0
            total_w = max(1, w1 + w2)
            self.bey1.vx -= nx * kick * (w2/total_w)
            self.bey1.vy -= ny * kick * (w2/total_w)
            self.bey2.vx += nx * kick * (w1/total_w)
            self.bey2.vy += ny * kick * (w1/total_w)

    def _check_edges(self):
        for i, bey in enumerate([self.bey1, self.bey2], 1):
            if not bey.is_alive():
                continue
            dist = math.sqrt(bey.x * bey.x + bey.y * bey.y)
            if dist >= STADIUM_RADIUS + 5:
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
            self.finish_points = 2
        elif loser.is_ring_out:
            self.finish_type = "xtreme"
            self.finish_points = 3
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
            bey.action_buff_ticks = ACTION_DURATION_TICKS
            self.events.append({"type": "action_chosen", "player": player_num,
                                "action": action, "bey_name": bey.name})
        elif action == bey.special_move_id and not bey.special_used:
            bey.special_used = True
            bey.action_buff = "special"
            bey.action_buff_ticks = ACTION_DURATION_TICKS
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
    dmg_1to2 = max(0.5, dmg_1to2 * 0.7 - bey2.get_effective_defense() * 0.03)
    dmg_2to1 = atk2 * 0.15 * type_mult_2v1 * elem_mult_2v1 * spin_mult * height_mult_2
    dmg_2to1 *= weight_mult_2 * special_mult_2 * v2
    dmg_2to1 = max(0.5, dmg_2to1 * 0.7 - bey1.get_effective_defense() * 0.03)
    br1 = max(0.15, 1.0 - bey1.burst_resist / 150.0)
    br2 = max(0.15, 1.0 - bey2.burst_resist / 150.0)
    burst_to_1 = dmg_2to1 * 1.0 * br1
    burst_to_2 = dmg_1to2 * 1.0 * br2
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
    # Edge ring-out push (big hits near edge can knock out)
    for attacker, defender, dmg in [(bey1, bey2, dmg_1to2), (bey2, bey1, dmg_2to1)]:
        dist = math.sqrt(defender.x ** 2 + defender.y ** 2)
        if dist > STADIUM_RADIUS - EDGE_ZONE and dmg > 8 and attacker.get_effective_speed() > 60:
            if dist > 0:
                nx, ny = defender.x / dist, defender.y / dist
                defender.vx += nx * dmg * 0.4
                defender.vy += ny * dmg * 0.4
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


def simulate_battle(bey1, bey2, bey1_spin="right", bey2_spin="left",
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

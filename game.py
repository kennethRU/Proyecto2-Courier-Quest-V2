# game.py
from __future__ import annotations
import copy
import math
import json
import os
from typing import List, Optional, Tuple, Dict, Any 
import random
from collections import deque
import time
import pygame

from api import ApiClient
from config import (
    SCREEN_WIDTH, SCREEN_HEIGHT, TILE_SIZE, TARGET_FPS, 
    GAME_DURATION_SECONDS, MAX_WEIGHT,
    V0_CELLS_PER_SEC, STAMINA_BASE_COST_PER_CELL, STAMINA_MAX,
    STAMINA_RECOVERY_IDLE, STAMINA_WEIGHT_THRESHOLD, STAMINA_WEIGHT_EXTRA_PER_UNIT,
    STAMINA_WEATHER_EXTRA, REPUTATION_START, REPUTATION_FAIL, REPUTATION_BONUS_THRESHOLD,
    REPUTATION_PAYOUT_BONUS, REP_DELIVERY_ON_TIME, REP_DELIVERY_EARLY,
    REP_LATE_30, REP_LATE_120, REP_LATE_OVER, REP_CANCEL_ACCEPTED, REP_STREAK_3,
    WEATHER_TRANSITION_TIME
)
from models import CityMap, Job, Player, GameState, WeatherState 
from sorting import merge_sort
from weather import pick_burst_duration, pick_next_condition, weather_multiplier, interpolate
from utils import iso_to_datetime, clamp
from persistence import save_binary, load_binary, load_scores, save_scores
from inventory import DoublyLinkedList
from collections import deque

MEDIUM_MAX_PATH_STEPS = 2
MEDIUM_MOVE_COOLDOWN = 15     
MEDIUM_PAUSE_CHANCE = 0.25
MEDIUM_AVOID_CONFLICT_RADIUS = 5
MEDIUM_MAX_CONCURRENT_JOBS = 1

MIN_STAMINA_TO_MOVE = 0.3 * STAMINA_MAX



def shortest_path(grid, start, goal):
    rows, cols = len(grid), len(grid[0])
    visited = set()
    queue = deque([(start, [start])])

    while queue:
        (r, c), path = queue.popleft()
        if (r, c) == goal:
            return path
        for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
            nr, nc = r+dr, c+dc
            if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == 0 and (nr,nc) not in visited:
                visited.add((nr,nc))
                queue.append(((nr,nc), path+[(nr,nc)]))
    return []

class Game:
    def __init__(self) -> None:
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Courier Quest")
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("consolas", 18)
        self.big_font = pygame.font.SysFont("consolas", 28)
        self.current_path: List[Tuple[int, int]] = []

        api = ApiClient()
        city_raw = api.get_city_map()
        jobs_raw = api.get_jobs()
        weather_raw = api.get_weather()

        city = self._parse_city(city_raw)
        jobs = self._parse_jobs(jobs_raw, city)
        weather = self._init_weather(weather_raw)

        player = Player(x=1, y=1, stamina=STAMINA_MAX, reputation=REPUTATION_START)
        cpu_player = Player(x=2, y=2, stamina=STAMINA_MAX, reputation=REPUTATION_START)

        self.state = GameState(
            city=city,
            player=player,
            cpu=cpu_player,# nuevo jugador CPU
            jobs_all=jobs,
            jobs_active=[],
            inventory=DoublyLinkedList(),         
            inv_cursor=0,
            weather=weather,
            elapsed=0.0,
            game_over=False,
            victory=False,
            message="",
            current_path=[]
        )
        self.undo_stack: List[GameState] = []
        self.inventory_view_mode = "natural"
        self.fpos_x = float(player.x)
        self.fpos_y = float(player.y)
        self.cpu_difficulty = "easy"  # valores: "easy", "medium", "hard"

        # --- Historial ---
        self.history_file = os.path.join("data", "history.json")
        self.history = self._load_history()
        self.showing_history = False
        self._load_assets() 
        
        self._cpu_idle_timer = 0.0


    # --------- Parsers ---------
    def _parse_city(self, raw: Dict[str, Any]) -> CityMap:
        if 'data' in raw:
            raw = raw['data']

        required_keys = ["width", "height", "tiles", "legend"]
        if not all(k in raw for k in required_keys):
            raise ValueError(f"La respuesta de la API no es un mapa válido. Claves faltantes: {set(required_keys) - set(raw.keys())}")


        legend = {}
        for code, info in raw["legend"].items():
            legend[code] = {
                "name": info.get("name", ""),
                "surface_weight": float(info.get("surface_weight", 1.0)),
                "blocked": bool(info.get("blocked", False))
            }
        
        return CityMap(
            version=raw.get("version", "1.0"),
            width=int(raw["width"]),
            height=int(raw["height"]),
            tiles=raw["tiles"],
            legend=legend,
            goal=int(raw.get("goal", 1000))
        )
    
    def _parse_jobs(self, raw: Dict[str, Any], city: CityMap) -> List[Job]:
        if isinstance(raw, dict) and 'data' in raw and isinstance(raw['data'], list):
            raw_list = raw['data']
        elif isinstance(raw, list):
            raw_list = raw
        else:
            raise ValueError(f"Respuesta de la API de trabajos no válida: {raw}")

        jobs: List[Job] = []
        for it in raw_list:
            pickup = tuple(it["pickup"])
            dropoff = tuple(it["dropoff"])
            jobs.append(Job(
                id=it["id"],
                pickup=pickup,
                dropoff=dropoff,
                payout=int(it["payout"]),
                deadline=iso_to_datetime(it["deadline"]),
                weight=int(it["weight"]),
                priority=int(it["priority"]),
                release_time=int(it.get("release_time", 0))
            ))
        return jobs


    def _is_blocked(self, position: Tuple[int, int]) -> bool:
        x, y = position
        tile = self.state.city.tiles[y][x]
        return tile in ('B', 'P')


    def _init_weather(self, raw) -> WeatherState:
        bursts = raw.get("bursts", [])
        first_cond = "clear"
        intensity = 0.0
        if bursts:
            b0 = bursts[0]
            first_cond = b0.get("condition", "clear")
            intensity = float(b0.get("intensity", 0.0))
        
        return WeatherState(
            current=first_cond,
            target=first_cond,
            intensity=intensity,
            burst_time_left=float(bursts[0].get("duration", 60)) if bursts else float(pick_burst_duration()),
            transition_time_left=0.0,
            bursts=bursts,   
            burst_index=0    
        )


    # --------- Loop principal ---------
    def run(self) -> None:
        # --- Menú de selección de dificultad ---
        selecting = True
        while selecting:
            self.screen.fill((0, 0, 0))
            title = self.big_font.render("Selecciona dificultad del CPU", True, (255, 255, 0))
            easy = self.font.render("1 - Fácil", True, (0, 255, 0))
            medium = self.font.render("2 - Medio", True, (255, 165, 0))
            hard = self.font.render("3 - Difícil", True, (255, 0, 0))

            self.screen.blit(title, (SCREEN_WIDTH//2 - title.get_width()//2, 150))
            self.screen.blit(easy, (SCREEN_WIDTH//2 - easy.get_width()//2, 250))
            self.screen.blit(medium, (SCREEN_WIDTH//2 - medium.get_width()//2, 300))
            self.screen.blit(hard, (SCREEN_WIDTH//2 - hard.get_width()//2, 350))

            pygame.display.flip()

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    pygame.quit()
                    return
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_1:
                        self.cpu_difficulty = "easy"
                        selecting = False
                    elif event.key == pygame.K_2:
                        self.cpu_difficulty = "medium"
                        selecting = False
                    elif event.key == pygame.K_3:
                        self.cpu_difficulty = "hard"
                        selecting = False

        # --- Loop principal del juego ---
        running = True
        while running:
            dt = self.clock.tick(TARGET_FPS) / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    self._on_keydown(event.key)

            if not self.state.game_over:
                self._update(dt)
            self._draw()

        pygame.quit()


    # --------- Input ---------
    def _on_keydown(self, key: int) -> None:
        if key == pygame.K_ESCAPE:
            if self.showing_history:
                self.showing_history = False
            else:
                pygame.event.post(pygame.event.Event(pygame.QUIT))
        elif key == pygame.K_u:
            self._undo()
        elif key == pygame.K_s:
            self._save_game()
        elif key == pygame.K_l:
            self._load_game()
        elif key == pygame.K_1:
            self.inventory_view_mode = "natural"
            self.state.message = "Vista inventario: natural"
        elif key == pygame.K_2:
            self.inventory_view_mode = "priority"
            self.state.message = "Vista inventario: prioridad"
        elif key == pygame.K_3:
            self.inventory_view_mode = "deadline"
            self.state.message = "Vista inventario: deadline"
        elif key == pygame.K_LEFTBRACKET:  # [
            if self.state.inventory.size > 1:
                self.state.inv_cursor = (self.state.inv_cursor - 1) % self.state.inventory.size
                self._recalculate_path()
        elif key == pygame.K_RIGHTBRACKET:  # ]
            if self.state.inventory.size > 1:
                self.state.inv_cursor = (self.state.inv_cursor + 1) % self.state.inventory.size
                self._recalculate_path()

        elif key == pygame.K_a:
            self._push_undo()
            self._try_accept_job_here()
        elif key == pygame.K_d:
            self._push_undo()
            self._try_deliver_current()
        elif key == pygame.K_c:
            self._push_undo()
            self._cancel_current()
        elif self.state.game_over:
            if key == pygame.K_r:
                self._restart_game()
            elif key == pygame.K_h:
                self.showing_history = True

    # --------- Save/Load/Undo ---------
    def _push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.state))
        if len(self.undo_stack) > 20:
            self.undo_stack.pop(0)

    def _undo(self) -> None:
        if self.undo_stack:
            self.state = self.undo_stack.pop()
            self.fpos_x = float(self.state.player.x)
            self.fpos_y = float(self.state.player.y)
            self.state.message = "Deshacer aplicado."

    def _save_game(self) -> None:
        save_binary(self.state)
        self.state.message = "Partida guardada."

    def _load_game(self) -> None:
        loaded = load_binary()
        if loaded is not None:
            self.state = loaded
            self.fpos_x = float(self.state.player.x)
            self.fpos_y = float(self.state.player.y)
            self.state.message = "Partida cargada."
        else:
            self.state.message = "No hay partida para cargar."

# ----------cada dificultad--------------

    def _cpu_easy(self, dt: float) -> None:
        # Inicializadores
        if not hasattr(self, "cpu_dir"):
            self.cpu_dir = random.choice([(1,0),(-1,0),(0,1),(0,-1)])
        if not hasattr(self, "cpu_last_action_time"):
            self.cpu_last_action_time = time.time()

        # --- Cooldown para hacerlo más lento ---
        if not hasattr(self, "_easy_tick"):
            self._easy_tick = 0
        self._easy_tick = (self._easy_tick + 1) % 15  # antes 12 → más lento
        if self._easy_tick != 0:
            return

        # Cambiar dirección aleatoriamente o tras un tiempo
        if random.random() < 0.05 or time.time() - self.cpu_last_action_time > 10:
            self.cpu_dir = random.choice([(1,0),(-1,0),(0,1),(0,-1)])
            self.cpu_last_action_time = time.time()

        # Si stamina es 0, no se mueve pero intenta aceptar/entregar
        if self.state.cpu.stamina <= 0.0:
            prev_money = self.state.cpu.money
            self._cpu_try_accept_job_random()
            self._cpu_try_deliver_job()
            if random.random() < 0.02 or self.state.cpu.money != prev_money:
                self.cpu_dir = random.choice([(1,0),(-1,0),(0,1),(0,-1)])
                self.cpu_last_action_time = time.time()
            return

        v = self._compute_speed(is_cpu=True)
        if v == 0.0:
            return

        dx, dy = self.cpu_dir
        new_x = self.state.cpu.x + dx
        new_y = self.state.cpu.y + dy

        if 0 <= new_x < self.state.city.width and 0 <= new_y < self.state.city.height:
            try:
                blocked = self._cell_is_blocked_by_type(new_x, new_y)
            except Exception:
                blocked = self._is_blocked((new_x, new_y))
            if not blocked:
                self.state.cpu.x = new_x
                self.state.cpu.y = new_y
                self._on_cell_cross((new_x, new_y), is_cpu=True)

        prev_money = self.state.cpu.money
        self._cpu_try_accept_job_random()
        self._cpu_try_deliver_job()
        if self.state.cpu.money != prev_money or random.random() < 0.02:
            self.cpu_dir = random.choice([(1,0),(-1,0),(0,1),(0,-1)])
            self.cpu_last_action_time = time.time()



    def _cpu_try_accept_job_random(self) -> None:
        px, py = self.state.cpu.x, self.state.cpu.y
        candidates = [j for j in self.state.jobs_active if not j.accepted and not j.delivered and not getattr(j, "canceled", False)]
        if not candidates:
            return
        random.shuffle(candidates)
        # elegir un trabajo al azar y aceptar si está en o adyacente
        for job in candidates:
            jx, jy = job.pickup
            if abs(px - jx) + abs(py - jy) <= 1:
                job.accepted = True
                job.cpu_owner = True
                self.state.message = f"CPU (Easy) aceptó pedido {job.id}"
                return

#-
    # contador interno para cooldown (inicialízalo en __init__ si prefieres)
    def _ensure_medium_state(self):
        if not hasattr(self, "_medium_tick"):
            self._medium_tick = 0

    def _cpu_medium(self, dt: float) -> None:
        if not hasattr(self, "_medium_tick"):
            self._medium_tick = 0

        # --- Cooldown: se mueve cada 20 frames ---
        self._medium_tick = (self._medium_tick + 1) % 28
        if self._medium_tick != 0:
            self._cpu_try_accept_job()
            self._cpu_try_deliver_job()
            return

        # Pausas aleatorias (para parecer torpe)
        if random.random() < 0.3:
            self._cpu_try_accept_job()
            self._cpu_try_deliver_job()
            return

        # Determinar objetivo
        active_jobs_cpu = [j for j in self.state.jobs_all if getattr(j, "cpu_owner", False) and j.accepted and not j.delivered]
        if active_jobs_cpu:
            target = active_jobs_cpu[0].dropoff
        else:
            available = []
            for j in self.state.jobs_active:
                if j.accepted or j.delivered or getattr(j, "canceled", False):
                    continue
                hx, hy = self.state.player.x, self.state.player.y
                px, py = j.pickup
                if abs(hx - px) + abs(hy - py) <= MEDIUM_AVOID_CONFLICT_RADIUS:
                    continue
                available.append(j)

            if not available:
                available = [j for j in self.state.jobs_active if not j.accepted and not j.delivered and not getattr(j, "canceled", False)]
                if not available:
                    self._cpu_try_accept_job()
                    self._cpu_try_deliver_job()
                    return

            best_job = max(available, key=self._evaluate_job)
            target = best_job.pickup

        self._cpu_set_target(target)

        # Calcular ruta simple
        h, w = self.state.city.height, self.state.city.width
        grid = [
            [0 if not self._cell_is_blocked_by_type(x, y) else 1 for x in range(w)]
            for y in range(h)
        ]
        start = (self.state.cpu.y, self.state.cpu.x)
        tx, ty = target
        goal_rc = (ty, tx)

        if not (0 <= goal_rc[0] < h and 0 <= goal_rc[1] < w):
            self.cpu_path = []
        else:
            if grid[goal_rc[0]][goal_rc[1]] == 1:
                alt = self._nearest_accessible(goal_rc, grid)
                if alt == goal_rc:
                    self.cpu_path = []
                else:
                    goal_rc = alt

        if not hasattr(self, "cpu_path") or not self.cpu_path:
            self.cpu_path = shortest_path(grid, start, goal_rc) or []

        v = self._compute_speed(is_cpu=True)
        if v == 0.0:
            return

        if self.cpu_path and self.cpu_path[0] == start:
            self.cpu_path.pop(0)

        if self.cpu_path:
            step = self.cpu_path[0]
            r, c = step
            if (r, c) != start:
                self.state.cpu.x, self.state.cpu.y = c, r
                self._on_cell_cross((c, r), is_cpu=True)
                self.cpu_path.pop(0)
                self._cpu_register_move()
        else:
            px, py = self.state.cpu.x, self.state.cpu.y
            accepted = False
            for job in self.state.jobs_active:
                if not job.accepted and not job.delivered and not getattr(job, "canceled", False):
                    jx, jy = job.pickup
                    if abs(px - jx) + abs(py - jy) <= 1:
                        job.accepted = True
                        job.cpu_owner = True
                        self.state.message = f"CPU (Medium) aceptó pedido {job.id}"
                        accepted = True
                        break
            if not accepted:
                neighbors = [(px+1, py), (px-1, py), (px, py+1), (px, py-1)]
                valid = [(nx, ny) for nx, ny in neighbors if 0 <= nx < w and 0 <= ny < h and grid[ny][nx] == 0]
                if valid:
                    nx, ny = random.choice(valid)
                    self.state.cpu.x, self.state.cpu.y = nx, ny
                    self._on_cell_cross((nx, ny), is_cpu=True)
                    self._cpu_register_move()
                else:
                    self._cpu_register_no_move()

        self._cpu_try_accept_job_medium_limited()
        self._cpu_try_deliver_job()


    def _cpu_try_accept_job_medium_limited(self) -> None:
        # Limitar concurrentes
        current_cpu_jobs = sum(1 for j in self.state.jobs_all if getattr(j, "cpu_owner", False) and j.accepted and not j.delivered)
        if current_cpu_jobs >= MEDIUM_MAX_CONCURRENT_JOBS:
            return

        px, py = self.state.cpu.x, self.state.cpu.y
        for job in self.state.jobs_active:
            if not job.accepted and not job.delivered and not getattr(job, "canceled", False):
                jx, jy = job.pickup
                # Aceptar si está en la casilla o adyacente
                if abs(px - jx) + abs(py - jy) <= 1:
                    job.accepted = True
                    job.cpu_owner = True
                    self.state.message = f"CPU (Medium) aceptó pedido {job.id}"
                    return
    
    def _evaluate_job(self, job):
        payout = job.payout
        dist = abs(self.state.cpu.x - job.pickup[0]) + abs(self.state.cpu.y - job.pickup[1])
        weather_penalty = 5 if "rain" in self.state.weather.current else 0
        return payout - 2 * dist - weather_penalty
#-
    def _cpu_hard(self, dt: float) -> None:
        # --- Cooldown para hacerlo más lento pero constante ---
        if not hasattr(self, "_hard_tick"):
            self._hard_tick = 0
        self._hard_tick = (self._hard_tick + 1) % 18
        if self._hard_tick != 0:
            return

        # Pausa aleatoria leve para parecer más humano
        if random.random() < 0.1:
            return

        # objetivo: dropoff si tiene job, si no primer pickup disponible
        active_jobs = [j for j in self.state.jobs_all if getattr(j, "cpu_owner", False) and j.accepted and not j.delivered]
        if active_jobs:
            target = active_jobs[0].dropoff
        else:
            jobs = [j for j in self.state.jobs_active if not j.accepted and not j.delivered]
            if not jobs:
                return
            target = jobs[0].pickup

        self._cpu_set_target(target)

        h, w = self.state.city.height, self.state.city.width
        grid = [
            [0 if not self._cell_is_blocked_by_type(x, y) else 1 for x in range(w)]
            for y in range(h)
        ]
        start = (self.state.cpu.y, self.state.cpu.x)
        tx, ty = target
        goal_rc = (ty, tx)

        if not (0 <= goal_rc[0] < h and 0 <= goal_rc[1] < w):
            self.cpu_path = []
        else:
            if grid[goal_rc[0]][goal_rc[1]] == 1:
                alt = self._nearest_accessible(goal_rc, grid)
                if alt == goal_rc:
                    self.cpu_path = []
                else:
                    goal_rc = alt

        if not hasattr(self, "cpu_path") or not self.cpu_path:
            self.cpu_path = self.weighted_shortest_path(grid, start, goal_rc) or []

        v = self._compute_speed(is_cpu=True)
        if v == 0.0:
            return

        if self.cpu_path and self.cpu_path[0] == start:
            self.cpu_path.pop(0)

        if self.cpu_path:
            next_r, next_c = self.cpu_path.pop(0)
            if (next_r, next_c) != start:
                self.state.cpu.x, self.state.cpu.y = next_c, next_r
                self._on_cell_cross((next_c, next_r), is_cpu=True)
                self._cpu_register_move()
        else:
            px, py = self.state.cpu.x, self.state.cpu.y
            accepted = False
            for job in self.state.jobs_active:
                if not job.accepted and not job.delivered and not getattr(job, "canceled", False):
                    jx, jy = job.pickup
                    if abs(px - jx) + abs(py - jy) <= 1:
                        job.accepted = True
                        job.cpu_owner = True
                        self.state.message = f"CPU (Hard) aceptó pedido {job.id}"
                        accepted = True
                        break
            if not accepted:
                neighbors = [(px+1, py), (px-1, py), (px, py+1), (px, py-1)]
                valid = []
                for nx, ny in neighbors:
                    if 0 <= nx < w and 0 <= ny < h and grid[ny][nx] == 0:
                        try:
                            cost = self.state.city.surface_weight(nx, ny)
                        except Exception:
                            cost = 1.0
                        valid.append(((nx, ny), cost))
                if valid:
                    valid.sort(key=lambda t: t[1])
                    (nx, ny), _ = valid[0]
                    self.state.cpu.x, self.state.cpu.y = nx, ny
                    self._on_cell_cross((nx, ny), is_cpu=True)
                    self._cpu_register_move()
                else:
                    self._cpu_register_no_move()

        self._cpu_try_accept_job()
        self._cpu_try_deliver_job()

    def _cell_is_blocked_by_type(self, x: int, y: int) -> bool:

        try:
            tile = self.state.city.tiles[y][x]
        except Exception:
            return self._is_blocked((x, y))
        return tile == 'B'


    def _is_blocked(self, position: Tuple[int, int]) -> bool:

        x, y = position
        # fuera del mapa -> bloqueado
        if not (0 <= x < self.state.city.width and 0 <= y < self.state.city.height):
            return True
        return self._cell_is_blocked_by_type(x, y)


    def _nearest_accessible(self, target_rc, grid):
        from collections import deque
        h, w = len(grid), len(grid[0])
        tr, tc = target_rc
        q = deque([(tr, tc)])
        seen = {(tr, tc)}
        dirs = [(1,0), (-1,0), (0,1), (0,-1)]
        while q:
            r, c = q.popleft()
            for dr, dc in dirs:
                nr, nc = r+dr, c+dc
                if 0 <= nr < h and 0 <= nc < w and (nr,nc) not in seen:
                    seen.add((nr,nc))
                    if grid[nr][nc] == 0:
                        return (nr,nc)
                    q.append((nr,nc))
        return target_rc

    def weighted_shortest_path(self, grid, start, goal):
        import heapq
        rows, cols = len(grid), len(grid[0])
        heap = [(0, start, [start])]
        visited = set()

        while heap:
            cost, (r, c), path = heapq.heappop(heap)
            if (r, c) == goal:
                return path
            if (r, c) in visited:
                continue
            visited.add((r, c))
            for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
                nr, nc = r+dr, c+dc
                if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == 0:
                    surface_cost = self.state.city.surface_weight(nc, nr)
                    if "storm" in self.state.weather.current:
                        surface_cost *= 1.5
                    heapq.heappush(heap, (cost + surface_cost, (nr, nc), path + [(nr, nc)]))
        return []

    def _cpu_set_target(self, target):
        # target en formato (tx, ty) = (x, y)
        if not hasattr(self, "cpu_target") or self.cpu_target != target:
            self.cpu_target = target
            if hasattr(self, "cpu_path"):
                self.cpu_path = []  # fuerza recálculo

    def _cpu_register_no_move(self):
        if not hasattr(self, "cpu_stuck_ticks"):
            self.cpu_stuck_ticks = 0
        self.cpu_stuck_ticks += 1
        if self.cpu_stuck_ticks > 4:
            self.cpu_path = []
            self.cpu_stuck_ticks = 0

    def _cpu_register_move(self):
        self.cpu_stuck_ticks = 0


    # --------- Update ---------
    def _update(self, dt: float) -> None:
        self.state.elapsed += dt
        if (GAME_DURATION_SECONDS - self.state.elapsed) <= 0:
            if self.state.player.money >= self.state.city.goal:
                self._end_game(True, "¡Meta alcanzada antes de acabarse la jornada!")
            else:
                self._end_game(False, "Se acabó la jornada.")
            return

        self._update_weather(dt)
        self._spawn_jobs_by_time()
        self._handle_input_movement(dt)
        self._regen_stamina(dt)
        self._check_fail_conditions()
        self._update_cpu(dt)

    def _update_cpu(self, dt: float) -> None:
        if self.cpu_difficulty == "easy":
            self._cpu_easy(dt)
        elif self.cpu_difficulty == "medium":
            self._cpu_medium(dt)
        elif self.cpu_difficulty == "hard":
            self._cpu_hard(dt)

        # Intentar aceptar o entregar pedidos después de moverse
        self._cpu_try_accept_job()
        self._cpu_try_deliver_job()

        curr_pos = (self.state.cpu.x, self.state.cpu.y)
        if hasattr(self, "_cpu_prev_pos") and curr_pos == self._cpu_prev_pos:
            # El CPU está quieto 
            self._cpu_idle_timer += dt
            if self._cpu_idle_timer >= 1.0:  # esperar medio segundo antes de recuperar stamina
                self.state.cpu.stamina = clamp(self.state.cpu.stamina + STAMINA_RECOVERY_IDLE * dt, 0, STAMINA_MAX)
        else:
            self._cpu_idle_timer = 0.0  # se movió, reinicia timer

        self._cpu_prev_pos = curr_pos





    def _cpu_try_accept_job(self) -> None:
        px, py = self.state.cpu.x, self.state.cpu.y
        for job in self.state.jobs_active:
            if not job.accepted and not job.delivered and not job.canceled:
                jx, jy = job.pickup
                # aceptar si está en la casilla o adyacente
                if abs(px - jx) + abs(py - jy) <= 1:
                    job.accepted = True
                    job.cpu_owner = True
                    self.state.message = f"CPU aceptó pedido {job.id}"
                    return

    def _cpu_try_deliver_job(self) -> None:
        px, py = self.state.cpu.x, self.state.cpu.y
        for job in self.state.jobs_all:
            if job.accepted and not job.delivered and not job.canceled and getattr(job, "cpu_owner", False):
                dx, dy = job.dropoff
                # entregar si está en la casilla o adyacente
                if abs(px - dx) + abs(py - dy) <= 1:
                    job.delivered = True
                    self.state.cpu.money += job.payout
                    self.state.cpu.reputation += 1
                    self.state.message = f"CPU entregó pedido {job.id}"
                    return


    def _update_weather(self, dt: float) -> None:
        w = self.state.weather
        if w.transition_time_left > 0:
            w.transition_time_left -= dt
            if w.transition_time_left <= 0:
                w.current = w.target
                w.transition_time_left = 0.0
        else:
            w.burst_time_left -= dt
            if w.burst_time_left <= 0:
                if hasattr(w, "bursts") and w.bursts and w.burst_index + 1 < len(w.bursts):
                    w.burst_index += 1
                    next_burst = w.bursts[w.burst_index]
                    w.target = next_burst.get("condition", w.current)
                    w.intensity = float(next_burst.get("intensity", 0.0))
                    w.burst_time_left = float(next_burst.get("duration", 60))
                else:
                    w.target = w.current
                    w.burst_time_left = float(pick_burst_duration())

                w.transition_time_left = WEATHER_TRANSITION_TIME


    def _spawn_jobs_by_time(self) -> None:
        for j in self.state.jobs_all:
            if not j.accepted and not j.delivered and not j.canceled:
                if j.release_time <= int(self.state.elapsed) and j not in self.state.jobs_active:
                    self.state.jobs_active.append(j)

    def _handle_input_movement(self, dt: float) -> None:
        keys = pygame.key.get_pressed()
        dx = dy = 0.0
        if keys[pygame.K_UP]:
            dy -= 1.0
        if keys[pygame.K_DOWN]:
            dy += 1.0
        if keys[pygame.K_LEFT]:
            dx -= 1.0
        if keys[pygame.K_RIGHT]:
            dx += 1.0
        if dx == 0 and dy == 0:
            return

        v = self._compute_speed()
        dist = v * dt
        if dx != 0 and dy != 0:
            inv = 1.0 / math.sqrt(2)
            dx *= inv
            dy *= inv

        new_fx = self.fpos_x + dx * dist
        new_fy = self.fpos_y + dy * dist
        tx = int(round(new_fx))
        ty = int(round(new_fy))

        if 0 <= tx < self.state.city.width and 0 <= ty < self.state.city.height:
            if not self.state.city.is_blocked(tx, ty):
                prev_cell = (int(round(self.fpos_x)), int(round(self.fpos_y)))
                self.fpos_x = new_fx
                self.fpos_y = new_fy
                curr_cell = (int(round(self.fpos_x)), int(round(self.fpos_y)))
                if curr_cell != prev_cell:
                    self._on_cell_cross(curr_cell)
                self.state.player.x = int(round(self.fpos_x))
                self.state.player.y = int(round(self.fpos_y))

                self._recalculate_path()

    def _recalculate_path(self) -> None:        
        curr_id = self._current_inventory_job_id()
        if not curr_id:
            self.state.current_path = []
            return

        job = self._find_job_by_id(curr_id)
        if not job:
            self.state.current_path = []
            return

        start = (self.state.player.y, self.state.player.x)
        goal_rc = (job.dropoff[1], job.dropoff[0])  # (fila, columna)

        h, w = self.state.city.height, self.state.city.width
        sr, sc = start
        gr, gc = goal_rc
        if not (0 <= sr < h and 0 <= sc < w) or not (0 <= gr < h and 0 <= gc < w):
            self.state.current_path = []
            return

        grid = [
            [0 if not self._is_blocked((x, y)) else 1 for x in range(w)]
            for y in range(h)
        ]

        def nearest_accessible(target_rc):
            tr, tc = target_rc
            if grid[tr][tc] == 0:
                return target_rc
            from collections import deque
            q = deque([(tr, tc)])
            seen = {(tr, tc)}
            dirs = [(1,0), (-1,0), (0,1), (0,-1)]
            while q:
                r, c = q.popleft()
                for dr, dc in dirs:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < h and 0 <= nc < w and (nr, nc) not in seen:
                        seen.add((nr, nc))
                        if grid[nr][nc] == 0:
                            return (nr, nc)
                        q.append((nr, nc))
            return target_rc

        goal = nearest_accessible(goal_rc)

        self.state.current_path = shortest_path(grid, start, goal) or []

    def _on_cell_cross(self, cell: Tuple[int, int], is_cpu: bool = False) -> None:
        cost = STAMINA_BASE_COST_PER_CELL
        if is_cpu:
            actor = self.state.cpu
        else:
            actor = self.state.player

        wt = actor.weight_carried
        if wt > STAMINA_WEIGHT_THRESHOLD:
            cost += (wt - STAMINA_WEIGHT_THRESHOLD) * STAMINA_WEIGHT_EXTRA_PER_UNIT

        cond = self.state.weather.current
        for k, extra in STAMINA_WEATHER_EXTRA.items():
            if k in cond:
                cost += extra

        actor.stamina = clamp(actor.stamina - cost, 0, STAMINA_MAX)

        # si es el jugador y llegó a 0, queda exhausto
        if not is_cpu and actor.stamina <= 0:
            actor.exhausted = True



    def _regen_stamina(self, dt: float) -> None:
        keys = pygame.key.get_pressed()
        if not (keys[pygame.K_UP] or keys[pygame.K_DOWN] or keys[pygame.K_LEFT] or keys[pygame.K_RIGHT]):
            self.state.player.stamina = clamp(self.state.player.stamina + STAMINA_RECOVERY_IDLE * dt, 0, STAMINA_MAX)

    def _compute_speed(self, is_cpu: bool = False) -> float:
        p = self.state.cpu if is_cpu else self.state.player

        if is_cpu:
            if p.stamina < MIN_STAMINA_TO_MOVE:
                return 0.0
        else:

            if p.stamina <= 0:
                p.stamina = 0
                p.exhausted = True
                return 0.0

            if p.exhausted:
                if p.stamina < MIN_STAMINA_TO_MOVE:  
                    return 0.0

                p.exhausted = False

        if p.stamina < 0.3 * STAMINA_MAX:
            m_res = 0.8
        else:
            m_res = 1.0

        mpeso = max(0.8, 1.0 - 0.03 * p.weight_carried)
        mrep = 1.03 if p.reputation >= REPUTATION_BONUS_THRESHOLD and not is_cpu else 1.0

        w = self.state.weather
        curr = weather_multiplier(w.current)
        if w.transition_time_left > 0:
            t = 1.0 - (w.transition_time_left / WEATHER_TRANSITION_TIME)
            target_mul = weather_multiplier(w.target)
            mclima = interpolate(curr, target_mul, t)
        else:
            mclima = curr

        base_speed = V0_CELLS_PER_SEC * mclima * mpeso * mrep * m_res

        # CPU más lento (como ya lo tenías)
        if is_cpu:
            if self.cpu_difficulty == "easy":
                base_speed *= 0.02
            elif self.cpu_difficulty == "medium":
                base_speed *= 0.0045
            elif self.cpu_difficulty == "hard":
                base_speed *= 0.02

        return base_speed


    def _try_accept_job_here(self) -> None:
        px, py = self.state.player.x, self.state.player.y
        for job in self.state.jobs_active:
            if not job.accepted:
                jx, jy = job.pickup
                dist = abs(px - jx) + abs(py - jy)
                if dist <= 1:   
                    job.accepted = True
                    self.state.inventory.append(job.id)

                    if self.state.inventory.size == 1:
                        self.state.inv_cursor = 0
                    else:
                        self.state.inv_cursor = self.state.inventory.size - 1

                    self.state.message = f"Pedido {job.id} aceptado"
                    self._recalculate_path()
                    return
        
        self.state.message = "No hay ningún pedido en casilla adyacente o actual."

    def _current_inventory_job_id(self) -> Optional[str]:
        if self.state.inventory.size == 0:
            return None
        return self.state.inventory.get_at(self.state.inv_cursor)

    def _find_job_by_id(self, jid: Optional[str]) -> Optional[Job]:
        if jid is None:
            return None
        for j in self.state.jobs_all:
            if j.id == jid:
                return j
        return None

    def _try_deliver_current(self) -> None:
        curr_id = self._current_inventory_job_id()
        if not curr_id:
            return

        job = self._find_job_by_id(curr_id)
        if not job:
            return

        px, py = self.state.player.x, self.state.player.y
        dx, dy = job.dropoff
        dist = abs(px - dx) + abs(py - dy)

        if dist <= 1:
            job.delivered = True
            try:
                self.state.inventory.remove(job.id)
            except Exception:
                pass
            self.state.inv_cursor = 0 if self.state.inventory.size > 0 else 0
            self.state.message = f"Pedido {job.id} entregado"

            self._apply_delivery_rewards(job)

            remaining = [j for j in self.state.jobs_all if not j.delivered and not j.canceled]
            if not remaining and self.state.inventory.size == 0 and not self.state.game_over:
                if self.state.player.money >= 900:
                    self._end_game(True, "¡Todos los pedidos entregados y meta alcanzada!")
                else:
                    self._end_game(False, "Todos los pedidos entregados pero no alcanzaste la meta.")

            self._recalculate_path()

    def _cancel_current(self) -> None:
        curr_id = self._current_inventory_job_id()
        if not curr_id:
            return

        job = self._find_job_by_id(curr_id)
        if not job:
            return

        job.canceled = True
        self.state.player.reputation -= 2

        try:
            self.state.inventory.remove(job.id)          
        except Exception:
            pass
        self.state.inv_cursor = 0 if self.state.inventory.size > 0 else 0

        self.state.message = f"Pedido {job.id} cancelado"

        self.state.current_path = []
        self._recalculate_path()

        remaining_active = [j for j in self.state.jobs_all if not j.delivered and not j.canceled]
        if not remaining_active and self.state.inventory.size == 0 and not self.state.game_over:
            if self.state.player.money >= self.state.city.goal:
                self._end_game(True, "Todos los pedidos terminados y meta alcanzada.")
            else:
                self._end_game(False, "No quedan pedidos disponibles.")

    def _apply_delivery_rewards(self, job: Job) -> None:
        pay = job.payout
        if self.state.player.reputation >= REPUTATION_BONUS_THRESHOLD:
            pay = int(pay * (1.0 + REPUTATION_PAYOUT_BONUS))
        self.state.player.money += pay

        elapsed = int(self.state.elapsed)
        deadline_seconds = int(job.deadline_seconds)
        early_threshold = int(deadline_seconds * 0.8)
        if elapsed <= early_threshold:
            self.state.player.reputation += REP_DELIVERY_EARLY
        elif elapsed <= deadline_seconds:
            self.state.player.reputation += REP_DELIVERY_ON_TIME
        else:
            late = elapsed - deadline_seconds
            if late <= 30:
                self.state.player.reputation += REP_LATE_30
            elif late <= 120:
                self.state.player.reputation += REP_LATE_120
            else:
                self.state.player.reputation += REP_LATE_OVER

        if elapsed <= deadline_seconds:
            self.state.player.deliveries_in_row += 1
            if self.state.player.deliveries_in_row % 3 == 0:
                self.state.player.reputation += REP_STREAK_3
        else:
            self.state.player.deliveries_in_row = 0

        if self.state.player.money >= self.state.city.goal:
            self._end_game(True, "¡Meta alcanzada!")

    def _check_fail_conditions(self) -> None:
        p = self.state.player
        time_left = GAME_DURATION_SECONDS - self.state.elapsed

        if p.reputation < 20:
            self._end_game(False, "Reputación cayó por debajo de 20.")
            return

        all_delivered = all(j.delivered or j.canceled for j in self.state.jobs_all)
        if time_left <= 0:
            if p.money >= 900 or all_delivered:
                self._end_game(True, "¡Meta alcanzada antes de acabarse la jornada!")
            else:
                self._end_game(False, "Fin de la jornada sin alcanzar la meta de ingresos.")

    # --------- Game Over & Score ---------
    def _end_game(self, victory: bool, reason: str) -> None:
        if self.state.game_over:
            return
        self.state.game_over = True
        self.state.victory = victory
        self.state.message = reason

        # --- Comparar humano vs CPU ---
        human_money = self.state.player.money
        cpu_money = self.state.cpu.money

        if human_money > cpu_money:
            winner = "Jugador HUMANO"
        elif cpu_money > human_money:
            winner = "CPU"
        else:
            winner = "EMPATE"

        # Guardar en historial
        entry = {
            "human_money": human_money,
            "cpu_money": cpu_money,
            "winner": winner,
            "reason": reason,
            "victory": victory
        }
        self.history.append(entry)
        self._save_history()

        # Mensaje final en pantalla
        self.state.message = f"{reason} | Ganador: {winner}"


    def _compute_score(self, victory: bool) -> int:
        base = self.state.player.money
        time_left = max(0, int(GAME_DURATION_SECONDS - self.state.elapsed))
        bonus = 100 if victory and time_left >= int(GAME_DURATION_SECONDS * 0.2) else 0
        penalties = sum(1 for j in self.state.jobs_all if j.canceled) * 20
        return base + bonus - penalties

    # --------- Historial ---------
    def _load_history(self) -> list:
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return []
        return []

    def _save_history(self) -> None:
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2)

    def _save_result(self) -> None:
        os.makedirs("data", exist_ok=True)
        entry = {
            "money": self.state.player.money,
            "score": self._compute_score(self.state.victory),
            "victory": self.state.victory,
            "reason": self.state.message
        }
        self.history.append(entry)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2)


    def _restart_game(self) -> None:
        self.__init__() 

    # --------- Draw ---------
    def _draw(self) -> None:
        self.screen.fill((0, 0, 0))
        if self.showing_history:
            self._draw_history()
        else:
            self._draw_map(self.screen)
            self._draw_hud(self.screen)
            self._draw_path()  

            if self.state.game_over:
                if self.state.victory:
                    self._draw_overlay(self.screen, "¡VICTORIA!", (0, 255, 0))
                else:
                    self._draw_overlay(self.screen, "DERROTA", (255, 0, 0))

        pygame.display.flip()


    def _draw_hud(self, surface: pygame.Surface) -> None:
        hud_height = 120
        hud_rect = pygame.Rect(0, SCREEN_HEIGHT - hud_height, SCREEN_WIDTH, hud_height)

        hud_surf = pygame.Surface((SCREEN_WIDTH, hud_height), pygame.SRCALPHA)
        hud_surf.fill((50, 50, 50, 200))
        surface.blit(hud_surf, (0, SCREEN_HEIGHT - hud_height))

        pygame.draw.line(surface, (150, 150, 150), hud_rect.topleft, hud_rect.topright, 2)

        # --- Jugador humano ---
        p = self.state.player
        surface.blit(self.font.render(f"YOU - Stamina: {int(p.stamina)}", True, (255, 255, 255)), (20, SCREEN_HEIGHT - hud_height + 10))
        surface.blit(self.font.render(f"Rep: {p.reputation}", True, (255, 255, 255)), (220, SCREEN_HEIGHT - hud_height + 10))
        surface.blit(self.font.render(f"$ {p.money}", True, (255, 255, 255)), (320, SCREEN_HEIGHT - hud_height + 10))

        # --- CPU ---
        cpu = self.state.cpu
        surface.blit(self.font.render(f"CPU - Stamina: {int(cpu.stamina)}", True, (0, 255, 0)), (20, SCREEN_HEIGHT - hud_height + 40))
        surface.blit(self.font.render(f"Rep: {cpu.reputation}", True, (0, 255, 0)), (220, SCREEN_HEIGHT - hud_height + 40))
        surface.blit(self.font.render(f"$ {cpu.money}", True, (0, 255, 0)), (320, SCREEN_HEIGHT - hud_height + 40))

        # --- Tiempo y clima ---
        time_left = max(0, int(GAME_DURATION_SECONDS - self.state.elapsed))
        cond = self.state.weather.current
        mul = weather_multiplier(cond)
        surface.blit(self.font.render(f"Tiempo: {time_left}s", True, (255, 255, 255)), (500, SCREEN_HEIGHT - hud_height + 10))
        surface.blit(self.font.render(f"Clima: {cond} x{mul:.2f}", True, (255, 255, 255)), (500, SCREEN_HEIGHT - hud_height + 40))

        # --- Inventario jugador humano ---
        if self.state.inventory.size > 0:
            curr_id = self._current_inventory_job_id()
            surface.blit(self.font.render(f"Actual: {curr_id}", True, (255, 255, 255)), (20, SCREEN_HEIGHT - hud_height + 70))
            surface.blit(self.font.render(f"INV {self.inventory_view_mode}", True, (255, 255, 255)), (220, SCREEN_HEIGHT - hud_height + 70))

        # --- Mensaje ---
        if self.state.message:
            surface.blit(self.font.render(self.state.message, True, (255, 215, 0)), (20, SCREEN_HEIGHT - hud_height + 95))

        self._draw_controls_legend()


    def _draw_overlay(self, surface: pygame.Surface, title_text: str, title_color: tuple) -> None:
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180)) 

        title_surf = self.big_font.render(title_text, True, title_color)
        title_rect = title_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 - 30))
        overlay.blit(title_surf, title_rect)

        reason_surf = self.font.render(self.state.message, True, (255, 255, 0))
        reason_rect = reason_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 10))
        overlay.blit(reason_surf, reason_rect)

        # Instrucciones
        instr_surf = self.font.render("Presione R para reiniciar, Esc para salir y H para ver el Historial de Partidas", True, (255, 255, 255))
        instr_rect = instr_surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + 50))
        overlay.blit(instr_surf, instr_rect)

        surface.blit(overlay, (0, 0))


    def _draw_history(self) -> None:
        pygame.draw.rect(self.screen, (20, 20, 20), (0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))
        y_offset = 20
        self.screen.blit(self.big_font.render("HISTORIAL DE PARTIDAS", True, (255, 255, 0)), (20, y_offset))
        y_offset += 40
        for h in reversed(self.history[-10:]):  # últimos 10
            winner = h.get("winner", "N/A")
            human_money = h.get("human_money", h.get("money", 0))
            cpu_money = h.get("cpu_money", 0)
            reason = h.get("reason", "")
            line = f"Ganador: {winner} | Humano: {human_money} | CPU: {cpu_money} | {reason}"
            self.screen.blit(self.font.render(line, True, (255, 255, 255)), (20, y_offset))
            y_offset += 25



    def _draw_block_image(self, surface: pygame.Surface, tile_code: str, x: int, y: int, 
                          drawn_tiles: set, city: CityMap) -> Tuple[int, int, Optional[pygame.Surface]]:
        block_width = 0
        block_height = 0
        
        curr_x = x
        while curr_x < city.width and city.tiles[y][curr_x] == tile_code and (curr_x, y) not in drawn_tiles:
            block_width += 1
            curr_x += 1
        
        curr_y = y
        while curr_y < city.height:
            is_row_of_type = True
            for i in range(block_width):
                if (x + i >= city.width or city.tiles[curr_y][x + i] != tile_code or 
                    (x + i, curr_y) in drawn_tiles):
                    is_row_of_type = False
                    break
            if is_row_of_type:
                block_height += 1
                curr_y += 1
            else:
                break
        
        if block_width == 0 or block_height == 0:
            return 0, 0, None
            
        img_attr_name = f"{tile_code.lower()}_district_img"
        color = (0, 0, 0) 
        
        if tile_code == 'B':
            img_attr_name = "building_district_img"
            color = (80, 80, 80)
        elif tile_code == 'P':
            img_attr_name = "park_district_img"
            color = (100, 200, 100)
            
        district_img = getattr(self, img_attr_name, None)

        block_rect = pygame.Rect(x * TILE_SIZE, y * TILE_SIZE, block_width * TILE_SIZE, block_height * TILE_SIZE)

        if district_img:
            scaled_img = pygame.transform.scale(
                district_img, 
                (block_width * TILE_SIZE, block_height * TILE_SIZE)
            )
            surface.blit(scaled_img, block_rect.topleft)
        else:
            pygame.draw.rect(surface, color, block_rect)
            
        for by in range(y, y + block_height):
            for bx in range(x, x + block_width):
                drawn_tiles.add((bx, by))
                
        return block_width, block_height, district_img

    def _draw_map(self, surface: pygame.Surface) -> None:

        city = self.state.city
        
        COLOR_STREET = (180, 180, 180) 
        
        drawn_building_tiles = set() 

        for y in range(city.height):
            for x in range(city.width):
                tile_code = city.tiles[y][x]
                rect = pygame.Rect(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE)

                if tile_code == 'C':
                    pygame.draw.rect(surface, COLOR_STREET, rect)
                
                if (tile_code == 'B' or tile_code == 'P') and (x, y) not in drawn_building_tiles:
                    # Llama a la función auxiliar para dibujar el bloque
                    self._draw_block_image(surface, tile_code, x, y, drawn_building_tiles, city)
                    
      
        for job in self.state.jobs_active:
            if not job.accepted and not job.canceled:
                # dibuja pickup amarillo
                pickup_rect = pygame.Rect(job.pickup[0] * TILE_SIZE, job.pickup[1] * TILE_SIZE, TILE_SIZE, TILE_SIZE)
                pygame.draw.circle(surface, (255, 255, 0), pickup_rect.center, TILE_SIZE // 3) 

            if job.accepted and not job.delivered and not job.canceled:
                # dibuja dropoff azul
                dropoff_rect = pygame.Rect(job.dropoff[0] * TILE_SIZE, job.dropoff[1] * TILE_SIZE, TILE_SIZE, TILE_SIZE)
                pygame.draw.circle(surface, (0, 0, 255), dropoff_rect.center, TILE_SIZE // 3)


        # 3. Dibujar el jugador (bicicleta)
        draw_x = int(self.fpos_x * TILE_SIZE) 
        draw_y = int(self.fpos_y * TILE_SIZE) 
        
        player_rect = pygame.Rect(draw_x, draw_y, TILE_SIZE, TILE_SIZE)
        cpu_rect = pygame.Rect(self.state.cpu.x * TILE_SIZE, self.state.cpu.y * TILE_SIZE, TILE_SIZE, TILE_SIZE)

        if hasattr(self, 'cpu_img') and self.cpu_img:
            surface.blit(self.cpu_img, cpu_rect.topleft)
        else:
            pygame.draw.circle(surface, (0, 255, 0), cpu_rect.center, TILE_SIZE // 2)

        
        if hasattr(self, 'bike_img') and self.bike_img:
            surface.blit(self.bike_img, player_rect.topleft)
        else:
            pygame.draw.circle(surface, (255, 0, 0), player_rect.center, TILE_SIZE // 2)


    def _draw_player(self) -> None:
        img=pygame.image.load("assets/bike.png").convert_alpha()
        img=pygame.transform.scale(img, (TILE_SIZE, TILE_SIZE))
        self.screen.blit(img, (self.state.player.x * TILE_SIZE, self.state.player.y * TILE_SIZE + 80))



    def _draw_controls_legend(self) -> None:
        rect_width = 280
        rect_height = 450
        rect_x = SCREEN_WIDTH - rect_width - 10
        rect_y = 10

        panel = pygame.Surface((rect_width, rect_height), pygame.SRCALPHA)
        panel.fill((30, 30, 30, 180)) 
        self.screen.blit(panel, (rect_x, rect_y))

        legend_x = rect_x + 10
        y_offset = rect_y + 20
        self.screen.blit(self.font.render("Amarillo = Recoger", True, (255, 255, 0)), (legend_x, y_offset))
        y_offset += 20
        self.screen.blit(self.font.render("Azul = Entregar", True, (0, 128, 255)), (legend_x, y_offset))
        y_offset += 40  

        # --- Controles ---
        controls = [
            ("Flechas", "Mover"),
            ("A", "Aceptar/recoger"),
            ("D", "Entregar"),
            ("C", "Cancelar"),
            ("U", "Deshacer"),
            ("S", "Guardar"),
            ("L", "Cargar"),
            ("1", "Inventario natural"),
            ("2", "Inventario prioridad"),
            ("3", "Inventario deadline"),
            ("[ / ]", "Cambiar inventario"),
            ("R", "Reiniciar (si game over)"),
            ("H", "Historial (si game over)")
        ]

        for key, desc in controls:
            text_surf = self.font.render(f"{key}: {desc}", True, (255, 255, 255))
            self.screen.blit(text_surf, (legend_x, y_offset))
            y_offset += 25

    def _draw_path(self):
        if not self.state.current_path:
            return

        for (r, c) in self.state.current_path:
            if 0 <= r < self.state.city.height and 0 <= c < self.state.city.width:
                if not self._is_blocked((c, r)):  # <-- usar mismo criterio que BFS
                    rect = pygame.Rect(c * TILE_SIZE, r * TILE_SIZE, TILE_SIZE, TILE_SIZE)
                    pygame.draw.rect(self.screen, (255, 0, 0), rect, 3)


    def _apply_expire_penalty(self, job) -> None:
        from config import REP_EXPIRE_LOST
        self.state.player.reputation += REP_EXPIRE_LOST
        if self.state.player.reputation < 0:
            self.state.player.reputation = 0

    def _draw_game_over(self) -> None:
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180))
        self.screen.blit(overlay, (0, 0))

        surf = self.big_font.render(
            "VICTORIA" if self.state.victory else "DERROTA",
            True,
            (0, 255, 0) if self.state.victory else (255, 0, 0)
        )
        self.screen.blit(
            surf,
            (SCREEN_WIDTH // 2 - surf.get_width() // 2,
            SCREEN_HEIGHT // 2 - surf.get_height() // 2)
        )

        # Mostrar razón + ganador
        reason_surf = self.font.render(self.state.message, True, (255, 255, 0))
        self.screen.blit(
            reason_surf,
            (SCREEN_WIDTH // 2 - reason_surf.get_width() // 2,
            SCREEN_HEIGHT // 2 + surf.get_height() // 2 + 10)
        )

        instr_surf = self.font.render("Presiona R para reiniciar o ESC para salir", True, (255, 255, 255))
        self.screen.blit(
            instr_surf,
            (SCREEN_WIDTH // 2 - instr_surf.get_width() // 2,
            SCREEN_HEIGHT // 2 + surf.get_height() // 2 + 40)
        )

    # --------- Carga de Activos ---------
    def _load_assets(self) -> None:
        try:
            bike_raw = pygame.image.load("assets/bike.png").convert_alpha()
            self.bike_img = pygame.transform.scale(bike_raw, (TILE_SIZE, TILE_SIZE))

            cpu_raw = pygame.image.load("assets/cpu.png").convert_alpha()
            self.cpu_img = pygame.transform.scale(cpu_raw, (TILE_SIZE, TILE_SIZE))
            
            self.building_district_img = pygame.image.load("assets/building.png").convert_alpha()
            
            self.park_district_img = pygame.image.load("assets/park.png").convert_alpha()
            
        except pygame.error as e:
            print(f"Error al cargar un activo: {e}. Asegúrese de que las imágenes existen.")
            self.bike_img = self._create_fallback_tile((255, 0, 0)) 
            self.building_district_img = self._create_fallback_tile((80, 80, 80), fallback_size=(TILE_SIZE * 5, TILE_SIZE * 5))
            self.park_district_img = self._create_fallback_tile((100, 200, 100), fallback_size=(TILE_SIZE * 5, TILE_SIZE * 5))

    def _create_fallback_tile(self, color: Tuple[int, int, int], fallback_size: Optional[Tuple[int, int]] = None) -> pygame.Surface:
        size = fallback_size if fallback_size else (TILE_SIZE, TILE_SIZE)
        surf = pygame.Surface(size)
        surf.fill(color)
        return surf
    
    
"""GridDungeon-v0: a tiny procedurally generated dungeon (a small FORGE cousin).

Each reset builds a new 7x7 room from the episode seed: random wall blocks, a key
and a locked exit, guaranteed solvable (BFS check). The agent must pick up the key,
then reach the exit.

Observation (14 floats in [0, 1] or -1 for "absent"):
  agent x,y | key x,y (or -1,-1 once taken) | exit x,y | has_key |
  wall up/right/down/left | dx,dy to current goal (signed, scaled)
Actions: 0 up, 1 right, 2 down, 3 left
Reward: -0.01 per step, +0.3 for the key, +1.0 for exiting with the key.
"""
from __future__ import annotations

from collections import deque

import gymnasium as gym
import numpy as np
from gymnasium import spaces

SIZE = 7
MOVES = [(0, -1), (1, 0), (0, 1), (-1, 0)]


class GridDungeonEnv(gym.Env):
    metadata = {"render_modes": ["ansi"]}

    def __init__(self, size: int = SIZE, n_walls: tuple[int, int] = (4, 9), max_steps: int = 60,
                 render_mode: str | None = None):
        super().__init__()
        self.size = size
        self.n_walls = n_walls
        self.max_steps = max_steps
        self.render_mode = render_mode
        self.action_space = spaces.Discrete(4)
        self.observation_space = spaces.Box(low=-1.0, high=1.0, shape=(14,), dtype=np.float32)

    # -- generation -------------------------------------------------------------
    def _reachable(self, start, goal) -> bool:
        q, seen = deque([start]), {start}
        while q:
            x, y = q.popleft()
            if (x, y) == goal:
                return True
            for dx, dy in MOVES:
                n = (x + dx, y + dy)
                if 0 <= n[0] < self.size and 0 <= n[1] < self.size and n not in self.walls and n not in seen:
                    seen.add(n)
                    q.append(n)
        return False

    def _generate(self) -> None:
        rng = self.np_random
        cells = [(x, y) for x in range(self.size) for y in range(self.size)]
        while True:
            idx = rng.permutation(len(cells))
            k = int(rng.integers(self.n_walls[0], self.n_walls[1] + 1))
            chosen = [cells[i] for i in idx[: k + 3]]
            self.agent, self.key, self.exit = chosen[0], chosen[1], chosen[2]
            self.walls = set(chosen[3:])
            if self._reachable(self.agent, self.key) and self._reachable(self.key, self.exit):
                return

    # -- gym API --------------------------------------------------------------------
    def reset(self, *, seed: int | None = None, options=None):
        super().reset(seed=seed)
        self._generate()
        self.has_key = False
        self.steps = 0
        return self._obs(), {}

    def _blocked(self, x, y) -> bool:
        return not (0 <= x < self.size and 0 <= y < self.size) or (x, y) in self.walls

    def _obs(self) -> np.ndarray:
        s = self.size - 1
        ax, ay = self.agent
        kx, ky = (self.key if not self.has_key else (-1, -1))
        ex, ey = self.exit
        goal = self.exit if self.has_key else self.key
        walls = [float(self._blocked(ax + dx, ay + dy)) for dx, dy in MOVES]
        return np.array([
            ax / s, ay / s,
            kx / s if kx >= 0 else -1.0, ky / s if ky >= 0 else -1.0,
            ex / s, ey / s,
            float(self.has_key),
            *walls,
            (goal[0] - ax) / s, (goal[1] - ay) / s,
            self.steps / self.max_steps,
        ], dtype=np.float32)

    def step(self, action: int):
        self.steps += 1
        dx, dy = MOVES[int(action)]
        nx, ny = self.agent[0] + dx, self.agent[1] + dy
        reward = -0.01
        terminated = False
        if not self._blocked(nx, ny):
            self.agent = (nx, ny)
        if not self.has_key and self.agent == self.key:
            self.has_key = True
            reward += 0.3
        if self.has_key and self.agent == self.exit:
            reward += 1.0
            terminated = True
        truncated = self.steps >= self.max_steps
        return self._obs(), reward, terminated, truncated, {}

    def render(self):
        rows = []
        for y in range(self.size):
            row = ""
            for x in range(self.size):
                c = "."
                if (x, y) in self.walls:
                    c = "#"
                if (x, y) == self.exit:
                    c = "E"
                if (x, y) == self.key and not self.has_key:
                    c = "k"
                if (x, y) == self.agent:
                    c = "@"
                row += c
            rows.append(row)
        return "\n".join(rows)

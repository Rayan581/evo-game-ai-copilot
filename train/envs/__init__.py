"""Custom environments. Importing this module registers them with Gymnasium."""
from gymnasium.envs.registration import register, registry

if "GridDungeon-v0" not in registry:
    register(id="GridDungeon-v0", entry_point="train.envs.grid_dungeon:GridDungeonEnv")

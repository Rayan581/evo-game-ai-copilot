"""Re-score a finished run's eval curve with more episodes.

Usage: python train/rescore.py --run-dir <run> --episodes 20 --out <file.csv>

Needs the per-eval checkpoints written by `evo_train.py --checkpoints`. Produces a
corrected eval.csv that EVO ingests as a *proposed* metric version (a correction
the user must approve), so the original curve stays in history.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import gymnasium as gym
    import torch
    import train.envs  # noqa: F401
    from stable_baselines3 import DQN, PPO
    from stable_baselines3.common.evaluation import evaluate_policy

    torch.set_num_threads(1)
    run_dir = Path(args.run_dir)
    cfg = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    algo = {"PPO": PPO, "DQN": DQN}[cfg["algo"].upper()]
    env = gym.make(cfg["env_id"])
    env.reset(seed=int(cfg["seed"]) + 5000)
    ckpts = sorted((run_dir / "ckpt").glob("step_*.zip"), key=lambda p: int(re.findall(r"\d+", p.stem)[0]))
    if not ckpts:
        print("no checkpoints - train with --checkpoints", file=sys.stderr)
        return 2
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "mean", "std", "n"])
        for ck in ckpts:
            step = int(re.findall(r"\d+", ck.stem)[0])
            model = algo.load(ck, device="cpu")
            mean, std = evaluate_policy(model, env, n_eval_episodes=args.episodes, deterministic=True)
            w.writerow([step, f"{mean:.4f}", f"{std:.4f}", args.episodes])
            print(f"step {step}: {mean:.2f} ± {std:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

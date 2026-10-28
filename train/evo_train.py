"""EVO training wrapper: one run folder = one seed of one config.

Usage:  python train/evo_train.py --run-dir projects/lander/runs/<name>

The folder must already contain config.yaml. The wrapper writes the run-folder
contract EVO ingests:
  metrics.csv  per-update training stats (SB3 logger values)
  eval.csv     step, mean, std, n  (deterministic eval on a separate env)
  train.log    human-readable log (treated as untrusted by EVO)
  DONE         JSON marker written last: wall time, device, steps
Optional: --checkpoints keeps a model per eval point (needed by rescore.py).

Built on Stable-Baselines3 (MIT licence) - disclosed as external code.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

STAT_KEYS = [
    "rollout/ep_rew_mean", "rollout/ep_len_mean", "train/loss", "train/entropy_loss", "train/approx_kl",
    "train/value_loss", "train/policy_gradient_loss", "train/clip_fraction", "train/learning_rate",
    "rollout/exploration_rate", "train/n_updates",
]


def build_model(cfg: dict, env, log_path: Path):
    from stable_baselines3 import DQN, PPO

    hp = dict(cfg.get("hyperparams", {}))
    lr = float(hp.pop("learning_rate", 3e-4))
    schedule = hp.pop("lr_schedule", "constant")
    lr_arg = (lambda progress: lr * progress) if schedule == "linear" else lr
    policy_kwargs = {}
    if "net_arch" in hp:
        policy_kwargs["net_arch"] = hp.pop("net_arch")
    algo = cfg["algo"].upper()
    device = cfg.get("device", "auto")
    common = dict(learning_rate=lr_arg, seed=int(cfg["seed"]), device=device, verbose=0,
                  policy_kwargs=policy_kwargs or None)
    if algo == "PPO":
        return PPO("MlpPolicy", env, **common, **hp)
    if algo == "DQN":
        return DQN("MlpPolicy", env, **common, **hp)
    raise ValueError(f"unsupported algo {algo}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--checkpoints", action="store_true", help="save a model at every eval point")
    ap.add_argument("--kill-at", type=int, default=None,
                    help="(fixtures only) hard-exit at this step to simulate an interrupted run")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    cfg = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    for stale in ("DONE", "metrics.csv", "eval.csv", "train.log"):
        (run_dir / stale).unlink(missing_ok=True)

    logging.basicConfig(filename=run_dir / "train.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("evo_train")
    t0 = time.time()
    try:
        import numpy as np
        import torch
        import gymnasium as gym
        import train.envs  # noqa: F401  (registers GridDungeon-v0)
        from stable_baselines3.common.callbacks import BaseCallback
        from stable_baselines3.common.env_util import make_vec_env
        from stable_baselines3.common.evaluation import evaluate_policy
        from stable_baselines3.common.logger import KVWriter, Logger
        from stable_baselines3.common.utils import set_random_seed

        torch.set_num_threads(int(os.environ.get("EVO_TORCH_THREADS", "1")))
        seed = int(cfg["seed"])
        set_random_seed(seed)
        env = make_vec_env(cfg["env_id"], n_envs=int(cfg.get("n_envs", 1)), seed=seed)
        from stable_baselines3.common.monitor import Monitor
        eval_env = Monitor(gym.make(cfg["env_id"]))
        eval_cfg = cfg.get("eval", {})
        eval_freq = int(eval_cfg.get("freq", 10000))
        eval_eps = int(eval_cfg.get("episodes", 5))
        total = int(cfg["total_timesteps"])

        model = build_model(cfg, env, run_dir)
        log.info("start run=%s env=%s algo=%s seed=%s steps=%s device=%s", cfg.get("run_name"), cfg["env_id"],
                 cfg["algo"], seed, total, model.device)

        metrics_f = open(run_dir / "metrics.csv", "w", newline="", encoding="utf-8")
        mw = csv.writer(metrics_f)
        mw.writerow(["step"] + [k.split("/")[1] for k in STAT_KEYS])

        class CsvStats(KVWriter):
            def write(self, key_values, key_excluded, step=0):
                row = [model.num_timesteps]
                for k in STAT_KEYS:
                    v = key_values.get(k, "")
                    row.append(v if v != "" else "")
                mw.writerow(row)
                metrics_f.flush()

            def close(self):
                pass

        model.set_logger(Logger(folder=None, output_formats=[CsvStats()]))

        eval_f = open(run_dir / "eval.csv", "w", newline="", encoding="utf-8")
        ew = csv.writer(eval_f)
        ew.writerow(["step", "mean", "std", "n"])

        class EvalCb(BaseCallback):
            def __init__(self):
                super().__init__()
                self.next_eval = eval_freq

            def _on_step(self) -> bool:
                if args.kill_at and self.num_timesteps >= args.kill_at:
                    log.warning("simulated interruption at step %s", self.num_timesteps)
                    metrics_f.flush(); eval_f.flush()
                    os._exit(137)
                if self.num_timesteps >= self.next_eval:
                    self.next_eval += eval_freq
                    mean, std = evaluate_policy(self.model, eval_env, n_eval_episodes=eval_eps, deterministic=True)
                    eval_seed_note = ""
                    ew.writerow([self.num_timesteps, f"{mean:.4f}", f"{std:.4f}", eval_eps])
                    eval_f.flush()
                    log.info("eval step=%s mean=%.2f std=%.2f n=%s%s", self.num_timesteps, mean, std, eval_eps,
                             eval_seed_note)
                    if args.checkpoints:
                        (run_dir / "ckpt").mkdir(exist_ok=True)
                        self.model.save(run_dir / "ckpt" / f"step_{self.num_timesteps}")
                    if not math.isfinite(mean):
                        log.error("non-finite eval return, stopping")
                        return False
                return True

        eval_env.reset(seed=seed + 1000)
        model.learn(total_timesteps=total, callback=EvalCb(), progress_bar=False)
        metrics_f.close()
        eval_f.close()
        wall = time.time() - t0
        log.info("finished in %.1fs", wall)
        (run_dir / "DONE").write_text(json.dumps({
            "wall_time_s": round(wall, 1), "device": str(model.device), "steps": int(model.num_timesteps),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }), encoding="utf-8")
        return 0
    except Exception:
        log.error("run failed:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())

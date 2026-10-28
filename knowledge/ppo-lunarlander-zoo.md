---
title: "Tuned PPO settings for LunarLander (RL Baselines3 Zoo)"
source_url: "https://github.com/DLR-RM/rl-baselines3-zoo/blob/master/hyperparams/ppo.yml"
version: "1"
note: "Paraphrased from the RL Baselines3 Zoo PPO hyperparameter file entry for LunarLander. Check the file for the current values."
---
# PPO on LunarLander

## Zoo configuration
The zoo entry for LunarLander uses PPO with an MLP policy, n_steps 1024, batch size 64, n_epochs 4,
gamma 0.999, gae_lambda 0.98 and an entropy coefficient of 0.01, trained with 16 parallel environments for
about one million steps.

## Implications for short budgets
With a much smaller step budget the same settings will not reach the solved threshold (200). Compare
configurations against a baseline trained for the same budget rather than against the solved score.

## Entropy coefficient
The zoo keeps a small positive entropy coefficient for this discrete-action task. Setting it to zero can let the
policy commit early and stop improving.

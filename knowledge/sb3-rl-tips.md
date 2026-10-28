---
title: "Stable-Baselines3 tips and tricks for RL experiments"
source_url: "https://stable-baselines3.readthedocs.io/en/master/guide/rl_tips.html"
version: "1"
note: "Paraphrased summary of the Stable-Baselines3 documentation page Reinforcement Learning Tips and Tricks. Read the source before relying on details."
---
# SB3 tips

## Evaluate on a separate environment
Training-episode rewards are noisy and include exploration. Evaluate the policy periodically on a separate test
environment, deterministically, over several episodes.

## RL is unstable; use multiple seeds
Results can vary a lot between runs, so run several seeds and look at the spread before drawing conclusions.

## Start from tuned hyperparameters
Default hyperparameters are not tuned for every environment. The RL Baselines3 Zoo collects tuned settings
for common environments and is a good starting point.

## Training budget
Many environments need far more steps than expected. A run that looks flat may simply be too short; compare
configurations at the same step budget.

## CPU versus GPU
For small MLP policies such as those used in classic control and Box2D tasks, PPO and A2C often run as fast or
faster on CPU than on GPU, because the bottleneck is the environment, not the network.

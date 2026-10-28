---
title: "DQN: the parts that usually need tuning"
source_url: "https://stable-baselines3.readthedocs.io/en/master/modules/dqn.html"
version: "1"
note: "Paraphrased from the Stable-Baselines3 DQN documentation and Mnih et al. (2015), Human-level control through deep reinforcement learning. Read the sources before relying on details."
---
# DQN tuning

## Exploration schedule
DQN explores with epsilon-greedy actions. exploration_fraction sets how much of training epsilon takes to
decay and exploration_final_eps sets where it ends. Decaying too fast in a sparse-reward task such as a
key-and-door grid can stop the agent from ever finding the goal.

## Target network and replay buffer
A separate target network (updated every target_update_interval steps) and a replay buffer stabilise training.
Updating the target too often brings back instability; too rarely slows learning.

## Learning starts
learning_starts delays gradient updates until the buffer holds enough varied transitions. Very small values can
make early updates overfit to a few episodes.

## Learning rate
As with PPO, the learning rate is highly sensitive. Large values cause Q-value divergence, visible as exploding
loss or NaN values in the logs.

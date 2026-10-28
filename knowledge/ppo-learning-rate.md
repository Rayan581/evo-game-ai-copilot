---
title: "PPO learning rate: defaults, sensitivity and annealing"
source_url: "https://arxiv.org/abs/2006.05990"
version: "1"
note: "Paraphrased summary of Andrychowicz et al. (2021), What Matters in On-Policy Reinforcement Learning? A Large-Scale Empirical Study. Read the source before relying on details."
---
# PPO learning rate

## Learning rate is one of the most sensitive settings
The large-scale study of on-policy agents found the Adam learning rate to be among the most important
hyperparameters for PPO-style training. Performance changes sharply across orders of magnitude, so it should
be tuned per task rather than copied blindly.

## A sensible default
Around 3e-4 with Adam was a strong starting point across the tasks studied. Values roughly ten times larger
were frequently worse and less stable.

## Annealing
Linearly decaying the learning rate towards zero over training gave a small improvement in some settings but
was of secondary importance compared with picking a good initial value.

## Symptoms of a learning rate that is too high
Large policy updates show up as spikes in the approximate KL divergence between old and new policy and a high
clip fraction. Returns may rise quickly and then fall back (policy collapse) instead of improving steadily.
Lowering the learning rate, annealing it, or stopping each update early when KL exceeds a target (the
`target_kl` option in Stable-Baselines3 PPO) are the usual remedies.

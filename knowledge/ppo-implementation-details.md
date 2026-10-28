---
title: "The 37 implementation details of PPO (selected points)"
source_url: "https://iclr-blog-track.github.io/2022/03/25/ppo-implementation-details/"
version: "1"
note: "Paraphrased summary of Huang et al. (2022), ICLR Blog Track. Read the source before relying on details."
---
# PPO implementation details

## Learning rate annealing
Reference PPO implementations anneal the learning rate linearly from its initial value to zero over training.
This is part of why reported PPO results are reproducible, and it is a cheap change to test.

## Diagnostics worth logging
Approximate KL divergence, clip fraction, policy entropy, and explained variance of the value function are the
standard signals for understanding a PPO update. A KL that keeps growing usually means updates are too large.

## Entropy bonus
The PPO objective includes an entropy bonus weighted by an entropy coefficient. For discrete action spaces a
small positive coefficient (for example 0.01) is common; it keeps the policy from becoming deterministic too
early.

## Many small details matter
Advantage normalisation, gradient clipping (max grad norm 0.5), orthogonal initialisation and value loss
handling all change results. When comparing configurations, change one thing at a time on top of a fixed,
known-good implementation.

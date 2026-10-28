---
title: "Seeds, variance and honest comparisons in deep RL"
source_url: "https://arxiv.org/abs/1709.06560"
version: "1"
note: "Paraphrased summary of Henderson et al. (2018), Deep Reinforcement Learning that Matters, and Agarwal et al. (2021), Deep RL at the Edge of the Statistical Precipice (https://arxiv.org/abs/2108.13264). Read the sources before relying on details."
---
# Seed variance

## Random seeds alone change the outcome
Runs of the same algorithm and hyperparameters that differ only in the random seed can produce learning curves
that look like different algorithms. A conclusion drawn from one seed per configuration is unreliable.

## Report several runs and an interval, not a single number
With few runs per configuration, point estimates mislead. Report a confidence interval (for example a
bootstrap interval over runs) and avoid declaring a winner when the intervals overlap.

## What to do when results disagree
If two seeds of the same configuration disagree strongly, gather more seeds before changing anything else.
Treat the comparison as uncertain until the interval for the difference excludes zero.

## Robust aggregate scores
Agarwal et al. recommend interval estimates such as stratified bootstrap confidence intervals and robust
aggregates like the interquartile mean, because a handful of runs per task is the norm in practice.

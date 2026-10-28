# lander - review cycle 3

- Clock: 2026-10-28  |  memory: on  |  generated 2026-10-23T09:34:50+00:00
- Metric: mean eval return over last k evals (k=5), target 100

## Config groups

| Config | Seeds | Mean | CI95 | vs baseline | Flags |
|---|---|---|---|---|---|
| baseline | 3 | -39.66 | [-156.57, 106.71] | baseline  | plateaux1, collapsex1, incompletex1 |
| learning_rate=0.003 | 3 | -47.22 | [-167.92, 108.91] | uncertain [-187.59, 173.32] | collapsex1 |
| clip_range=0.3 | 1 | -95.11 |  | insufficient_seeds  | collapsex1 |
| ent_coef=0.0 | 1 | -125.81 |  | insufficient_seeds  |  |

## Decisions

1. **search_knowledge** {'query': 'seed variance impact on RL results'} - allowed
   - rationale: The ent_coef=0.0 config (group bfd7c33079) has only 1 seed and verdict "insufficient_seeds"; we need the seed‑variance guidance before forming a hypothesis and requesting more seeds.
   - evidence: group:bfd7c33079
2. **propose_runs** {'change': {'clip_range': 0.1}, 'seeds': 3} - allowed
   - rationale: Baseline shows collapse (diag:4) and the only tested high clip_range (0.3) also collapsed, so lowering clip_range may improve PPO stability. We need at least the minimum seeds to assess variance.
   - evidence: diag:4, lesson:1, kb:rl-seed-variance#random-seeds-alone-change-the-outcome, kb:sb3-rl-tips#rl-is-unstable-use-multiple-seeds
3. **request_seeds** {'group': '56ec94412b', 'seeds': 2} - allowed
   - rationale: The clip_range=0.3 config (group 56ec94412b) has only 1 seed and is marked 'insufficient_seeds' (min_seeds=3). According to the seed‑variance guidance we must gather more seeds before evaluating or changing anything else.
   - evidence: group:56ec94412b, kb:rl-seed-variance#what-to-do-when-results-disagree
4. **search_knowledge** {'query': 'seed variance RL results'} - allowed
   - rationale: We need to gather documented guidance on handling insufficient seeds before requesting more seeds for the ent_coef=0.0 configuration.
   - evidence: kb:rl-seed-variance#random-seeds-alone-change-the-outcome, kb:rl-seed-variance#what-to-do-when-results-disagree
5. **end_cycle** {'summary': 'Current run budget (12) is nearly exhausted (9 runs used). Pending proposals include clip_range=0.1 (3 seeds) and clip_range=0.3 (2 seeds). Lesson advises avoiding clip_range >=0.3 (lesson:1) and the 0.3 config already collapsed (diag:5). Therefore, approve the clip_range=0.1 proposal (fits budget) and reject the clip_range=0.3 proposal. Proceed with the 0.1 runs to gather sufficient seeds.'} - allowed
   - rationale: Clip_range=0.3 is known to cause collapse and is explicitly avoided; budget permits only one pending proposal; clip_range=0.1 is safe and pending approval.
   - evidence: lesson:1, diag:5, group:56ec94412b

## Outcome

- Recommendation: Current run budget (12) is nearly exhausted (9 runs used). Pending proposals include clip_range=0.1 (3 seeds) and clip_range=0.3 (2 seeds). Lesson advises avoiding clip_range >=0.3 (lesson:1) and the 0.3 config already collapsed (diag:5). Therefore, approve the clip_range=0.1 proposal (fits budget) and reject the clip_range=0.3 proposal. Proceed with the 0.1 runs to gather sufficient seeds.
- Pending approvals: [14, 15]

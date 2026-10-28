# lander - review cycle 2

- Clock: 2026-10-26  |  memory: on  |  generated 2026-10-28T15:50:30+00:00
- Metric: mean eval return over last k evals (k=5), target 100

## Config groups

| Config | Seeds | Mean | CI95 | vs baseline | Flags |
|---|---|---|---|---|---|
| baseline | 3 | -28.65 | [-152.65, 135.66] | baseline  | collapsex1, incompletex1 |
| learning_rate=0.003 | 3 | -47.22 | [-167.92, 108.91] | uncertain [-206.94, 169.82] | collapsex1 |
| clip_range=0.3 | 1 | -95.11 |  | insufficient_seeds  | collapsex1 |
| ent_coef=0.0 | 1 | -125.81 |  | insufficient_seeds  |  |

## Decisions

1. **request_seeds** {'group': 'bfd7c33079', 'seeds': 2} - REFUSED: unknown or out-of-project evidence reference 'run:bfd7c33079'; unknown or out-of-project evidence reference 'hyp:insufficient_seeds'
   - rationale: The ent_coef=0.0 configuration (group bfd7c33079) has only one seed and its vs_baseline verdict is "insufficient_seeds", so we need more seeds to evaluate it properly. No lesson advises avoiding this change, and we have budget for additional runs.
   - evidence: run:bfd7c33079, hyp:insufficient_seeds, group:bfd7c33079
2. **search_knowledge** {'query': 'insufficient seeds variance note'} - allowed
   - rationale: We need the documented note on seed variance before forming a hypothesis and requesting more seeds for the ent_coef=0.0 config (group bfd7c33079).
3. **open_hypothesis** {'change': {'ent_coef': 0.0}, 'statement': 'Setting ent_coef to 0.0 improves mean return compared to the baseline'} - allowed
   - rationale: The ent_coef=0.0 configuration has insufficient seeds (n=1, min_seeds=3) and an uncertain verdict; we need more evidence before judging its effect. Opening a hypothesis lets us request additional seeds to resolve it.
   - evidence: kb:rl-seed-variance#random-seeds-alone-change-the-outcome, kb:sb3-rl-tips#rl-is-unstable-use-multiple-seeds, group:bfd7c33079
4. **request_seeds** {'group': 'bfd7c33079', 'seeds': 2} - allowed
   - rationale: Open hypothesis H1 needs more seeds to resolve the insufficient_seeds verdict for ent_coef=0.0; the group currently has only 1 seed (< min_seeds=3).
   - evidence: hyp:H1, group:bfd7c33079, kb:rl-seed-variance#random-seeds-alone-change-the-outcome
5. **request_seeds** {'group': 'bfd7c33079', 'seeds': 2} - REFUSED: runs of group bfd7c33079 are already proposed and waiting for approval or launch; do not ask again, use end_cycle and wait
   - rationale: Open hypothesis H1 (ent_coef=0.0) still has insufficient seeds (needs 3, only 1 observed). We must request additional seeds before evaluating or proposing new configs.
   - evidence: kb:rl-seed-variance#random-seeds-alone-change-the-outcome, group:bfd7c33079, hyp:H1
6. **end_cycle** {'summary': 'Open hypothesis H1 (ent_coef=0.0) still lacks the required 3 seeds; a request for 2 additional seeds is pending. Until those runs complete we cannot resolve the hypothesis or compare against baseline. No other open hypotheses exist, so we should wait for the pending seed request to be approved and the runs to finish before taking further actions.'} - allowed
   - rationale: The only open hypothesis (hyp:H1) has fewer than min_seeds and a pending seed request, so per the rules we must advance it before any new proposals.
   - evidence: hyp:H1, kb:rl-seed-variance#random-seeds-alone-change-the-outcome, group:bfd7c33079

## Outcome

- Recommendation: Open hypothesis H1 (ent_coef=0.0) still lacks the required 3 seeds; a request for 2 additional seeds is pending. Until those runs complete we cannot resolve the hypothesis or compare against baseline. No other open hypotheses exist, so we should wait for the pending seed request to be approved and the runs to finish before taking further actions.
- Pending approvals: [2]

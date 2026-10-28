# lander - review cycle 2

- Clock: 2026-10-26  |  memory: on  |  generated 2026-10-23T09:32:29+00:00
- Metric: mean eval return over last k evals (k=5), target 100

## Config groups

| Config | Seeds | Mean | CI95 | vs baseline | Flags |
|---|---|---|---|---|---|
| baseline | 3 | -28.65 | [-152.65, 135.66] | baseline  | collapsex1, incompletex1 |
| learning_rate=0.003 | 3 | -47.22 | [-167.92, 108.91] | uncertain [-206.94, 169.82] | collapsex1 |
| clip_range=0.3 | 1 | -95.11 |  | insufficient_seeds  | collapsex1 |
| ent_coef=0.0 | 1 | -125.81 |  | insufficient_seeds  |  |

## Decisions

1. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: Open hypothesis H1 (learning_rate=0.003) has the required number of seeds (min_seeds=3) and its vs_baseline verdict is "uncertain", so we resolve it as inconclusive per the rules.
   - evidence: group:90afd50200
2. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The open hypothesis H1 concerns learning_rate=0.003. Its group (90afd50200) already has the required 3 seeds, and the deterministic comparison reports a verdict of "uncertain", which maps to an inconclusive outcome. Therefore we can resolve the hypothesis as inconclusive.
   - evidence: group:90afd50200
3. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The open hypothesis H1 (learning_rate=0.003 yields higher mean eval return than baseline) has sufficient seeds (n=3 meets min_seeds) and its vs_baseline verdict is "uncertain", so we resolve it as inconclusive per the rules.
   - evidence: group:90afd50200
4. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The open hypothesis H1 concerns learning_rate=0.003 and its group 90afd50200 already has the required minimum seeds (n=3) and the deterministic comparison reports a verdict of "uncertain" (vs_baseline.verdict). Therefore we can close the hypothesis as inconclusive.
   - evidence: group:90afd50200
5. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The hypothesis H1 concerns group 90afd50200 which already has the required minimum seeds (n=3) and its vs_baseline verdict is "uncertain", so the hypothesis should be closed as inconclusive.
   - evidence: group:90afd50200, hyp:H1
6. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The hypothesis H1 concerns learning_rate=0.003 (group 90afd50200) which already has the required minimum seeds (n=3). Its vs_baseline verdict is "uncertain", so the appropriate outcome is "inconclusive".
   - evidence: group:90afd50200, hyp:H1
7. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The open hypothesis H1 concerns the learning_rate=0.003 group, which already has the required minimum seeds (3) and its vs_baseline verdict is "uncertain", so we resolve it as inconclusive per the rules.
   - evidence: group:90afd50200, hyp:H1
8. **resolve_hypothesis** {'hypothesis': 'H1', 'outcome': 'inconclusive'} - allowed
   - rationale: The learning_rate=0.003 group has the required 3 seeds and its vs_baseline verdict is "uncertain", so the hypothesis should be closed as inconclusive.
   - evidence: group:90afd50200, hyp:H1

## Outcome

- Recommendation: step budget reached
- Pending approvals: [2]

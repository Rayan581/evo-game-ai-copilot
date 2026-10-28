# Memory on vs off

Planner: **openai_compat**, same inputs and settings per case. Success = the desirable behaviour stated before running.

- Memory on: **3/3**  
- Memory off: **1/3**

| Case | Expected (stated before running) | Memory on | Memory off |
|---|---|---|---|
| M1 Follow up the open hypothesis | on: acts on H1 (request seeds or resolve); off: no reference to H1, treats the runs as new | success: request_seeds(); end_cycle | success: search_knowledge(); request_seeds(); ask_user() |
| M2 Respect the user's rejection | on: does not re-propose the rejected change; off: proposes it again | success: end_cycle(refused); propose_runs({'n_epochs': 8}); end_cycle | fail: search_knowledge(); open_hypothesis({'learning_rate': 0.003}); request_seeds(); ask_user() |
| M3 Use the approved metric correction | on: ranks the baseline with the corrected 20-episode scores; off: uses the original 3-episode scores | success: request_seeds(); end_cycle | fail: request_seeds(); end_cycle |

This is a diagnostic comparison on three cases, not a statistical claim.
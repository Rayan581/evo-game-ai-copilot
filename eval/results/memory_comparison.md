# Memory on vs off

Planner: **openai_compat**, same inputs and settings per case. Success = the desirable behaviour stated before running.

- Memory on: **2/3**  
- Memory off: **1/3**

| Case | Expected (stated before running) | Memory on | Memory off |
|---|---|---|---|
| M1 Follow up the open hypothesis | on: acts on H1 (request seeds or resolve); off: no reference to H1, treats the runs as new | fail: ask_user() | fail: ask_user() |
| M2 Respect the user's rejection | on: does not re-propose the rejected change; off: proposes it again | success: ask_user() | success: ask_user() |
| M3 Use the approved metric correction | on: ranks the baseline with the corrected 20-episode scores; off: uses the original 3-episode scores | success: ask_user() | fail: ask_user() |

This is a diagnostic comparison on three cases, not a statistical claim.
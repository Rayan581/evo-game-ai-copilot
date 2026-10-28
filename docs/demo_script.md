# EVO live demo (8-10 minutes)

Prep (before the viva): fresh clone, `pip install -e .`, `.env` with the LLM key,
`config.yaml` with `llm.provider: openai_compat`. For a demo that never waits on training,
set `runner.mode: fixture` (approved runs are replayed from real fixture runs and labelled
as replays); otherwise approved runs train live in the background on the laptop.
Delete `data/` and `projects/lander/runs/` to start clean.

| Time | Show | Command | Point to make |
|---|---|---|---|
| 0:00 | Problem | - | RL experiments are noisy and spread over days; EVO keeps the memory and the discipline |
| 0:45 | Onboarding | `evo onboard projects/lander/project.yaml` | Facts extracted and approved once; stored as version 1 |
| 1:30 | Initial action | `evo stage lander cycle1` then `evo cycle lander` | One seed of lr=3e-3 beats the 3-seed baseline by ~137 points. Instead of adopting it, the planner checks the seed-variance note and proposes 3 more seeds (gate-checked). Approve at the prompt |
| 3:00 | Audit | `evo decisions lander`, `evo log lander` | Rationale + evidence IDs per step; memory reads/writes per tool call |
| 3:45 | Restart | close terminal, rename `project.yaml`, new terminal: `evo status lander` | Hypothesis, runs, lessons, pending approvals all reload from `data/evo.db` |
| 4:30 | Later update | `evo stage lander cycle2`, `evo cycle lander` | Starts from H1: seeds disagree (one collapsed), CI spans zero, so it asks for 2 more seeds instead of declaring a winner. The interrupted run is flagged, not scored; the planted log line is flagged and ignored |
| 6:00 | Correction | `evo correct-metric lander lander-base-s0 projects/lander/fixtures/corrections/lander-base-s0.eval20.csv --reason "..."` (repeat s1, s2), `evo correct lander run_budget 12 --reason "..."` | Old versions kept (`evo history lander --run lander-base-s0`), next cycle uses the new ones |
| 7:00 | Failure + retry | `evo approve lander <id> --inject-fault run_experiment:after:1` | Failure logged, retry finds the existing run, 0 duplicates (`evo log lander`) |
| 7:45 | Cycle 3 | `evo cycle lander` | Ranks with the corrected scores; proposes closing H1 as inconclusive (6 seeds, CI spans zero) - the gate would refuse 'supported' |
| 8:30 | Isolation + ablation | `evo history dungeon --run lander-base-s0`; show `eval/results/memory_comparison.md` | Cross-project read refused; memory on vs off on identical inputs |
| 9:15 | Dashboard + register | `evo dashboard lander --open`; open `projects/lander/reports/register.xlsx` | One read-only page over evo.db: curves, groups vs baseline with CIs, each decision with its evidence and gate verdict. Generated views; User columns preserved across exports |

Tip: keep the dashboard open in a browser tab and refresh it (rerun `evo dashboard lander`) after each cycle to
show the audience what changed.

## Browser version (same storyline)

`evo serve`, then Settings (sliders icon) -> Demo checklist: every row above is one button (add runs, run a cycle,
apply the corrections, cut the budget). On **Work**, press **Start** with the LLM selected to show the live planner:
each step appears with its evidence and the gate's verdict, and proposals appear as **Waiting for your OK**. Use
**Test a change** with an out-of-range value (e.g. learning_rate 0.05) to show the gate refusing it. Tick "also try it
without memory" to show the memory effect on identical inputs (a dry run on scratch copies). For a demo that never
waits on training, choose "Replay prepared real runs" under Settings -> Approved runs.

Backup: `eval/results/cli_transcript.txt` is a full recorded run of the same storyline.

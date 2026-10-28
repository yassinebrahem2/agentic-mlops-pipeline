# Stage 5: Retraining, evaluation and promotion

Date: 2026-10-28. Status: draft, not approved.
Contracts are in `docs/roadmap.md`; this spec changes them there first where it says so. Background and decisions: `project_knowledge.md` (entries of 2026-10-27 and 2026-10-28). It builds on the stage 3 spec (handoff, tracking, connections, proposals) and the stage 4 spec (results, incidents, action hooks, rules, the three tiers).

## Goal

When a production model is worse than it should be, the app prepares a better fix and helps a person decide whether to put it in production, the way a careful senior data scientist and MLOps engineer would: it chooses the right fix (often not a retrain), trains on the team's own infrastructure with a defensible data window, judges the candidate honestly against the current model in a harness proven to reproduce production, and hands the person a report card whose numbers are computed by code. Promotion is always a person's decision.

## Where each stage's responsibility ends

- **Stage 4** watches production, raises and diagnoses incidents (Data Quality, Model Performance), and reacts within the tiers: observe, notify, hold, roll back, rerun.
- **Stage 5** owns the fix: when a diagnosing agent concludes that the model itself needs changing, it requests a fix from Retraining (§1). Retraining chooses the fix, trains, evaluates and proposes the promotion; Deployment & Rollback runs the promotion and, after it, stage 4 watches the new version.
- **One evaluation implementation** (§3) serves both the pre-promotion comparison here and stage 4's post-deploy comparison, which calls it.

## Scope

In:
- Fix requests and the fix menu: re-threshold, recalibrate, retrain (observe and roll back stay stage 4's).
- Parameterised hooks, including `ci_dispatch`.
- Training on the team's infrastructure, linked back to the tracker.
- Candidate evaluation (champion against challenger), with a parity check on the harness, and the report card.
- The promotion proposal, its gate in code, re-validation at approval, and promotion through the team's deploy hook.
- Gradual rollout where the team's deploy supports it.
- The Deployments page.
- Updated Retraining and Deployment & Rollback definitions.

Out:
- Building serving or rollout infrastructure for the team.
- Hyperparameter search services, feature stores, model registries other than MLflow (the team's own tools are used through their commands).

## Answers to the roadmap's stage 5 questions

- **Where hooks run.** A hook runs where its work belongs. Light commands (moving an alias, writing a config) run in the sandbox with the hook's own credential. Heavy work (training, evaluating a large model) runs on the team's infrastructure, started through a `ci_dispatch` hook (their CI workflow, with inputs) or their own remote command; the app never trains on its own machine. The report card says where each number was computed.
- **Promotion and rollback gates and thresholds.** The promotion gate (§4) is computed in code from the evaluation, with margins proposed by the agent as rules from the handoff's error costs and approved by the person. Automatic reverting after a deploy is stage 4's pre-approved rollback rule.

## Changes to agreed decisions

These change the roadmap's stage 5 bullets or the approved stage 4 spec, and need the lead's approval with this spec:
1. **Shadow evaluation** (roadmap: "in shadow") becomes a replay: both models score the same logged production inputs, after a parity check proves the harness reproduces production, plus a time-based labeled holdout. A live shadow deployment needs serving infrastructure the app must not assume; where the team's serving supports shadow traffic, its deploy hook can run one.
2. **Gradual rollout (canary)** happens only through the team's deploy hook when it supports it; "reverts automatically on degradation" is stage 4's pre-approved rollback rule.
3. **Training under a training policy.** Stage 4 makes the `train` hook ask at every level. Training does not touch production, so a training policy (§2) approved by the person lets it run without asking within its limits, so a candidate can be ready by morning. Without a policy, or at `suggest`, training asks. Promotion stays a person's decision at every level, `bypass` included. Stage 4's table, test and Done-when change accordingly.
4. **Stage 4's response menu:** its item "retrain or deploy something new" becomes "request a fix from Retraining" (§1).

## 1. Fix requests and the fix menu

- A diagnosing agent (Data Quality, Model Performance, Deployment & Rollback) that concludes the model needs changing calls `RequestFix(incident_id, evidence)` (risk class `read`: it writes the app's own records). The app wakes Retraining in its session with the incident, labeled "fix request". Only Retraining chooses a fix.
- The fix menu, cheapest and safest first; Retraining states why it picked one and why not the others:
  1. **Re-threshold:** the model still ranks well (stable ranking metrics) but the operating threshold no longer matches the error costs or the base rate.
  2. **Recalibrate:** the ranking is fine but the probabilities are off.
  3. **Retrain:** the relationship changed (concept drift, real input drift) or the model is out of date.
  Or decline: send the incident back to observe (stage 4) with the reason.
- Re-threshold and recalibrate produce a candidate too, and go through the same evaluation and promotion (tier 3). They are delivered through the team's commands where the handoff has them (a train hook with a `mode` input, a threshold setting command), else as a PR to the team's decision config or calibration step followed by a train run.
- Guards in code: no fix trained on data under a hold or quarantine (stage 4); none while the incident's cause is unresolved bad data; none on labels younger than the handoff's label maturity; the training policy's limits (§2).

## 2. Hooks and training

**Parameterised hooks** (extends stage 4's action hooks; registered from the handoff's commands, which stage 3's handoff payload carries in this shape):
- `type: "command"`: a command template with named placeholders.
- `type: "ci_dispatch"`: workflow, ref and an inputs map with named placeholders; run through the app's GitHub client (the sandbox never holds a GitHub token).
- Placeholders the app fills: `{request_id}`, `{model}`, `{candidate_version}`, `{champion_version}`, `{window_start}`, `{window_end}`, `{mode}` (`retrain`, `recalibrate`, `threshold`), `{threshold}`. A hook declares which it takes; a missing required value is refused before running.

**Training policy** (`pol`), proposed by Retraining (proposal kind `training_policy`) and approved by the person: the model, the hook, the data window rule (for example "the last N months with mature labels"), the minimum interval between runs, the maximum runs per week, and a run-time cap the team's job enforces. The gate checks the policy's limits in code; within them, the train hook runs without asking at `ask` and above.

**Training.** Each run is a training request (`trq`) with a correlation ID passed as `{request_id}`. The team's training writes it as a tag (`mlops_agents.request_id`) on its MLflow run, together with the training window it actually used (`mlops_agents.window_start`, `mlops_agents.window_end`) and registers the result as a new model version (the candidate). If the team's training cannot do this, Retraining's first audit proposes the change by PR. When a training request starts, the app registers an expectation (stage 4): a tagged run by a deadline; silence past it raises an `operational` signal.

**Reproducibility** is recorded with the candidate: data versions, git commit, parameters, seed, environment (lock file hash), where it ran.

## 3. Evaluation (one implementation)

`evaluation/` in the app, used here and by stage 4's post-deploy comparison and label confirmation. It computes, never asserts:
- **Parity first.** The champion's scores, replayed in the evaluation harness on logged serving inputs (the handoff's `serving_inputs` table, which must hold the model's raw inputs as served), are compared with its logged production predictions. Above a tolerance, the harness is invalid: the replay is skipped and evaluation falls back to the team's `evaluate` hook.
- **Replay:** both models score the same recent serving inputs; disagreements are counted by group and feature range; the decision rate (the share of positive decisions at the threshold) is compared.
- **Labeled holdout:** a time-based window after the candidate's recorded training window (checked against its tags), with labels mature by the handoff's rule.
- **Metrics:** the primary metric and the ones suited to the problem (never accuracy alone) from one metrics module, at the operating threshold and over thresholds; per group; calibration; the expected cost of errors from the handoff's costs; confidence intervals by bootstrap; "not enough data" below the minimum sample.
- **Serving compatibility:** the candidate's input signature against the serving inputs' columns and types; its output format; its size, and its latency where the model is served online; and that the rollback target still accepts today's inputs.
- **Where it runs:** the app downloads both versions' artifacts through the tracker's API (an MLflow server serving its artifacts) and mounts them read-only into an evaluation container; the environment image must load them. When the artifacts are not reachable that way (an object store the app has no credential for), or the image cannot load them, evaluation runs through the team's `evaluate` hook, which writes its results as tagged metrics; the report card says where.

## 4. The report card, the gate and promotion

**The report card** (`evl`, the evaluation record) has two parts:
- **Computed by code** (§3), never typed by the agent: parity, metrics overall and per group with intervals, calibration, cost at the threshold, decision rate, replay disagreements, holdout window against the training window, serving compatibility.
- **Asserted by the agent**, each a required field, a value or "not applicable" with a reason: the fix chosen and why not the others; the data window and exclusions; leakage (features unavailable at prediction time, and how it checked); label maturity, selection bias and feedback loops; class imbalance and its handling; training-serving skew; risks and what to watch after promotion; the rollback plan (the version to return to and the hook).

**The promotion gate**, in code, from the computed part and the approved promotion rule (proposed by the agent from the handoff's error costs, approved by the person): the candidate passes when it is non-inferior on the primary metric within the rule's margin, no listed group is worse past the group margin, the expected cost is not higher, parity holds, and it is serving-compatible. A candidate that fails can still be proposed only with a stated reason (for example a required feature change); the card shows the failure in red.

**Promotion** is a proposal (kind `promotion`, keyed by the model, so a newer candidate for the same model supersedes the older), always asking, `bypass` included. At approval, code re-validates: the champion is still the version the evaluation compared against, and the evaluation is younger than a limit (a setting); otherwise approval is refused and the agent re-evaluates. Approving runs the team's `deploy` hook with `{candidate_version}`; stage 4 then watches the new version and can roll it back under its rules.

## 5. The agents' instructions

The Retraining and Deployment & Rollback definitions tell the agents to reason as a senior data scientist and MLOps engineer: question every number, prefer the least risky fix, look for leakage, distrust random splits and small samples, check groups and calibration, weigh error costs, consider feedback loops and label bias, check that the harness reproduces production before trusting a comparison, and say plainly what they do not know. The report card's computed part and required fields are the enforced part of that instruction.

## 6. UI

- **Deployments page:** which version runs in each environment (from the tracker's aliases; the handoff maps aliases to environments), the history of promotions and rollbacks with who or which rule decided, and each promotion's report card. The tracker's alias history is the source of what runs; promotions link the report cards to it.
- **Promotion card:** the report card, champion against challenger side by side, computed values marked as computed, the gate's result, Approve and Reject.

## 7. Storage

New in `project.db`: fix requests, training requests, training policies, evaluations (report cards), promotions. One contracts plan for stages 4 and 5 writes their schema; stage 3's contracts plan writes stage 3's and the full handoff payload, including the parameterised commands and the alias-to-environment map.

## Changes to the roadmap

- Stage 5 "Agreed" rewritten from this spec, with the four changes above.
- Hook types `command` and `ci_dispatch` with placeholders; training policies; the `deploy` table's `train` row: within an approved training policy, allow at `ask` and above; otherwise ask; promotion (`deploy` hook) asks at every level, `bypass` included.
- Proposal kinds `training_policy`, `promotion`; proposals supersede by kind and key.
- ID prefixes `fix` (fix request), `trq` (training request), `pol` (training policy), `evl` (evaluation), `prm` (promotion).
- Stage 3 handoff: commands carry type, template or workflow and inputs, and placeholders; aliases map to environments; `serving_inputs` holds the raw inputs as served.

## 8. Testing

- Fakes: the fake MLflow server gains run tags, model versions and served artifacts; the fake GitHub server gains workflow dispatch.
- Fix requests: only Retraining chooses; the guards refuse held data, unresolved bad data, immature labels, and runs outside the policy.
- Hooks: placeholders filled; a missing required value refused; `ci_dispatch` carries the inputs.
- Training: the run's tags link it to its request; a missing run past the deadline raises a silence signal; a candidate whose recorded window overlaps the holdout is refused.
- Evaluation: parity failure skips the replay and says so; metrics per group with intervals; "not enough data"; serving-incompatible candidate flagged.
- Gate and promotion: non-inferiority within margins; a failing candidate needs a reason; a changed champion or a stale evaluation refuses approval; approval runs the deploy hook; promotion asks at every level.
- End to end (fake model, fixture project): a confirmed decay incident, Model Performance requests a fix, Retraining picks retrain, a dispatched run linked back, evaluation with parity, the promotion card, approval, the deploy hook, stage 4 watching the new version.

## Done when

- The end-to-end test passes and CI is green on every job.
- No promotion runs without a person's approval, a complete report card and fresh re-validation; no training runs outside an approved policy or an approval.

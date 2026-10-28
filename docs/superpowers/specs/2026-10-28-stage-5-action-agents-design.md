# Stage 5: Training, evaluation and promotion on the team's pipeline

Date: 2026-10-28. Status: draft, not approved.
Contracts are in `docs/roadmap.md`; this spec changes them there first where it says so. Background and decisions: `project_knowledge.md` (entries of 2026-10-27 and 2026-10-28). It builds on the stage 3 spec (handoff, tracking, connections, proposals) and the stage 4 spec (results, incidents, action hooks, rules, the three tiers).

## Goal

There is one system, the team's. The app works on it as a 24/7 senior data scientist and MLOps engineer would: it decides when the model needs a fix and which fix, runs the team's own training pipeline, makes the team's evaluation, promotion and deployment steps as rigorous as they should be, and changes any part of them through PRs whenever a need appears. It never adds a second training, evaluation or deployment path of its own, and it respects the team's engineering choices (full CD, manual approvals) while improving them.

## The principle every agent follows (stages 3 to 5)

- **Change through PRs, continuously.** Any change to the team's system is made through a PR by default: training code, evaluation, the promotion gate, the deployment workflow, checks, monitoring, data pipelines, configuration. Agents open PRs whenever a need appears (after an incident, after an audit, when a better practice applies, when a PR of theirs was rejected and they learned why), not once at onboarding. Stage 2's tools and gates apply (OpenPullRequest, the PR reminder, the `github` risk class).
- **Run, never replace.** Agents run the team's pipelines and commands (action hooks, stage 4) and read their outputs through the tracker and the connections. The app has no training job, evaluation harness or deploy mechanism of its own.
- **Respect the team's choices, then improve them.** With full CD, the team's gate decides promotions; agents make that gate better through PRs. With a manual approval step, the person approves; agents prepare what the person needs.

## Where each stage's responsibility ends

- **Stage 4** watches production, raises and diagnoses incidents, and reacts within the tiers (observe, notify, hold, roll back, rerun). Its diagnosing agents also fix causes through PRs (a broken upstream check, a missing validation).
- **Stage 5** owns the model fix: when a diagnosing agent concludes the model itself needs changing, it requests a fix from Retraining (§1). Retraining chooses the fix and runs the team's pipeline; Deployment & Rollback owns the team's evaluation, promotion and deployment steps and their quality.

## Scope

In:
- Fix requests and the fix menu.
- Running the team's training pipeline with parameters, linked back to the tracker.
- Improving the team's evaluation, promotion gate and deployment workflow through PRs, continuously.
- Promotion as the team defines it: through the team's automated gate (full CD) or a person's approval, with the app's report in both cases.
- The Deployments page.
- Updated Retraining and Deployment & Rollback definitions, and the continuous-improvement principle in all seven definitions.

Out:
- Any training, evaluation or deployment path inside the app.
- Building serving infrastructure for a team that has none; agents may propose it through a PR like any other change.

## Answers to the roadmap's stage 5 questions

- **Where hooks run.** Wherever the team's pipeline runs: the app starts it through the team's command or CI (`ci_dispatch`, §2). Light commands (moving an alias) may run in the sandbox with the hook's own credential. The app never trains on its own machine.
- **Promotion and rollback gates and thresholds.** The promotion gate is the team's, in their pipeline; agents make it rigorous through PRs (non-inferiority on the primary metric, per-group floors, calibration, cost at the threshold, from the handoff's error costs). Reverting after a deploy is stage 4's pre-approved rollback rule.

## Changes to agreed decisions

Approved with this spec:
1. **Shadow evaluation** (roadmap: "in shadow") means the champion and challenger compared on the same recent production inputs inside the team's evaluation step (which agents add or improve through a PR), plus a time-based holdout. A live shadow deployment exists where the team's serving supports it, or once a PR adds it.
2. **Gradual rollout (canary)** is the team's deployment workflow's, added or improved through a PR where it is missing; "reverts automatically on degradation" is stage 4's pre-approved rollback rule.
3. **Running the team's pipeline** is gated by what it does: a run that only trains and registers a candidate is a `train` action, allowed without asking within an approved training policy (§2), else asking; a run whose pipeline deploys at its end (full CD) is a `deploy` action, so a person approves the run unless the team's gate is set as the promotion authority (§4).
4. **Stage 4's response menu:** "retrain or deploy something new" becomes "request a fix from Retraining" (§1).

## 1. Fix requests and the fix menu

- A diagnosing agent that concludes the model needs changing calls `RequestFix(incident_id, evidence)` (risk class `read`). The app wakes Retraining with the incident, labeled "fix request". Only Retraining chooses the fix.
- The fix menu, cheapest and safest first, with why one was chosen over the others:
  1. **Re-threshold:** the model still ranks well but the operating threshold no longer matches the error costs or the base rate (a PR to the team's decision configuration).
  2. **Recalibrate:** ranking fine, probabilities off (a PR adding or updating the team's calibration step, then a pipeline run).
  3. **Retrain:** the relationship changed or the model is out of date (a pipeline run, with a PR first if the training itself needs changing: the window, features, imbalance handling, leakage).
  Or decline, sending the incident back to observe (stage 4) with the reason.
- Guards in code: no run on data under a hold or quarantine; none while the cause is unresolved bad data; none on labels younger than the handoff's label maturity; the training policy's limits.

## 2. Running the team's pipeline

- **Parameterised hooks** (extends stage 4's action hooks; the handoff's commands carry this shape): `type: "command"` (a template with named placeholders) or `type: "ci_dispatch"` (workflow, ref, an inputs map with placeholders), run through the app's GitHub client (the sandbox never holds a GitHub token). Placeholders the app fills: `{request_id}`, `{model}`, `{champion_version}`, `{window_start}`, `{window_end}`, `{mode}`. A hook declares which it takes and whether its pipeline deploys at the end (`deploys: bool`), which decides its risk class (change 3).
- **Training policy** (`pol`), proposed by Retraining (proposal kind `training_policy`), approved by the person: the model, the hook, the data window rule, the minimum interval between runs, the maximum runs per week, and a run-time cap the team's job enforces. The gate checks its limits in code.
- **Linking back.** Each run is a training request (`trq`) with a correlation ID passed as `{request_id}`; the team's pipeline writes it as a tag on its MLflow run, with the training window it used. When the pipeline cannot, Retraining makes it able to through a PR. A started request registers a stage 4 expectation: a tagged run by a deadline, silence past it raising a signal.
- **Reproducibility** comes from the team's run (data versions, commit, parameters, seed, environment); where the pipeline does not record them, agents add it through a PR.

## 3. Evaluation, promotion gate and deployment: the team's, made rigorous

Deployment & Rollback owns the quality of these steps in the team's pipeline and keeps improving them through PRs. The rigorous evaluation agents bring into the team's evaluation step:
- **parity:** the champion's scores in the evaluation environment reproduce its logged production predictions on the same inputs, before any comparison is trusted;
- **champion against challenger** on the same recent serving inputs (disagreements by group and feature range, the decision rate at the threshold);
- **a time-based holdout** after the candidate's training window, with mature labels;
- **metrics suited to the problem** (never accuracy alone), at the threshold and over thresholds, per group, calibration, the expected cost of errors from the handoff, confidence intervals, "not enough data" below the minimum sample;
- **serving compatibility:** input signature, output format, size and latency where online, and that the rollback target still accepts today's inputs.
The step writes its results to the tracker (metrics and a report artifact on the candidate's run). The promotion gate, in the team's pipeline, reads them (non-inferiority within the team's margins, per-group floors, no higher expected cost, parity holding). Agents improve the deployment workflow the same way (gradual rollout, a rollback command if missing, the stage 4 heartbeat job).

The app does not re-implement these: it reads the results from the tracker and shows them.

## 4. Promotion

- The handoff records who promotes: `team_gate` (full CD: the team's automated gate decides) or `person` (a manual approval step). It changes only through a new handoff.
- **`person`:** Retraining proposes the promotion (proposal kind `promotion`, keyed by model, asking at every level, `bypass` included). Its card is the app's report (below). At approval, code re-validates that the champion is still the version the evaluation compared against and that the evaluation is recent (a setting); then the team's deploy hook runs.
- **`team_gate`:** the run's pipeline promotes or not by its own gate; the app's report is still written to the run, the person is notified of the result, and stage 4 watches the new version.
- **The app's report**, on the candidate, from the tracker plus the agent: the computed results the team's evaluation step wrote (shown as such), and the agent's required assessments, each a value or "not applicable" with a reason: the fix chosen and why not the others; data window and exclusions; leakage; label maturity, selection bias and feedback loops; imbalance; training-serving skew; risks and what to watch; the rollback plan. A report missing a required field is refused. Where the team's evaluation lacks a result the report needs, the report says so, and Deployment & Rollback opens a PR adding it.

## 5. The agents' instructions

All seven definitions state the continuous-improvement principle above. Retraining and Deployment & Rollback reason as a senior data scientist and MLOps engineer: question every number, prefer the least risky fix, look for leakage, distrust random splits and small samples, check groups and calibration, weigh error costs, consider feedback loops and label bias, check parity before trusting a comparison, say plainly what they do not know, and fix the system's weaknesses through PRs as they find them.

## 6. UI

- **Deployments page:** which version runs in each environment (from the tracker's aliases; the handoff maps aliases to environments), the history of promotions and rollbacks with who or which gate or rule decided, the report behind each, and the agents' open PRs on the pipeline.
- **Promotion card** (`person` mode): the report, champion against challenger, computed values marked as computed, Approve and Reject.

## 7. Storage

New in `project.db`: fix requests, training requests, training policies, reports, promotions. One contracts plan for stages 4 and 5 writes their schema; stage 3's contracts plan writes the full handoff payload, including the parameterised commands, who promotes, and the alias-to-environment map.

## Changes to the roadmap

- Stage 5 "Agreed" rewritten from this spec, with the changes above; the continuous-improvement principle stated once in the roadmap's agent section.
- Hook types `command` and `ci_dispatch` with placeholders and `deploys`; training policies; the `deploy` table: a `train` run within an approved training policy is allowed at `ask` and above, otherwise asks; a run that deploys asks at every level unless the handoff makes the team's gate the promotion authority; promotion in `person` mode asks at every level, `bypass` included.
- Proposal kinds `training_policy`, `promotion`; proposals supersede by kind and key.
- ID prefixes `fix`, `trq`, `pol`, `rep` (report), `prm`.
- Stage 3 handoff: commands carry type, template or workflow and inputs, placeholders and `deploys`; who promotes; aliases map to environments; `serving_inputs` holds the raw inputs as served.

## 8. Testing

- Fakes: the fake MLflow server gains run tags, model versions and run artifacts; the fake GitHub server gains workflow dispatch.
- Fix requests: only Retraining chooses; the guards refuse held data, unresolved bad data, immature labels, and runs outside the policy.
- Hooks: placeholders filled, a missing required value refused; a hook with `deploys: true` is gated as a deploy.
- Linking: tags link a run to its request; a missing run raises a silence signal.
- Promotion: in `person` mode a report missing a field is refused, a changed champion or stale report refuses approval, approval runs the deploy hook; in `team_gate` mode the app never deploys and reports the gate's outcome.
- End to end (fake model, fixture project): a confirmed decay incident, Model Performance requests a fix, Retraining runs the team's pipeline, the run is linked back, the team's evaluation results are read into the report, promotion per mode, stage 4 watching the new version; and an agent PR improving the team's evaluation step.

## Done when

- The end-to-end test passes and CI is green on every job.
- The app has no training, evaluation or deploy path of its own; every change to the team's system is a PR or a run of the team's own pipeline.
- No deploy happens outside the team's own promotion mechanism and the tiers.

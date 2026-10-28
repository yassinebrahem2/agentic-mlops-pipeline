# Stage 5: Retraining, evaluation and promotion

Date: 2026-10-28. Status: draft, not approved.
Contracts are in `docs/roadmap.md`; this spec changes them there first where it says so. Background and decisions: `project_knowledge.md` (entries of 2026-10-27 and 2026-10-28). It builds on the stage 3 spec (handoff, tracking, connections, proposals) and the stage 4 spec (results, incidents, action hooks, rules, the three tiers).

## Goal

When a production model is worse than it should be, the app prepares a better one and helps a person decide whether to put it in production, the way a careful senior data scientist and MLOps engineer would: it chooses the right fix (often not a retrain), trains on the team's own infrastructure with a defensible data window, judges the candidate honestly against the current model, and hands the person a report card whose every claim is checked. Promotion is always a person's decision.

**Where stage 4 ends and stage 5 begins.** Stage 4 watches production and reacts after a deploy (comparison with the previous version, rollback through rules). Stage 5 owns everything before a new model reaches production: deciding what fix to make, training, evaluating the candidate, proposing its promotion. Stage 4's post-deploy comparison is referenced here, not restated.

## Scope

In:
- The fix menu: retrain, re-threshold, recalibrate, roll back, do nothing (observe).
- Training on the team's infrastructure, linked back to the tracker.
- Candidate evaluation (champion against challenger) and the report card.
- The promotion proposal, its required checklist, and promotion through the team's deploy hook.
- Gradual rollout where the team's deploy supports it.
- The Deployments page.
- Updated Retraining and Deployment & Rollback definitions.

Out:
- Building serving or rollout infrastructure for the team (the app never assumes it).
- Hyperparameter search services, feature stores and model registries other than MLflow (the team's own tools are used through their commands).

## Answers to the roadmap's stage 5 questions

- **Where hooks run.** A hook runs where its work belongs. Light commands (a rollback that moves an alias, a re-threshold that writes a config) run in the sandbox with the hook's own credential. Heavy work (training, evaluating a large model) runs on the team's infrastructure, started through a `ci_dispatch` hook (their CI workflow, with inputs) or their own remote command; the app never trains on its own machine. The report card says where each number was computed.
- **Promotion and rollback gates and thresholds.** Thresholds come from the handoff's error costs and the team's primary metric, are proposed by the agent as rules (stage 4's rule mechanism) and approved by the person, never invented per run. Promotion is a person's decision (tier 3). Automatic reverting after a deploy is stage 4's pre-approved rollback rule.

## Changes to agreed decisions

These change the roadmap's stage 5 bullets or the approved stage 4 spec, and need the lead's approval with this spec:
1. **Shadow evaluation** (roadmap: "evaluates the new model against the current one in shadow") becomes a replay: both models score the same logged production inputs in the sandbox, plus a time-based labeled holdout. A live shadow deployment needs serving infrastructure the app must not assume. Where the team's serving supports shadow traffic, its deploy hook can run one.
2. **Gradual rollout (canary)** happens only through the team's deploy hook when it supports it; "reverts automatically on degradation" is stage 4's pre-approved rollback rule.
3. **Training under a rule.** Stage 4's table makes the `train` hook ask at every level. Training does not touch production, so it moves to the rule-governed group: under an approved rule (how often, the data window, a cost or compute cap) it runs without asking, so a candidate can be ready by morning. Promotion (the `deploy` hook) stays a person's decision at every level.

## 1. The fix menu

Retraining considers, cheapest and safest first, and states why it picked one:
1. **Do nothing and observe**, with a deadline (stage 4's follow-up), when the evidence is weak, the samples are small, or the cause is bad data that is being fixed.
2. **Roll back** to the last good version (stage 4's hook and rules) when the current version is the problem and an earlier one was better.
3. **Re-threshold:** the model still ranks well but the operating threshold no longer matches the error costs or the base rate.
4. **Recalibrate:** the ranking is fine but the probabilities are off (calibration drift).
5. **Retrain** when the relationship changed (concept drift, real input drift) or the model is out of date.

Guards in code:
- no retraining on data under a hold or quarantine (stage 4);
- no retraining while the incident's cause is unresolved bad data;
- a minimum interval between retrains of the same model (a rule limit);
- no retraining on labels younger than the handoff's label maturity.

## 2. Training

- **The data window** is part of the proposal or rule: the period, why (recent enough to reflect the world, long enough for the rarer class and seasonality), what is excluded (held batches, immature labels) and the data versions it reads.
- **Hooks.** The handoff's commands give the `train` and `evaluate` hooks; a new hook type, `ci_dispatch` (workflow, ref, inputs), starts the team's CI workflow through the app's GitHub client. The sandbox never holds a GitHub token.
- **Linking back.** Every train hook run passes a correlation ID (input or environment variable) that the team's training writes as a tag on its MLflow run (`mlops_agents.request_id`); if the team's training cannot take it, the agent's first audit proposes the one-line change by PR. Tracking sync (stage 3) then knows which candidate answers which request. Training takes hours: the agent sets a follow-up (stage 4) on the tag appearing, with a deadline after which it reports the run as missing.
- **Reproducibility** is recorded with the candidate: data versions, git commit, parameters, seed, environment (lock file hash), where it ran.

## 3. Evaluating the candidate

The Deployment & Rollback agent evaluates the candidate (the challenger) against the production version (the champion):
- **Same inputs:** both score the same recent logged production inputs (the handoff's `serving_inputs` table). Disagreements are counted and characterized (which groups, which feature ranges). Without logged serving inputs, the report card says so and the replay is skipped.
- **Labeled holdout:** a time-based window after the training window, never random, with labels mature by the handoff's rule. Both models are scored on it.
- **Metrics:** the primary metric and the ones suited to the problem (stage 4's definitions, one implementation shared with the checks; never accuracy alone), at the operating threshold and over thresholds; per group (the handoff's groups); calibration; the expected cost of errors at the threshold from the handoff's costs; uncertainty (confidence intervals or bootstrap), and "not enough data" below the minimum sample.
- **Threshold:** whether the candidate needs a different operating threshold, and the proposed one.
- **Models in the sandbox:** the app downloads both versions' artifacts through the tracker API and mounts them read-only into the evaluation container; the environment image must load them. Where it cannot (a large model, special hardware, a missing runtime), evaluation runs through the team's `evaluate` hook, and the report card says where.

## 4. The report card and the promotion proposal

Promotion is a proposal (stage 3's mechanism, new kind `promotion`), always asking, `bypass` included. Its payload is the report card, and every item below is a required field: a value, or "not applicable" with a reason. A missing item is a validation error, so the agent cannot skip one.
- the incident or reason, and the fix chosen over the others, with why;
- training: data window and exclusions, data versions, commit, parameters, seed, environment, where it ran;
- leakage: features unavailable at prediction time, and how the agent checked;
- splits: time-based, and the holdout's period;
- label maturity and label selection bias or feedback loops;
- class imbalance and how it was handled;
- metrics champion against challenger, overall and per group, with uncertainty;
- calibration;
- the operating threshold and the expected cost of errors at it;
- training-serving skew between the training data and recent serving inputs;
- the replay's disagreements;
- risks and what to watch after promotion (the stage 4 rules that will guard it);
- the rollback plan: the version to return to and the hook that does it.

Approving runs the team's `deploy` hook with the candidate; stage 4 then watches it and can roll it back under its rules.

## 5. The agents' instructions

The Retraining and Deployment & Rollback definitions tell the agents to reason as a senior data scientist and MLOps engineer: question every number, prefer the least risky fix, look for leakage, distrust random splits and small samples, check groups and calibration, weigh error costs, consider feedback loops and label bias, and say plainly what they do not know. The report card's required fields are the enforced part of that instruction.

## 6. UI

- **Deployments page:** which version runs where (from the tracker's aliases), the history of promotions and rollbacks with who or which rule decided, and each promotion's report card.
- **Promotion card:** the report card rendered readably, champion against challenger side by side, with Approve and Reject.

## 7. Storage

New in `project.db`: training requests (correlation ID, hook, window, status, the run and version they produced), candidate evaluations (the report card, where computed), promotions. The stage 3 to 5 contracts plan writes the schema.

## Changes to the roadmap

- Stage 5 "Agreed" rewritten from this spec, including the three changes above.
- Hook types: `command` (sandbox) and `ci_dispatch` (the team's CI); risk classes as stage 4's table, with `train` moved to the rule-governed group.
- Proposal kind `promotion`.
- ID prefixes `trq` (training request), `evl` (candidate evaluation).

## 8. Testing

- Fakes: the fake MLflow server (stage 3) gains run tags and model artifacts; the fake GitHub server gains workflow dispatch.
- The fix menu's guards: no retrain on held data, on unresolved bad data, within the minimum interval, on immature labels.
- Training: a dispatch carries the correlation ID; sync links the tagged run to its request; a missing run past the deadline is reported.
- Evaluation: time-based holdout only; metrics from the shared implementation; per group; "not enough data" below the minimum; the replay skipped and stated without serving inputs.
- The promotion proposal: a missing report card field is refused; approval runs the deploy hook; it asks at every level.
- End to end (fake model, fixture project): a confirmed decay incident, Retraining picks retrain, a dispatched training run linked back, an evaluation, a promotion card, approval, the deploy hook, stage 4 watching the new version.

## Done when

- The end-to-end test passes and CI is green on every job.
- No promotion runs without a person's approval and a complete report card.

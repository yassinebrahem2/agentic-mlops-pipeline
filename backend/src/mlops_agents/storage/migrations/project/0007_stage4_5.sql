-- Stage 4-5: Results, expectations, scheduler jobs, checks, signals, incidents,
-- follow-ups, recent changes, agent wakes, maintenance windows, on-call people,
-- notification log, monitoring settings, hooks, rules, verdicts, baseline links,
-- fixes, training requests and policies, reports, and promotions.

CREATE TABLE results (
  id TEXT PRIMARY KEY NOT NULL,
  check_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN ('team', 'agent', 'tracker')),
  ran_at TEXT NOT NULL,
  fired INTEGER NOT NULL CHECK (fired IN (0, 1)),
  value REAL,
  threshold REAL,
  category TEXT NOT NULL CHECK (category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  subject TEXT NOT NULL,
  model TEXT,
  detail TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  version TEXT,
  not_enough_data INTEGER NOT NULL DEFAULT 0 CHECK (not_enough_data IN (0, 1)),
  source_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  delivered_at TEXT,
  UNIQUE (source_id, source_key)
);
CREATE INDEX results_check_model_ran ON results (check_id, model, ran_at);
CREATE INDEX results_subject_model_ran ON results (subject, model, ran_at);

CREATE TABLE result_sources (
  source_id TEXT PRIMARY KEY NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file', 'ci', 'mlflow')),
  cursor TEXT NOT NULL,
  last_read TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE parsers (
  source_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  spec TEXT NOT NULL,
  proposal_id TEXT NOT NULL UNIQUE,
  approved TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source_id, version)
);

CREATE TABLE expectations (
  kind TEXT NOT NULL CHECK (kind IN ('feed', 'job', 'check', 'training_request')),
  ref TEXT NOT NULL,
  model TEXT,
  spec TEXT NOT NULL,
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  last_seen TEXT,
  last_signalled_first TEXT,
  last_signalled_second TEXT
);
CREATE UNIQUE INDEX expectations_key ON expectations (kind, ref, coalesce(model, ''));

CREATE TABLE scheduler_jobs (
  kind TEXT NOT NULL CHECK (kind IN (
    'sync_tracking', 'sync_prs', 'collect_results', 'after_sync', 'check_expectations',
    'follow_ups', 'retry_notifications', 'escalate', 'morning_report'
  )),
  due TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  started TEXT,
  finished TEXT,
  error TEXT,
  UNIQUE (kind, due, subject)
);

CREATE TABLE checks (
  id TEXT PRIMARY KEY NOT NULL,
  check_id TEXT NOT NULL UNIQUE,
  category TEXT NOT NULL CHECK (category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  cadence TEXT NOT NULL,
  timezone TEXT NOT NULL,
  results_location TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  agent TEXT NOT NULL,
  version TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending_pr', 'active', 'retired')),
  registered TEXT NOT NULL
);

CREATE TABLE signals (
  id TEXT PRIMARY KEY NOT NULL,
  incident_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('fired_result', 'silence', 'parser_failure', 'source_failure', 'notification_failure')),
  category TEXT NOT NULL CHECK (category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  subject TEXT NOT NULL,
  model TEXT,
  ran_at TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  cause_key TEXT NOT NULL,
  detail TEXT NOT NULL,
  result_id TEXT,
  source_ref TEXT,
  expectation TEXT,
  cleared INTEGER NOT NULL DEFAULT 0 CHECK (cleared IN (0, 1))
);
CREATE UNIQUE INDEX signals_result_id ON signals (result_id)
  WHERE result_id IS NOT NULL;

CREATE TABLE incidents (
  id TEXT PRIMARY KEY NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  subtype TEXT,
  severity TEXT NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
  status TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'mitigated', 'resolved', 'closed', 'reopened', 'merged')),
  cause TEXT,
  cause_key TEXT NOT NULL,
  model TEXT,
  prs TEXT NOT NULL DEFAULT '[]',
  issues TEXT NOT NULL DEFAULT '[]',
  held INTEGER NOT NULL DEFAULT 0 CHECK (held IN (0, 1)),
  merged_into TEXT,
  started_at TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  acknowledged_at TEXT,
  mitigated_at TEXT,
  resolved_at TEXT,
  closed_at TEXT,
  snoozed_until TEXT,
  resolved_offline INTEGER NOT NULL DEFAULT 0 CHECK (resolved_offline IN (0, 1)),
  reopened_count INTEGER NOT NULL DEFAULT 0,
  hold_scope TEXT,
  hold_reason TEXT
);

CREATE TABLE incident_follow_ups (
  id TEXT PRIMARY KEY NOT NULL,
  incident TEXT NOT NULL,
  agent TEXT NOT NULL,
  due TEXT,
  after_days INTEGER,
  after_labels INTEGER,
  label_base_count INTEGER,
  note TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1))
);

CREATE TABLE recent_changes (
  kind TEXT NOT NULL CHECK (kind IN ('agent_pr', 'ci_run', 'deployment')),
  ref TEXT NOT NULL,
  at TEXT NOT NULL,
  detail TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);

CREATE TABLE agent_wakes (
  agent TEXT NOT NULL,
  label TEXT NOT NULL CHECK (label IN ('incident', 'fix_request', 'post_deploy', 'follow_up')),
  subject TEXT NOT NULL,
  key TEXT NOT NULL,
  text TEXT NOT NULL,
  created TEXT NOT NULL,
  posted_at TEXT,
  PRIMARY KEY (agent, label, subject, key)
);

CREATE TABLE maintenance_windows (
  id TEXT PRIMARY KEY NOT NULL,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  scope TEXT NOT NULL,
  reason TEXT NOT NULL,
  proposal_id TEXT,
  created_by TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE UNIQUE INDEX maintenance_windows_proposal_id ON maintenance_windows (proposal_id)
  WHERE proposal_id IS NOT NULL;

CREATE TABLE on_call_people (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  contacts TEXT NOT NULL,
  hours TEXT NOT NULL,
  timezone TEXT NOT NULL,
  escalation_order INTEGER NOT NULL
);

CREATE TABLE notification_log (
  id TEXT PRIMARY KEY NOT NULL,
  incident TEXT,
  person TEXT NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('incident', 'escalation', 'report', 'promotion')),
  escalation_step INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('sent', 'retrying', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt TEXT,
  last_error TEXT,
  message TEXT NOT NULL,
  created TEXT NOT NULL,
  sent TEXT
);

CREATE TABLE monitoring_settings (
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
);

CREATE TABLE hooks (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  model TEXT,
  spec TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'disabled')),
  suspended_reason TEXT,
  approved_commit TEXT,
  deploys_verified INTEGER CHECK (deploys_verified IS NULL OR deploys_verified IN (0, 1)),
  source TEXT NOT NULL CHECK (source IN ('handoff', 'proposal')),
  created TEXT NOT NULL
);
CREATE UNIQUE INDEX hooks_model_name ON hooks (coalesce(model, ''), name);

CREATE TABLE hook_pins (
  hook TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  [commit] TEXT NOT NULL,
  PRIMARY KEY (hook, path)
);

CREATE TABLE hook_runs (
  id TEXT PRIMARY KEY NOT NULL,
  hook_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  incident_id TEXT,
  fix_id TEXT,
  rule_id TEXT,
  policy_id TEXT,
  reason TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('rule', 'policy', 'approval', 'none')),
  basis_ref TEXT,
  evidence TEXT NOT NULL DEFAULT '[]',
  ok INTEGER CHECK (ok IS NULL OR ok IN (0, 1)),
  detail TEXT,
  request_id TEXT,
  started TEXT NOT NULL,
  ended TEXT
);

CREATE TABLE rules (
  id TEXT PRIMARY KEY NOT NULL,
  spec TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  proposal_id TEXT NOT NULL UNIQUE,
  approved TEXT NOT NULL
);

CREATE TABLE verdicts (
  incident_id TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL CHECK (value IN ('real_issue', 'not_an_issue', 'wrong_type')),
  right_category TEXT CHECK (right_category IS NULL OR right_category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  right_subtype TEXT,
  source TEXT NOT NULL CHECK (source IN ('user', 'labels')),
  check_versions TEXT NOT NULL DEFAULT '{}',
  note TEXT,
  created TEXT NOT NULL
);

CREATE TABLE silent_alert_verdicts (
  result_id TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL CHECK (value IN ('real_issue', 'not_an_issue')),
  by TEXT NOT NULL,
  created TEXT NOT NULL
);

CREATE TABLE misses (
  id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
  description TEXT NOT NULL,
  category TEXT CHECK (category IS NULL OR category IN ('data_quality', 'drift', 'model_performance', 'operational')),
  occurred_at TEXT NOT NULL,
  reported_at TEXT NOT NULL
);

CREATE TABLE baseline_links (
  incident_id TEXT NOT NULL,
  team_check_id TEXT NOT NULL,
  first_fired_at TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  lead_seconds REAL NOT NULL,
  PRIMARY KEY (incident_id, team_check_id)
);

CREATE TABLE fix_requests (
  id TEXT PRIMARY KEY NOT NULL,
  incident_id TEXT NOT NULL,
  model TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  evidence TEXT NOT NULL,
  choice TEXT CHECK (choice IS NULL OR choice IN ('re_threshold', 'recalibrate', 'retrain', 'decline')),
  why TEXT,
  why_not TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('requested', 'chosen', 'running', 'done', 'declined', 'escalated')),
  outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'cleared', 'not_cleared')),
  created TEXT NOT NULL
);

CREATE TABLE training_requests (
  id TEXT PRIMARY KEY NOT NULL,
  fix_id TEXT,
  model TEXT NOT NULL,
  hook_id TEXT NOT NULL,
  policy_id TEXT,
  window TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('train', 'evaluate')),
  status TEXT NOT NULL CHECK (status IN ('started', 'linked', 'failed', 'silent')),
  run_key TEXT,
  ci_run_url TEXT,
  started TEXT NOT NULL
);

CREATE TABLE training_policies (
  id TEXT PRIMARY KEY NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'disabled')),
  suspended_reason TEXT,
  proposal_id TEXT NOT NULL UNIQUE,
  approved TEXT NOT NULL
);

CREATE TABLE reports (
  id TEXT PRIMARY KEY NOT NULL,
  model TEXT NOT NULL,
  candidate_version TEXT NOT NULL,
  fix_id TEXT,
  training_request_id TEXT,
  assessments TEXT NOT NULL,
  author TEXT NOT NULL,
  created TEXT NOT NULL
);

CREATE TABLE promotions (
  id TEXT PRIMARY KEY NOT NULL,
  model TEXT NOT NULL,
  candidate_version TEXT NOT NULL,
  champion_version TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('team_gate', 'person')),
  decided_by TEXT NOT NULL CHECK (decided_by IN ('person', 'agent_bypass', 'team_gate')),
  status TEXT NOT NULL CHECK (status IN ('proposed', 'applied', 'refused', 'rejected')),
  report_id TEXT,
  refusals TEXT NOT NULL DEFAULT '[]',
  created TEXT NOT NULL,
  applied_at TEXT,
  proposal_id TEXT
);
CREATE UNIQUE INDEX promotions_proposal_id ON promotions (proposal_id)
  WHERE proposal_id IS NOT NULL;
CREATE UNIQUE INDEX promotions_team_gate ON promotions (model, candidate_version)
  WHERE mode = 'team_gate';

CREATE INDEX events_subject_seq ON events (subject_id, seq);

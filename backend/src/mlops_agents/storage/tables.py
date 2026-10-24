"""SQLAlchemy Core views of the tables created by migrations/. Kept in sync by test_tables.py."""

from sqlalchemy import Column, Integer, MetaData, Table, Text

app_metadata = MetaData()
project_metadata = MetaData()

projects = Table(
    "projects",
    app_metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("local_path", Text),
    Column("local_path_key", Text),
    Column("github_full_name", Text),
    Column("github_key", Text),
    Column("default_branch", Text),
    Column("status", Text, nullable=False),
    Column("status_detail", Text),
    Column("created_at", Text, nullable=False),
    Column("last_opened_at", Text),
)

providers = Table(
    "providers",
    app_metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("base_url", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_detail", Text),
    Column("created_at", Text, nullable=False),
)

provider_keys = Table(
    "provider_keys",
    app_metadata,
    Column("provider_id", Text, primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("keystore_ref", Text, nullable=False),
    Column("hint", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

models = Table(
    "models",
    app_metadata,
    Column("address", Text, primary_key=True),
    Column("provider_id", Text, nullable=False),
    Column("wire_model_id", Text, nullable=False),
    Column("context_window", Integer),
    Column("tool_calling", Integer, nullable=False),
    Column("streaming", Integer, nullable=False),
    Column("availability", Text, nullable=False),
    Column("availability_detail", Text),
    Column("verified_at", Text),
)

agent_model_defaults = Table(
    "agent_model_defaults",
    app_metadata,
    Column("agent", Text, primary_key=True),
    Column("model_address", Text, nullable=False),
)

integrations = Table(
    "integrations",
    app_metadata,
    Column("name", Text, primary_key=True),
    Column("mode", Text, nullable=False),
    Column("login", Text),
    Column("verified_at", Text),
)

app_settings = Table(
    "settings",
    app_metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

sessions = Table(
    "sessions",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("agent", Text, nullable=False),
    Column("summary", Text),
    Column("compacted_through", Text),
    Column("created_at", Text, nullable=False),
)

turns = Table(
    "turns",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("trigger_message_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("created_at", Text, nullable=False),
    Column("started_at", Text),
    Column("ended_at", Text),
)

messages = Table(
    "messages",
    project_metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("turn_id", Text),
    Column("author_kind", Text, nullable=False),
    Column("author_name", Text, nullable=False),
    Column("from_agent", Text),
    Column("parts", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("exchange_id", Text),
    Column("hop", Integer),
    Column("read_by_turn", Text),
)

llm_calls = Table(
    "llm_calls",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("turn_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("model_address", Text, nullable=False),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("latency_ms", Integer, nullable=False),
    Column("created_at", Text, nullable=False),
)

project_settings = Table(
    "project_settings",
    project_metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

tasks = Table(
    "tasks",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("worktree", Text, nullable=False),
    Column("base_ref", Text, nullable=False),
    Column("base_sha", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("review_kind", Text),
    Column("pr_number", Integer),
    Column("pr_url", Text),
    Column("created_at", Text, nullable=False),
    Column("ended_at", Text),
)

task_reads = Table(
    "task_reads",
    project_metadata,
    Column("task_id", Text, primary_key=True),
    Column("path", Text, primary_key=True),
    Column("sha256", Text, nullable=False),
)

file_claims = Table(
    "file_claims",
    project_metadata,
    Column("task_id", Text, primary_key=True),
    Column("path", Text, primary_key=True),
)

approvals = Table(
    "approvals",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("turn_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("message_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("tool_call_id", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("risk_class", Text, nullable=False),
    Column("preview", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("note", Text),
    Column("created_at", Text, nullable=False),
    Column("decided_at", Text),
)

events = Table(
    "events",
    project_metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("id", Text, nullable=False),
    Column("ts", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("subject_id", Text),
    Column("payload", Text, nullable=False),
)

audit = Table(
    "audit",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("ts", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("risk_class", Text, nullable=False),
    Column("permission_level", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("detail", Text, nullable=False),
)

exchanges = Table(
    "exchanges",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("created_at", Text, nullable=False),
)

wakeups = Table(
    "wakeups",
    project_metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("due_at", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("fired_at", Text),
)

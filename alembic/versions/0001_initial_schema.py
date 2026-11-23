"""Initial schema (DESIGN.md section 4.1).

The Text-to-SQL views and read-only role (DESIGN.md 4.2) come in a later migration (phase 4).

Revision ID: 0001
Revises:
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NIL_UUID = "'00000000-0000-0000-0000-000000000000'"

UPGRADE_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE teams (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key_prefix           text NOT NULL,
  key_hash             text NOT NULL UNIQUE,
  owner_name           text NOT NULL,
  role                 text NOT NULL CHECK (role IN ('admin','user')),
  team_id              uuid REFERENCES teams(id),
  daily_request_limit  int,
  daily_token_limit    int,
  is_active            boolean NOT NULL DEFAULT true,
  created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE files (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL,
  source_type      text NOT NULL CHECK (source_type IN ('upload','url')),
  source_url       text,
  mime_type        text NOT NULL,
  size_bytes       bigint,
  sha256           text,
  raw_path         text,
  md_path          text,
  scope            text NOT NULL DEFAULT 'global' CHECK (scope IN ('global','team')),
  team_id          uuid REFERENCES teams(id),
  status           text NOT NULL CHECK (status IN (
                     'awaiting_upload','queued','converting','pending_review',
                     'approved','rejected','failed')),
  error_code       text,
  error_message    text,
  attempts         int  NOT NULL DEFAULT 0,
  locked_at        timestamptz,
  convert_method   text,
  category         text,
  tags             text[] NOT NULL DEFAULT '{{}}',
  tags_source      text,
  description      text,
  author           text,
  summary          text,
  uploaded_by      uuid REFERENCES api_keys(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  approved_at      timestamptz,
  CHECK (scope = 'global' OR team_id IS NOT NULL)
);
CREATE UNIQUE INDEX files_uniq_hash ON files (sha256, scope, COALESCE(team_id, {NIL_UUID}))
  WHERE status <> 'rejected' AND sha256 IS NOT NULL;
CREATE UNIQUE INDEX files_uniq_name ON files (name, scope, COALESCE(team_id, {NIL_UUID}))
  WHERE status <> 'rejected';
CREATE INDEX files_status_idx ON files (status);

CREATE TABLE chunks (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  file_id          uuid NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  seq              int  NOT NULL,
  content          text NOT NULL,
  search_text      text NOT NULL,
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
  embedding        vector(768),
  embedding_model  text NOT NULL,
  location         jsonb,
  is_repaired      boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX chunks_emb_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_file_idx ON chunks (file_id);

CREATE TABLE md_revisions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  file_id            uuid NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  reason             text NOT NULL CHECK (reason IN ('convert','manual_edit','reprocess','auto_repair')),
  diff               text,
  trigger_answer_id  uuid,
  created_by         uuid REFERENCES api_keys(id),
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  api_key_id  uuid NOT NULL REFERENCES api_keys(id),
  summary     text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answers (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id   uuid REFERENCES conversations(id) ON DELETE CASCADE,
  api_key_id        uuid NOT NULL REFERENCES api_keys(id),
  question          text NOT NULL,
  intent            text,
  mode              text NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto','ai_only','library_only')),
  answer            text,
  source_stage      text CHECK (source_stage IN ('cache','md','source_file','ai','sql')),
  is_ai_generated   boolean NOT NULL DEFAULT false,
  citations         jsonb NOT NULL DEFAULT '[]',
  chunk_ids         uuid[] NOT NULL DEFAULT '{{}}',
  retrieval_score   real,
  llm_confidence    real,
  stages_tried      text[] NOT NULL DEFAULT '{{}}',
  model             text,
  input_tokens      int NOT NULL DEFAULT 0,
  output_tokens     int NOT NULL DEFAULT 0,
  estimated_cost    numeric(12,6) NOT NULL DEFAULT 0,
  latency_ms        int,
  quota_exceeded    boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX answers_key_time_idx ON answers (api_key_id, created_at DESC);
CREATE INDEX answers_created_idx ON answers (created_at);

CREATE TABLE feedback (
  answer_id   uuid PRIMARY KEY REFERENCES answers(id) ON DELETE CASCADE,
  rating      text NOT NULL CHECK (rating IN ('up','down')),
  comment     text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answer_cache (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_key            text NOT NULL,
  normalized_question  text NOT NULL,
  question_embedding   vector(768),
  answer_id            uuid NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
  file_ids             uuid[] NOT NULL DEFAULT '{{}}',
  created_at           timestamptz NOT NULL DEFAULT now(),
  expires_at           timestamptz NOT NULL
);
CREATE UNIQUE INDEX answer_cache_exact_idx ON answer_cache (scope_key, normalized_question);
CREATE INDEX answer_cache_emb_idx ON answer_cache USING hnsw (question_embedding vector_cosine_ops);
CREATE INDEX answer_cache_files_idx ON answer_cache USING gin (file_ids);

CREATE TABLE usage_daily (
  api_key_id  uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  day         date NOT NULL,
  requests    int  NOT NULL DEFAULT 0,
  tokens      int  NOT NULL DEFAULT 0,
  PRIMARY KEY (api_key_id, day)
);

CREATE TABLE sql_cache (
  question_hash  text PRIMARY KEY,
  generated_sql  text NOT NULL,
  result         jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- The app connects with the service role; block Supabase's public REST API (anon/authenticated)
-- from reading these tables directly.
ALTER TABLE teams         ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys      ENABLE ROW LEVEL SECURITY;
ALTER TABLE files         ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks        ENABLE ROW LEVEL SECURITY;
ALTER TABLE md_revisions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE answers       ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback      ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_cache  ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_daily   ENABLE ROW LEVEL SECURITY;
ALTER TABLE sql_cache     ENABLE ROW LEVEL SECURITY;
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS sql_cache, usage_daily, answer_cache, feedback, answers, conversations,
  md_revisions, chunks, files, api_keys, teams CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)

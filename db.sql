-- =====================================================================
-- PSI Scientific Model-Orchestrated Intelligence System
-- PostgreSQL 15+ schema (requires pgvector)
-- Mirrors the UML class diagram: evidence, ingestion, computation,
-- orchestration and claim verification.
-- Usage:  createdb psi && psql -d psi -f psi_schema.sql
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy text search

CREATE SCHEMA IF NOT EXISTS psi;
SET search_path TO psi, public;

-- ---------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------
CREATE TYPE validation_status AS ENUM ('validated', 'needs_validation', 'quarantined', 'rejected');
CREATE TYPE fidelity_level    AS ENUM ('direct_equation', 'empirical_correlation', 'surrogate', 'numerical_solver');
CREATE TYPE gate_decision     AS ENUM ('allow', 'deny', 'abstain');
CREATE TYPE claim_basis       AS ENUM ('experiment', 'validated_calculation', 'labeled_hypothesis');
CREATE TYPE job_status        AS ENUM ('queued', 'running', 'succeeded', 'failed', 'needs_review');
CREATE TYPE computability_class AS ENUM ('directly_measurable', 'derivable', 'model_dependent', 'missing', 'unsupported');
CREATE TYPE review_decision   AS ENUM ('pending', 'approved', 'corrected', 'rejected');
CREATE TYPE artifact_kind     AS ENUM ('tabular', 'pdf', 'image', 'video', 'sensor_series', 'binary', 'other');

-- Shared updated_at trigger
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------
-- 1. Source artifacts (raw material is preserved, never replaced)
-- ---------------------------------------------------------------------
CREATE TABLE source_artifact (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  uri             text        NOT NULL,                 -- object-storage location
  original_name   text,
  kind            artifact_kind NOT NULL,
  media_type      text,
  size_bytes      bigint CHECK (size_bytes >= 0),
  sha256          char(64)    NOT NULL,
  license         text,
  source_url      text,
  version_label   text,
  acquired_at     timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sha256, uri)
);
CREATE INDEX idx_artifact_kind ON source_artifact (kind);

-- ---------------------------------------------------------------------
-- 2. Experiment hierarchy
--    Investigation -> Experiment -> Test Run -> Measurement Series
-- ---------------------------------------------------------------------
CREATE TABLE investigation (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id   text UNIQUE,                            -- e.g. NASA PSI identifier
  title         text NOT NULL,
  family        text,                                   -- e.g. FLEX, BASS, SAME
  objectives    text,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_investigation_touch BEFORE UPDATE ON investigation
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX idx_investigation_title_trgm ON investigation USING gin (title gin_trgm_ops);

CREATE TABLE experiment (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  investigation_id uuid NOT NULL REFERENCES investigation(id) ON DELETE CASCADE,
  name             text NOT NULL,
  protocol         text,
  apparatus        text,
  metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (investigation_id, name)
);
CREATE TRIGGER trg_experiment_touch BEFORE UPDATE ON experiment
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE test_run (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id  uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
  run_label      text NOT NULL,                         -- run identifier from the source
  started_at     timestamptz,
  notes          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_id, run_label)
);

-- Per-run conditions: each run keeps its own context (no leakage across runs)
CREATE TABLE run_condition (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_run_id     uuid NOT NULL REFERENCES test_run(id) ON DELETE CASCADE,
  variable_id     uuid,                                 -- FK added after scientific_variable exists
  name            text NOT NULL,                        -- e.g. oxygen_mole_fraction
  value_num       double precision,
  value_text      text,
  unit            text,                                 -- Pint-compatible unit string
  CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)
);
CREATE INDEX idx_run_condition_run  ON run_condition (test_run_id);
CREATE INDEX idx_run_condition_name ON run_condition (name, value_num);

CREATE TABLE experiment_source (
  experiment_id uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
  artifact_id   uuid NOT NULL REFERENCES source_artifact(id) ON DELETE RESTRICT,
  test_run_id   uuid REFERENCES test_run(id) ON DELETE CASCADE,   -- NULL = applies to whole experiment
  role          text,
  PRIMARY KEY (experiment_id, artifact_id)
);

-- ---------------------------------------------------------------------
-- 3. Provenance and scientific semantic representation
-- ---------------------------------------------------------------------
CREATE TABLE provenance_trace (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id      uuid NOT NULL REFERENCES source_artifact(id) ON DELETE RESTRICT,
  sheet            text,
  cell_ref         text,                                -- e.g. B14 or row/col
  page_number      int,
  parsing_rule     text,
  unit_conversion  text,
  steps            jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ordered transformation log
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_prov_artifact ON provenance_trace (artifact_id);

CREATE TABLE scientific_variable (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_run_id    uuid REFERENCES test_run(id) ON DELETE CASCADE,
  name           text NOT NULL,                         -- standardized, e.g. oxygen_mole_fraction
  raw_label      text,                                  -- original column header, e.g. "O2 (%)"
  value_num      double precision,
  value_text     text,
  unit           text,
  basis          text,                                  -- e.g. mole fraction, volume fraction
  basis_verified boolean NOT NULL DEFAULT false,
  uncertainty    double precision CHECK (uncertainty IS NULL OR uncertainty >= 0),
  status         validation_status NOT NULL DEFAULT 'needs_validation',
  provenance_id  uuid REFERENCES provenance_trace(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_scivar_touch BEFORE UPDATE ON scientific_variable
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE INDEX idx_scivar_run    ON scientific_variable (test_run_id, name);
CREATE INDEX idx_scivar_status ON scientific_variable (status);

ALTER TABLE run_condition
  ADD CONSTRAINT fk_run_condition_variable
  FOREIGN KEY (variable_id) REFERENCES scientific_variable(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------
-- 4. Measurement series and observations
-- ---------------------------------------------------------------------
CREATE TABLE measurement_series (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_run_id      uuid NOT NULL REFERENCES test_run(id) ON DELETE CASCADE,
  variable_name    text NOT NULL,
  axis_name        text NOT NULL DEFAULT 'time',        -- time or space
  axis_unit        text,
  value_unit       text,
  sampling_rate_hz double precision CHECK (sampling_rate_hz IS NULL OR sampling_rate_hz > 0),
  instrument       text,
  calibration      jsonb NOT NULL DEFAULT '{}'::jsonb,
  status           validation_status NOT NULL DEFAULT 'needs_validation',
  provenance_id    uuid REFERENCES provenance_trace(id),
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_series_run ON measurement_series (test_run_id, variable_name);

CREATE TABLE measurement_point (
  series_id  uuid NOT NULL REFERENCES measurement_series(id) ON DELETE CASCADE,
  seq        bigint NOT NULL,
  axis_value double precision NOT NULL,
  value      double precision,
  PRIMARY KEY (series_id, seq)
);

-- Derived or directly observed physical property
CREATE TABLE observation (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_run_id    uuid NOT NULL REFERENCES test_run(id) ON DELETE CASCADE,
  series_id      uuid REFERENCES measurement_series(id) ON DELETE SET NULL,
  trait          text NOT NULL,
  value_num      double precision,
  unit           text,
  uncertainty    double precision CHECK (uncertainty IS NULL OR uncertainty >= 0),
  is_derived     boolean NOT NULL DEFAULT false,
  status         validation_status NOT NULL DEFAULT 'needs_validation',
  provenance_id  uuid REFERENCES provenance_trace(id),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_observation_trait ON observation (trait, test_run_id);

-- ---------------------------------------------------------------------
-- 5. Ingestion jobs, quarantine and human review
-- ---------------------------------------------------------------------
CREATE TABLE ingestion_job (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id   uuid NOT NULL REFERENCES source_artifact(id) ON DELETE CASCADE,
  stage         text NOT NULL CHECK (stage IN ('acquire','parse','interpret','validate','register')),
  status        job_status NOT NULL DEFAULT 'queued',
  attempts      int NOT NULL DEFAULT 0,
  error         text,
  started_at    timestamptz,
  finished_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_job_queue ON ingestion_job (status, created_at);

CREATE TABLE quarantine_item (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  variable_id       uuid REFERENCES scientific_variable(id) ON DELETE CASCADE,
  series_id         uuid REFERENCES measurement_series(id) ON DELETE CASCADE,
  reason            text NOT NULL,                      -- ambiguous unit, negative concentration, etc.
  suggested_fix     jsonb,                              -- optional LLM proposal, never auto-applied
  decision          review_decision NOT NULL DEFAULT 'pending',
  reviewer          text,
  reviewed_at       timestamptz,
  audit_rule        text,                               -- auditable rule behind any correction
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (variable_id IS NOT NULL OR series_id IS NOT NULL)
);
CREATE INDEX idx_quarantine_pending ON quarantine_item (decision) WHERE decision = 'pending';

CREATE TABLE column_mapping (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id    uuid NOT NULL REFERENCES source_artifact(id) ON DELETE CASCADE,
  raw_label      text NOT NULL,
  variable_name  text NOT NULL,
  proposed_by    text NOT NULL CHECK (proposed_by IN ('rule','llm','human')),
  status         validation_status NOT NULL DEFAULT 'needs_validation',
  source_location text,
  UNIQUE (artifact_id, raw_label)
);

-- ---------------------------------------------------------------------
-- 6. Computation graph: traits, models, applicability contracts
-- ---------------------------------------------------------------------
CREATE TABLE trait (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL UNIQUE,
  description  text,
  unit         text,
  is_measurable boolean NOT NULL DEFAULT false
);

CREATE TABLE trait_dependency (
  trait_id      uuid NOT NULL REFERENCES trait(id) ON DELETE CASCADE,
  depends_on_id uuid NOT NULL REFERENCES trait(id) ON DELETE RESTRICT,
  PRIMARY KEY (trait_id, depends_on_id),
  CHECK (trait_id <> depends_on_id)
);

CREATE TABLE physical_model (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL,
  version      text NOT NULL,
  fidelity     fidelity_level NOT NULL,
  target_trait_id uuid NOT NULL REFERENCES trait(id),
  equation     text,
  implementation_ref text NOT NULL,                     -- Python import path
  relative_cost numeric NOT NULL DEFAULT 1 CHECK (relative_cost > 0),
  validated_against text,
  is_active    boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);
CREATE INDEX idx_model_trait ON physical_model (target_trait_id, fidelity);

CREATE TABLE model_input (
  model_id     uuid NOT NULL REFERENCES physical_model(id) ON DELETE CASCADE,
  trait_id     uuid NOT NULL REFERENCES trait(id),
  required     boolean NOT NULL DEFAULT true,
  PRIMARY KEY (model_id, trait_id)
);

-- Applicability contract: declared regimes where the model is valid
CREATE TABLE applicability_contract (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_id     uuid NOT NULL REFERENCES physical_model(id) ON DELETE CASCADE,
  variable     text NOT NULL,                           -- e.g. oxygen_mole_fraction
  unit         text,
  min_value    double precision,
  max_value    double precision,
  allowed_values text[],
  rationale    text,
  CHECK (min_value IS NULL OR max_value IS NULL OR min_value <= max_value)
);
CREATE INDEX idx_contract_model ON applicability_contract (model_id);

-- ---------------------------------------------------------------------
-- 7. Computation results with full lineage (evidence-computation dual graph)
-- ---------------------------------------------------------------------
CREATE TABLE computation_result (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_run_id   uuid REFERENCES test_run(id) ON DELETE SET NULL,
  trait_id      uuid NOT NULL REFERENCES trait(id),
  model_id      uuid NOT NULL REFERENCES physical_model(id),
  gate          gate_decision NOT NULL,
  gate_detail   jsonb NOT NULL DEFAULT '{}'::jsonb,
  value_num     double precision,
  unit          text,
  uncertainty   double precision CHECK (uncertainty IS NULL OR uncertainty >= 0),
  residuals     jsonb,
  computed_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (gate <> 'allow' OR value_num IS NOT NULL)      -- numbers only if the gate allowed it
);
CREATE INDEX idx_result_run_trait ON computation_result (test_run_id, trait_id);

CREATE TABLE result_input (
  result_id       uuid NOT NULL REFERENCES computation_result(id) ON DELETE CASCADE,
  observation_id  uuid REFERENCES observation(id),
  variable_id     uuid REFERENCES scientific_variable(id),
  upstream_result_id uuid REFERENCES computation_result(id),
  CHECK (num_nonnulls(observation_id, variable_id, upstream_result_id) = 1)
);
CREATE INDEX idx_result_input_result ON result_input (result_id);

CREATE TABLE computation_opportunity (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id  uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
  model_id       uuid NOT NULL REFERENCES physical_model(id) ON DELETE CASCADE,
  blocking_issue text,                                  -- e.g. image scale unresolved
  is_enabled     boolean NOT NULL DEFAULT false,
  voi_score      double precision,                      -- value-of-information ranking
  discovered_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_id, model_id)
);

CREATE TABLE computability_profile (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id  uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
  trait_id       uuid NOT NULL REFERENCES trait(id),
  class          computability_class NOT NULL,
  detail         text,
  generated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_id, trait_id)
);

-- ---------------------------------------------------------------------
-- 8. Documents and embeddings (pgvector)
-- ---------------------------------------------------------------------
CREATE TABLE document_chunk (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id   uuid NOT NULL REFERENCES source_artifact(id) ON DELETE CASCADE,
  investigation_id uuid REFERENCES investigation(id) ON DELETE SET NULL,
  chunk_index   int NOT NULL,
  page_number   int,
  content       text NOT NULL,
  embedding     vector(1536),
  embedding_model text,
  UNIQUE (artifact_id, chunk_index)
);
CREATE INDEX idx_chunk_embedding ON document_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chunk_fts ON document_chunk USING gin (to_tsvector('english', content));

-- ---------------------------------------------------------------------
-- 9. LLM gateway log, queries, answers and claim verification
-- ---------------------------------------------------------------------
CREATE TABLE llm_call (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role           text NOT NULL,                         -- extractor, planner, interpreter, reviewer
  provider       text NOT NULL,
  model          text NOT NULL,
  prompt_hash    char(64),
  input_tokens   int,
  output_tokens  int,
  cost_usd       numeric(12,6),
  latency_ms     int,
  cache_hit      boolean NOT NULL DEFAULT false,
  success        boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_call_role_time ON llm_call (role, created_at);

CREATE TABLE query_session (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  question      text NOT NULL,
  plan          jsonb,                                  -- validated query plan
  unknowns      jsonb NOT NULL DEFAULT '[]'::jsonb,
  suggested_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tool_invocation (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    uuid NOT NULL REFERENCES query_session(id) ON DELETE CASCADE,
  tool_name     text NOT NULL CHECK (tool_name IN
                  ('find_investigations','resolve_trait','compare_conditions','calculate_trait')),
  arguments     jsonb NOT NULL,
  args_valid    boolean NOT NULL,
  result        jsonb,
  llm_call_id   uuid REFERENCES llm_call(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_tool_session ON tool_invocation (session_id);

CREATE TABLE claim (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id    uuid NOT NULL REFERENCES query_session(id) ON DELETE CASCADE,
  text          text NOT NULL,
  basis         claim_basis NOT NULL,
  verified      boolean NOT NULL DEFAULT false,
  reviewer_note text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE claim_support (
  claim_id        uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
  observation_id  uuid REFERENCES observation(id),
  result_id       uuid REFERENCES computation_result(id),
  chunk_id        uuid REFERENCES document_chunk(id),
  CHECK (num_nonnulls(observation_id, result_id, chunk_id) = 1)
);
CREATE INDEX idx_claim_support_claim ON claim_support (claim_id);

-- A claim may be marked verified only if it has support, or is a labeled hypothesis
CREATE OR REPLACE FUNCTION enforce_claim_support() RETURNS trigger AS $$
BEGIN
  IF NEW.verified AND NEW.basis <> 'labeled_hypothesis'
     AND NOT EXISTS (SELECT 1 FROM claim_support WHERE claim_id = NEW.id) THEN
    RAISE EXCEPTION 'Claim % cannot be verified without supporting evidence', NEW.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_claim_support BEFORE INSERT OR UPDATE ON claim
  FOR EACH ROW EXECUTE FUNCTION enforce_claim_support();

-- ---------------------------------------------------------------------
-- 10. Views
-- ---------------------------------------------------------------------
-- Run conditions pivoted for experiment comparison
CREATE VIEW v_run_conditions AS
SELECT tr.id AS test_run_id, e.id AS experiment_id, i.title AS investigation,
       e.name AS experiment, tr.run_label,
       jsonb_object_agg(rc.name, COALESCE(rc.value_num::text, rc.value_text)) AS conditions
FROM test_run tr
JOIN experiment e ON e.id = tr.experiment_id
JOIN investigation i ON i.id = e.investigation_id
LEFT JOIN run_condition rc ON rc.test_run_id = tr.id
GROUP BY tr.id, e.id, i.title, e.name, tr.run_label;

-- Full lineage from a computed result back to source cells
CREATE VIEW v_result_lineage AS
SELECT cr.id AS result_id, t.name AS trait, pm.name AS model, pm.version,
       cr.value_num, cr.unit, cr.uncertainty,
       sa.original_name AS source_file, pt.sheet, pt.cell_ref, pt.parsing_rule
FROM computation_result cr
JOIN trait t ON t.id = cr.trait_id
JOIN physical_model pm ON pm.id = cr.model_id
LEFT JOIN result_input ri ON ri.result_id = cr.id
LEFT JOIN observation o ON o.id = ri.observation_id
LEFT JOIN scientific_variable sv ON sv.id = ri.variable_id
LEFT JOIN provenance_trace pt ON pt.id = COALESCE(o.provenance_id, sv.provenance_id)
LEFT JOIN source_artifact sa ON sa.id = pt.artifact_id;

-- Variables awaiting human review
CREATE VIEW v_review_queue AS
SELECT q.id, q.reason, q.created_at, sv.name AS variable, sv.raw_label, sv.value_num, sv.unit
FROM quarantine_item q
LEFT JOIN scientific_variable sv ON sv.id = q.variable_id
WHERE q.decision = 'pending';

COMMIT;
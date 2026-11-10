CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    room        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('light', 'fan', 'camera', 'plug')),
    state       TEXT NOT NULL CHECK (state IN ('OFFLINE', 'ONLINE', 'ON', 'OFF')),
    updated_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id           BIGSERIAL PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    payload_json JSONB NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry (device_id, received_at DESC);
-- For the retention job, which deletes by age across all devices.
CREATE INDEX IF NOT EXISTS idx_telemetry_received ON telemetry (received_at);

CREATE TABLE IF NOT EXISTS commands (
    id              TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    -- A double click, or the app resending on a flaky network, must produce exactly
    -- one command. The constraint lives in the DB, not only in code.
    idempotency_key TEXT NOT NULL UNIQUE,
    action          TEXT NOT NULL CHECK (action IN ('ON', 'OFF')),
    created_at      TIMESTAMPTZ NOT NULL
);

-- Commands go at QoS 1: the broker ensures a command REACHES the device at least
-- once, but reaching isn't finishing. The device sends an ack; no ack by the
-- deadline means the command missed, rather than staying PENDING forever.
ALTER TABLE commands ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING', 'ACKED', 'FAILED', 'TIMED_OUT'));
ALTER TABLE commands ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ NULL;
CREATE INDEX IF NOT EXISTS idx_commands_pending ON commands (created_at) WHERE status = 'PENDING';

-- One row per LLM call: tokens and money. This is the true source for costs; the
-- counter in Redis is only a fast ceiling for blocking and can drift slightly.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 BIGSERIAL PRIMARY KEY,
    purpose            TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens      INTEGER NOT NULL CHECK (output_tokens >= 0),
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_usd           NUMERIC(12, 6) NOT NULL CHECK (cost_usd >= 0),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls (created_at DESC);

-- Each site (warehouse, store...) runs its own Frigate. `ui_url` is the address
-- the operator's browser opens; `api_url` is what VigiSift calls to recover missed
-- alerts. The two usually differ (the internal network isn't the operator's network).
CREATE TABLE IF NOT EXISTS sites (
    id           TEXT PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name         TEXT NOT NULL,
    ui_url       TEXT NOT NULL CHECK (ui_url ~ '^https?://'),
    api_url      TEXT NOT NULL CHECK (api_url ~ '^https?://'),
    -- The mark reconciled with the Frigate API; NULL means never reconciled.
    synced_until TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per Frigate review. Frigate reports the same review several times
-- (new, update, end, then genai once summarised), possibly out of order or
-- duplicated; every report merges into this one row.
CREATE TABLE IF NOT EXISTS camera_alerts (
    site_id           TEXT NOT NULL REFERENCES sites(id),
    review_id         TEXT NOT NULL,
    camera            TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('alert', 'detection')),
    objects           TEXT[] NOT NULL DEFAULT '{}',
    zones             TEXT[] NOT NULL DEFAULT '{}',
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ NULL,
    -- Frigate widens the times by up to 5 seconds at each end before sending the
    -- summary. TRUE means both time columns came only from that, so they're approximate.
    times_approximate BOOLEAN NOT NULL DEFAULT FALSE,
    -- From the VLM through Frigate: untrusted data, validated before writing.
    threat_level      SMALLINT NULL CHECK (threat_level BETWEEN 0 AND 2),
    title             TEXT NULL,
    summary           TEXT NULL,
    scene             TEXT NULL,
    confidence        REAL NULL CHECK (confidence BETWEEN 0 AND 1),
    other_concerns    TEXT[] NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, review_id),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS idx_alerts_started ON camera_alerts (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_site_started ON camera_alerts (site_id, started_at DESC);

-- The people watching cameras. There is no default account: create one with
-- scripts/create_operator.py. Only an argon2id hash is stored, never the password.
CREATE TABLE IF NOT EXISTS operators (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,31}$'),
    display_name  TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('operator', 'admin')),
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Operators judge each alert: is it really suspicious. This is the ground truth for
-- evals, so it's a person's judgment of the clip, NOT "was the AI right": labels
-- must not depend on what the model said.
-- One verdict per operator per alert; changing their mind overwrites it.
CREATE TABLE IF NOT EXISTS alert_verdicts (
    site_id         TEXT NOT NULL,
    review_id       TEXT NOT NULL,
    operator_id     BIGINT NOT NULL REFERENCES operators(id),
    verdict         TEXT NOT NULL CHECK (verdict IN ('suspicious', 'normal', 'unclear')),
    note            TEXT NULL CHECK (note IS NULL OR length(note) <= 500),
    -- The suspicion level the AI had given WHEN the operator decided (summaries can arrive later).
    ai_threat_level SMALLINT NULL CHECK (ai_threat_level BETWEEN 0 AND 2),
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, review_id, operator_id),
    FOREIGN KEY (site_id, review_id) REFERENCES camera_alerts (site_id, review_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_verdicts_decided ON alert_verdicts (decided_at DESC);

-- Each camera's suspicion rules. VigiSift holds the truth; Frigate receives them
-- built into a prompt. `applied_sha` is the version Frigate last received: differing
-- from `sha` means a push is pending (Frigate off, network down), and reconcile retries.
CREATE TABLE IF NOT EXISTS camera_rules (
    site_id     TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    camera      TEXT NOT NULL CHECK (camera ~ '^[A-Za-z0-9_-]{1,64}$'),
    rules       JSONB NOT NULL,
    prompt      TEXT NOT NULL,
    concerns    TEXT[] NOT NULL DEFAULT '{}',
    sha         TEXT NOT NULL,
    applied_sha TEXT NULL,
    applied_at  TIMESTAMPTZ NULL,
    apply_error TEXT NULL,
    updated_by  BIGINT NULL REFERENCES operators(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, camera)
);

-- Password generation: resetting a password bumps this, ending all older sessions.
ALTER TABLE operators ADD COLUMN IF NOT EXISTS session_epoch INTEGER NOT NULL DEFAULT 0;
-- This operator's FIRST judgment, unchanged when they change their mind: handling
-- time counts from here, not from the last edit.
ALTER TABLE alert_verdicts ADD COLUMN IF NOT EXISTS first_decided_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- When the summary FIRST arrived. Unchanged on later merges: summary delay counts from here.
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS summarised_at TIMESTAMPTZ NULL;
-- Text re-check against the camera's rules (features/rules/check.py): the level derived
-- from the matched indicator, that indicator verbatim, and the rules version (sha) used.
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS observations TEXT[] NULL;
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS rule_level SMALLINT NULL
    CHECK (rule_level BETWEEN 0 AND 2);
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS rule_match TEXT NULL;
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS rule_sha TEXT NULL;
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS rule_checked_at TIMESTAMPTZ NULL;
-- The re-check queue: only alerts that have a summary and no re-check yet.
CREATE INDEX IF NOT EXISTS camera_alerts_rule_pending
    ON camera_alerts (summarised_at) WHERE rule_checked_at IS NULL AND summarised_at IS NOT NULL;
-- The effective suspicion level: the higher of Frigate's level and the rule re-check's.
-- Postgres computes it, so the queue order, the filters, the metrics and the screens agree.
ALTER TABLE camera_alerts ADD COLUMN IF NOT EXISTS level SMALLINT
    GENERATED ALWAYS AS (GREATEST(threat_level, rule_level)) STORED;
-- It filtered on Frigate's level alone, which no query has used since the re-check added rule_level.
DROP INDEX IF EXISTS idx_alerts_suspicious;

-- An alert as operators see it: the alert, its site's UI address and the latest verdict
-- (with several operators, the last wins). The one definition behind the queue, history,
-- metrics, label export and live events. CREATE OR REPLACE can only add columns at the end.
CREATE OR REPLACE VIEW alert_list AS
SELECT a.site_id, a.review_id, a.camera, a.severity, a.objects, a.zones, a.started_at,
       a.ended_at, a.times_approximate, a.threat_level, a.title, a.summary, a.scene,
       a.confidence, a.other_concerns, a.observations, a.rule_level, a.rule_match, a.level,
       a.first_seen_at, a.updated_at, a.summarised_at, s.ui_url,
       v.verdict, v.decided_at AS verdict_at, o.username AS verdict_by
FROM camera_alerts a
JOIN sites s ON s.id = a.site_id
LEFT JOIN LATERAL (
    SELECT verdict, decided_at, operator_id FROM alert_verdicts
    WHERE site_id = a.site_id AND review_id = a.review_id
    ORDER BY decided_at DESC LIMIT 1
) v ON TRUE
LEFT JOIN operators o ON o.id = v.operator_id;

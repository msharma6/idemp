-- StateStore v0: version-stamped compare-and-swap for shared agent state
-- No semantics yet, per the plan -- this proves the plumbing only.

DROP TABLE IF EXISTS state_objects;
DROP TABLE IF EXISTS conflict_log;

CREATE TABLE state_objects (
    object_id       TEXT PRIMARY KEY,
    version         BIGINT NOT NULL DEFAULT 0,
    value           JSONB NOT NULL,
    owner_agent_id  TEXT,             -- who currently holds the lease, if anyone
    lease_expires_at TIMESTAMPTZ,     -- leases expire so a crashed agent can't block forever
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every rejected write gets logged here -- this is the audit trail that answers
-- "who tried to overwrite whose work, and when."
CREATE TABLE conflict_log (
    id              BIGSERIAL PRIMARY KEY,
    object_id       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    expected_version BIGINT NOT NULL,
    actual_version  BIGINT NOT NULL,
    attempted_value JSONB NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

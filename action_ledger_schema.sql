-- Idempotency ledger for irreversible real-world actions (sending an email,
-- charging a card, executing a trade). Unlike state_objects, there's nothing
-- to "version" here -- the risk is the ACTION firing twice, not a value
-- being overwritten. So the primitive is different: claim-before-execute,
-- record-after-execute, and a durable record of what actually happened.

DROP TABLE IF EXISTS compensations;
DROP TABLE IF EXISTS action_ledger CASCADE;

CREATE TABLE action_ledger (
    action_id       TEXT PRIMARY KEY,   -- caller-supplied idempotency key
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | completed | failed
    claimed_by      TEXT NOT NULL,      -- which agent claimed the right to execute
    claim_expires_at TIMESTAMPTZ NOT NULL,
    result          JSONB,              -- cached result, returned on any duplicate call
    error           TEXT,
    action_type     TEXT NOT NULL,      -- e.g. 'send_refund_email', 'charge_card'
    attempt_count   INTEGER NOT NULL DEFAULT 1,  -- incremented on EVERY claim call,
                                                  -- including calls that hit an existing
                                                  -- row -- this is what makes "duplicates
                                                  -- prevented" a real, measured number
                                                  -- instead of an inferred proxy.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

-- If a saga needs to unwind (step 3 of 4 failed, steps 1-2 already fired for
-- real), this is where compensating actions get queued for the operator/agent
-- to execute -- e.g. "step 1 sent an email, here's the retraction to send."
CREATE TABLE compensations (
    id              BIGSERIAL PRIMARY KEY,
    saga_id         TEXT NOT NULL,
    action_id       TEXT NOT NULL REFERENCES action_ledger(action_id),
    compensating_action TEXT NOT NULL,   -- human/agent-readable description
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued | done
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

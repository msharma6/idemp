"""
ActionLedger -- idempotency + compensating-action primitive for irreversible
real-world actions (send an email, charge a card, execute a trade).

This is a genuinely different problem from StateStore's version-stamped CAS:
there's no "value" to protect from being overwritten. The risk is the ACTION
itself firing more than once. Two agents both deciding, independently, to
send the same customer a refund email is a conflict that CAS literally
cannot see -- nothing was "written" to contest.

Core pattern (standard distributed-systems idempotency-key design):
  1. claim(action_id)   -- "I intend to execute this exactly once"
  2. execute the real side effect (simulated here)
  3. complete(action_id, result)  -- durably record what happened

Any second caller with the same action_id, whether it's a genuine duplicate
agent or the SAME agent retrying after a lost response, gets back the
cached result instead of re-executing -- that's the actual guarantee.
"""

from __future__ import annotations
import json
import time
import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional

import psycopg2
import psycopg2.extras

from state_store import DSN


class AlreadyClaimed(Exception):
    """Someone else is currently executing this action -- do not proceed."""
    def __init__(self, action_id: str, claimed_by: str, expires_at):
        self.action_id = action_id
        self.claimed_by = claimed_by
        self.expires_at = expires_at
        super().__init__(
            f"Action '{action_id}' already claimed by '{claimed_by}' "
            f"until {expires_at}. Refusing to execute a possible duplicate."
        )


class AlreadyCompleted(Exception):
    """This action already ran to completion -- here is the cached result,
    do NOT execute the real side effect again."""
    def __init__(self, action_id: str, result: dict, attempt_count: int = 1):
        self.action_id = action_id
        self.result = result
        self.attempt_count = attempt_count
        super().__init__(
            f"Action '{action_id}' already completed (this is attempt #{attempt_count}). "
            f"Returning cached result instead of re-executing the real-world side effect."
        )


class NeedsManualReview(Exception):
    """The agent that claimed this action never reported back (crash, timeout)
    and its claim has expired. We CANNOT safely assume the side effect did or
    didn't happen -- that's the fundamental limit of exactly-once semantics
    across a real crash. This must be surfaced to a human or a higher-trust
    resolver, never silently retried."""
    def __init__(self, action_id: str, claimed_by: str):
        self.action_id = action_id
        self.claimed_by = claimed_by
        super().__init__(
            f"Action '{action_id}' was claimed by '{claimed_by}' but never "
            f"completed, and its claim has expired. Whether the real-world "
            f"side effect fired is UNKNOWN. Flagging for manual review -- "
            f"refusing to auto-retry an action that might double-charge, "
            f"double-email, or double-execute."
        )


class ActionLedger:
    def __init__(self, dsn: str = DSN):
        self.dsn = dsn

    def _conn(self):
        return psycopg2.connect(self.dsn)

    def claim_or_get_result(
        self, action_id: str, agent_id: str, action_type: str, claim_ttl_seconds: int = 30
    ) -> Optional[dict]:
        """
        The entry point every agent calls BEFORE doing the real side effect.

        Returns None if this agent has successfully claimed the right to
        execute (proceed with the real action).
        Raises AlreadyCompleted if it already ran -- use .result, don't redo it.
        Raises AlreadyClaimed if another agent is actively working on it.
        Raises NeedsManualReview if a prior claim expired without completing.
        """
        with self._conn() as conn, conn.cursor() as cur:
            # Atomic insert-if-absent: if two agents race here at the exact
            # same instant, only one INSERT wins; the other falls through to
            # the SELECT below and gets routed through normal conflict
            # handling, instead of crashing on a unique-constraint violation.
            cur.execute(
                """
                INSERT INTO action_ledger (action_id, status, claimed_by, claim_expires_at, action_type)
                VALUES (%s, 'pending', %s, now() + (%s || ' seconds')::interval, %s)
                ON CONFLICT (action_id) DO NOTHING
                RETURNING action_id
                """,
                (action_id, agent_id, claim_ttl_seconds, action_type),
            )
            won_the_insert = cur.fetchone() is not None
            if won_the_insert:
                return None

            cur.execute(
                """
                UPDATE action_ledger
                SET attempt_count = attempt_count + 1
                WHERE action_id = %s
                RETURNING status, claimed_by, claim_expires_at, result, attempt_count
                """,
                (action_id,),
            )
            row = cur.fetchone()

            status, claimed_by, claim_expires_at, result, attempt_count = row

            # Commit the attempt_count increment NOW, before any of the
            # branches below raise -- otherwise psycopg2's `with conn`
            # context manager rolls back this UPDATE along with the
            # exception, and the attempt counter silently stops being
            # accurate the moment it matters most (duplicate detection).
            # Same class of bug as the conflict_log rollback in state_store.py.
            conn.commit()

            if status == "completed":
                raise AlreadyCompleted(action_id, result, attempt_count)

            if status == "pending":
                now = dt.datetime.now(dt.timezone.utc)
                if claim_expires_at > now:
                    raise AlreadyClaimed(action_id, claimed_by, claim_expires_at)
                # Claim expired without completing -- do NOT auto-retry.
                # We genuinely don't know if the side effect fired.
                raise NeedsManualReview(action_id, claimed_by)

            if status == "failed":
                # A clean, known failure (the action ran, errored cleanly, and
                # we KNOW the side effect did not take -- e.g. the payment API
                # returned a hard decline). Safe to reclaim and retry.
                cur.execute(
                    """
                    UPDATE action_ledger
                    SET status = 'pending', claimed_by = %s,
                        claim_expires_at = now() + (%s || ' seconds')::interval,
                        error = NULL
                    WHERE action_id = %s
                    """,
                    (agent_id, claim_ttl_seconds, action_id),
                )
                return None

    def complete(self, action_id: str, result: dict):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE action_ledger
                SET status = 'completed', result = %s, completed_at = now()
                WHERE action_id = %s
                """,
                (json.dumps(result), action_id),
            )

    def fail(self, action_id: str, error: str):
        """Use ONLY when you are certain the side effect did NOT occur
        (e.g. a clean validation error before any external call was made,
        or an API that confirmed no charge/send happened)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE action_ledger SET status = 'failed', error = %s WHERE action_id = %s",
                (error, action_id),
            )

    def queue_compensation(self, saga_id: str, action_id: str, compensating_action: str):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO compensations (saga_id, action_id, compensating_action) VALUES (%s, %s, %s)",
                (saga_id, action_id, compensating_action),
            )

    def pending_compensations(self, saga_id: str) -> list[dict]:
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM compensations WHERE saga_id = %s AND status = 'queued' ORDER BY created_at",
                (saga_id,),
            )
            return list(cur.fetchall())


def execute_idempotently(
    ledger: ActionLedger,
    action_id: str,
    agent_id: str,
    action_type: str,
    real_action: Callable[[], dict],
) -> dict:
    """
    Convenience wrapper implementing the full claim -> execute -> complete
    pattern. `real_action` is the actual side effect (sending the email,
    calling the payment API) -- it only runs if this agent wins the claim.
    """
    try:
        ledger.claim_or_get_result(action_id, agent_id, action_type)
    except AlreadyCompleted as e:
        return {"outcome": "SKIPPED_ALREADY_DONE", "result": e.result}
    except AlreadyClaimed as e:
        return {"outcome": "SKIPPED_IN_PROGRESS", "detail": str(e)}
    except NeedsManualReview as e:
        return {"outcome": "MANUAL_REVIEW_REQUIRED", "detail": str(e)}

    # We won the claim -- safe to actually perform the real-world side effect.
    try:
        result = real_action()
        ledger.complete(action_id, result)
        return {"outcome": "EXECUTED", "result": result}
    except Exception as e:
        ledger.fail(action_id, str(e))
        return {"outcome": "FAILED_CLEANLY", "error": str(e)}

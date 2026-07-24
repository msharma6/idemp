"""
StateStore v0 -- Version-stamped compare-and-swap for shared agent state.

This is the plumbing layer only (Phase 1, Week 3 of the roadmap): no semantic
conflict classification yet, just a proof that concurrent writes from
different "agents" can't silently clobber each other.

Core idea: every write must present the version it *thinks* it's updating.
If that version doesn't match what's actually in the database, the write is
rejected with a Conflict -- instead of the classic "last write wins" failure
mode where one agent's work vanishes with no trace.
"""

from __future__ import annotations
import json
import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

import psycopg2
import psycopg2.extras

DSN = "dbname=idemp user=postgres password=postgres host=localhost"


class Conflict(Exception):
    """Raised when a write's expected_version doesn't match reality."""
    def __init__(self, object_id: str, expected_version: int, actual_version: int):
        self.object_id = object_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"CONFLICT on '{object_id}': expected version {expected_version}, "
            f"but current version is {actual_version}. Your write was NOT applied."
        )


class LeaseHeld(Exception):
    """Raised when another agent currently holds an active lease on this object."""
    def __init__(self, object_id: str, holder: str, expires_at):
        self.object_id = object_id
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(
            f"LEASE HELD on '{object_id}' by agent '{holder}' until {expires_at}."
        )


@dataclass
class StateObject:
    object_id: str
    version: int
    value: dict
    owner_agent_id: Optional[str]
    lease_expires_at: Optional[dt.datetime]


class StateStore:
    """A thin client around the Postgres-backed CAS store."""

    def __init__(self, dsn: str = DSN):
        self.dsn = dsn

    def _conn(self):
        return psycopg2.connect(self.dsn)

    def create(self, object_id: str, initial_value: dict) -> StateObject:
        """Create a new tracked object at version 0. Fails if it already exists."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO state_objects (object_id, version, value)
                VALUES (%s, 0, %s)
                ON CONFLICT (object_id) DO NOTHING
                RETURNING object_id, version, value, owner_agent_id, lease_expires_at
                """,
                (object_id, json.dumps(initial_value)),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Object '{object_id}' already exists")
            return StateObject(row[0], row[1], row[2], row[3], row[4])

    def read(self, object_id: str) -> StateObject:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT object_id, version, value, owner_agent_id, lease_expires_at
                FROM state_objects WHERE object_id = %s
                """,
                (object_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"No such object: {object_id}")
            return StateObject(row[0], row[1], row[2], row[3], row[4])

    def claim(self, object_id: str, agent_id: str, ttl_seconds: int = 30) -> StateObject:
        """
        Try to take an exclusive lease on an object. Leases expire automatically
        (TTL) so a crashed/hung agent can never permanently block the object --
        that's the practical fix for the classic distributed-lock failure mode.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE state_objects
                SET owner_agent_id = %s,
                    lease_expires_at = now() + (%s || ' seconds')::interval
                WHERE object_id = %s
                  AND (owner_agent_id IS NULL
                       OR lease_expires_at < now()
                       OR owner_agent_id = %s)
                RETURNING object_id, version, value, owner_agent_id, lease_expires_at
                """,
                (agent_id, ttl_seconds, object_id, agent_id),
            )
            row = cur.fetchone()
            if row is None:
                # Someone else holds a live lease -- report exactly who and until when
                cur.execute(
                    "SELECT owner_agent_id, lease_expires_at FROM state_objects WHERE object_id = %s",
                    (object_id,),
                )
                holder, expires = cur.fetchone()
                raise LeaseHeld(object_id, holder, expires)
            return StateObject(row[0], row[1], row[2], row[3], row[4])

    def write(
        self,
        object_id: str,
        agent_id: str,
        new_value: dict,
        expected_version: int,
    ) -> StateObject:
        """
        The core primitive: compare-and-swap. Succeeds ONLY if expected_version
        matches the current version in the database. On mismatch, the write is
        rejected outright (not merged, not silently dropped) and logged.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE state_objects
                SET value = %s,
                    version = version + 1,
                    updated_at = now()
                WHERE object_id = %s AND version = %s
                RETURNING object_id, version, value, owner_agent_id, lease_expires_at
                """,
                (json.dumps(new_value), object_id, expected_version),
            )
            row = cur.fetchone()
            if row is not None:
                return StateObject(row[0], row[1], row[2], row[3], row[4])

            # Write rejected -- find the real current version for the error report,
            # and log the rejected attempt for the audit trail.
            cur.execute(
                "SELECT version FROM state_objects WHERE object_id = %s", (object_id,)
            )
            result = cur.fetchone()
            if result is None:
                raise KeyError(f"No such object: {object_id}")
            actual_version = result[0]

            cur.execute(
                """
                INSERT INTO conflict_log
                    (object_id, agent_id, expected_version, actual_version, attempted_value)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (object_id, agent_id, expected_version, actual_version, json.dumps(new_value)),
            )
            # Commit the log entry explicitly -- otherwise the `with conn` context
            # manager rolls back the whole transaction (including this INSERT)
            # when we raise below, and the audit trail silently loses the record
            # of its own conflict, which would be a fittingly bad bug to ship.
            conn.commit()
            raise Conflict(object_id, expected_version, actual_version)

    def conflict_history(self, object_id: str) -> list[dict]:
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM conflict_log WHERE object_id = %s ORDER BY occurred_at",
                (object_id,),
            )
            return list(cur.fetchall())

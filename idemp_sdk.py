"""
Idemp SDK -- the actual developer-facing integration layer.

This is what turns three manual REST calls (claim -> execute -> complete/fail)
into one function call or one decorator. It talks to the Idemp service over
plain HTTP, so it works from any Python codebase -- LangGraph, CrewAI, or
custom -- with zero framework-specific code.

Three-tier action classification (per the design discussion):
  SAFE       -- no side effect, or side effect is naturally idempotent
               (reads, text generation, setting a field to a fixed value).
               Never touches the ledger. Retry freely.
  IDEMPOTENT -- has a side effect, but retrying reaches the same end state
               (e.g. "set status = approved"). Uses StateStore's version
               check to catch real conflicts, but doesn't need the full
               action-ledger machinery.
  IRREVERSIBLE -- retrying can cause a DIFFERENT or cumulative outcome
               (send an email, charge a card, hard-delete a record).
               MUST go through the full claim/complete/fail ledger.

Critically: automatic retry is only ever applied to CLEAN, KNOWN failures.
A crash or ambiguous failure on an IRREVERSIBLE action is NEVER silently
retried -- it surfaces as NeedsManualReview, exactly as designed in
action_ledger.py. This SDK does not paper over that distinction to make
the API feel simpler; doing so would reintroduce the exact bug this whole
system exists to prevent.
"""

from __future__ import annotations
import functools
import time
from enum import Enum
from typing import Callable, Optional, TypeVar, Any

import requests

T = TypeVar("T")


class ActionTier(Enum):
    SAFE = "safe"                # no ledger involvement at all
    IDEMPOTENT = "idempotent"    # StateStore version-check only
    IRREVERSIBLE = "irreversible"  # full Action Ledger claim/complete/fail


class ManualReviewRequired(Exception):
    """Raised when an irreversible action's prior attempt crashed and we
    genuinely don't know if the real-world side effect fired. This must
    surface to the caller -- never silently retried."""
    def __init__(self, action_id: str, detail: dict):
        self.action_id = action_id
        self.detail = detail
        super().__init__(
            f"Action '{action_id}' needs manual review -- a prior attempt "
            f"was claimed but never completed, and we cannot safely guess "
            f"whether the real side effect occurred. Detail: {detail}"
        )


class InProgress(Exception):
    """Raised when another agent currently holds an active claim on this
    exact action_id. Back off and don't proceed."""
    def __init__(self, action_id: str, detail: dict):
        self.action_id = action_id
        self.detail = detail
        super().__init__(f"Action '{action_id}' is currently being executed elsewhere: {detail}")


class IdempClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- SAFE tier: no ledger at all, just a passthrough for symmetry -----
    def run_safe(self, fn: Callable[[], T]) -> T:
        """No side-effect protection needed -- just run it. Exists so
        callers can classify every action through one API, even the ones
        that need zero ledger involvement."""
        return fn()

    # -- IDEMPOTENT tier: StateStore version check, no action ledger ------
    def run_idempotent(
        self, object_id: str, agent_id: str, fn: Callable[[dict], dict],
    ) -> dict:
        """
        Read-modify-write against a StateStore-tracked object. `fn` receives
        the current value and returns the new value. On a version conflict,
        retries automatically by re-reading and re-applying `fn` -- this is
        safe ONLY because idempotent writes reach the same end state
        regardless of how many times they're retried against fresh state.
        """
        for attempt in range(5):
            r = requests.get(f"{self.base_url}/objects/{object_id}", timeout=self.timeout)
            r.raise_for_status()
            current = r.json()

            new_value = fn(current["value"])

            r = requests.post(
                f"{self.base_url}/objects/{object_id}/write",
                json={"agent_id": agent_id, "new_value": new_value,
                      "expected_version": current["version"]},
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json()
            # 409 conflict -- someone else wrote first; re-read and retry,
            # safe because this tier is defined as "same fn, same end state."
            time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(f"Could not apply idempotent write to '{object_id}' after 5 attempts")

    # -- IRREVERSIBLE tier: full claim/execute/complete/fail ledger -------
    def run_irreversible(
        self,
        action_id: str,
        agent_id: str,
        action_type: str,
        fn: Callable[[], dict],
        claim_ttl_seconds: int = 30,
    ) -> dict:
        """
        The core protected path for real-world side effects. Returns the
        result dict on success (fresh execution OR a cached prior result).
        Raises InProgress if someone else is actively working on it right
        now. Raises ManualReviewRequired if a prior attempt crashed and the
        outcome is genuinely unknown -- this is NEVER auto-retried.
        """
        r = requests.post(
            f"{self.base_url}/actions/{action_id}/claim",
            json={"agent_id": agent_id, "action_type": action_type,
                  "claim_ttl_seconds": claim_ttl_seconds},
            timeout=self.timeout,
        )

        if r.status_code == 200:
            body = r.json()
            if body.get("outcome") == "ALREADY_COMPLETED":
                return body["result"]  # duplicate call -- return cached result, do NOT re-run fn

        elif r.status_code == 409:
            detail = r.json().get("detail", {})
            outcome = detail.get("outcome")
            if outcome == "MANUAL_REVIEW_REQUIRED":
                raise ManualReviewRequired(action_id, detail)
            raise InProgress(action_id, detail)
        else:
            r.raise_for_status()

        # We won the claim -- safe to actually perform the real side effect.
        try:
            result = fn()
        except Exception as e:
            requests.post(
                f"{self.base_url}/actions/{action_id}/fail",
                json={"error": str(e)}, timeout=self.timeout,
            )
            raise

        requests.post(
            f"{self.base_url}/actions/{action_id}/complete",
            json={"result": result}, timeout=self.timeout,
        )
        return result


# ---------------------------------------------------------------------------
# Decorator sugar -- @safe_action for the common irreversible-action case
# ---------------------------------------------------------------------------

_default_client = IdempClient()


def safe_action(
    action_type: str,
    action_id_fn: Callable[..., str],
    agent_id: str = "default-agent",
    client: Optional[IdempClient] = None,
):
    """
    Decorator for functions that perform an IRREVERSIBLE side effect.

    `action_id_fn` derives a stable idempotency key from the decorated
    function's own arguments -- e.g. lambda customer_id, **kw: f"refund-{customer_id}".
    This is the piece a caller MUST get right: the key has to represent
    "this specific real-world intent," not just "this function call."

    Usage:
        @safe_action("send_refund_email", lambda customer_id: f"refund-email-{customer_id}")
        def send_refund(customer_id: str) -> dict:
            ... actually send the email ...
            return {"sent_to": customer_id}
    """
    def decorator(fn: Callable[..., dict]):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            c = client or _default_client
            action_id = action_id_fn(*args, **kwargs)
            return c.run_irreversible(
                action_id=action_id,
                agent_id=agent_id,
                action_type=action_type,
                fn=lambda: fn(*args, **kwargs),
            )
        return wrapper
    return decorator

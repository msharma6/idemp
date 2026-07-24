"""
Idemp MCP Server -- exposes the reliability layer as MCP tools.

This is the highest-leverage integration point: any MCP-compatible agent
(Claude Agent SDK, and a growing list of others) can use these tools with
zero framework-specific integration code -- just an MCP server connection.

Run: python3 mcp_server.py
(stdio transport -- add this server to an MCP client's config to use it)
"""

from __future__ import annotations
import json
from mcp.server.fastmcp import FastMCP

from idemp_sdk import IdempClient, ManualReviewRequired, InProgress

mcp_app = FastMCP("idemp")
client = IdempClient()


@mcp_app.tool()
def create_shared_object(object_id: str, initial_value: dict) -> str:
    """Create a new shared, version-tracked object. Use this once per
    logical record (e.g. a customer record, a task) before any agent
    tries to claim or write to it."""
    import requests
    r = requests.post(f"{client.base_url}/objects",
                       json={"object_id": object_id, "initial_value": initial_value})
    if r.status_code == 409:
        return f"Object '{object_id}' already exists."
    r.raise_for_status()
    return json.dumps(r.json())


@mcp_app.tool()
def read_shared_object(object_id: str) -> str:
    """Read the current value and version of a shared object."""
    import requests
    r = requests.get(f"{client.base_url}/objects/{object_id}")
    if r.status_code == 404:
        return f"No such object: '{object_id}'"
    r.raise_for_status()
    return json.dumps(r.json())


@mcp_app.tool()
def write_shared_object(object_id: str, agent_id: str, new_value: dict, expected_version: int) -> str:
    """
    Attempt a compare-and-swap write to a shared object. Pass the version
    number you read most recently as expected_version. If another agent
    wrote first, this call is REJECTED (not silently applied) -- re-read
    the object and retry with the fresh version if that happens."""
    import requests
    r = requests.post(
        f"{client.base_url}/objects/{object_id}/write",
        json={"agent_id": agent_id, "new_value": new_value, "expected_version": expected_version},
    )
    if r.status_code == 409:
        detail = r.json().get("detail", {})
        return (f"CONFLICT: your write was NOT applied. You expected version "
                f"{detail.get('expected_version')} but the current version is "
                f"{detail.get('actual_version')}. Re-read the object and retry.")
    r.raise_for_status()
    return json.dumps(r.json())


@mcp_app.tool()
def perform_irreversible_action(action_id: str, agent_id: str, action_type: str, note: str) -> str:
    """
    Use this BEFORE performing any real-world irreversible action (sending
    an email, charging a card, executing a trade, deleting a record).
    action_id must be a STABLE identifier representing the real-world
    intent (e.g. "refund-email-customer-4471"), not a random ID -- the
    same action_id called twice means "this is the same real-world action,
    don't do it again."

    This tool only CLAIMS the right to act -- it does not perform the
    action itself (this server has no real email/payment integration).
    In a real deployment, your own code performs the actual side effect
    only after this call returns CLAIMED, then reports back via
    confirm_action_complete or confirm_action_failed.
    """
    import requests
    r = requests.post(
        f"{client.base_url}/actions/{action_id}/claim",
        json={"agent_id": agent_id, "action_type": action_type, "claim_ttl_seconds": 60},
    )
    if r.status_code == 200:
        body = r.json()
        if body.get("outcome") == "ALREADY_COMPLETED":
            return (f"ALREADY DONE -- do not perform this action again. "
                    f"Cached result: {json.dumps(body.get('result'))}")
        return f"CLAIMED -- you may now perform the action, then call confirm_action_complete('{action_id}', ...)."
    if r.status_code == 409:
        detail = r.json().get("detail", {})
        if detail.get("outcome") == "MANUAL_REVIEW_REQUIRED":
            return (f"STOP -- MANUAL REVIEW REQUIRED. A prior attempt at this action crashed "
                    f"and we cannot confirm whether it actually happened. Do NOT retry automatically. "
                    f"Escalate to a human. Detail: {detail.get('message')}")
        return f"IN PROGRESS -- another agent ({detail.get('claimed_by')}) is already handling this. Do not proceed."
    r.raise_for_status()


@mcp_app.tool()
def confirm_action_complete(action_id: str, result: dict) -> str:
    """Call this AFTER you have actually performed the real-world side
    effect successfully, to record the result. Any future duplicate call
    to perform_irreversible_action with this same action_id will return
    this cached result instead of allowing the action to repeat."""
    import requests
    requests.post(f"{client.base_url}/actions/{action_id}/complete", json={"result": result})
    return f"Recorded '{action_id}' as complete."


@mcp_app.tool()
def confirm_action_failed(action_id: str, error: str) -> str:
    """Call this ONLY when you are certain the real-world side effect did
    NOT occur (e.g. a clean validation error, a hard decline, before any
    external call fired). This makes the action safely retriable. Do NOT
    call this after an ambiguous failure or a crash -- just let the claim
    time out, which routes future attempts to manual review instead."""
    import requests
    requests.post(f"{client.base_url}/actions/{action_id}/fail", json={"error": error})
    return f"Recorded '{action_id}' as a clean failure -- safely retriable."


@mcp_app.tool()
def get_conflict_history(object_id: str) -> str:
    """Return the audit trail of every write that was rejected for this
    object -- who tried what, and when."""
    import requests
    r = requests.get(f"{client.base_url}/objects/{object_id}/conflicts")
    r.raise_for_status()
    return json.dumps(r.json())


if __name__ == "__main__":
    mcp_app.run()

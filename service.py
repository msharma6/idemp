"""
StateStore Service -- the actual network-callable sidecar.

This is the Week 4 milestone: everything so far (state_store.py, action_ledger.py)
was a Python library, callable only from Python code that imports it directly.
That's not a product a LangGraph agent AND a CrewAI agent can both use --
they'd each need custom Python integration.

This wraps both primitives as a plain HTTP service. Any agent, in any
framework, in any language, that can make an HTTP request can use this.
That's the actual "framework-agnostic sidecar" claim -- proven here, not
just asserted.

Run: uvicorn service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations
from typing import Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from state_store import StateStore, Conflict, LeaseHeld
from action_ledger import (
    ActionLedger, AlreadyCompleted, AlreadyClaimed, NeedsManualReview,
)

app = FastAPI(
    title="StateStore Service",
    description=(
        "Cross-framework state coordination for AI agents: version-stamped "
        "compare-and-swap for shared records, plus an idempotency ledger for "
        "irreversible real-world actions (emails, charges, trades)."
    ),
    version="0.1.0",
)

sc = StateStore()
ledger = ActionLedger()

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Idemp Dashboard</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 13px; margin-bottom: 24px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 32px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card .value { font-size: 28px; font-weight: 600; }
  .card .label { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .card.good .value { color: #3fb950; }
  .card.bad .value { color: #f85149; }
  .card.neutral .value { color: #58a6ff; }
  section { margin-bottom: 32px; }
  section h2 { font-size: 15px; border-bottom: 1px solid #30363d; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: #8b949e; padding: 6px 8px; border-bottom: 1px solid #30363d; }
  td { padding: 6px 8px; border-bottom: 1px solid #21262d; vertical-align: top; }
  .status-completed { color: #3fb950; }
  .status-failed { color: #f85149; }
  .status-pending { color: #d29922; }
  .mono { font-family: ui-monospace, monospace; }
  .empty { color: #8b949e; font-style: italic; padding: 12px; }
  .refresh-note { color: #8b949e; font-size: 11px; }
</style>
</head>
<body>
  <h1>Idemp Reliability Dashboard</h1>
  <div class="subtitle">Cross-framework state conflicts and irreversible-action idempotency, live from Postgres. <span class="refresh-note">Auto-refreshes every 3s.</span></div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Duplicate / Conflicting Actions Prevented</h2>
    <div id="conflicts-table"></div>
  </section>

  <section>
    <h2>Action Timeline &amp; Retry History</h2>
    <div id="actions-table"></div>
  </section>

  <section>
    <h2>Tracked Shared Objects</h2>
    <div id="objects-table"></div>
  </section>

<script>
async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

function fmtTime(ts) {
  if (!ts) return '-';
  return new Date(ts).toLocaleString();
}

function renderCards(s) {
  const cards = [
    {label: 'Tracked Objects', value: s.total_tracked_objects, cls: 'neutral'},
    {label: 'Conflicts Prevented', value: s.conflicts_prevented, cls: 'good'},
    {label: 'Duplicate Actions Prevented', value: s.duplicate_actions_prevented, cls: 'good'},
    {label: 'Objects w/ Conflicts', value: s.objects_with_conflicts, cls: 'neutral'},
    {label: 'Total Actions', value: s.total_actions, cls: 'neutral'},
    {label: 'Completed Actions', value: s.completed_actions, cls: 'good'},
    {label: 'Failed (Retriable)', value: s.failed_actions, cls: 'bad'},
    {label: 'Pending / In-flight', value: s.pending_actions, cls: 'neutral'},
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card ${c.cls}"><div class="value">${c.value}</div><div class="label">${c.label}</div></div>`
  ).join('');
}

function renderConflicts(rows) {
  const el = document.getElementById('conflicts-table');
  if (!rows.length) { el.innerHTML = '<div class="empty">No conflicts logged yet -- no duplicate writes caught.</div>'; return; }
  el.innerHTML = `<table><tr><th>Object</th><th>Agent (rejected)</th><th>Expected v</th><th>Actual v</th><th>Attempted Value</th><th>When</th></tr>` +
    rows.map(r => `<tr>
      <td class="mono">${r.object_id}</td>
      <td>${r.agent_id}</td>
      <td>${r.expected_version}</td>
      <td>${r.actual_version}</td>
      <td class="mono">${JSON.stringify(r.attempted_value)}</td>
      <td>${fmtTime(r.occurred_at)}</td>
    </tr>`).join('') + `</table>`;
}

function renderActions(rows) {
  const el = document.getElementById('actions-table');
  if (!rows.length) { el.innerHTML = '<div class="empty">No actions tracked yet.</div>'; return; }
  el.innerHTML = `<table><tr><th>Action ID</th><th>Type</th><th>Status</th><th>Attempts</th><th>Claimed By</th><th>Result / Error</th><th>Created</th><th>Completed</th></tr>` +
    rows.map(r => `<tr>
      <td class="mono">${r.action_id}</td>
      <td>${r.action_type}</td>
      <td class="status-${r.status}">${r.status}</td>
      <td>${r.attempt_count}${r.attempt_count > 1 ? ' <span style="color:#3fb950">(' + (r.attempt_count - 1) + ' duplicate prevented)</span>' : ''}</td>
      <td>${r.claimed_by || '-'}</td>
      <td class="mono">${r.result ? JSON.stringify(r.result) : (r.error || '-')}</td>
      <td>${fmtTime(r.created_at)}</td>
      <td>${fmtTime(r.completed_at)}</td>
    </tr>`).join('') + `</table>`;
}

function renderObjects(rows) {
  const el = document.getElementById('objects-table');
  if (!rows.length) { el.innerHTML = '<div class="empty">No shared objects tracked yet.</div>'; return; }
  el.innerHTML = `<table><tr><th>Object ID</th><th>Version</th><th>Current Owner (lease)</th><th>Lease Expires</th><th>Last Updated</th></tr>` +
    rows.map(r => `<tr>
      <td class="mono">${r.object_id}</td>
      <td>${r.version}</td>
      <td>${r.owner_agent_id || '-'}</td>
      <td>${fmtTime(r.lease_expires_at)}</td>
      <td>${fmtTime(r.updated_at)}</td>
    </tr>`).join('') + `</table>`;
}

async function refresh() {
  try {
    const [summary, conflicts, actions, objects] = await Promise.all([
      fetchJSON('/dashboard/summary'),
      fetchJSON('/dashboard/conflicts'),
      fetchJSON('/dashboard/actions'),
      fetchJSON('/dashboard/objects'),
    ]);
    renderCards(summary);
    renderConflicts(conflicts.conflicts);
    renderActions(actions.actions);
    renderObjects(objects.objects);
  } catch (e) {
    console.error('dashboard refresh failed', e);
  }
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# StateStore core: shared, versioned records
# ---------------------------------------------------------------------------

class CreateObjectRequest(BaseModel):
    object_id: str
    initial_value: dict


class ClaimRequest(BaseModel):
    agent_id: str
    ttl_seconds: int = 30


class WriteRequest(BaseModel):
    agent_id: str
    new_value: dict
    expected_version: int


@app.post("/objects", tags=["state"])
def create_object(req: CreateObjectRequest):
    """Create a new tracked shared object at version 0."""
    try:
        obj = sc.create(req.object_id, req.initial_value)
        return {"object_id": obj.object_id, "version": obj.version, "value": obj.value}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/objects/{object_id}", tags=["state"])
def read_object(object_id: str):
    """Read the current value and version of a shared object."""
    try:
        obj = sc.read(object_id)
        return {
            "object_id": obj.object_id, "version": obj.version, "value": obj.value,
            "owner_agent_id": obj.owner_agent_id,
            "lease_expires_at": obj.lease_expires_at.isoformat() if obj.lease_expires_at else None,
        }
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/objects/{object_id}/claim", tags=["state"])
def claim_object(object_id: str, req: ClaimRequest):
    """Take an exclusive, TTL-limited lease on an object before working on it."""
    try:
        obj = sc.claim(object_id, req.agent_id, req.ttl_seconds)
        return {"object_id": obj.object_id, "version": obj.version,
                "lease_expires_at": obj.lease_expires_at.isoformat()}
    except LeaseHeld as e:
        raise HTTPException(status_code=409, detail={
            "error": "lease_held", "holder": e.holder,
            "expires_at": e.expires_at.isoformat(), "message": str(e),
        })


@app.post("/objects/{object_id}/write", tags=["state"])
def write_object(object_id: str, req: WriteRequest):
    """
    Compare-and-swap write. Succeeds ONLY if req.expected_version matches
    the object's current version -- otherwise rejected with 409 and logged,
    not silently applied. This is the core anti-silent-overwrite primitive.
    """
    try:
        obj = sc.write(object_id, req.agent_id, req.new_value, req.expected_version)
        return {"object_id": obj.object_id, "version": obj.version, "value": obj.value}
    except Conflict as e:
        raise HTTPException(status_code=409, detail={
            "error": "conflict",
            "expected_version": e.expected_version,
            "actual_version": e.actual_version,
            "message": str(e),
        })
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/objects/{object_id}/conflicts", tags=["state"])
def get_conflict_history(object_id: str):
    """The audit trail: every write that was rejected, by whom, and when."""
    return {"object_id": object_id, "conflicts": sc.conflict_history(object_id)}


# ---------------------------------------------------------------------------
# Action Ledger: idempotency for irreversible side effects
# ---------------------------------------------------------------------------

class ActionClaimRequest(BaseModel):
    agent_id: str
    action_type: str
    claim_ttl_seconds: int = 30


class ActionCompleteRequest(BaseModel):
    result: dict


class ActionFailRequest(BaseModel):
    error: str


@app.post("/actions/{action_id}/claim", tags=["actions"])
def claim_action(action_id: str, req: ActionClaimRequest):
    """
    Call this BEFORE performing any irreversible side effect (send an email,
    charge a card, execute a trade). Empty 200 response means you won the
    claim -- proceed with the real action, then call /complete or /fail.
    Non-200 responses tell you NOT to perform the side effect.
    """
    try:
        ledger.claim_or_get_result(action_id, req.agent_id, req.action_type, req.claim_ttl_seconds)
        return {"outcome": "CLAIMED", "action_id": action_id}
    except AlreadyCompleted as e:
        return {"outcome": "ALREADY_COMPLETED", "action_id": action_id, "result": e.result,
                "attempt_count": e.attempt_count}
    except AlreadyClaimed as e:
        raise HTTPException(status_code=409, detail={
            "outcome": "ALREADY_CLAIMED", "claimed_by": e.claimed_by,
            "expires_at": e.expires_at.isoformat(),
        })
    except NeedsManualReview as e:
        raise HTTPException(status_code=409, detail={
            "outcome": "MANUAL_REVIEW_REQUIRED",
            "message": str(e),
            "claimed_by": e.claimed_by,
        })


@app.post("/actions/{action_id}/complete", tags=["actions"])
def complete_action(action_id: str, req: ActionCompleteRequest):
    """Call this AFTER the real side effect succeeds, to record the result
    and make any future duplicate claim return this cached result instead
    of re-executing."""
    ledger.complete(action_id, req.result)
    return {"outcome": "RECORDED_COMPLETE", "action_id": action_id}


@app.post("/actions/{action_id}/fail", tags=["actions"])
def fail_action(action_id: str, req: ActionFailRequest):
    """Call this ONLY when you're certain the side effect did NOT occur
    (e.g. a clean validation error before any external call fired). This
    makes the action safely retriable. Do NOT call this after a crash or
    an ambiguous failure -- just let the claim expire, which routes future
    attempts to MANUAL_REVIEW_REQUIRED instead of a silent retry."""
    ledger.fail(action_id, req.error)
    return {"outcome": "RECORDED_FAILED", "action_id": action_id}


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard data endpoints -- aggregated views over both primitives' tables
# ---------------------------------------------------------------------------

@app.get("/dashboard/summary", tags=["dashboard"])
def dashboard_summary():
    """High-level counters for the dashboard's top cards."""
    import psycopg2
    with psycopg2.connect(sc.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM state_objects")
        total_objects = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM conflict_log")
        conflicts_prevented = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM action_ledger")
        total_actions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM action_ledger WHERE status = 'completed'")
        completed_actions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM action_ledger WHERE status = 'failed'")
        failed_actions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM action_ledger WHERE status = 'pending'")
        pending_actions = cur.fetchone()[0]

        # Real measured number now (not a proxy): attempt_count is
        # incremented on every claim call for an action_id, including calls
        # that find an existing row (duplicate/in-progress/already-completed).
        # SUM(attempt_count) - COUNT(*) = total extra attempts beyond each
        # action's first -- i.e. exactly how many repeat calls were caught
        # and prevented from re-executing the real side effect.
        cur.execute("SELECT COALESCE(SUM(attempt_count), 0), COUNT(*) FROM action_ledger")
        total_attempts, distinct_actions = cur.fetchone()
        duplicate_actions_prevented = total_attempts - distinct_actions

        cur.execute("SELECT COUNT(DISTINCT object_id) FROM conflict_log")
        objects_with_conflicts = cur.fetchone()[0]

    return {
        "total_tracked_objects": total_objects,
        "conflicts_prevented": conflicts_prevented,
        "duplicate_actions_prevented": duplicate_actions_prevented,
        "objects_with_conflicts": objects_with_conflicts,
        "total_actions": total_actions,
        "completed_actions": completed_actions,
        "failed_actions": failed_actions,
        "pending_actions": pending_actions,
    }


@app.get("/dashboard/conflicts", tags=["dashboard"])
def dashboard_all_conflicts(limit: int = 50):
    """Every rejected write across all objects, most recent first."""
    import psycopg2
    import psycopg2.extras
    with psycopg2.connect(sc.dsn) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM conflict_log ORDER BY occurred_at DESC LIMIT %s", (limit,)
        )
        return {"conflicts": list(cur.fetchall())}


@app.get("/dashboard/actions", tags=["dashboard"])
def dashboard_all_actions(limit: int = 50):
    """Every tracked action across the ledger, most recent first -- this
    is the 'action timeline' and 'retry history' view combined."""
    import psycopg2
    import psycopg2.extras
    with psycopg2.connect(sc.dsn) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT action_id, status, claimed_by, action_type, result, error,
                   attempt_count, created_at, completed_at, claim_expires_at
            FROM action_ledger ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        return {"actions": list(cur.fetchall())}


@app.get("/dashboard/objects", tags=["dashboard"])
def dashboard_all_objects(limit: int = 50):
    """Every tracked shared object and its current version/owner state."""
    import psycopg2
    import psycopg2.extras
    with psycopg2.connect(sc.dsn) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT object_id, version, owner_agent_id, lease_expires_at, updated_at
            FROM state_objects ORDER BY updated_at DESC LIMIT %s
            """,
            (limit,),
        )
        return {"objects": list(cur.fetchall())}


@app.get("/dashboard", response_class=HTMLResponse, tags=["dashboard"])
def dashboard_page():
    """The hosted dashboard itself -- a single HTML page that polls the
    data endpoints above and renders them. Served directly by this same
    service, so 'hosted' means: run this service, open this URL."""
    return DASHBOARD_HTML

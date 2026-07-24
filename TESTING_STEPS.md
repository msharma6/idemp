# Exact Testing Steps

Everything below has been run and verified in a real environment (Postgres + FastAPI + real LangGraph and CrewAI SDKs). Follow these in order.

## 0. Prerequisites
```
pip install -r requirements.txt
```
Postgres running locally, with an `idemp` database (see earlier setup steps if not already done). Password in `state_store.py`'s `DSN` must match your Postgres setup.

## 1. Reset the database to a clean state
```
python3 -c "
import psycopg2
from state_store import DSN
with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
    with open('state_schema.sql') as f: cur.execute(f.read())
    with open('action_ledger_schema.sql') as f: cur.execute(f.read())
print('schemas reset clean')
"
```
Run this any time you want a fresh start (it wipes all tracked objects, actions, and audit history).

## 2. Start the service
```
uvicorn service:app --host 0.0.0.0 --port 8000
```
Leave this running in its own terminal. Verify it's up:
```
curl http://localhost:8000/health
```
Expect: `{"status":"ok"}`

## 3. Test the core state-conflict mechanism (two real frameworks racing)
In a second terminal:
```
curl -X POST http://localhost:8000/objects -H "Content-Type: application/json" \
  -d '{"object_id": "customer-5501", "initial_value": {"status": "pending", "notes": []}}'

python3 agent_langgraph_sim.py &
python3 agent_crewai_sim.py &
wait
```
**Expect:** one agent's write succeeds (version bumps to 1), the other is rejected with a `409` and a clear conflict message. Confirm the audit trail caught it:
```
curl http://localhost:8000/objects/customer-5501/conflicts
```
Should show exactly one logged conflict.

## 4. Test the real LangGraph integration
```
curl -X POST http://localhost:8000/objects -H "Content-Type: application/json" \
  -d '{"object_id": "customer-langgraph-test", "initial_value": {"status": "pending", "notes": []}}'

python3 langgraph_integration.py customer-langgraph-test
```
**Expect:** `[LangGraph graph run] billing node -> version 1` — this runs through a real `langgraph.graph.StateGraph`, not a simulation.

## 5. Test the real CrewAI integration (proves no duplicate email send)
```
python3 crewai_integration.py
```
**Expect output ending in:**
```
[FRESH SEND] Refund email to cust-9001: ...
[CACHED -- NOT RE-SENT] Refund email to cust-9001: ...
>>> PROOF: actual email API was called 1 time(s) despite the tool being invoked twice.
```
This uses a real `crewai.tools.BaseTool` subclass — if this assertion fails, something is broken in the idempotency path.

## 6. Test the MCP server tools directly
```
python3 -c "
from mcp_server import create_shared_object, read_shared_object, write_shared_object, perform_irreversible_action, confirm_action_complete, get_conflict_history

print(create_shared_object('customer-mcp-test', {'status': 'pending', 'notes': []}))
print(read_shared_object('customer-mcp-test'))
print(write_shared_object('customer-mcp-test', 'mcp-agent-1', {'status': 'verified'}, 0))
print(write_shared_object('customer-mcp-test', 'mcp-agent-2', {'status': 'fraud'}, 0))  # should be REJECTED
print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-1', 'send_refund_email', 'test'))
print(confirm_action_complete('mcp-refund-1', {'sent': True}))
print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-2', 'send_refund_email', 'test'))  # should say ALREADY DONE
"
```
**Expect:** the second `write_shared_object` call returns a conflict message; the second `perform_irreversible_action` call for the same `mcp-refund-1` returns `ALREADY DONE` with the cached result, not a fresh claim.

To actually connect this MCP server to a real MCP client (e.g. Claude Desktop, Claude Code), add it to the client's MCP server config pointing at:
```
python3 /full/path/to/mcp_server.py
```

## 7. View the live dashboard
With the service still running (step 2), generate some real activity first (steps 3-6 above all populate real data), then open in a browser:
```
http://localhost:8000/dashboard
```
**Expect:** cards showing real counts (tracked objects, conflicts prevented, completed/failed actions), plus three live tables (conflicts, action timeline, tracked objects) that auto-refresh every 3 seconds. This is served directly by the running service — no separate hosting step needed; wherever `service.py` runs, `/dashboard` is available.

## 8. Full regression check (all-in-one)
To re-run everything from scratch in one go:
```
python3 -c "
import psycopg2
from state_store import DSN
with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
    with open('state_schema.sql') as f: cur.execute(f.read())
    with open('action_ledger_schema.sql') as f: cur.execute(f.read())
"
python3 demo.py               # StateClaw core: 3 scenarios
python3 demo_idempotency.py   # Action Ledger: 5 scenarios
```
Both should end with all scenarios passing / assertions holding, printed clearly in the output.

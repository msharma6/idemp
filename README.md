# Idemp

A cross-framework reliability layer for AI agents: prevents silent state
overwrites between agents in different frameworks, and prevents
irreversible real-world actions (emails, charges, trades) from firing
more than once — even across retries, crashes, and duplicate agent calls.

Not tied to any orchestrator. Any agent, in any framework or language,
that can make an HTTP call (or use the SDK, or an MCP client) can use this.

## Requirements

- **Python >=3.10, <3.14** — CrewAI does not yet support Python 3.14+.
  If your system's default Python is 3.14 or newer, install Python 3.12
  alongside it and use `py -3.12` for every command in this project (see
  `TESTING_STEPS.md`'s Windows/PowerShell gotchas section for the full
  setup path — this was hit and resolved during real testing).
- PostgreSQL, running locally or reachable over the network. **You need
  a known password for the `postgres` user** — if you're not certain
  what it is (very common if Postgres was installed a while ago, or came
  bundled with something else), see the "Postgres password" section at
  the top of `TESTING_STEPS.md` for a full reset procedure, covering
  Windows, Mac, and Linux.
- **Tested and confirmed working end-to-end on both Linux and Windows**
  (PowerShell) — every scenario below has been personally run and
  verified on real hardware, not just asserted.

## The two problems this solves

1. **Silent state overwrites** — two agents update the same shared record
   at once; one agent's work vanishes with zero error, zero trace.
2. **Duplicate irreversible actions** — two agents independently decide to
   send the same refund email, or a retried task re-charges a card that
   already succeeded. There's no "state" to protect here — the action
   itself is the risk.

Both are real, independently confirmed problems — see `research-evidence-log.md`
and `idemp-brief.md` for citations (CrewAI issues #5802, #6125, #2881,
Discussion #4111; CLAN as related prior art for problem #1 specifically).

## Architecture

```
Agent (any framework)
       |
       v
  idemp_sdk.py  --  the developer-facing client (safe_execute / @safe_action)
       |
       v
  service.py    --  the live network service (FastAPI/REST + dashboard)
       |
       +-- StateStore core (state_store.py)  -- version-stamped CAS for shared objects
       +-- Action Ledger (action_ledger.py)   -- idempotency ledger for irreversible actions
       |
       v
   Postgres ("idemp" database) -- durable storage + full audit trail
```

Two additional integration surfaces sit on top of the same service:
- `mcp_server.py` — exposes the same operations as MCP tools, for any
  MCP-compatible agent (highest-leverage integration point — zero
  framework-specific code required).
- `langgraph_integration.py` / `crewai_integration.py` — real integration
  examples using the actual LangGraph and CrewAI SDKs, not simulations.

## What's here, file by file

**Core primitives**
- `state_schema.sql` / `state_store.py` — `state_objects` + `conflict_log`
  tables; `StateStore` class with `create()`, `read()`, `claim()` (leased
  ownership, auto-expiry), `write()` (the compare-and-swap operation itself).
- `action_ledger_schema.sql` / `action_ledger.py` — `action_ledger` +
  `compensations` tables; `claim_or_get_result()`, `complete()`, `fail()`,
  `queue_compensation()`. Tracks `attempt_count` per action so "duplicates
  prevented" is a real measured number, not an estimate.

**Network service**
- `service.py` — FastAPI app wrapping both primitives as REST endpoints
  (`/objects/*`, `/actions/*`), plus dashboard data endpoints and the
  dashboard page itself (`/dashboard`).

**Developer-facing SDK**
- `idemp_sdk.py` — `IdempClient` with three action tiers:
  - `run_safe()` — no side effect, or naturally idempotent; no ledger involved.
  - `run_idempotent()` — StateStore version-check only; safe to auto-retry
    because retrying reaches the same end state.
  - `run_irreversible()` — full Action Ledger claim/execute/complete/fail;
    NEVER auto-retries an ambiguous crash — surfaces `ManualReviewRequired`
    instead, on purpose.
  - `@safe_action` decorator for the common irreversible-action case.

**Integration surfaces**
- `mcp_server.py` — MCP tools: `create_shared_object`, `read_shared_object`,
  `write_shared_object`, `perform_irreversible_action`,
  `confirm_action_complete`, `confirm_action_failed`, `get_conflict_history`.
- `langgraph_integration.py` — a real `langgraph.graph.StateGraph` node
  calling the SDK.
- `crewai_integration.py` — a real `crewai.tools.BaseTool` subclass
  (`SendRefundEmailTool`), directly targeting CrewAI Issue #5802's exact
  failure mode, with instrumentation proving the underlying action only
  fires once even when the tool is called twice.

**Proof / test scripts**
- `reset_db.py` — resets both schemas to a clean state. A real script
  file rather than an inline snippet, specifically because multi-line
  `python -c "..."` commands don't work in Windows cmd/PowerShell.
- `demo.py` — three StateStore scenarios: unprotected race (the problem),
  protected race (CAS catches it), leasing with auto-expiry.
- `demo_idempotency.py` — five Action Ledger scenarios: duplicate agents,
  lost-response retry, clean failure retry, crash → manual review, saga
  compensation.
- `agent_langgraph_sim.py` / `agent_crewai_sim.py` — two independent
  HTTP-only clients (no shared code/imports) proving two different
  "frameworks" can race against the live service and get a correctly
  arbitrated result.
- `test_mcp.py` — direct test of the MCP server's tool functions.

## Running it

See `TESTING_STEPS.md` for the exact, verified, copy-pasteable sequence —
**it includes a full Windows/PowerShell gotchas section** covering every
real issue hit during actual Windows testing (Python version conflicts,
PowerShell's `curl` alias, backgrounding processes, Postgres password
resets, and more). Short version (Linux/Mac; see TESTING_STEPS.md for the
Windows/PowerShell equivalents):

```bash
pip install -r requirements.txt

# create the database (once)
createdb idemp   # or via pgAdmin: create a database named "idemp"

# reset schema
python3 reset_db.py

# start the service
uvicorn service:app --host 0.0.0.0 --port 8000

# in another terminal: view the live dashboard
open http://localhost:8000/dashboard
```

## What this does NOT do yet (by design)

- No semantic conflict classification (is Agent B's write actually
  *incompatible* with Agent A's, or just a harmless addition?) — a
  possible future layer, not built. Current behavior: any version
  mismatch is a conflict, full stop.
- No auto-merge or conflict-resolution strategy — on purpose. Flag for
  review, don't guess.
- No A2A protocol wrapper yet — the service is plain REST + MCP; A2A
  adoption is still young enough that this was deprioritized versus MCP.
- No production-grade auth/multi-tenancy on the service itself yet.

## Known gotchas for anyone touching this code

**`with connection` + `raise` silently rolls back everything in that block — including work unrelated to the exception.** This has bitten this codebase three separate times during development:

1. The `conflict_log` audit insert in `state_store.py`'s `write()` — the INSERT recording a rejected write was getting rolled back by the very `Conflict` exception it was supposed to be logging.
2. A TOCTOU race in `action_ledger.py`'s `claim_or_get_result()` — two concurrent inserts on the same `action_id` crashed on a unique-constraint violation instead of being routed through proper conflict handling.
3. The `attempt_count` increment in `action_ledger.py`'s `claim_or_get_result()` — the counter update was computed correctly and even reported correctly in the API response, but silently reverted in the database because it happened inside the same `with self._conn() as conn` block as the `AlreadyCompleted`/`AlreadyClaimed`/`NeedsManualReview` exceptions that signal the caller.

**The pattern to watch for:** psycopg2's connection context manager (`with psycopg2.connect(...) as conn`) commits on normal exit but **rolls back the entire transaction if any exception propagates out of the block** — including exceptions you're deliberately raising as a *signal* to the caller, not as an error condition. Since this codebase's whole design is "compute something, then raise a specific exception type to tell the caller what happened" (`Conflict`, `LeaseHeld`, `AlreadyCompleted`, `AlreadyClaimed`, `NeedsManualReview`), this bug shape will keep recurring anywhere a new code path adds a database write followed by a raise inside the same `with` block.

**The fix, every time:** call `conn.commit()` explicitly right before the `raise`, whenever the write needs to survive the exception. Don't rely on the `with` block's implicit commit-on-exit for any write that happens on a path that also raises.

**Before adding any new exception-signaling path in this codebase**, ask: does anything get written to the database on this path before the exception fires? If yes, it needs an explicit `conn.commit()` immediately before the `raise`, not after.

**A separate, one-time gotcha from the rename itself:** this project was originally called StateClaw, and its core module was `stateclaw.py` with a `StateClaw` class. It has since been fully renamed to Idemp / `state_store.py` / `StateStore`, including the Postgres database name (`stateclaw` → `idemp`). If you're working from an old clone or old notes, expect those old names — they no longer exist anywhere in this codebase as of this rename.

## The core claim this project is built on

Every scenario above has been run against real Postgres, not asserted from a diagram — including catching four real bugs along the way (three rollback-pattern bugs above, plus a test-isolation mistake in `demo_idempotency.py`). That's arguably the most important property of this repo: nothing here is aspirational. If a claim is made about what it does, there's a runnable script proving it.

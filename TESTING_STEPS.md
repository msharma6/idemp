# Exact Testing Steps

Everything below has been run and verified in two environments: a Linux
sandbox (bash) and a real Windows VM (PowerShell) — including working
through every gotcha we actually hit. Follow in order.

**Read "Postgres password" below FIRST, regardless of OS** — this is the
single most common thing to get stuck on, since most people never set a
Postgres password deliberately in the first place.

**If you're on Windows/PowerShell specifically, also read the
"Windows/PowerShell gotchas" section** — several commands below need
adjusting there too.

---

## Postgres password (read this first, on any OS)

Every step below needs a working Postgres connection with a **known**
password for the `postgres` user. This trips people up constantly,
because:
- If you installed Postgres yourself, you set a password once during
  install and may not remember it now.
- If Postgres came bundled with something else (a framework, a Docker
  setup, a package manager default), you may never have set one at all —
  many installs default to no-password-required for local connections.
- On Mac (via Homebrew) and Linux (via apt), it's common to have **no
  password configured** for local trust-based connections initially,
  which then breaks the moment a tool (like `state_store.py`'s `DSN`)
  explicitly supplies a password expecting it to be checked.

**If you don't know your password with certainty, don't guess repeatedly
— reset it directly.** The procedure is the same shape on every OS: find
`pg_hba.conf`, temporarily allow passwordless local connection, set a
real password, then lock it back down.

### Windows
1. Open `C:\Program Files\PostgreSQL\<version>\data\pg_hba.conf` in
   **Notepad running as Administrator** (right-click Notepad → Run as
   administrator, then File → Open; switch the file filter to "All
   Files" to see `.conf` files — this folder is protected, so opening
   Notepad normally first avoids permission errors).
2. Find the two lines under "IPv4 local connections" and "IPv6 local
   connections" ending in `scram-sha-256` (or `md5`) and change just
   that last word to `trust` on both. Save.
3. Restart the Postgres service: Start menu → "Services" → find
   `postgresql-x64-<version>` → right-click → Restart.
4. Connect with no password now:
   ```
   "C:\Program Files\PostgreSQL\<version>\bin\psql.exe" -U postgres -h localhost -d postgres
   ```
5. Set a real, known password:
   ```sql
   ALTER USER postgres PASSWORD 'yournewpassword';
   ```
6. Create the database if it doesn't exist yet:
   ```sql
   CREATE DATABASE idemp;
   ```
7. `\q` to exit.
8. **Revert `pg_hba.conf` back to `scram-sha-256`** (undo step 2) and
   restart the service again — leaving `trust` in place means anyone on
   the machine can connect with no password at all.
9. Update the `DSN` line in `state_store.py` with your new password.

### Mac (Homebrew Postgres)
1. Find `pg_hba.conf` — typically at
   `/opt/homebrew/var/postgresql@<version>/pg_hba.conf` (Apple Silicon)
   or `/usr/local/var/postgres/pg_hba.conf` (Intel). If unsure, run
   `psql -U $(whoami) -d postgres -c "SHOW hba_file;"` to get the exact
   path.
2. Edit it (`nano` or any editor), same change as Windows: set the
   `local` and `host ... 127.0.0.1/32` / `::1/128` lines' method to
   `trust` temporarily.
3. Restart Postgres: `brew services restart postgresql@<version>`.
4. Connect with no password: `psql -U postgres -h localhost -d postgres`
   (if the `postgres` role doesn't exist yet, connect as your own mac
   username instead: `psql -d postgres`, then
   `CREATE ROLE postgres WITH LOGIN SUPERUSER PASSWORD 'yournewpassword';`)
5. Otherwise, same as Windows: `ALTER USER postgres PASSWORD '...'`,
   `CREATE DATABASE idemp;`, `\q`.
6. Revert `pg_hba.conf` back to its original method, restart again.
7. Update `state_store.py`'s `DSN`.

### Linux (apt-installed Postgres)
1. `pg_hba.conf` is typically at `/etc/postgresql/<version>/main/pg_hba.conf`.
2. Edit as root/sudo: `sudo nano /etc/postgresql/<version>/main/pg_hba.conf`,
   same `trust` change as above on the `local` and `127.0.0.1`/`::1` lines.
3. Restart: `sudo service postgresql restart`.
4. Connect: `sudo -u postgres psql` (this uses OS-level trust, works even
   before the `trust` edit in many default apt installs — try this
   first before editing the conf file, it may already work).
5. Same as above: `ALTER USER postgres PASSWORD '...'`,
   `CREATE DATABASE idemp;`, `\q`.
6. Revert `pg_hba.conf`, restart again.
7. Update `state_store.py`'s `DSN`.

**After any of the above, always double check `state_store.py` has the
exact password you just set** — this is the single most common way this
whole procedure "doesn't work" on a second attempt: the reset succeeds,
but the code still has the old or placeholder password.

---

## Windows / PowerShell gotchas (read this first if on Windows)

### 1. Use `py -3.12`, not `python` or `python3`
Some frameworks (CrewAI specifically) require Python `>=3.10,<3.14`. If
your system's default Python is 3.14 or newer, install Python 3.12
alongside it (via python.org — check "Add python.exe to PATH" during
install) and always call it explicitly:
```
py -3.12 -m pip install -r requirements.txt
py -3.12 script_name.py
```
Verify what versions are registered with:
```
py --list
```

### 2. Multi-line `python -c "..."` commands do NOT work in cmd/PowerShell
Anywhere you see a command like this in older docs/notes:
```
python3 -c "
import psycopg2
...
"
```
**Don't type this directly into cmd or PowerShell** — each line gets
interpreted as a separate terminal command instead of Python code, and
you'll get a wall of `'import' is not recognized...` errors. Instead,
save the code as a real `.py` file and run that file. This repo now
includes `reset_db.py` for exactly this reason — use it instead of any
inline `-c` snippet:
```
py -3.12 reset_db.py
```

### 3. PowerShell's `curl` is NOT real curl
PowerShell aliases `curl` to `Invoke-WebRequest`, which uses completely
different syntax (no `-X`, `-H`, `-d` flags) and will throw confusing
`ParameterBindingException` errors if you paste a normal curl command.
Two fixes:
- **Force real curl** (ships with Windows 10/11) by calling `curl.exe`
  explicitly, and escape inner quotes:
  ```
  curl.exe -X POST http://localhost:8000/objects -H "Content-Type: application/json" -d "{\"object_id\": \"customer-5501\", \"initial_value\": {\"status\": \"pending\", \"notes\": []}}"
  ```
- **Or use PowerShell's native equivalent**, which is often cleaner:
  ```
  Invoke-RestMethod -Uri "http://localhost:8000/objects" -Method Post -ContentType "application/json" -Body '{"object_id": "customer-5501", "initial_value": {"status": "pending", "notes": []}}'
  ```
  For GET requests, just:
  ```
  Invoke-RestMethod -Uri "http://localhost:8000/objects/customer-5501/conflicts"
  ```

### 4. Backslash line continuation (`\`) doesn't work in PowerShell
Bash uses `\` at the end of a line to continue a command on the next
line; PowerShell uses a backtick (`` ` ``) instead, and mixing them causes
errors. **Easiest fix: just write the whole command on one line** —
that's what every command in this doc now does.

### 5. `&` for background processes doesn't work like Bash
In PowerShell, `&` is the "call operator" and does NOT background a
process the way it does in Bash. To run two agents concurrently (for the
race-condition test), **open two separate PowerShell windows** instead:
- Window 1: keep `uvicorn` running
- Window 2: type (but don't run yet) `py -3.12 agent_langgraph_sim.py`
- Window 3: type (but don't run yet) `py -3.12 agent_crewai_sim.py`
- Press Enter in window 2, then immediately switch and press Enter in
  window 3 — `agent_crewai_sim.py` has a built-in short delay, so an
  imperfect manual overlap is still enough to trigger a real race.

### 6. `psql.exe` needs the full path and quotes (see "Postgres password" section above for the full reset procedure)
`psql` likely isn't on your PATH by default. Use the full path, wrapped
in quotes because of the space in "Program Files":
```
"C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d postgres
```
(adjust the version number `18` to match what's actually installed —
check `C:\Program Files\PostgreSQL\` in File Explorer if unsure)

### 7. Don't paste multi-line commands — type them fresh
Several issues in this session turned out to be a command getting pasted
twice or split oddly by the terminal. If a command errors in a way that
doesn't match what's described above, try retyping it manually before
assuming something deeper is wrong.

---

## 0. Prerequisites
```
py -3.12 -m pip install -r requirements.txt
```
Postgres running locally, with an `idemp` database created (see gotcha #6
above if you hit password errors). Password in `state_store.py`'s `DSN`
must match your actual Postgres password.

## 1. Reset the database to a clean state
```
py -3.12 reset_db.py
```
Expected output: `schemas reset clean`. Run this any time you want a
fresh start (wipes all tracked objects, actions, and audit history).

## 2. Start the service
```
py -3.12 -m uvicorn service:app --host 0.0.0.0 --port 8000
```
Leave this running in its own terminal window. In a **second** window,
verify it's up:
```
Invoke-RestMethod -Uri "http://localhost:8000/health"
```
Expect: `status: ok`

## 3. Test the core state-conflict mechanism (two real frameworks racing)
Create the shared object:
```
Invoke-RestMethod -Uri "http://localhost:8000/objects" -Method Post -ContentType "application/json" -Body '{"object_id": "customer-5501", "initial_value": {"status": "pending", "notes": []}}'
```
Then, per gotcha #5 above, open two more terminal windows and fire both
agent scripts as close together in time as you can:
```
py -3.12 agent_langgraph_sim.py
```
```
py -3.12 agent_crewai_sim.py
```
**Expect:** one succeeds, one is REJECTED with a conflict message (which
agent wins depends on exact timing — that's fine, the point is exactly
one succeeds and the other is caught, not silently dropped). Confirm the
audit trail:
```
Invoke-RestMethod -Uri "http://localhost:8000/objects/customer-5501/conflicts"
```

## 4. Test the real LangGraph integration
```
Invoke-RestMethod -Uri "http://localhost:8000/objects" -Method Post -ContentType "application/json" -Body '{"object_id": "customer-langgraph-test", "initial_value": {"status": "pending", "notes": []}}'
py -3.12 langgraph_integration.py customer-langgraph-test
```
**Expect:** `[LangGraph graph run] billing node -> version 1` — runs
through a real `langgraph.graph.StateGraph`, not a simulation.

## 5. Test the real CrewAI integration (proves no duplicate email send)
```
py -3.12 crewai_integration.py
```
**Expect output ending in:**
```
[FRESH SEND] Refund email to cust-9001: ...
[CACHED -- NOT RE-SENT] Refund email to cust-9001: ...
>>> PROOF: actual email API was called 1 time(s) despite the tool being invoked twice.
```

## 6. Test the MCP server tools directly
Save this as `test_mcp.py` in your project folder (per gotcha #2 — don't
type this inline):
```python
from mcp_server import create_shared_object, read_shared_object, write_shared_object, perform_irreversible_action, confirm_action_complete, get_conflict_history

print(create_shared_object('customer-mcp-test', {'status': 'pending', 'notes': []}))
print(read_shared_object('customer-mcp-test'))
print(write_shared_object('customer-mcp-test', 'mcp-agent-1', {'status': 'verified'}, 0))
print(write_shared_object('customer-mcp-test', 'mcp-agent-2', {'status': 'fraud'}, 0))  # should be REJECTED
print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-1', 'send_refund_email', 'test'))
print(confirm_action_complete('mcp-refund-1', {'sent': True}))
print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-2', 'send_refund_email', 'test'))  # should say ALREADY DONE
```
Run it:
```
py -3.12 test_mcp.py
```
**Expect:** the second `write_shared_object` call returns a conflict
message; the second `perform_irreversible_action` call returns
`ALREADY DONE` with the cached result, not a fresh claim.

To connect this MCP server to a real MCP client (Claude Desktop, Claude
Code), add it to the client's MCP server config pointing at:
```
py -3.12 C:\full\path\to\mcp_server.py
```

## 7. View the live dashboard
With the service still running (step 2), and after generating some real
activity (steps 3-6 above all populate real data), open in a browser:
```
http://localhost:8000/dashboard
```
**Expect:** cards showing real counts, plus three live tables (conflicts,
action timeline, tracked objects) auto-refreshing every 3 seconds.

## 8. Full regression check (all-in-one)
```
py -3.12 reset_db.py
py -3.12 demo.py
py -3.12 demo_idempotency.py
```
Both demo scripts should end with all scenarios passing / assertions
holding, printed clearly in the output.

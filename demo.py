"""
Demo: prove the plumbing works.

Scenario: two agents ("LangGraph-Agent" and "CrewAI-Agent") both read a shared
customer record at the same moment, then both try to write their own update
based on that shared starting point -- the exact "broken telephone" /
"race condition" scenario from the pitch.

With plain last-write-wins storage, Agent B's write would silently erase
Agent A's work with no trace. Here, StateStore catches it.
"""

import threading
import time
import psycopg2

from state_store import StateStore, Conflict, DSN


def reset_db():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        with open("state_schema.sql") as f:
            cur.execute(f.read())
    print("[setup] schema reset\n")


def print_object(sc: StateStore, object_id: str, label: str):
    obj = sc.read(object_id)
    print(f"{label}: version={obj.version} value={obj.value}")


def scenario_naive_race():
    """First, show what the PROBLEM looks like without CAS protection --
    i.e. what happens if you ignore the version and just overwrite."""
    print("=" * 70)
    print("SCENARIO A: naive 'last write wins' (the problem we're fixing)")
    print("=" * 70)
    sc = StateStore()
    sc.create("customer-4471", {"status": "pending_review", "notes": []})

    # Both agents read the same starting state
    starting = sc.read("customer-4471")
    print_object(sc, "customer-4471", "[both agents read] shared record")

    # Agent A does real work and computes its own version of the update
    agent_a_update = dict(starting.value)
    agent_a_update["notes"] = agent_a_update["notes"] + ["Agent A: verified billing info"]
    agent_a_update["status"] = "billing_verified"

    # Agent B does real work on the SAME starting state, unaware of Agent A
    agent_b_update = dict(starting.value)
    agent_b_update["notes"] = agent_b_update["notes"] + ["Agent B: flagged fraud risk"]
    agent_b_update["status"] = "fraud_review"

    # Naive approach: whoever writes last just... wins. No check at all.
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE state_objects SET value = %s WHERE object_id = %s",
                    ('{"status": "billing_verified", "notes": ["Agent A: verified billing info"]}',
                     "customer-4471"))
        conn.commit()
        time.sleep(0.05)
        cur.execute("UPDATE state_objects SET value = %s WHERE object_id = %s",
                    ('{"status": "fraud_review", "notes": ["Agent B: flagged fraud risk"]}',
                     "customer-4471"))
        conn.commit()

    print_object(sc, "customer-4471", "[after both 'wrote']  final record")
    print("\n>>> Agent A's fraud-relevant billing verification is GONE. No error. No trace.")
    print(">>> This is the silent-overwrite failure mode from the pitch.\n")


def scenario_statestore_protected():
    """Now the same race, but through StateStore's CAS write() -- this time
    the second write is REJECTED instead of silently applied."""
    print("=" * 70)
    print("SCENARIO B: same race, protected by StateStore compare-and-swap")
    print("=" * 70)
    sc = StateStore()
    sc.create("customer-9981", {"status": "pending_review", "notes": []})

    starting = sc.read("customer-9981")
    print_object(sc, "customer-9981", "[both agents read] shared record @ v0")

    results = {}

    def agent_a():
        update = dict(starting.value)
        update["notes"] = update["notes"] + ["Agent A: verified billing info"]
        update["status"] = "billing_verified"
        try:
            obj = sc.write("customer-9981", "langgraph-agent-A", update, expected_version=starting.version)
            results["A"] = f"SUCCESS -> now at version {obj.version}"
        except Conflict as e:
            results["A"] = f"REJECTED -> {e}"

    def agent_b():
        time.sleep(0.05)  # B writes slightly after A, using the SAME stale version
        update = dict(starting.value)
        update["notes"] = update["notes"] + ["Agent B: flagged fraud risk"]
        update["status"] = "fraud_review"
        try:
            obj = sc.write("customer-9981", "crewai-agent-B", update, expected_version=starting.version)
            results["B"] = f"SUCCESS -> now at version {obj.version}"
        except Conflict as e:
            results["B"] = f"REJECTED -> {e}"

    t1 = threading.Thread(target=agent_a)
    t2 = threading.Thread(target=agent_b)
    t1.start(); t2.start()
    t1.join(); t2.join()

    print(f"\nAgent A result: {results['A']}")
    print(f"Agent B result: {results['B']}")
    print_object(sc, "customer-9981", "\n[final state]  record")

    print("\n>>> One write succeeded. The other was REJECTED, not silently dropped.")
    print(">>> The rejected agent gets a clear signal to re-read and retry --")
    print(">>> nobody's work vanished without a trace.\n")

    history = sc.conflict_history("customer-9981")
    print(f"[audit trail] {len(history)} conflict(s) logged for this object:")
    for h in history:
        print(f"   - agent '{h['agent_id']}' tried version {h['expected_version']}, "
              f"actual was {h['actual_version']} (attempt logged at {h['occurred_at']})")


def scenario_lease():
    """Show the lease mechanism preventing a second agent from even starting
    work on an object someone else has claimed."""
    print("\n" + "=" * 70)
    print("SCENARIO C: leasing -- stop the second agent before it even starts")
    print("=" * 70)
    sc = StateStore()
    sc.create("customer-1122", {"status": "pending_review"})

    sc.claim("customer-1122", "langgraph-agent-A", ttl_seconds=5)
    print("[Agent A] claimed lease on customer-1122 for 5s")

    try:
        sc.claim("customer-1122", "crewai-agent-B", ttl_seconds=5)
    except Exception as e:
        print(f"[Agent B] tried to claim same object -> BLOCKED: {e}")

    print("[wait] lease expiring in 5s so a crashed agent can't block forever...")
    time.sleep(5.2)
    obj = sc.claim("customer-1122", "crewai-agent-B", ttl_seconds=5)
    print(f"[Agent B] claim retried after expiry -> SUCCESS, now holds lease (v{obj.version})")


if __name__ == "__main__":
    reset_db()
    scenario_naive_race()
    print()
    scenario_statestore_protected()
    scenario_lease()

"""
Simulates a LangGraph-style agent. Critically: this file NEVER imports
state_store.py or action_ledger.py. It only speaks plain HTTP -- proving
this could just as easily be a Python, JS, Rust, or curl-based agent from
any framework, not just something wired into our own Python library.
"""
import sys
import requests

BASE = "http://localhost:8000"


def run():
    # Read the shared object (simulates: agent starts its turn, checks state)
    r = requests.get(f"{BASE}/objects/customer-5501")
    starting = r.json()
    print(f"[langgraph-agent] read customer-5501 @ version {starting['version']}")

    update = dict(starting["value"])
    update["notes"] = update.get("notes", []) + ["LangGraph agent: verified billing"]
    update["status"] = "billing_verified"

    r = requests.post(
        f"{BASE}/objects/customer-5501/write",
        json={"agent_id": "langgraph-agent-A", "new_value": update,
              "expected_version": starting["version"]},
    )
    if r.status_code == 200:
        print(f"[langgraph-agent] WRITE SUCCESS -> version {r.json()['version']}")
    else:
        print(f"[langgraph-agent] WRITE REJECTED -> {r.json()}")


if __name__ == "__main__":
    run()

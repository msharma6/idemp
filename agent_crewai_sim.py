"""
Simulates a CrewAI-style agent. Also never imports our Python code --
plain HTTP only, same as the LangGraph simulator. Two different "frameworks"
(here, two different scripts, deliberately never sharing code) coordinating
through the network service instead of a shared in-process orchestrator.
"""
import time
import requests

BASE = "http://localhost:8000"


def run():
    r = requests.get(f"{BASE}/objects/customer-5501")
    starting = r.json()
    print(f"[crewai-agent] read customer-5501 @ version {starting['version']}")

    # Simulate doing real work (an LLM call, a tool call) that takes a moment --
    # this is what creates the real race window in production.
    time.sleep(0.15)

    update = dict(starting["value"])
    update["notes"] = update.get("notes", []) + ["CrewAI agent: flagged fraud risk"]
    update["status"] = "fraud_review"

    r = requests.post(
        f"{BASE}/objects/customer-5501/write",
        json={"agent_id": "crewai-agent-B", "new_value": update,
              "expected_version": starting["version"]},
    )
    if r.status_code == 200:
        print(f"[crewai-agent] WRITE SUCCESS -> version {r.json()['version']}")
    else:
        print(f"[crewai-agent] WRITE REJECTED -> {r.json()}")


if __name__ == "__main__":
    run()

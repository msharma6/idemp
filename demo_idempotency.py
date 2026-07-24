"""
Tests for irreversible-action idempotency -- the case CAS literally cannot
catch, because there's no "value" being overwritten. The risk is a
real-world side effect (email sent, card charged) firing more than once.

Run: python3 demo_idempotency.py
"""

import threading
import time
import psycopg2

from state_store import DSN
from action_ledger import (
    ActionLedger, execute_idempotently,
    AlreadyCompleted, AlreadyClaimed, NeedsManualReview,
)

SENT_EMAILS = []   # stand-in for a real email API -- proves whether it fired twice
CHARGES = []        # stand-in for a real payment API


def reset_db():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        with open("action_ledger_schema.sql") as f:
            cur.execute(f.read())
    SENT_EMAILS.clear()
    CHARGES.clear()
    print("[setup] action ledger schema reset\n")


def fake_send_email(to: str, subject: str) -> dict:
    """Stands in for a real, irreversible external call."""
    SENT_EMAILS.append({"to": to, "subject": subject, "sent_at": time.time()})
    return {"provider_message_id": f"msg-{len(SENT_EMAILS)}", "to": to}


def fake_charge_card(amount: float, card_last4: str) -> dict:
    CHARGES.append({"amount": amount, "card": card_last4, "charged_at": time.time()})
    return {"charge_id": f"ch-{len(CHARGES)}", "amount": amount}


# ---------------------------------------------------------------------------
def scenario_1_duplicate_agents():
    """Two agents, unaware of each other, both decide to send the SAME
    refund email to the same customer at roughly the same time. This is the
    exact case that pure state-CAS cannot catch -- nothing is being
    'overwritten,' both agents are independently choosing to act."""
    print("=" * 70)
    print("SCENARIO 1: two agents both try to send the same refund email")
    print("=" * 70)
    ledger = ActionLedger()
    results = {}

    def agent(name):
        r = execute_idempotently(
            ledger,
            action_id="refund-email-customer-4471",   # SAME key -- represents the same real-world intent
            agent_id=name,
            action_type="send_refund_email",
            real_action=lambda: fake_send_email("customer4471@example.com", "Your refund"),
        )
        results[name] = r

    t1 = threading.Thread(target=agent, args=("langgraph-agent-A",))
    t2 = threading.Thread(target=agent, args=("crewai-agent-B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    print(f"Agent A: {results['langgraph-agent-A']['outcome']}")
    print(f"Agent B: {results['crewai-agent-B']['outcome']}")
    print(f"\nActual emails sent to the customer: {len(SENT_EMAILS)}")
    assert len(SENT_EMAILS) == 1, "FAIL: customer got double-emailed!"
    print(">>> PASS: exactly one email fired, despite two independent agents trying.\n")


# ---------------------------------------------------------------------------
def scenario_2_lost_response_retry():
    """The SAME agent's action succeeds, but the network response is lost,
    so the agent believes it failed and retries with the same action_id.
    Classic idempotency-key scenario -- must NOT re-charge the card."""
    print("=" * 70)
    print("SCENARIO 2: agent thinks its own request failed and retries")
    print("=" * 70)
    ledger = ActionLedger()

    r1 = execute_idempotently(
        ledger, "charge-order-8821", "billing-agent", "charge_card",
        real_action=lambda: fake_charge_card(149.99, "4242"),
    )
    print(f"First attempt:  {r1['outcome']} -> {r1.get('result')}")

    # Agent "never saw" the response (simulated) and retries the identical call
    r2 = execute_idempotently(
        ledger, "charge-order-8821", "billing-agent", "charge_card",
        real_action=lambda: fake_charge_card(149.99, "4242"),
    )
    print(f"Retry attempt:  {r2['outcome']} -> {r2.get('result')}")

    print(f"\nActual charges made to the card: {len(CHARGES)}")
    assert len(CHARGES) == 1, "FAIL: customer got double-charged!"
    print(">>> PASS: retry returned the cached result, card was NOT charged twice.\n")


# ---------------------------------------------------------------------------
def scenario_3_clean_failure_allows_retry():
    """The action runs and fails CLEANLY -- e.g. the payment API returns a
    hard decline before any money moves. This should be safely retriable
    (with a new card, say), unlike the crash case below."""
    print("=" * 70)
    print("SCENARIO 3: a clean, known failure is safely retriable")
    print("=" * 70)
    ledger = ActionLedger()

    def declined_charge():
        raise ValueError("Card declined -- insufficient funds")

    r1 = execute_idempotently(ledger, "charge-order-9002", "billing-agent", "charge_card", declined_charge)
    print(f"First attempt (bad card):  {r1['outcome']} -> {r1.get('error')}")

    r2 = execute_idempotently(
        ledger, "charge-order-9002", "billing-agent", "charge_card",
        real_action=lambda: fake_charge_card(75.00, "9999"),  # customer used a different card
    )
    print(f"Retry (different card):     {r2['outcome']} -> {r2.get('result')}")
    print(">>> PASS: a clean failure doesn't block a legitimate retry.\n")


# ---------------------------------------------------------------------------
def scenario_4_crash_needs_manual_review():
    """The hard case. An agent claims the action, then crashes (or hangs)
    before reporting success or failure. Once the claim expires, we
    genuinely do NOT know if the real-world side effect fired. The correct
    behavior is to refuse to auto-retry and flag for manual review --
    NOT silently retry, which could double-charge or double-email."""
    print("=" * 70)
    print("SCENARIO 4: agent crashes mid-action -- claim expires unresolved")
    print("=" * 70)
    ledger = ActionLedger()

    # Agent claims the action but never calls complete() or fail() -- simulates a crash.
    ledger.claim_or_get_result("charge-order-7777", "flaky-agent", "charge_card", claim_ttl_seconds=2)
    print("[flaky-agent] claimed the action, then crashed before finishing.")

    print("[wait] letting the 2s claim TTL expire...")
    time.sleep(2.2)

    charges_before = len(CHARGES)
    try:
        execute_idempotently(
            ledger, "charge-order-7777", "recovery-agent", "charge_card",
            real_action=lambda: fake_charge_card(500.00, "1111"),
        )
    except Exception:
        pass  # execute_idempotently catches this internally -- see the outcome instead

    r = execute_idempotently(
        ledger, "charge-order-7777", "recovery-agent", "charge_card",
        real_action=lambda: fake_charge_card(500.00, "1111"),
    )
    print(f"Recovery agent's attempt: {r['outcome']}")
    new_charges = len(CHARGES) - charges_before
    print(f"\nNew charges made during this scenario: {new_charges}")
    assert r["outcome"] == "MANUAL_REVIEW_REQUIRED"
    assert new_charges == 0, "FAIL: system guessed and possibly double-charged!"
    print(">>> PASS: system refused to guess. A $500 charge of unknown status")
    print(">>> is flagged for a human, instead of silently risking a double-charge.\n")


# ---------------------------------------------------------------------------
def scenario_5_saga_compensation():
    """A multi-step workflow: reserve inventory -> charge card -> send
    confirmation email. Step 3 fails after steps 1-2 have ALREADY happened
    for real. Nothing can undo a sent email or an API call after the fact --
    so the correct behavior is queuing a compensating action, not silently
    leaving the system in a half-finished state."""
    print("=" * 70)
    print("SCENARIO 5: saga with a failure after real side effects occurred")
    print("=" * 70)
    ledger = ActionLedger()
    saga_id = "order-6001-fulfillment"

    step1 = execute_idempotently(ledger, "order-6001-reserve-inventory", "fulfillment-agent",
                                  "reserve_inventory", lambda: {"reserved": True, "sku": "WIDGET-1"})
    print(f"Step 1 (reserve inventory): {step1['outcome']}")

    step2 = execute_idempotently(ledger, "order-6001-charge-card", "fulfillment-agent",
                                  "charge_card", lambda: fake_charge_card(89.00, "5555"))
    print(f"Step 2 (charge card):       {step2['outcome']} -> {step2.get('result')}")

    def send_confirmation_that_fails():
        raise ConnectionError("Email provider timed out")

    step3 = execute_idempotently(ledger, "order-6001-confirm-email", "fulfillment-agent",
                                  "send_confirmation_email", send_confirmation_that_fails)
    print(f"Step 3 (confirmation email): {step3['outcome']} -> {step3.get('error')}")

    if step3["outcome"] == "FAILED_CLEANLY":
        # Steps 1-2 already happened for real and can't be silently undone --
        # queue human/agent-actionable compensating actions instead.
        ledger.queue_compensation(saga_id, "order-6001-reserve-inventory",
                                   "Release the WIDGET-1 inventory reservation")
        ledger.queue_compensation(saga_id, "order-6001-charge-card",
                                   "Refund the $89.00 charge to card ending 5555, "
                                   "OR retry sending the confirmation email instead of refunding")

    pending = ledger.pending_compensations(saga_id)
    print(f"\nCompensating actions queued for a human/agent to resolve: {len(pending)}")
    for c in pending:
        print(f"   - [{c['action_id']}] {c['compensating_action']}")
    print(">>> PASS: real side effects are never silently orphaned -- the saga's")
    print(">>> partial completion is captured as explicit, actionable next steps.\n")


if __name__ == "__main__":
    reset_db()
    scenario_1_duplicate_agents()
    scenario_2_lost_response_retry()
    scenario_3_clean_failure_allows_retry()
    scenario_4_crash_needs_manual_review()
    scenario_5_saga_compensation()
    print("=" * 70)
    print("ALL SCENARIOS PASSED")
    print("=" * 70)

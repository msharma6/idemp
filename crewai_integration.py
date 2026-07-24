"""
Real CrewAI integration test -- imports the actual crewai package and
defines a real crewai.tools.BaseTool subclass, not a mock. This is what
a CrewAI agent would actually be given as a tool in its toolset.

This directly targets the exact pain from CrewAI Issue #5802: "tool
re-execution on task retry has no idempotency guard." This tool IS that
guard, wired as a genuine CrewAI Tool.
"""
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from idemp_sdk import IdempClient, ManualReviewRequired, InProgress

client = IdempClient()
EMAIL_SEND_LOG: list[dict] = []  # proof instrument -- a real email API isn't observable from here


class SendRefundEmailInput(BaseModel):
    customer_id: str = Field(..., description="The customer to refund")
    amount: float = Field(..., description="Refund amount in USD")


class SendRefundEmailTool(BaseTool):
    """A real CrewAI Tool. If CrewAI retries the task this tool was called
    from (per #5802 and #2881 -- both real, filed CrewAI issues), this
    tool will NOT re-send the email or double-process the refund. It will
    return the cached result from the first successful attempt instead."""

    name: str = "send_refund_email"
    description: str = (
        "Sends a refund confirmation email to a customer. Safe to call "
        "even if this task is retried -- will not send a duplicate email."
    )
    args_schema: type[BaseModel] = SendRefundEmailInput

    def _run(self, customer_id: str, amount: float) -> str:
        action_id = f"refund-email-{customer_id}"  # stable key = the real-world intent

        def actually_send_email() -> dict:
            # Stand-in for a real email API call -- instrumented with a
            # shared log so we can PROVE it only runs once, not just trust it.
            EMAIL_SEND_LOG.append({"to": customer_id, "amount": amount})
            return {"to": customer_id, "amount": amount, "status": "sent"}

        sends_before = len(EMAIL_SEND_LOG)
        try:
            result = client.run_irreversible(
                action_id=action_id,
                agent_id="crewai-refund-tool",
                action_type="send_refund_email",
                fn=actually_send_email,
            )
            sends_after = len(EMAIL_SEND_LOG)
            tag = "FRESH SEND" if sends_after > sends_before else "CACHED -- NOT RE-SENT"
            return f"[{tag}] Refund email to {customer_id}: {result} (total real sends so far: {sends_after})"
        except ManualReviewRequired as e:
            return (f"STOPPED -- a prior attempt at this refund crashed and its "
                    f"outcome is unknown. Escalating to a human instead of "
                    f"risking a duplicate refund. Detail: {e}")
        except InProgress as e:
            return f"STOPPED -- this refund is already being processed elsewhere: {e}"


if __name__ == "__main__":
    tool = SendRefundEmailTool()
    print(f"[CrewAI Tool] name={tool.name!r}")
    print("[CrewAI Tool] first call: ", tool.run(customer_id="cust-9001", amount=49.99))
    print("[CrewAI Tool] simulated retry (same task, same customer):",
          tool.run(customer_id="cust-9001", amount=49.99))
    print(f"\n>>> PROOF: actual email API was called {len(EMAIL_SEND_LOG)} time(s) "
          f"despite the tool being invoked twice.")
    assert len(EMAIL_SEND_LOG) == 1, "FAIL: customer would have been double-emailed!"

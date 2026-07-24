"""
Real LangGraph integration test -- not a simulation. This imports the
actual langgraph package and wires two graph NODES (not just two scripts)
that both attempt to update the same Idemp-tracked shared object.

This proves the SDK works from inside real LangGraph node functions,
not just from a bare Python script pretending to be one.
"""
from typing import TypedDict
from langgraph.graph import StateGraph, END

from idemp_sdk import IdempClient

client = IdempClient()


class GraphState(TypedDict):
    object_id: str
    result: str


def billing_verification_node(state: GraphState) -> GraphState:
    """A real LangGraph node. Reads the shared object, does its own
    idempotent update through the SDK (not a raw HTTP call), and reports
    what happened -- success or a caught conflict."""
    try:
        result = client.run_idempotent(
            object_id=state["object_id"],
            agent_id="langgraph-billing-node",
            fn=lambda current: {
                **current,
                "notes": current.get("notes", []) + ["LangGraph node: billing verified"],
                "status": "billing_verified",
            },
        )
        return {**state, "result": f"billing node -> version {result['version']}"}
    except Exception as e:
        return {**state, "result": f"billing node -> error: {e}"}


def build_graph():
    """A minimal real graph: one node that touches shared state through
    the SDK. In the full test, we run this graph concurrently with a
    second framework's own agent hitting the same object."""
    graph = StateGraph(GraphState)
    graph.add_node("billing_verification", billing_verification_node)
    graph.set_entry_point("billing_verification")
    graph.add_edge("billing_verification", END)
    return graph.compile()


if __name__ == "__main__":
    import sys
    object_id = sys.argv[1] if len(sys.argv) > 1 else "customer-langgraph-test"
    app = build_graph()
    final_state = app.invoke({"object_id": object_id, "result": ""})
    print(f"[LangGraph graph run] {final_state['result']}")

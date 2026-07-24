"""
Direct test of the MCP server's tool functions -- confirms the MCP
integration surface works correctly, independent of connecting an actual
MCP client. Exists as a real file (not an inline snippet) for the same
reason as reset_db.py: multi-line commands don't work reliably typed
directly into Windows cmd/PowerShell.

Run: py -3.12 test_mcp.py  (with the service already running)
"""
from mcp_server import (
    create_shared_object, read_shared_object, write_shared_object,
    perform_irreversible_action, confirm_action_complete, get_conflict_history,
)

print(create_shared_object('customer-mcp-test', {'status': 'pending', 'notes': []}))
print(read_shared_object('customer-mcp-test'))
print(write_shared_object('customer-mcp-test', 'mcp-agent-1', {'status': 'verified'}, 0))
print(write_shared_object('customer-mcp-test', 'mcp-agent-2', {'status': 'fraud'}, 0))  # should be REJECTED

print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-1', 'send_refund_email', 'test'))
print(confirm_action_complete('mcp-refund-1', {'sent': True}))
print(perform_irreversible_action('mcp-refund-1', 'mcp-agent-2', 'send_refund_email', 'test'))  # should say ALREADY DONE

print(get_conflict_history('customer-mcp-test'))

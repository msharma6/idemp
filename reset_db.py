"""
Reset the Idemp database schema to a clean state. Exists as a real script
file (not an inline python -c snippet) specifically because multi-line
-c commands don't work in Windows cmd/PowerShell -- each line gets
interpreted as a separate terminal command instead of Python code.
"""
import psycopg2
from state_store import DSN

with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
    with open('state_schema.sql') as f:
        cur.execute(f.read())
    with open('action_ledger_schema.sql') as f:
        cur.execute(f.read())

print('schemas reset clean')

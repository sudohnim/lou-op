## Iteration 1 — ✓
**Files:** Done. Implementation:

1. **`lou_op/audit.py`** — `AuditLog` class:
   - `__init__(root: Path)` writes to `<root>/.lou-op/audit.jsonl`
   - `record(event, data)` appends JSON lines with ISO-8601 UTC timestamp
   - Creates parent dirs on first write

2. **`lou_op/backends/native_agent.py`** — audit integration:
   - Imports `AuditLog`
   - Creates log instance in `run_iteration` from `ctx.repo_path`
   - Records `tool_call` (name + args) before each tool execution
   - Records `tool_result` (name + first line of result) after each tool execution

All 14 tests pass.
**Validators:** PASS: python -m pytest tests/test_audit.py tests/test_native_agent.py -q

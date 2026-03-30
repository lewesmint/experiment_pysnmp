# experiment_pysnmp Report

## Overview
`experiment_pysnmp` is a manager-side SNMP experimentation harness focused on validating mailbox-driven request flow and trap handling against `snmp-sim`.

## Main Components
- `manager_app.py`: interactive/scriptable manager harness and assertion commands.
- `trap_thread.py`: long-lived worker that owns SNMP I/O, request correlation, timeouts, and trap classification.
- `snmp_manager.py`: per-manager request state container.
- `integration_test.txt`: end-to-end command script used by the harness.
- `run_integration_test.py`: runner that starts `snmp-sim`, waits for readiness, executes script, then shuts down.

## Implemented Behavior
- Manager mailbox flow: `open`, `get`, `set`, `close`.
- GET and SET verification with result-code assertions.
- Agent-log assertions through REST debug endpoints.
- Trap assertions for three classes:
  - completion trap
  - event trap
  - regular trap (classified as value-change)

## Current Validation Status
- End-to-end integration run: PASS.
- Machine-readable summary emitted by manager script:
  - `INTEGRATION_SUMMARY status=PASS assertions_failed=0`
  - or `INTEGRATION_SUMMARY status=FAIL assertions_failed=<N>`

## Typical Run Command
From this folder:

```bash
.venv/bin/python run_integration_test.py
```

## Important Notes
- The integration runner starts and stops `snmp-sim` automatically.
- Trap send commands in `manager_app.py` send traps directly via SNMP and do not depend on optional trap-catalog REST routes.
- The generated MIB Python module is kept out of static-checking scope (mypy/pyright/pylint excludes), since it is compiler output.

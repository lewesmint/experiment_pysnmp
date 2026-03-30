# Integration Test Harness Report

## Scope
This report explains how the end-to-end integration harness works for:
- manager mailbox flow (open/get/set/close)
- SNMP simulator process lifecycle
- trap validation for completion, event, and regular (value-change classified) traps

## Files Involved
- `run_integration_test.py`
- `integration_test.txt`
- `manager_app.py`
- `trap_thread.py`

## Execution Pipeline
1. `run_integration_test.py` starts `snmp-sim/run_agent_with_rest.py` in a subprocess.
2. It waits until both endpoints are reachable:
   - REST TCP: `127.0.0.1:8800`
   - SNMP UDP: `127.0.0.1:11161`
3. It launches `manager_app.py` in script mode:
   - `--startup-listen`
   - `--script integration_test.txt`
   - `--agent-url http://127.0.0.1:8800`
4. After script completion, it sends `SIGINT` to stop the simulator and exits with the manager script return code.

## Script Command Flow (`integration_test.txt`)
### Setup
- `clear-agent-log`
- `listen`
- `new mgr1 127.0.0.1 11161 162 public private`
- `open mgr1`
- `sleep 0.5`

### Test 1: GET round-trip
- `assert-get mgr1 1.3.6.1.2.1.1.1.0 "" 3.0`
- `assert-result mgr1 RESULT_OK`
- `assert-agent-saw get 1.3.6.1.2.1.1.1.0`

### Test 2: SET round-trip
- `setstr mgr1 1.3.6.1.2.1.1.6.0 integration-test-room 2.0`
- `assert-result mgr1 RESULT_OK`
- `assert-agent-saw set 1.3.6.1.2.1.1.6.0 integration-test-room`
- `assert-get mgr1 1.3.6.1.2.1.1.6.0 integration-test-room 3.0`
- `assert-result mgr1 RESULT_OK`

### Test 3: Completion trap
- `cleartraps`
- `setstr mgr1 1.3.6.1.2.1.1.6.0 model-normal 2.0`
- `assert-result mgr1 RESULT_OK`
- `wait-trap completion 5.0`

### Test 4: Event trap
- `cleartraps`
- `setstr mgr1 1.3.6.1.2.1.1.6.0 alarm-major 2.0`
- `assert-result mgr1 RESULT_OK`
- `wait-trap completion 5.0`
- `wait-trap event 5.0`

### Test 5: Regular trap
- `cleartraps`
- `send-regular-trap 127.0.0.1 162`
- `wait-trap value-change 5.0`

### Teardown
- `close mgr1`
- `stoplisten`
- `quit`

## Trap Classification Rules
Trap kind is determined in `trap_thread.py` using trap OID:
- Completion trap OID: `1.3.6.1.4.1.99998.0.2`
- Event trap OID: `1.3.6.1.4.1.99998.0.3`
- Any other trap OID -> classified as value-change trap

## Behavior Model Ownership
Trap generation for completion/event is now in the simulator agent runtime:
- On successful SNMP SET commit, the agent emits a completion trap.
- If the SET transitions a value from non-alarm to alarm-like state, the agent emits an event trap.

The manager harness no longer auto-emits modeled completion/event traps after SET, so integration results reflect true agent behavior.

## Machine-Readable Summary Output
At the end of script execution, `manager_app.py` now emits one of:
- `INTEGRATION_SUMMARY status=PASS assertions_failed=0`
- `INTEGRATION_SUMMARY status=FAIL assertions_failed=<N>`

This line is intended for CI parsing.

## Pass/Fail Contract
- PASS:
  - all assertions and trap waits succeed
  - manager exits code `0`
  - `run_integration_test.py` exits code `0`
- FAIL:
  - any assertion fails or trap wait times out
  - manager exits code `1`
  - `run_integration_test.py` exits code `1`

## Current Status
Latest run completed with:
- `=== RESULT: PASS ===`
- `INTEGRATION_SUMMARY status=PASS assertions_failed=0`

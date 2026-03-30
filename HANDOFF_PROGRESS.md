# PySNMP Modernization Handoff

Date: 2026-03-30

## Goal
Replace legacy PySNMP asyncsock-based threading/dispatcher code with a modern implementation that preserves legacy behavior, then build a manager-side harness to drive it and validate mailbox/trap flows.

## What Is Implemented

### 1) Legacy behavior replacement
- Implemented TrapThread runtime in [trap_thread.py](trap_thread.py).
- Preserved mailbox-driven model:
  - OPEN, CLOSE, GET, SET command handling
  - asynchronous response handling and request-id matching
  - timer-based timeout handling
  - background thread ownership of SNMP I/O
- Implemented trap classification logic:
  - completion trap OID -> COMPLETION_TRAP
  - event trap OID -> EVENT_TRAP
  - all other trap OIDs -> VALUE_CHANGE_TRAP
- Added module singleton: the_trap_thread

### 2) SNMPManager moved to separate module
- Extracted SNMPManager into [snmp_manager.py](snmp_manager.py).
- Updated imports in [trap_thread.py](trap_thread.py) and [manager_app.py](manager_app.py).
- Backward usage validated through runtime tests.

### 3) Manager-side test harness
- Built interactive manager harness in [manager_app.py](manager_app.py).
- Supports manager lifecycle and mailbox commands:
  - new, open, close
  - get, setint, setstr
  - listen, stoplisten
  - status, list, cleartraps
- Added non-interactive script execution:
  - --script <path>
  - line-by-line command processing
  - comments and blank lines ignored
  - added sleep command for deterministic scripted timing

### 4) Test script
- Created [manager_script_example.txt](manager_script_example.txt).
- Current target manager endpoint is 127.0.0.1:11161.

### 5) MIB work
- Added and validated MIB with completion and event notifications:
  - [TEST-ENUM-MIB.mib](TEST-ENUM-MIB.mib)
- Compiled successfully to:
  - [TEST-ENUM-MIB.py](TEST-ENUM-MIB.py)

## Important Runtime Fixes Applied

### Trap/manager port conflict fix
- TrapThread previously crashed when a manager socket bind conflicted with trap listener bind.
- Updated behavior:
  - manager request socket always binds to ephemeral local source port
  - trap listener uses configured trap port logic
  - trap bind failure is handled without crashing the thread

## Validation Completed

### Lint and type/syntax checks
- Pylint status reached 10.00/10 for active modules:
  - [trap_thread.py](trap_thread.py)
  - [manager_app.py](manager_app.py)
  - [snmp_manager.py](snmp_manager.py)
- Compile checks passed with py_compile during development.

### Functional scripted run
- Script mode was run successfully using [manager_script_example.txt](manager_script_example.txt).
- GET against port 11161 returned RESULT_OK in recent verification.
- SET operations returned RESULT_NOT_SENT in the observed run (depends on external target write support).

## Commands That Work

### Run manager app interactively
python manager_app.py --startup-listen

### Run manager app from script
python manager_app.py --startup-listen --script manager_script_example.txt

### Compile MIB locally (offline base MIB source)
.venv/bin/mibdump --mib-source=file://$PWD --mib-source=file:///usr/share/snmp/mibs --destination-directory=$PWD TEST-ENUM-MIB

## Known Behavior / Assumptions
- Community string is fixed to public in TrapThread.
- Trap classification is based on snmpTrapOID.0 value in varbind index 1 for SNMPv2 trap PDUs.
- Value-change traps are currently the fallback class for unknown trap OIDs.

## Suggested Next Actions For New Agent
1. Confirm whether SET should map target-side SNMP errors to a more detailed result code model than RESULT_NOT_SENT.
2. Optionally centralize constants (mailbox/result/trap constants) into a dedicated shared constants module.
3. Add automated tests that simulate:
   - response correlation by request ID
   - timeout path
   - completion/event/unknown trap classification
4. Confirm production trap listener port and community string configuration requirements.

## Key Files
- [trap_thread.py](trap_thread.py)
- [snmp_manager.py](snmp_manager.py)
- [manager_app.py](manager_app.py)
- [manager_script_example.txt](manager_script_example.txt)
- [TEST-ENUM-MIB.mib](TEST-ENUM-MIB.mib)
- [TEST-ENUM-MIB.py](TEST-ENUM-MIB.py)

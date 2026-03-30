"""Interactive manager-side test app for trap_thread.

This app does not emulate an SNMP agent. It acts as a manager harness:
- creates managers
- enqueues mailbox commands (open/get/set/close)
- starts/stops trap listening
- prints classified traps received by TrapThread

Use your external application to generate traps and to respond to GET/SET.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from pyasn1.type.univ import Integer, OctetString
from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore[import-untyped]
    CommunityData,
    ContextData,
    NotificationType,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    send_notification,
)
from pysnmp.proto.rfc1902 import ObjectName

from snmp_manager import SNMPManager
from trap_thread import (
    COMPLETION_TRAP,
    EVENT_TRAP,
    MAILBOX_CLOSE,
    MAILBOX_GET,
    MAILBOX_OPEN,
    MAILBOX_SET,
    RESULT_NOT_SENT,
    RESULT_OK,
    RESULT_TIMEOUT,
    Trap,
    the_trap_thread,
)


logger = logging.getLogger(__name__)


@dataclass
class ManagerRecord:
    """Named container for a manager instance."""

    name: str
    manager: SNMPManager


class ManagerApp:
    """Interactive manager harness that drives TrapThread via mailbox commands."""

    def __init__(self, agent_url: str = "http://127.0.0.1:8800") -> None:
        self._records: dict[str, ManagerRecord] = {}
        self._stop_event = threading.Event()
        self._trap_printer = threading.Thread(target=self._trap_watch_loop, daemon=True)
        self._trap_printer_started = False
        self._agent_url = agent_url.rstrip("/")
        self._assert_failures = 0

    def _start_background_workers(self) -> None:
        if not self._trap_printer_started:
            self._trap_printer.start()
            self._trap_printer_started = True

    @property
    def has_assert_failures(self) -> bool:
        """True when one or more script assertions have failed."""
        return self._assert_failures > 0

    def run(self) -> None:
        """Run the interactive command loop until user exits."""
        logger.info("Manager test app ready. Type 'help' for commands.")
        self._start_background_workers()

        try:
            while True:
                try:
                    line = input("manager> ").strip()
                except EOFError:
                    logger.info("")
                    break
                if not line:
                    continue
                if line in {"exit", "quit"}:
                    break
                self._handle_line(line)
        finally:
            self.shutdown()

    def run_script(self, script_path: str) -> None:
        """Execute manager commands from a script file."""
        path = Path(script_path)
        if not path.exists():
            logger.error("Script file not found: %s", path)
            return

        logger.info("Running script: %s", path)
        self._start_background_workers()

        self._assert_failures = 0
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                logger.info("manager(script)> %s", line)
                if line in {"exit", "quit"}:
                    break
                self._handle_line(line)
        finally:
            self.shutdown()

        if self._assert_failures == 0:
            logger.info("\n=== RESULT: PASS ===")
            logger.info("INTEGRATION_SUMMARY status=PASS assertions_failed=0")
        else:
            logger.error(
                "\n=== RESULT: FAIL (%s assertion(s) failed) ===",
                self._assert_failures,
            )
            logger.error(
                "INTEGRATION_SUMMARY status=FAIL assertions_failed=%s",
                self._assert_failures,
            )

    def shutdown(self) -> None:
        """Gracefully close managers, stop trap reception and stop TrapThread."""
        self._stop_event.set()
        for record in list(self._records.values()):
            record.manager.msg_type = MAILBOX_CLOSE
            the_trap_thread.mailbox.put(record.manager)
        time.sleep(0.05)
        the_trap_thread.stop_trap_receiver()
        the_trap_thread.close()
        logger.info("Shutdown complete.")

    def _handle_line(self, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as ex:
            logger.error("Parse error: %s", ex)
            return

        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        handlers: dict[str, Callable[[list[str]], None]] = {
            "help": lambda _unused: self._print_help(),
            "listen": self._cmd_listen,
            "stoplisten": self._cmd_stoplisten,
            "new": self._cmd_new,
            "open": self._cmd_open,
            "close": self._cmd_close,
            "get": self._cmd_get,
            "setint": self._cmd_setint,
            "setstr": self._cmd_setstr,
            "status": self._cmd_status,
            "list": self._cmd_list,
            "cleartraps": self._cmd_cleartraps,
            "sleep": self._cmd_sleep,
            # --- test harness assertions ---
            "agent-url": self._cmd_agent_url,
            "clear-agent-log": self._cmd_clear_agent_log,
            "assert-agent-saw": self._cmd_assert_agent_saw,
            "assert-get": self._cmd_assert_get,
            "assert-result": self._cmd_assert_result,
            "wait-trap": self._cmd_wait_trap,
            "send-completion-trap": self._cmd_send_completion_trap,
            "send-event-trap": self._cmd_send_event_trap,
            "send-regular-trap": self._cmd_send_regular_trap,
        }
        handler = handlers.get(cmd)
        if handler is None:
            logger.warning("Unknown command: %s", cmd)
            return
        handler(args)

    def _print_help(self) -> None:
        print("Commands:")
        print("  help")
        print("  listen")
        print("  stoplisten")
        print("  new <name> [ip] [dest_port] [send_port]")
        print("  open <name>")
        print("  close <name>")
        print("  get <name> <oid> [timeout_sec]")
        print("  setint <name> <oid> <int_value> [timeout_sec]")
        print("  setstr <name> <oid> <string_value> [timeout_sec]")
        print("  status <name>")
        print("  list")
        print("  cleartraps")
        print("  sleep <seconds>")
        print("  quit | exit")
        print("-- test harness --")
        print("  agent-url <url>")
        print("  clear-agent-log")
        print("  assert-agent-saw <get|getnext|set> <oid> [expected_value]")
        print("  assert-get <name> <oid> <expected_value> [timeout_sec]")
        print("  assert-result <name> <RESULT_OK|RESULT_TIMEOUT|RESULT_NOT_SENT>")
        print("  wait-trap <completion|event|value-change|any> <timeout_sec>")
        print(
            "  send-completion-trap [source] [code] [dest_host] [dest_port]"
        )
        print(
            "  send-event-trap [severity] [text] [dest_host] [dest_port]"
        )
        print("  send-regular-trap [dest_host] [dest_port]")

    def _cmd_listen(self, _args: list[str]) -> None:
        the_trap_thread.start_trap_receiver()
        logger.info("Trap receiver started.")

    def _cmd_stoplisten(self, _args: list[str]) -> None:
        the_trap_thread.stop_trap_receiver()
        logger.info("Trap receiver stopped.")

    def _cmd_new(self, args: list[str]) -> None:
        if len(args) < 1:
            logger.warning(
                "Usage: new <name> [ip] [dest_port] [send_port] "
                "[read_community] [write_community]"
            )
            return

        name = args[0]
        ip = args[1] if len(args) >= 2 else "127.0.0.1"
        dest_port = int(args[2]) if len(args) >= 3 else 161
        send_port = int(args[3]) if len(args) >= 4 else 0
        read_community = args[4].encode() if len(args) >= 5 else b"public"
        write_community = args[5].encode() if len(args) >= 6 else b"public"

        mgr = SNMPManager(ip, dest_port, send_port, read_community, write_community)
        self._records[name] = ManagerRecord(name=name, manager=mgr)
        logger.info(
            "Created manager '%s' -> %s:%s, send_port=%s, "
            "read_community=%r, write_community=%r",
            name,
            ip,
            dest_port,
            send_port,
            read_community,
            write_community,
        )

    def _cmd_open(self, args: list[str]) -> None:
        mgr = self._require_manager(args, "open <name>")
        if mgr is None:
            return
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_OPEN
        the_trap_thread.mailbox.put(mgr)
        logger.info("Open enqueued.")

    def _cmd_close(self, args: list[str]) -> None:
        mgr = self._require_manager(args, "close <name>")
        if mgr is None:
            return
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_CLOSE
        the_trap_thread.mailbox.put(mgr)
        logger.info("Close enqueued.")

    def _cmd_get(self, args: list[str]) -> None:
        if len(args) < 2:
            logger.warning("Usage: get <name> <oid> [timeout_sec]")
            return

        name, oid_text = args[0], args[1]
        timeout = float(args[2]) if len(args) >= 3 else 5.0

        record = self._records.get(name)
        if record is None:
            logger.warning("Unknown manager: %s", name)
            return

        mgr = record.manager
        mgr.timeout = timeout
        mgr.var_bind_sequence = [(ObjectName(oid_text), None)]
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_GET
        the_trap_thread.mailbox.put(mgr)

        if mgr.completion_event.wait(timeout + 0.5):
            logger.info(self._format_result(mgr))
        else:
            logger.warning("GET: no completion event observed")

    def _cmd_setint(self, args: list[str]) -> None:
        if len(args) < 3:
            logger.warning("Usage: setint <name> <oid> <int_value> [timeout_sec]")
            return

        name, oid_text, int_value_text = args[0], args[1], args[2]
        timeout = float(args[3]) if len(args) >= 4 else 5.0

        record = self._records.get(name)
        if record is None:
            logger.warning("Unknown manager: %s", name)
            return

        int_value = int(int_value_text)
        mgr = record.manager
        mgr.timeout = timeout
        mgr.var_bind_sequence = [(ObjectName(oid_text), Integer(int_value))]
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_SET
        the_trap_thread.mailbox.put(mgr)

        if mgr.completion_event.wait(timeout + 0.5):
            logger.info(self._format_result(mgr))
        else:
            logger.warning("SET: no completion event observed")

    def _cmd_setstr(self, args: list[str]) -> None:
        if len(args) < 3:
            logger.warning("Usage: setstr <name> <oid> <string_value> [timeout_sec]")
            return

        name, oid_text, str_value = args[0], args[1], args[2]
        timeout = float(args[3]) if len(args) >= 4 else 5.0

        record = self._records.get(name)
        if record is None:
            logger.warning("Unknown manager: %s", name)
            return

        mgr = record.manager
        mgr.timeout = timeout
        mgr.var_bind_sequence = [(ObjectName(oid_text), OctetString(str_value))]
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_SET
        the_trap_thread.mailbox.put(mgr)

        if mgr.completion_event.wait(timeout + 0.5):
            logger.info(self._format_result(mgr))
        else:
            logger.warning("SET: no completion event observed")

    def _cmd_status(self, args: list[str]) -> None:
        mgr = self._require_manager(args, "status <name>")
        if mgr is None:
            return
        logger.info(self._format_result(mgr))

    def _cmd_list(self, _args: list[str]) -> None:
        if not self._records:
            logger.info("No managers.")
            return
        for record in self._records.values():
            mgr = record.manager
            logger.info(
                "%s: dest=%s:%s send_port=%s request_id=%s",
                record.name,
                mgr.destination_ip_address,
                mgr.destination_port,
                mgr.send_port,
                mgr.request_id,
            )

    def _cmd_cleartraps(self, _args: list[str]) -> None:
        the_trap_thread.clear_trap_list()
        logger.info("Trap list cleared.")

    def _cmd_sleep(self, args: list[str]) -> None:
        if len(args) != 1:
            logger.warning("Usage: sleep <seconds>")
            return
        seconds = float(args[0])
        time.sleep(seconds)
        logger.info("Slept for %s seconds", seconds)

    # ------------------------------------------------------------------
    # Test harness commands
    # ------------------------------------------------------------------

    def _cmd_agent_url(self, args: list[str]) -> None:
        if len(args) != 1:
            logger.warning("Usage: agent-url <url>")
            return
        self._agent_url = args[0].rstrip("/")
        logger.info("Agent URL set to: %s", self._agent_url)

    def _cmd_clear_agent_log(self, _args: list[str]) -> None:
        """DELETE /debug/snmp-operations on the agent."""
        url = f"{self._agent_url}/debug/snmp-operations"
        try:
            resp = requests.delete(url, timeout=5)
            resp.raise_for_status()
            logger.info("Agent operation log cleared.")
        except requests.RequestException as exc:
            logger.error("clear-agent-log: HTTP error: %s", exc)

    def _cmd_assert_agent_saw(self, args: list[str]) -> None:
        """Assert the agent recorded a specific operation type and OID.

        Usage: assert-agent-saw <get|getnext|set> <oid> [expected_value]

        The oid and optional expected_value are matched as substrings
        against the entries in the agent debug log.
        """
        if len(args) < 2:
            logger.warning("Usage: assert-agent-saw <get|getnext|set> <oid> [expected_value]")
            return

        op_type = args[0].upper()
        oid = args[1]
        expected_value = args[2] if len(args) >= 3 else None

        url = f"{self._agent_url}/debug/snmp-operations"
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            ops: list[dict[str, str]] = resp.json().get("operations", [])
        except requests.RequestException as exc:
            logger.error("FAIL: assert-agent-saw: HTTP error: %s", exc)
            self._assert_failures += 1
            return

        for entry in ops:
            if entry.get("type") != op_type:
                continue
            if oid not in str(entry.get("oid") or ""):
                continue
            if expected_value is not None:
                if expected_value not in str(entry.get("value") or ""):
                    continue
            desc = f"{op_type} {oid}" + (f"={expected_value}" if expected_value else "")
            logger.info("PASS: agent saw %s", desc)
            return

        desc = f"{op_type} {oid}" + (f"={expected_value}" if expected_value else "")
        logger.error("FAIL: agent did not record %s", desc)
        self._assert_failures += 1

    def _cmd_assert_get(self, args: list[str]) -> None:
        """GET an OID via SNMP and assert the returned value matches.

        Usage: assert-get <name> <oid> <expected_value> [timeout_sec]

        The expected_value is matched as a substring of the pretty-printed
        response value.
        """
        if len(args) < 3:
            logger.warning("Usage: assert-get <name> <oid> <expected_value> [timeout_sec]")
            return

        name, oid_text, expected = args[0], args[1], args[2]
        timeout = float(args[3]) if len(args) >= 4 else 5.0

        record = self._records.get(name)
        if record is None:
            logger.warning("Unknown manager: %s", name)
            return

        mgr = record.manager
        mgr.timeout = timeout
        mgr.var_bind_sequence = [(ObjectName(oid_text), None)]
        mgr.completion_event.clear()
        mgr.msg_type = MAILBOX_GET
        the_trap_thread.mailbox.put(mgr)

        if not mgr.completion_event.wait(timeout + 0.5):
            logger.error("FAIL: assert-get %s: no completion event", oid_text)
            self._assert_failures += 1
            return

        if int(mgr.result_code) != int(RESULT_OK):
            logger.error(
                "FAIL: assert-get %s: result=%s (not RESULT_OK)",
                oid_text,
                mgr.result_code,
            )
            self._assert_failures += 1
            return

        if not mgr.var_bind_sequence:
            logger.error("FAIL: assert-get %s: empty var_bind_sequence", oid_text)
            self._assert_failures += 1
            return

        _oid, val = mgr.var_bind_sequence[0]
        pretty = getattr(val, "prettyPrint", None)
        actual = str(pretty()) if callable(pretty) else str(val)

        if expected in actual:
            logger.info(
                "PASS: assert-get %s: got '%s' (contains '%s')",
                oid_text,
                actual,
                expected,
            )
        else:
            logger.error("FAIL: assert-get %s: expected '%s' in '%s'", oid_text, expected, actual)
            self._assert_failures += 1

    def _cmd_assert_result(self, args: list[str]) -> None:
        """Assert the last result code of a named manager.

        Usage: assert-result <name> <RESULT_OK|RESULT_TIMEOUT|RESULT_NOT_SENT>
        """
        if len(args) != 2:
            logger.warning("Usage: assert-result <name> <RESULT_OK|RESULT_TIMEOUT|RESULT_NOT_SENT>")
            return

        expected_name = args[1].upper()
        expected_map = {
            "RESULT_OK": RESULT_OK,
            "RESULT_TIMEOUT": RESULT_TIMEOUT,
            "RESULT_NOT_SENT": RESULT_NOT_SENT,
        }
        if expected_name not in expected_map:
            logger.warning("Unknown result code: %s", args[1])
            return

        record = self._records.get(args[0])
        if record is None:
            logger.warning("Unknown manager: %s", args[0])
            return

        mgr = record.manager
        expected_code = expected_map[expected_name]
        if int(mgr.result_code) == int(expected_code):
            logger.info("PASS: assert-result %s: %s", args[0], expected_name)
        else:
            actual_name = next(
                (k for k, v in expected_map.items() if int(v) == int(mgr.result_code)),
                str(mgr.result_code),
            )
            logger.error(
                "FAIL: assert-result %s: expected %s, got %s",
                args[0],
                expected_name,
                actual_name,
            )
            self._assert_failures += 1

    def _cmd_wait_trap(self, args: list[str]) -> None:
        """Block until a trap of the given kind arrives, or until timeout.

        Usage: wait-trap <completion|event|value-change|any> <timeout_sec>

        Uses the threading.Events on TrapThread without modifying the class.
        On success prints PASS; on timeout prints FAIL and increments the
        assertion failure counter.
        """
        if len(args) != 2:
            logger.warning("Usage: wait-trap <completion|event|value-change|any> <timeout_sec>")
            return

        kind = args[0].lower()
        timeout = float(args[1])

        if kind == "completion":
            fired = the_trap_thread.completion_trap_event.wait(timeout)
        elif kind == "event":
            fired = the_trap_thread.event_trap_event.wait(timeout)
        elif kind in ("value-change", "valuechange"):
            fired = the_trap_thread.value_change_trap_event.wait(timeout)
        elif kind == "any":
            fired = False
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if (
                    the_trap_thread.completion_trap_event.is_set()
                    or the_trap_thread.event_trap_event.is_set()
                    or the_trap_thread.value_change_trap_event.is_set()
                ):
                    fired = True
                    break
                time.sleep(0.05)
        else:
            logger.warning("Unknown trap kind: %s (use completion|event|value-change|any)", kind)
            return

        if fired:
            logger.info("PASS: wait-trap received kind=%s", kind)
        else:
            logger.error("FAIL: wait-trap timeout waiting for kind=%s after %ss", kind, timeout)
            self._assert_failures += 1

    def _cmd_send_completion_trap(self, args: list[str]) -> None:
        """Trigger completionTrap via snmp-sim REST API.

        Usage:
            send-completion-trap [source] [code] [dest_host] [dest_port]
        """
        source = args[0] if len(args) >= 1 else "CLI"
        code = int(args[1]) if len(args) >= 2 else 0
        dest_host = args[2] if len(args) >= 3 else "127.0.0.1"
        dest_port = int(args[3]) if len(args) >= 4 else 162

        try:
            self._send_trap_direct(
                trap_oid="1.3.6.1.4.1.99998.0.2",
                dest_host=dest_host,
                dest_port=dest_port,
                var_binds=[
                    ("1.3.6.1.4.1.99998.1.1.3", OctetString(source)),
                    ("1.3.6.1.4.1.99998.1.1.4", Integer(code)),
                ],
            )
            logger.info(
                "Triggered completion trap (%s/%s) to %s:%s",
                source,
                code,
                dest_host,
                dest_port,
            )
        except requests.RequestException as exc:
            logger.error("FAIL: send-completion-trap HTTP error: %s", exc)
            self._assert_failures += 1

    def _cmd_send_event_trap(self, args: list[str]) -> None:
        """Trigger eventTrap via snmp-sim REST API.

        Usage:
            send-event-trap [severity] [text] [dest_host] [dest_port]
        """
        severity = int(args[0]) if len(args) >= 1 else 2
        text = args[1] if len(args) >= 2 else "Equipment event"
        dest_host = args[2] if len(args) >= 3 else "127.0.0.1"
        dest_port = int(args[3]) if len(args) >= 4 else 162

        try:
            self._send_trap_direct(
                trap_oid="1.3.6.1.4.1.99998.0.3",
                dest_host=dest_host,
                dest_port=dest_port,
                var_binds=[
                    ("1.3.6.1.4.1.99998.1.1.5", Integer(severity)),
                    ("1.3.6.1.4.1.99998.1.1.6", OctetString(text)),
                ],
            )
            logger.info(
                "Triggered event trap (%s/%s) to %s:%s",
                severity,
                text,
                dest_host,
                dest_port,
            )
        except requests.RequestException as exc:
            logger.error("FAIL: send-event-trap HTTP error: %s", exc)
            self._assert_failures += 1

    def _cmd_send_regular_trap(self, args: list[str]) -> None:
        """Trigger a regular test trap (coldStart) via snmp-sim REST API.

        Usage:
            send-regular-trap [dest_host] [dest_port]
        """
        dest_host = args[0] if len(args) >= 1 else "127.0.0.1"
        dest_port = int(args[1]) if len(args) >= 2 else 162

        try:
            self._send_trap_direct(
                trap_oid="1.3.6.1.6.3.1.1.5.1",
                dest_host=dest_host,
                dest_port=dest_port,
                var_binds=[],
            )
            logger.info("Triggered regular trap to %s:%s", dest_host, dest_port)
        except requests.RequestException as exc:
            logger.error("FAIL: send-regular-trap HTTP error: %s", exc)
            self._assert_failures += 1

    def _send_trap_direct(
        self,
        *,
        trap_oid: str,
        dest_host: str,
        dest_port: int,
        var_binds: list[tuple[str, Integer | OctetString]],
    ) -> None:
        """Send an SNMPv2c trap directly to *dest_host:dest_port*."""

        async def _send() -> tuple[object, object, object]:
            notification = NotificationType(ObjectIdentity(trap_oid))
            if var_binds:
                notification = notification.add_varbinds(
                    *[ObjectType(ObjectIdentity(oid), value) for oid, value in var_binds]
                )

            error_indication, error_status, error_index, _ = await send_notification(
                SnmpEngine(),
                CommunityData("public"),
                await UdpTransportTarget.create((dest_host, dest_port)),
                ContextData(),
                "trap",
                notification,
            )
            return error_indication, error_status, error_index

        error_indication, error_status, error_index = asyncio.run(_send())
        if error_indication:
            raise requests.RequestException(f"SNMP trap send error: {error_indication}")
        if error_status:
            raise requests.RequestException(
                f"SNMP trap send error: {error_status} at {error_index}"
            )

    def _trap_watch_loop(self) -> None:
        seen = 0
        while not self._stop_event.is_set():
            if seen < len(the_trap_thread.trap_list):
                trap = the_trap_thread.trap_list[seen]
                seen += 1
                logger.info(self._format_trap(trap))
                continue
            time.sleep(0.1)

    def _require_manager(self, args: list[str], usage: str) -> SNMPManager | None:
        if len(args) != 1:
            logger.warning("Usage: %s", usage)
            return None
        name = args[0]
        record = self._records.get(name)
        if record is None:
            logger.warning("Unknown manager: %s", name)
            return None
        return record.manager

    def _format_result(self, mgr: SNMPManager) -> str:
        if int(mgr.result_code) == int(RESULT_OK):
            code_text = "RESULT_OK"
        elif int(mgr.result_code) == int(RESULT_TIMEOUT):
            code_text = "RESULT_TIMEOUT"
        else:
            code_text = "RESULT_NOT_SENT"

        vbs = []
        for oid, value in mgr.var_bind_sequence:
            vbs.append(f"{oid}={value}")

        return (
            f"result={code_text} request_id={mgr.request_id} "
            f"var_binds=[{', '.join(vbs)}]"
        )

    def _format_trap(self, trap: Trap) -> str:
        trap_kind, oid, value = trap
        if trap_kind == COMPLETION_TRAP:
            kind = "COMPLETION_TRAP"
        elif trap_kind == EVENT_TRAP:
            kind = "EVENT_TRAP"
        else:
            kind = "VALUE_CHANGE_TRAP"
        return f"trap kind={kind} oid={oid} value={value}"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the manager harness."""
    parser = argparse.ArgumentParser(description="manager-side test app for trap_thread")
    parser.add_argument(
        "--startup-listen",
        action="store_true",
        help="start trap receiver immediately",
    )
    parser.add_argument(
        "--script",
        help="path to a script file containing one manager command per line",
    )
    parser.add_argument(
        "--agent-url",
        default="http://127.0.0.1:8800",
        help="base URL of the snmp-sim REST API (default: http://127.0.0.1:8800)",
    )
    return parser.parse_args()


def main() -> None:
    """Entrypoint for interactive manager test harness."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    app = ManagerApp(agent_url=args.agent_url)
    if args.startup_listen:
        the_trap_thread.start_trap_receiver()
    if args.script:
        app.run_script(args.script)
        if app.has_assert_failures:
            sys.exit(1)
    else:
        app.run()


if __name__ == "__main__":
    main()

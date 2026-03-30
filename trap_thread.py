"""Drop-in replacement for the legacy TrapThread / SNMPManager module.

Replaces the deprecated ``AsynsockDispatcher`` transport stack with a
``select``-based loop over plain :mod:`socket` objects, while preserving
all observable behaviour of the original implementation:

- long-lived daemon thread that owns all SNMP UDP I/O
- mailbox / command-driven socket lifecycle and request dispatch
- asynchronous trap and response reception
- request-ID correlation and ``threading.Event`` completion signalling
- timer-based timeout detection at ~20 ms tick resolution
- no busy-loop when there is no active work
"""

from __future__ import annotations

import select
import socket
import threading
import time
from queue import Empty, Queue
from typing import Any

from pyasn1.codec.ber import decoder, encoder  # type: ignore[import-untyped]
from pyasn1.error import PyAsn1Error  # type: ignore[import-untyped]
from pyasn1.type.base import Asn1Item  # type: ignore[import-untyped]
from pyasn1.type.univ import Integer, Null  # type: ignore[import-untyped]
from pysnmp.proto import api  # type: ignore[import-untyped]

from snmp_manager import SNMPManager

# ---------------------------------------------------------------------------
# Trap-kind constants
# ---------------------------------------------------------------------------
COMPLETION_TRAP: int = 0
VALUE_CHANGE_TRAP: int = 1
EVENT_TRAP: int = 2

# ---------------------------------------------------------------------------
# Result codes  (pyasn1 Integer values, matching legacy RESULT_OK etc.)
# ---------------------------------------------------------------------------
RESULT_OK = Integer(0)
RESULT_TIMEOUT = Integer(1)
RESULT_NOT_SENT = Integer(2)

# ---------------------------------------------------------------------------
# Mailbox command codes
# ---------------------------------------------------------------------------
MAILBOX_OPEN: int = 0
MAILBOX_CLOSE: int = 1
MAILBOX_GET: int = 2
MAILBOX_SET: int = 3

# ---------------------------------------------------------------------------
# Type aliases – match the legacy names exactly
# ---------------------------------------------------------------------------
type CompletionTrap = tuple[int, Asn1Item, Asn1Item]
type EventTrap = tuple[int, Asn1Item, Asn1Item]
type ValueChangeTrap = tuple[int, Asn1Item, Asn1Item]
type Trap = CompletionTrap | EventTrap | ValueChangeTrap

# ---------------------------------------------------------------------------
# Module-level protocol handle and tuneable constants
# ---------------------------------------------------------------------------
_proto = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]  # same object as api.v2c

COMMUNITY: bytes = b"public"  # default read community (kept for backwards compatibility)
DISPATCHER_TICK: float = 0.02       # 20 ms – mirrors legacy setTimerResolution(0.02)
TRAP_BIND_HOST: str = "0.0.0.0"
DEFAULT_TRAP_PORT: int = 162        # Trap listener port fallback.

# Notification OIDs from TEST-ENUM-MIB (testEnumNotifications branch)
COMPLETION_TRAP_OID = "1.3.6.1.4.1.99998.0.2"
EVENT_TRAP_OID = "1.3.6.1.4.1.99998.0.3"


# ---------------------------------------------------------------------------
# TrapThread
# ---------------------------------------------------------------------------

class TrapThread:  # pylint: disable=too-many-instance-attributes
    """Long-lived worker thread that owns all SNMP UDP I/O.

    A ``select``-based loop over plain :mod:`socket` objects replaces the
    legacy ``AsynsockDispatcher``.  The public API and all observable
    behaviours are preserved so callers require no changes.
    """

    def __init__(self) -> None:
        self.mailbox: Queue[SNMPManager] = Queue()
        self.manager_list: list[SNMPManager] = []
        self.trap_list: list[Trap] = []

        self.completion_trap_event: threading.Event = threading.Event()
        self.event_trap_event: threading.Event = threading.Event()
        self.value_change_trap_event: threading.Event = threading.Event()
        self.thread_changed_event: threading.Event = threading.Event()

        self._lock = threading.Lock()
        self._running = False
        self._trap_listening = False
        self._trap_socket: socket.socket | None = None
        self._trap_bind_error: OSError | None = None

        self._worker_thread = threading.Thread(
            target=self.trap_thread_main,
            name="TrapThread",
            daemon=True,
        )
        self._worker_thread.start()

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the worker thread and release all resources."""
        self._running = False
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

    def clear_trap_list(self) -> None:
        """Discard all queued traps and clear the associated events."""
        with self._lock:
            self.trap_list.clear()
        self.completion_trap_event.clear()
        self.event_trap_event.clear()
        self.value_change_trap_event.clear()

    def start_trap_receiver(self) -> None:
        """Enable the UDP trap listener socket."""
        self._trap_listening = True
        self.thread_changed_event.set()

    def stop_trap_receiver(self) -> None:
        """Disable and close the UDP trap listener socket."""
        self._trap_listening = False
        self.thread_changed_event.set()

    def busy(self) -> bool:
        """True when there is active work (trap listener open or request outstanding)."""
        return self._trap_listening or any(
            mgr.request_id is not None for mgr in self.manager_list
        )

    # ------------------------------------------------------------------
    # Callbacks – invoked from within the worker thread
    # ------------------------------------------------------------------

    def cb_timer(self, time_now: float) -> None:
        """Called on each dispatcher tick; marks overdue requests as timed-out."""
        for mgr in self.manager_list:
            if mgr.request_id is not None and mgr.send_time > 0:
                if time_now - mgr.send_time >= mgr.timeout:
                    mgr.result_code = RESULT_TIMEOUT
                    mgr.request_id = None
                    mgr.completion_event.set()

    def cb_trap_received(
        self,
        _transport_dispatcher_param: object,
        _transport_domain: object,
        transport_address: tuple[object, object],
        whole_message: bytes,  # raw BER bytes from the socket
    ) -> None:
        """Decode an inbound UDP datagram and route it as a trap or a response."""
        del _transport_dispatcher_param, _transport_domain, transport_address
        try:
            msg, _ = decoder.decode(whole_message, asn1Spec=_proto.Message())
        except (PyAsn1Error, TypeError, ValueError):
            return

        pdu = _proto.apiMessage.get_pdu(msg)

        if isinstance(pdu, _proto.SNMPv2TrapPDU):
            self._handle_trap(pdu)
        else:
            # GET / SET response – correlate by SNMP request ID
            req_id = int(_proto.apiPDU.get_request_id(pdu))
            for mgr in self.manager_list:
                if mgr.request_id == req_id:
                    if int(_proto.apiPDU.get_error_status(pdu)):
                        mgr.result_code = RESULT_NOT_SENT
                    else:
                        mgr.result_code = RESULT_OK
                        mgr.var_bind_sequence = list(_proto.apiPDU.get_varbinds(pdu))
                    mgr.request_id = None
                    mgr.completion_event.set()
                    break

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _handle_trap(self, pdu: Any) -> None:
        """Classify and store a received SNMPv2c trap PDU.
        """
        vbs = list(_proto.apiPDU.get_varbinds(pdu))

        trap_kind = VALUE_CHANGE_TRAP
        # SNMPv2 traps conventionally carry snmpTrapOID.0 as varbind index 1.
        if len(vbs) >= 2:
            trap_oid = str(vbs[1][1])
            if trap_oid == COMPLETION_TRAP_OID:
                trap_kind = COMPLETION_TRAP
            elif trap_oid == EVENT_TRAP_OID:
                trap_kind = EVENT_TRAP

        oid: Any = vbs[0][0] if vbs else Null()
        val: Any = vbs[0][1] if vbs else Null()

        trap: Trap = (trap_kind, oid, val)
        with self._lock:
            self.trap_list.append(trap)

        if trap_kind == COMPLETION_TRAP:
            self.completion_trap_event.set()
        elif trap_kind == VALUE_CHANGE_TRAP:
            self.value_change_trap_event.set()
        elif trap_kind == EVENT_TRAP:
            self.event_trap_event.set()

    def _open_manager_socket(self, mgr: SNMPManager) -> None:
        if mgr.socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            # Manager request/reply socket should remain ephemeral.
            sock.bind(("0.0.0.0", 0))
            mgr.socket = sock

    def _close_manager_socket(self, mgr: SNMPManager) -> None:
        if mgr.socket is not None:
            mgr.socket.close()
            mgr.socket = None
        mgr.request_id = None
        if mgr in self.manager_list:
            self.manager_list.remove(mgr)

    def _build_and_send(self, mgr: SNMPManager, pdu: Any, *, write: bool = False) -> None:
        """Wrap *pdu* in a v2c Message, encode it and send via mgr's socket."""
        msg = _proto.Message()
        _proto.apiMessage.set_defaults(msg)
        community = mgr.write_community if write else mgr.read_community
        _proto.apiMessage.set_community(msg, community)
        _proto.apiMessage.set_pdu(msg, pdu)
        raw = encoder.encode(msg)
        req_id = int(_proto.apiPDU.get_request_id(pdu))
        mgr.request_id = req_id
        mgr.send_time = time.monotonic()
        if mgr.socket is not None:
            mgr.socket.sendto(raw, (mgr.destination_ip_address, mgr.destination_port))

    def _send_get(self, mgr: SNMPManager) -> None:
        pdu = _proto.GetRequestPDU()
        _proto.apiPDU.set_defaults(pdu)
        _proto.apiPDU.set_varbinds(pdu, [(oid, Null()) for oid, _ in mgr.var_bind_sequence])
        self._build_and_send(mgr, pdu)

    def _send_set(self, mgr: SNMPManager) -> None:
        pdu = _proto.SetRequestPDU()
        _proto.apiPDU.set_defaults(pdu)
        _proto.apiPDU.set_varbinds(pdu, mgr.var_bind_sequence)
        self._build_and_send(mgr, pdu, write=True)

    def _ensure_trap_socket(self) -> None:
        """Open the trap listener socket if not already open.

        The port is taken from the first manager whose ``send_port`` is
        non-zero; falls back to ``DEFAULT_TRAP_PORT`` (162).
        """
        if self._trap_socket is not None:
            return
        port = DEFAULT_TRAP_PORT
        for mgr in self.manager_list:
            if mgr.send_port:
                port = mgr.send_port
                break
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        try:
            sock.bind((TRAP_BIND_HOST, port))
        except OSError as ex:
            sock.close()
            self._trap_bind_error = ex
            self._trap_listening = False
            return
        self._trap_bind_error = None
        self._trap_socket = sock

    def _collect_readable_sockets(self) -> list[socket.socket]:
        socks: list[socket.socket] = []
        if self._trap_socket is not None:
            socks.append(self._trap_socket)
        for mgr in self.manager_list:
            if mgr.socket is not None and mgr.request_id is not None:
                socks.append(mgr.socket)
        return socks

    def _process_mailbox_command(self, mgr: SNMPManager) -> None:
        handlers = {
            MAILBOX_OPEN: self._handle_mailbox_open,
            MAILBOX_CLOSE: self._handle_mailbox_close,
            MAILBOX_GET: self._handle_mailbox_get,
            MAILBOX_SET: self._handle_mailbox_set,
        }
        handler = handlers.get(mgr.msg_type)
        if handler is not None:
            handler(mgr)

    def _handle_mailbox_open(self, mgr: SNMPManager) -> None:
        self._open_manager_socket(mgr)
        if mgr not in self.manager_list:
            self.manager_list.append(mgr)
        self.thread_changed_event.set()

    def _handle_mailbox_close(self, mgr: SNMPManager) -> None:
        self._close_manager_socket(mgr)
        mgr.result_code = RESULT_NOT_SENT
        mgr.completion_event.set()
        self.thread_changed_event.set()

    def _handle_mailbox_get(self, mgr: SNMPManager) -> None:
        if mgr.socket is None:
            self._open_manager_socket(mgr)
        if mgr not in self.manager_list:
            self.manager_list.append(mgr)
        self._send_get(mgr)

    def _handle_mailbox_set(self, mgr: SNMPManager) -> None:
        if mgr.socket is None:
            self._open_manager_socket(mgr)
        if mgr not in self.manager_list:
            self.manager_list.append(mgr)
        self._send_set(mgr)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def trap_thread_main(self) -> None:
        """Worker thread entry point – mirrors legacy ``trap_thread_main``.

        Pumps socket I/O, the mailbox, callbacks, and timeout checks in
        DISPATCHER_TICK (20 ms) intervals, and sleeps when there is no
        active work (equivalent to only calling ``jobStarted`` / ``runDispatcher``
        when the dispatcher has something to do).
        """
        self._running = True
        while self._running:

            # --- drain the mailbox ---------------------------------------
            while True:
                try:
                    mgr = self.mailbox.get_nowait()
                except Empty:
                    break
                self._process_mailbox_command(mgr)

            # --- manage trap socket --------------------------------------
            if self._trap_listening:
                self._ensure_trap_socket()
            elif self._trap_socket is not None:
                self._trap_socket.close()
                self._trap_socket = None

            # --- sleep when idle (no jobStarted / runDispatcher) --------
            if not self.busy():
                time.sleep(DISPATCHER_TICK)
                continue

            # --- poll all sockets  (≡ runDispatcher(0.02)) --------------
            readable = self._collect_readable_sockets()
            if readable:
                try:
                    ready, _, _ = select.select(readable, [], [], DISPATCHER_TICK)
                except (OSError, ValueError):
                    ready = []

                for sock in ready:
                    try:
                        raw, addr = sock.recvfrom(65535)
                    except OSError:
                        continue
                    self.cb_trap_received(None, None, addr, raw)

            # --- timer tick  (≡ registerTimerCbFun tick) ----------------
            self.cb_timer(time.monotonic())


# ---------------------------------------------------------------------------
# Module-level singleton
# Import ``the_trap_thread`` wherever the legacy code used it.
# ---------------------------------------------------------------------------
the_trap_thread = TrapThread()

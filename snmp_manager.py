"""SNMP manager data container used by TrapThread and manager apps."""

from __future__ import annotations

import socket
import threading
from typing import Any

from pyasn1.type.univ import Integer  # type: ignore[import-untyped]


DEFAULT_TIMEOUT: float = 2.0
DEFAULT_MSG_TYPE: int = 0
DEFAULT_RESULT_NOT_SENT = Integer(-2)
DEFAULT_READ_COMMUNITY: bytes = b"public"
DEFAULT_WRITE_COMMUNITY: bytes = b"public"


class SNMPManager:  # pylint: disable=too-many-instance-attributes,too-few-public-methods,too-many-arguments,too-many-positional-arguments
    """Represents a single SNMP peer and request state."""

    def __init__(
        self,
        destination_ip_address: str = "127.0.0.1",
        destination_port: int = 161,
        send_port: int = 0,
        read_community: bytes = DEFAULT_READ_COMMUNITY,
        write_community: bytes = DEFAULT_WRITE_COMMUNITY,
    ) -> None:
        self.destination_ip_address = destination_ip_address
        self.destination_port = destination_port
        self.send_port = send_port
        self.read_community = read_community
        self.write_community = write_community

        # Set by caller before queueing work.
        self.msg_type: int = DEFAULT_MSG_TYPE
        self.var_bind_sequence: list[tuple[Any, Any]] = []

        # Written by TrapThread on completion.
        self.result_code: Integer = DEFAULT_RESULT_NOT_SENT
        self.completion_event: threading.Event = threading.Event()

        # Internal tracking used by TrapThread.
        self.request_id: int | None = None
        self.send_time: float = 0.0
        self.timeout: float = DEFAULT_TIMEOUT
        self.socket: socket.socket | None = None

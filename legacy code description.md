# Legacy PySNMP Behavior (Reference)

This is a cleaned description of the old implementation, to preserve behavior while migrating to modern PySNMP.

## Transport and Dispatcher Layer

The old implementation used PySNMP's deprecated low-level asyncsock transport stack:

- `AsynsockDispatcher` from `pysnmp.carrier.asynsock.dispatch`
- UDP transport from `pysnmp.carrier.asynsock.dgram.udp`, specifically `UdpSocketTransport`

A single long-lived thread (`TrapThread`) owned one dispatcher instance and configured it with:

- a receive callback via `registerRecvCbFun()`
- a timer callback via `registerTimerCbFun()`
- timer resolution of `0.02` seconds via `setTimerResolution()`

## Message Construction and I/O

SNMP messages were built manually using v2c protocol objects from `pysnmp.proto.api`:

- protocol module: `api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]`
- BER encoding/decoding: `pyasn1.codec.ber.encoder` and `pyasn1.codec.ber.decoder`
- raw send path: `dispatcher.sendMessage(...)`

Trap reception used a server UDP transport (`openServerMode()` + `registerTransport()` on domain index `0`).
Manager request sockets used client UDP transports (`openClientMode()`) and separate transport domain indices.

## Thread / Mailbox Runtime Model

Operationally, `TrapThread` acted as a mailbox-driven event loop. It processed commands to:

- open manager sockets
- close manager sockets
- issue GET requests
- issue SET requests

For GET/SET requests it:

- built a request PDU (`GetRequestPDU` / `SetRequestPDU`)
- populated varbinds
- wrapped the PDU in an SNMP `Message`
- sent it through the manager-specific transport domain

## Async Reply / Trap Handling

The receive callback decoded inbound packets, then:

- distinguished trap PDUs from response PDUs
- matched responses by SNMP request ID
- signaled completion events back to waiting caller code

Timeouts were enforced in the timer callback by checking each manager's `sendTime` against configured timeout values and marking overdue requests as failed.

## Dispatcher Pumping Semantics

The thread only pumped dispatcher work when there was active work:

- trap listener enabled, and/or
- at least one outstanding request

In that state, it called:

- `jobStarted(1)`
- `runDispatcher(0.02)`

This effectively advanced socket I/O, callbacks, retries, and timeout processing in short intervals.

## Migration Target (Behavioral Requirements)

The modern replacement should preserve these observable behaviors:

- long-lived worker runtime that owns SNMP I/O
- mailbox/command-driven socket lifecycle and request submission
- asynchronous trap + response reception
- request ID correlation and completion signaling
- timeout handling with ~20 ms tick behavior
- no busy loop when no active work
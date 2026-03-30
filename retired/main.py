"""Minimal pysnmp hello-world style script."""

import types
from typing import Any, Sequence, cast

from pyasn1.type.base import Asn1Type  # type: ignore[import-untyped]
from pysnmp.hlapi.v3arch.asyncio import SnmpEngine  # type: ignore[import-untyped]
from pysnmp.proto import api  # type: ignore[import-untyped]
from pysnmp.proto.rfc1902 import ObjectName, OctetString  # type: ignore[import-untyped]
from pysnmp.smi.builder import MibBuilder  # type: ignore[import-untyped]
from pysnmp.smi.view import MibViewController  # type: ignore[import-untyped]

# print(api.PROTOCOL_MODULES.keys())   # protocol version map
# print(api.v2c)                       # v2c protocol module

proto_mod_x = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
proto_mod_y = api.v2c

assert proto_mod_x is proto_mod_y

assert isinstance(proto_mod_x, types.ModuleType)
assert isinstance(proto_mod_y, types.ModuleType)

def resolve_oid(view: MibViewController,
                oid: ObjectName) -> tuple[ObjectName, tuple[str, ...], ObjectName]:
    """Resolve an OID to its (oid, label, suffix) components via MibViewController."""
    return view.get_node_name(oid)  # type: ignore[no-any-return]


def asn1_pretty(value: Asn1Type) -> str:
    """Return a stable string representation for pyasn1 values."""
    pretty = getattr(value, "prettyPrint", None)
    if callable(pretty):
        # pyasn1 stubs currently type this as returning None, but at runtime it
        # returns a printable value.
        return str(cast(Any, pretty)())
    return str(value)


def main() -> None:
    """Run a local pysnmp smoke test without network access."""
    engine = SnmpEngine()
    print("Hello from pysnmp 👋")
    print(f"SNMP engine created: {engine!r}")

    view = MibViewController(MibBuilder())
    sys_descr_oid = ObjectName("1.3.6.1.2.1.1.1.0")
    oid, label, suffix = resolve_oid(view, sys_descr_oid)
    oid_value: Asn1Type = oid
    suffix_value: Asn1Type = suffix
    print(f"OID:    {asn1_pretty(oid_value)}")
    print(f"Label:  {'.'.join(label)}")
    print(f"Suffix: {asn1_pretty(suffix_value)}")

    proto_mod = api.v2c
    rx_pdu = proto_mod.GetResponsePDU()
    proto_mod.apiPDU.set_varbinds(
        rx_pdu,
        [
            (ObjectName("1.3.6.1.2.1.1.1.0"), OctetString("hello")),
            (ObjectName("1.3.6.1.2.1.1.5.0"), OctetString("router-1")),
        ],
    )
    varbinds: Sequence[tuple[ObjectName, Asn1Type]] = proto_mod.apiPDU.get_varbinds(rx_pdu)
    for varbind in varbinds:
        vb_oid: ObjectName = varbind[0]
        vb_val: Asn1Type = varbind[1]
        print(f"VarBind: {vb_oid.prettyPrint()} = {asn1_pretty(vb_val)}")


if __name__ == "__main__":
    main()

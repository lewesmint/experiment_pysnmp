from __future__ import annotations

import types
from typing import Sequence

from pyasn1.type.base import SimpleAsn1Type  # type: ignore[import-untyped]
from pysnmp.proto import api  # type: ignore[import-untyped]
from pysnmp.proto.api import v2c  # type: ignore[import-untyped]
from pysnmp.proto.rfc1902 import ObjectName  # type: ignore[import-untyped]


# api.v2c is a protocol module object.
proto_mod: types.ModuleType = api.v2c


def extract_var_binds(rx_pdu: v2c.GetResponsePDU) -> Sequence[tuple[ObjectName, SimpleAsn1Type]]:
    """Return typed varbinds from an SNMPv2c response PDU."""
    var_binds: Sequence[tuple[ObjectName, SimpleAsn1Type]] = proto_mod.apiPDU.get_varbinds(rx_pdu)
    return var_binds

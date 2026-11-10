"""Pure decoder for Ambientika master UDP broadcasts (no Home Assistant imports)."""

from __future__ import annotations

from enum import StrEnum

from .models import (
    UDP_BASE_PORT,
    UDP_FRAME_HEADER,
    UDP_FRAME_LENGTH,
    UDP_SOURCE_BASE_PORT,
    Coupling,
    Direction,
    LocalSpeed,
    UdpFrame,
)

_COUPLINGS = {0: Coupling.IDLE, 2: Coupling.OPPOSITE, 3: Coupling.SAME}
_DIRECTIONS = {
    0: Direction.STOPPED,
    1: Direction.EXHAUST,
    2: Direction.INTAKE,
    3: Direction.UNKNOWN,
}
_SPEEDS = {
    0: LocalSpeed.NIGHT,
    1: LocalSpeed.LOW,
    2: LocalSpeed.MEDIUM,
    3: LocalSpeed.HIGH,
}


class RejectReason(StrEnum):
    """Why a datagram was not accepted as local state."""

    LENGTH = "length"
    HEADER = "header"
    ZONE_NIBBLE = "zone_nibble"
    ZONE_PORT = "zone_port"
    SOURCE_PORT = "source_port"
    UNKNOWN_ZONE = "unknown_zone"
    HOUSE_ID = "house_id"
    SENDER_PINNED = "sender_pinned"
    RATE_LIMIT = "rate_limit"


def decode_frame(
    data: bytes,
    source: tuple[str, int],
    dest_port: int,
    *,
    base_port: int = UDP_BASE_PORT,
    source_base_port: int | None = UDP_SOURCE_BASE_PORT,
) -> UdpFrame | RejectReason:
    """Decode one datagram or return the reason it is invalid.

    ``source`` is the sender ``(host, port)``, ``dest_port`` the local port the
    datagram arrived on. Pass ``source_base_port=None`` to skip the source port
    check.
    """
    if len(data) != UDP_FRAME_LENGTH:
        return RejectReason.LENGTH
    if data[0] != UDP_FRAME_HEADER:
        return RejectReason.HEADER
    if data[1] >> 4 != 0x3:
        return RejectReason.ZONE_NIBBLE
    zone = data[1] & 0x0F
    if dest_port != base_port + zone:
        return RejectReason.ZONE_PORT
    host, port = source
    if source_base_port is not None and port != source_base_port + zone:
        return RejectReason.SOURCE_PORT
    state = data[2]
    coupling_raw = state >> 4
    return UdpFrame(
        zone_index=zone,
        house_id=int.from_bytes(data[3:7], "big"),
        coupling=_COUPLINGS.get(coupling_raw, Coupling.UNKNOWN),
        coupling_raw=coupling_raw,
        direction=_DIRECTIONS[(state >> 2) & 0x3],
        speed=_SPEEDS[state & 0x3],
        state_raw=state,
        raw=bytes(data),
        source_host=host,
        source_port=port,
    )

"""Secure-channel frame codec (Phase 3)."""

from ghostlink.messaging.packets.frames import (
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
)

__all__ = ["Frame", "FrameType", "decode_frame", "encode_frame"]

"""File-transfer frame builders (Phase 4).

Thin typed wrappers over the shared secure-channel codec in
:mod:`ghostlink.messaging.packets.frames`. Validation of every field
happens in ``encode_frame``/``decode_frame`` — nothing reaches the transfer
manager unvalidated.
"""

from __future__ import annotations

import base64

from ghostlink.messaging.packets.frames import Frame, FrameType


def file_offer_frame(transfer_id: str, sealed_manifest: bytes) -> Frame:
    return Frame(
        FrameType.FILE_OFFER,
        {"id": transfer_id, "ct": base64.b64encode(sealed_manifest).decode("ascii")},
    )


def file_accept_frame(transfer_id: str, *, bitmap_b64: str | None = None) -> Frame:
    data: dict[str, str] = {"id": transfer_id}
    if bitmap_b64:
        data["bm"] = bitmap_b64
    return Frame(FrameType.FILE_ACCEPT, data)


def file_reject_frame(transfer_id: str, *, reason: str | None = None) -> Frame:
    data: dict[str, str] = {"id": transfer_id}
    if reason:
        data["reason"] = reason
    return Frame(FrameType.FILE_REJECT, data)


def file_chunk_frame(transfer_id: str, n: int, sealed_chunk: bytes) -> Frame:
    return Frame(
        FrameType.FILE_CHUNK,
        {
            "id": transfer_id,
            "n": n,
            "ct": base64.b64encode(sealed_chunk).decode("ascii"),
        },
    )


def file_ack_frame(transfer_id: str, n: int) -> Frame:
    return Frame(FrameType.FILE_ACK, {"id": transfer_id, "n": n})


def file_pause_frame(transfer_id: str) -> Frame:
    return Frame(FrameType.FILE_PAUSE, {"id": transfer_id})


def file_resume_frame(transfer_id: str) -> Frame:
    return Frame(FrameType.FILE_RESUME, {"id": transfer_id})


def file_cancel_frame(transfer_id: str, *, reason: str | None = None) -> Frame:
    data: dict[str, str] = {"id": transfer_id}
    if reason:
        data["reason"] = reason
    return Frame(FrameType.FILE_CANCEL, data)


def file_complete_frame(transfer_id: str, sha256: str) -> Frame:
    return Frame(FrameType.FILE_COMPLETE, {"id": transfer_id, "sha256": sha256})


def file_error_frame(transfer_id: str, code: str, message: str) -> Frame:
    return Frame(FrameType.FILE_ERROR, {"id": transfer_id, "code": code, "message": message})

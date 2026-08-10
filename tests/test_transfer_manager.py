"""File-transfer end-to-end over a real relay on loopback (Phase 4).

Sender ⇄ relay ⇄ receiver, with a real ChatSession handshake on both sides —
nothing about the network is mocked. Covers offers, accept/reject, chunked
cipher delivery, integrity verification, tamper/corruption detection, replay
handling, cancellation, pause/resume, reconnect-with-resume, concurrency
caps, quotas, expiry, wire confidentiality and log hygiene.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import time
from pathlib import Path

import pytest

from ghostlink.exceptions.base import GhostLinkError
from ghostlink.messaging.history import SessionHistory
from ghostlink.messaging.packets.frames import Frame
from ghostlink.models.room import generate_room_id
from ghostlink.transfer import packets
from ghostlink.transfer.integrity import derive_transfer_key, seal_chunk
from ghostlink.transfer.manager import (
    TransferEvent,
    TransferEventKind,
    TransferLimits,
    TransferManager,
)
from ghostlink.transfer.manifest import FileManifest
from ghostlink.transfer.models import TransferState
from ghostlink.transfer.storage import TransferStorage
from ghostlink.transport.relay.client import RelayClientConfig
from ghostlink.transport.relay.server import RelayServer
from tests.conftest import run, running_relay
from tests.test_chat_session import CHAT_FAST, FAST, RECONNECTING, Pair, make_pair

MARKER = b"GHOSTLINK-SECRET-MARKER-42"


def _limits(**overrides: object) -> TransferLimits:
    base: dict[str, object] = {
        "max_file_size_bytes": 8 * 1024 * 1024,
        "max_concurrent": 2,
        "chunk_size_bytes": 1024,
        "ack_timeout_seconds": 0.5,
        "retry_limit": 3,
        "temp_limit_bytes": 16 * 1024 * 1024,
        "transfer_expiry_seconds": 3600.0,
        "offer_timeout_seconds": 30.0,
        "terminal_ttl_seconds": 300.0,
    }
    base.update(overrides)
    return TransferLimits(**base)  # type: ignore[arg-type]


async def _wait_for(predicate: object, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the deadline")


def _make_file(path: Path, size: int, *, marker: bool = False) -> Path:
    block = (MARKER + bytes(size)) if marker else bytes(index % 251 for index in range(size))
    path.write_bytes(block[:size])
    return path


class TransferPair:
    def __init__(
        self,
        pair: Pair,
        host_mgr: TransferManager,
        guest_mgr: TransferManager,
        host_events: list[TransferEvent],
        guest_events: list[TransferEvent],
        host_storage: TransferStorage,
        guest_storage: TransferStorage,
        host_history: SessionHistory | None = None,
        guest_history: SessionHistory | None = None,
    ) -> None:
        self.pair = pair
        self.host_mgr = host_mgr
        self.guest_mgr = guest_mgr
        self.host_events = host_events
        self.guest_events = guest_events
        self.host_storage = host_storage
        self.guest_storage = guest_storage
        self.host_history = host_history
        self.guest_history = guest_history

    async def close(self) -> None:
        await self.host_mgr.close()
        await self.guest_mgr.close()
        await self.pair.close()


async def make_transfer_pair(
    server: RelayServer,
    tmp_path: Path,
    *,
    limits: TransferLimits | None = None,
    client_config: RelayClientConfig = FAST,
    host_history: SessionHistory | None = None,
    guest_history: SessionHistory | None = None,
    start: bool = True,
) -> TransferPair:
    # One history instance per side, shared by session + manager — exactly
    # how run_chat_session wires production.
    pair = await make_pair(
        server,
        client_config=client_config,
        host_history=host_history,
        guest_history=guest_history,
    )
    host_storage = TransferStorage(tmp_path / "host-state", download_dir=str(tmp_path / "host-dl"))
    guest_storage = TransferStorage(
        tmp_path / "guest-state", download_dir=str(tmp_path / "guest-dl")
    )
    host_mgr = TransferManager(pair.host, host_storage, limits or _limits(), history=host_history)
    guest_mgr = TransferManager(
        pair.guest, guest_storage, limits or _limits(), history=guest_history
    )
    host_events: list[TransferEvent] = []
    guest_events: list[TransferEvent] = []
    host_mgr.add_listener(host_events.append)
    guest_mgr.add_listener(guest_events.append)
    if start:
        await host_mgr.start()
        await guest_mgr.start()
    return TransferPair(
        pair,
        host_mgr,
        guest_mgr,
        host_events,
        guest_events,
        host_storage,
        guest_storage,
        host_history,
        guest_history,
    )


def _events(events: list[TransferEvent], kind: TransferEventKind) -> list[TransferEvent]:
    return [event for event in events if event.kind is kind]


def _states(events: list[TransferEvent]) -> list[str]:
    return [event.detail for event in events if event.kind is TransferEventKind.STATE]


async def _complete_transfer(
    transfer_pair: TransferPair, source: Path, *, timeout: float = 15.0
) -> str:
    """Offer, accept, and drive one transfer to completion. Returns the id."""

    snapshot = await transfer_pair.host_mgr.send_file(source)
    transfer_id = snapshot.transfer_id
    await _wait_for(
        lambda: any(
            event.kind is TransferEventKind.OFFER
            and event.transfer is not None
            and event.transfer.transfer_id == transfer_id
            for event in transfer_pair.guest_events
        )
    )
    await transfer_pair.guest_mgr.accept(transfer_id)
    await _wait_for(
        lambda: (
            transfer_pair.host_mgr.get(transfer_id) is not None
            and transfer_pair.host_mgr.get(transfer_id).state  # type: ignore[union-attr]
            is TransferState.COMPLETED
            and transfer_pair.guest_mgr.get(transfer_id) is not None
            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
            is TransferState.COMPLETED
        ),
        timeout=timeout,
    )
    return transfer_id


class TestEndToEnd:
    def test_small_file_full_flow(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(
                    server,
                    tmp_path,
                    host_history=SessionHistory(),
                    guest_history=SessionHistory(),
                )
                try:
                    source = _make_file(tmp_path / "report.txt", 3_000)
                    transfer_id = await _complete_transfer(transfer_pair, source)

                    guest_snapshot = transfer_pair.guest_mgr.get(transfer_id)
                    assert guest_snapshot is not None
                    assert guest_snapshot.saved_path is not None
                    saved = Path(guest_snapshot.saved_path)
                    assert saved.read_bytes() == source.read_bytes()
                    assert saved.parent == transfer_pair.guest_storage.download_dir
                    assert (saved.stat().st_mode & 0o777) == 0o600
                    # No temp state survives a completed transfer.
                    assert not transfer_pair.guest_storage.temp_path(transfer_id).exists()

                    host_details = _states(transfer_pair.host_events)
                    assert any("Waiting for peer approval" in detail for detail in host_details)
                    assert any("Peer accepted" in detail for detail in host_details)
                    assert any("completed" in detail for detail in host_details)
                    guest_details = _states(transfer_pair.guest_events)
                    assert any("Verified" in detail for detail in guest_details)
                    saved_events = _events(transfer_pair.guest_events, TransferEventKind.SAVED)
                    assert len(saved_events) == 1
                    assert saved_events[0].detail == str(saved)

                    for history in (
                        transfer_pair.host_history,
                        transfer_pair.guest_history,
                    ):
                        assert history is not None
                        entries = [e for e in history.entries() if e.kind == "transfer"]
                        assert len(entries) == 1
                    host_entry = transfer_pair.host_history.entries()[0]  # type: ignore[union-attr]
                    assert host_entry.kind == "transfer"
                    assert "report.txt" in host_entry.text
                    assert "GHOSTLINK" not in host_entry.text  # metadata only
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_large_file_with_progress(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "big.bin", 1_500_000)
                    transfer_id = await _complete_transfer(transfer_pair, source, timeout=60.0)
                    guest_snapshot = transfer_pair.guest_mgr.get(transfer_id)
                    assert guest_snapshot is not None
                    assert guest_snapshot.saved_path is not None
                    assert Path(guest_snapshot.saved_path).read_bytes() == source.read_bytes()
                    progress = _events(transfer_pair.guest_events, TransferEventKind.PROGRESS)
                    assert progress, "expected live progress events during a large transfer"
                    final = progress[-1].transfer
                    assert final is not None
                    assert final.total_chunks == final.chunks_done
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_reject_flow(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "declined.bin", 2_048)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.reject(transfer_id, reason="not today")
                    await _wait_for(
                        lambda: (
                            transfer_pair.host_mgr.get(transfer_id) is not None
                            and transfer_pair.host_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.REJECTED
                        )
                    )
                    assert any(
                        detail == "Peer declined: not today"
                        for detail in _states(transfer_pair.host_events)
                    )
                    assert not transfer_pair.guest_storage.temp_path(transfer_id).exists()
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_cancel_mid_transfer_notifies_peer_and_cleans_temp(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "abort.bin", 400_000)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.accept(transfer_id)
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).chunks_done  # type: ignore[union-attr]
                            > 5
                        )
                    )
                    await transfer_pair.host_mgr.cancel(transfer_id, reason="wrong file")
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.CANCELLED
                        )
                    )
                    assert transfer_pair.host_mgr.get(transfer_id).state is (  # type: ignore[union-attr]
                        TransferState.CANCELLED
                    )
                    # Incomplete bytes never become a visible download.
                    assert not transfer_pair.guest_storage.temp_path(transfer_id).exists()
                    assert list(transfer_pair.guest_storage.download_dir.iterdir()) == []
                    assert any(
                        detail == "Cancelled by the peer: wrong file"
                        for detail in _states(transfer_pair.guest_events)
                    )
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_pause_and_resume(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "pausable.bin", 300_000)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.accept(transfer_id)
                    await _wait_for(
                        lambda: (
                            transfer_pair.host_mgr.get(transfer_id) is not None
                            and transfer_pair.host_mgr.get(transfer_id).chunks_done  # type: ignore[union-attr]
                            > 5
                        )
                    )
                    await transfer_pair.host_mgr.pause(transfer_id)
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.PAUSED
                        )
                    )
                    assert transfer_pair.host_mgr.get(transfer_id).state is (  # type: ignore[union-attr]
                        TransferState.PAUSED
                    )
                    frozen = transfer_pair.guest_mgr.get(transfer_id).chunks_done  # type: ignore[union-attr]
                    await asyncio.sleep(1.0)
                    assert (
                        transfer_pair.guest_mgr.get(transfer_id).chunks_done  # type: ignore[union-attr]
                        == frozen
                    ), "chunks kept flowing while paused"

                    await transfer_pair.host_mgr.resume(transfer_id)
                    await _wait_for(
                        lambda: (
                            transfer_pair.host_mgr.get(transfer_id) is not None
                            and transfer_pair.host_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.COMPLETED
                        )
                    )
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.COMPLETED
                        )
                    )
                    saved = transfer_pair.guest_mgr.get(transfer_id).saved_path  # type: ignore[union-attr]
                    assert Path(saved).read_bytes() == source.read_bytes()
                finally:
                    await transfer_pair.close()

        run(scenario())


class TestAdversarial:
    def test_tampered_chunk_fails_loudly_and_purges(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    size = 32_768  # 32 chunks — the tail arrives late
                    source = _make_file(tmp_path / "victim.bin", size)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.accept(transfer_id)
                    # Forge a garbage-ciphertext chunk for the LAST chunk number
                    # (guaranteed not yet received, so it is never a duplicate).
                    tail = size - 31 * 1024
                    forged = packets.file_chunk_frame(transfer_id, 32, os.urandom(tail + 28))
                    await transfer_pair.pair.host.send_channel_frame(forged)
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.FAILED
                        )
                    )
                    # The peer learns of the tampering too.
                    await _wait_for(
                        lambda: (
                            transfer_pair.host_mgr.get(transfer_id) is not None
                            and transfer_pair.host_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.FAILED
                        )
                    )
                    assert not transfer_pair.guest_storage.temp_path(transfer_id).exists()
                    assert list(transfer_pair.guest_storage.download_dir.iterdir()) == []
                    notices = _events(transfer_pair.guest_events, TransferEventKind.NOTICE)
                    assert any("tampered" in notice.detail for notice in notices)
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_out_of_range_chunk_number_fails(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "ranges.bin", 20_480)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.accept(transfer_id)
                    forged = packets.file_chunk_frame(
                        transfer_id,
                        21,
                        os.urandom(1024 + 28),  # only 20 exist
                    )
                    await transfer_pair.pair.host.send_channel_frame(forged)
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.FAILED
                        )
                    )
                    assert any(
                        "out of range" in detail for detail in _states(transfer_pair.guest_events)
                    )
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_chunk_for_unknown_transfer_gets_error_reply(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    forged = packets.file_chunk_frame("tf_deadbeef", 1, os.urandom(1052))
                    await transfer_pair.pair.host.send_channel_frame(forged)
                    await _wait_for(
                        lambda: any(
                            event.kind is TransferEventKind.NOTICE
                            and "transfer problem" in event.detail
                            for event in transfer_pair.host_events
                        )
                    )
                    assert transfer_pair.guest_mgr.list_transfers() == []
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_malformed_wire_packet_dropped_silently(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    # n=0 is outside the FILE_CHUNK schema — the session codec
                    # must drop it before the manager ever runs.
                    raw = json.dumps(
                        {
                            "v": 1,
                            "t": "FILE_CHUNK",
                            "seq": 0,
                            "id": "zz1",
                            "ts": time.time(),
                            "data": {"id": "tf_deadbeef", "n": 0, "ct": "QUJD"},
                        }
                    ).encode()
                    await transfer_pair.pair.host_client.send_forward(
                        transfer_pair.pair.channel, base64.b64encode(raw).decode()
                    )
                    await asyncio.sleep(0.4)
                    assert transfer_pair.pair.guest.stats.frames_dropped >= 1
                    assert transfer_pair.guest_mgr.list_transfers() == []
                    # Oversized bodies never reach the wire at all.
                    with pytest.raises(GhostLinkError):
                        await transfer_pair.pair.host_client.send_forward(
                            transfer_pair.pair.channel, "A" * 20_000
                        )
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_replayed_chunk_re_acked_not_rewritten(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    source = _make_file(tmp_path / "replay.bin", 65_536)
                    snapshot = await transfer_pair.host_mgr.send_file(source)
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await transfer_pair.guest_mgr.accept(transfer_id)
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).chunks_done  # type: ignore[union-attr]
                            > 4
                        )
                    )
                    # Replay chunk 2 with a *valid* seal (in-process key access)
                    # — the receiver must re-ack without rewriting or failing.
                    session_key = transfer_pair.pair.host.session_key_copy()
                    assert session_key is not None
                    key = derive_transfer_key(session_key, transfer_id)
                    data = source.read_bytes()
                    sealed = seal_chunk(key, transfer_id, 2, data[1024:2048])
                    await transfer_pair.pair.host.send_channel_frame(
                        packets.file_chunk_frame(transfer_id, 2, sealed)
                    )
                    await _complete_and_verify(transfer_pair, transfer_id, source)
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_offer_with_hostile_filename_is_sanitized(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    evil = tmp_path / "evil.sh"
                    evil.write_bytes(b"#!/bin/sh\necho hi\n")
                    # Forge an offer whose *manifest* claims a traversal name.
                    session_key = transfer_pair.pair.host.session_key_copy()
                    assert session_key is not None
                    transfer_id = "tf_c0ffee01"
                    key = derive_transfer_key(session_key, transfer_id)
                    manifest = FileManifest(
                        protocol="gf1",
                        transfer_id=transfer_id,
                        filename="evil.sh",
                        size_bytes=20,
                        mime="application/x-sh",
                        chunk_size=1024,
                        total_chunks=1,
                        sha256="ab" * 32,
                    )
                    from ghostlink.transfer.integrity import seal_manifest

                    sealed = seal_manifest(key, transfer_id, manifest.to_json())
                    await transfer_pair.pair.host.send_channel_frame(
                        packets.file_offer_frame(transfer_id, sealed)
                    )
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    # Even with a hostile peer, destinations stay in-download-dir.
                    destination = transfer_pair.guest_storage.destination_for("../../x.sh")
                    assert (
                        transfer_pair.guest_storage.download_dir.resolve()
                        in destination.resolve().parents
                    )
                finally:
                    await transfer_pair.close()

        run(scenario())


async def _complete_and_verify(transfer_pair: TransferPair, transfer_id: str, source: Path) -> None:
    await _wait_for(
        lambda: (
            transfer_pair.host_mgr.get(transfer_id) is not None
            and transfer_pair.host_mgr.get(transfer_id).state  # type: ignore[union-attr]
            is TransferState.COMPLETED
            and transfer_pair.guest_mgr.get(transfer_id) is not None
            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
            is TransferState.COMPLETED
        ),
        timeout=30.0,
    )
    saved = transfer_pair.guest_mgr.get(transfer_id).saved_path  # type: ignore[union-attr]
    assert Path(saved).read_bytes() == source.read_bytes()


class TestReconnectResume:
    def test_resume_after_relay_restart_keeps_verified_chunks(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            first = RelayServer(host="127.0.0.1", port=port)
            await first.start()
            transfer_pair: TransferPair | None = None
            try:
                channel = generate_room_id()
                from tests.test_chat_session import _client

                host_client = await _client(first, "host", RECONNECTING)
                guest_client = await _client(first, "guest", RECONNECTING)
                from ghostlink.messaging.session.chat import ChatSession

                host = ChatSession(
                    host_client,
                    channel=channel,
                    role="host",
                    display_name="Nova",
                    config=CHAT_FAST,
                )
                guest = ChatSession(
                    guest_client,
                    channel=channel,
                    role="guest",
                    display_name="Ravi",
                    config=CHAT_FAST,
                )
                await host.start()
                await guest.start()
                await host.wait_ready(timeout_seconds=5.0)
                await guest.wait_ready(timeout_seconds=5.0)
                pair = Pair(host, guest, host_client, guest_client, [], [], channel)

                host_storage = TransferStorage(
                    tmp_path / "host-state", download_dir=str(tmp_path / "host-dl")
                )
                guest_storage = TransferStorage(
                    tmp_path / "guest-state", download_dir=str(tmp_path / "guest-dl")
                )
                host_mgr = TransferManager(host, host_storage, _limits())
                guest_mgr = TransferManager(guest, guest_storage, _limits())
                transfer_pair = TransferPair(
                    pair, host_mgr, guest_mgr, [], [], host_storage, guest_storage
                )
                await host_mgr.start()
                await guest_mgr.start()

                chunk_numbers: list[int] = []
                original_on_chunk = guest_mgr._on_chunk  # type: ignore[attr-defined]

                async def spy_on_chunk(frame: Frame) -> None:
                    chunk_numbers.append(int(frame.data["n"]))
                    await original_on_chunk(frame)

                guest_mgr._on_chunk = spy_on_chunk  # type: ignore[method-assign]

                source = _make_file(tmp_path / "resume.bin", 131_072)  # 128 chunks
                snapshot = await host_mgr.send_file(source)
                transfer_id = snapshot.transfer_id
                await _wait_for(lambda: guest_mgr.get(transfer_id) is not None)
                await guest_mgr.accept(transfer_id)
                await _wait_for(
                    lambda: (
                        guest_mgr.get(transfer_id) is not None
                        and guest_mgr.get(transfer_id).chunks_done >= 20
                    )  # type: ignore[union-attr]
                )

                # The relay dies mid-transfer: both sides auto-pause.
                await first.aclose()
                await _wait_for(
                    lambda: (
                        guest_mgr.get(transfer_id) is not None
                        and guest_mgr.get(transfer_id).state is TransferState.PAUSED
                    ),  # type: ignore[union-attr]
                    timeout=10.0,
                )
                assert host_mgr.get(transfer_id).state is TransferState.PAUSED  # type: ignore[union-attr]

                second = RelayServer(host="127.0.0.1", port=port)
                await second.start()
                try:
                    await host.wait_ready(timeout_seconds=15.0)
                    await guest.wait_ready(timeout_seconds=15.0)
                    await _wait_for(
                        lambda: (
                            host_mgr.get(transfer_id) is not None
                            and host_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.COMPLETED
                        ),
                        timeout=45.0,
                    )
                    await _wait_for(
                        lambda: (
                            guest_mgr.get(transfer_id) is not None
                            and guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.COMPLETED
                        ),
                        timeout=20.0,
                    )
                    saved = guest_mgr.get(transfer_id).saved_path  # type: ignore[union-attr]
                    assert Path(saved).read_bytes() == source.read_bytes()
                    # Resume correctness: every chunk arrived; retransmission of
                    # already-verified chunks stays within the in-flight window.
                    assert len(set(chunk_numbers)) == 128
                    duplicates = len(chunk_numbers) - len(set(chunk_numbers))
                    assert duplicates <= 16
                finally:
                    await second.aclose()
            finally:
                if transfer_pair is not None:
                    await transfer_pair.close()

        run(scenario())


class TestLimitsAndHousekeeping:
    def test_concurrency_cap_queues_and_promotes(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                limits = _limits(max_concurrent=2)
                transfer_pair = await make_transfer_pair(server, tmp_path, limits=limits)
                try:
                    sources = [
                        _make_file(tmp_path / f"file-{index}.bin", 12_000) for index in range(3)
                    ]
                    snapshots = [
                        await transfer_pair.host_mgr.send_file(source) for source in sources
                    ]
                    states = [snapshot.state for snapshot in snapshots]
                    assert states[:2] == [TransferState.OFFERED, TransferState.OFFERED]
                    assert states[2] is TransferState.QUEUED  # beyond the cap

                    for snapshot in snapshots:
                        await _wait_for(
                            lambda snap=snapshot: (
                                transfer_pair.guest_mgr.get(snap.transfer_id) is not None
                            )
                        )
                        await transfer_pair.guest_mgr.accept(snapshot.transfer_id)
                    for index, snapshot in enumerate(snapshots):
                        await _wait_for(
                            lambda snap=snapshot: (
                                transfer_pair.guest_mgr.get(snap.transfer_id) is not None
                                and transfer_pair.guest_mgr.get(snap.transfer_id).state  # type: ignore[union-attr]
                                is TransferState.COMPLETED
                            ),
                            timeout=30.0,
                        )
                        saved = transfer_pair.guest_mgr.get(snapshot.transfer_id)  # type: ignore[union-attr]
                        assert saved is not None and saved.saved_path is not None
                        assert Path(saved.saved_path).read_bytes() == sources[index].read_bytes()
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_receiver_busy_cap_auto_rejects(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                # Guest tolerates one active transfer; host can run two.
                guest_limits = _limits(max_concurrent=1)
                host_limits = _limits(max_concurrent=2)
                pair = await make_pair(server)
                host_storage = TransferStorage(
                    tmp_path / "host-state", download_dir=str(tmp_path / "host-dl")
                )
                guest_storage = TransferStorage(
                    tmp_path / "guest-state", download_dir=str(tmp_path / "guest-dl")
                )
                host_mgr = TransferManager(pair.host, host_storage, host_limits)
                guest_mgr = TransferManager(pair.guest, guest_storage, guest_limits)
                transfer_pair = TransferPair(
                    pair, host_mgr, guest_mgr, [], [], host_storage, guest_storage
                )
                try:
                    await host_mgr.start()
                    await guest_mgr.start()
                    first = await host_mgr.send_file(_make_file(tmp_path / "one.bin", 8_000))
                    second = await host_mgr.send_file(_make_file(tmp_path / "two.bin", 8_000))
                    assert second.state is TransferState.OFFERED
                    # The second offer never reaches the guest UI: busy → auto-reject.
                    await _wait_for(
                        lambda: (
                            host_mgr.get(second.transfer_id) is not None
                            and host_mgr.get(second.transfer_id).state  # type: ignore[union-attr]
                            is TransferState.REJECTED
                        )
                    )
                    assert guest_mgr.get(second.transfer_id) is None
                    assert guest_mgr.get(first.transfer_id) is not None
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_temp_quota_auto_rejects(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                host_storage = TransferStorage(
                    tmp_path / "host-state", download_dir=str(tmp_path / "host-dl")
                )
                guest_storage = TransferStorage(
                    tmp_path / "guest-state", download_dir=str(tmp_path / "guest-dl")
                )
                host_mgr = TransferManager(pair.host, host_storage, _limits())
                guest_mgr = TransferManager(
                    pair.guest, guest_storage, _limits(temp_limit_bytes=2_000)
                )
                transfer_pair = TransferPair(
                    pair, host_mgr, guest_mgr, [], [], host_storage, guest_storage
                )
                try:
                    await host_mgr.start()
                    await guest_mgr.start()
                    snapshot = await host_mgr.send_file(_make_file(tmp_path / "fat.bin", 5_000))
                    await _wait_for(
                        lambda: (
                            host_mgr.get(snapshot.transfer_id) is not None
                            and host_mgr.get(snapshot.transfer_id).state  # type: ignore[union-attr]
                            is TransferState.REJECTED
                        )
                    )
                    assert guest_mgr.get(snapshot.transfer_id) is None
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_offer_expiry_and_terminal_purge(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                limits = _limits(
                    offer_timeout_seconds=0.2,
                    terminal_ttl_seconds=0.05,
                    ack_timeout_seconds=5.0,  # avoid offer resends in this test
                )
                transfer_pair = await make_transfer_pair(server, tmp_path, limits=limits)
                try:
                    snapshot = await transfer_pair.host_mgr.send_file(
                        _make_file(tmp_path / "stale.bin", 4_000)
                    )
                    transfer_id = snapshot.transfer_id
                    await _wait_for(lambda: transfer_pair.guest_mgr.get(transfer_id) is not None)
                    await asyncio.sleep(0.3)  # let the offer go stale
                    await transfer_pair.host_mgr._sweep()  # type: ignore[attr-defined]
                    assert transfer_pair.host_mgr.get(transfer_id).state is (  # type: ignore[union-attr]
                        TransferState.EXPIRED
                    )
                    await _wait_for(
                        lambda: (
                            transfer_pair.guest_mgr.get(transfer_id) is not None
                            and transfer_pair.guest_mgr.get(transfer_id).state  # type: ignore[union-attr]
                            is TransferState.CANCELLED
                        )
                    )
                    assert any(
                        "did not answer" in detail for detail in _states(transfer_pair.host_events)
                    )
                    # Terminal entries are purged once their TTL passes.
                    await asyncio.sleep(0.1)
                    await transfer_pair.host_mgr._sweep()  # type: ignore[attr-defined]
                    assert transfer_pair.host_mgr.get(transfer_id) is None
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_orphan_temps_removed_on_manager_start(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                storage = TransferStorage(
                    tmp_path / "guest-state", download_dir=str(tmp_path / "guest-dl")
                )
                storage.ensure_directories()
                orphan = storage.temp_dir / "tf_0badf00d.part"
                orphan.write_bytes(b"leftover from a crash")
                pair = await make_pair(server)
                host_storage = TransferStorage(
                    tmp_path / "host-state", download_dir=str(tmp_path / "host-dl")
                )
                host_mgr = TransferManager(pair.host, host_storage, _limits())
                guest_mgr = TransferManager(pair.guest, storage, _limits())
                transfer_pair = TransferPair(
                    pair, host_mgr, guest_mgr, [], [], host_storage, storage
                )
                try:
                    await host_mgr.start()
                    await guest_mgr.start()
                    assert not orphan.exists()
                finally:
                    await transfer_pair.close()

        run(scenario())


class TestConfidentiality:
    def test_wire_never_carries_plaintext_or_filenames(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    bodies: list[str] = []
                    for client in (
                        transfer_pair.pair.host_client,
                        transfer_pair.pair.guest_client,
                    ):
                        original = client.send_forward

                        async def spy(
                            channel: str,
                            body: str,
                            *,
                            _original=original,  # type: ignore[misc]
                        ) -> None:
                            bodies.append(body)
                            await _original(channel, body)

                        client.send_forward = spy  # type: ignore[method-assign]

                    source = _make_file(tmp_path / "classified-gadget.txt", 64_000, marker=True)
                    await _complete_transfer(transfer_pair, source)
                    assert bodies, "expected FILE_* frames on the wire"
                    marker_b64 = base64.b64encode(MARKER).decode()
                    for body in bodies:
                        assert MARKER.decode() not in body
                        assert marker_b64 not in body
                        assert "classified-gadget" not in body
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_logs_never_leak_contents_or_keys(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                with caplog.at_level(logging.DEBUG, logger="ghostlink"):
                    transfer_pair = await make_transfer_pair(server, tmp_path)
                    try:
                        source = _make_file(tmp_path / "logged.txt", 16_000, marker=True)
                        await _complete_transfer(transfer_pair, source)
                        session_key = transfer_pair.pair.host.session_key_copy()
                        assert session_key is not None
                    finally:
                        await transfer_pair.close()
            text_log = caplog.text
            assert MARKER.decode() not in text_log
            assert session_key.hex() not in text_log
            assert "classified" not in text_log  # filename stays out of logs too

        run(scenario())


class TestLocalValidation:
    def test_send_rejects_missing_and_directory_paths(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    from ghostlink.exceptions.transfer import TransferValidationError

                    with pytest.raises(TransferValidationError, match="regular file"):
                        await transfer_pair.host_mgr.send_file(tmp_path / "nope.bin")
                    with pytest.raises(TransferValidationError, match="regular file"):
                        await transfer_pair.host_mgr.send_file(tmp_path)
                    assert transfer_pair.host_mgr.list_transfers() == []
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_oversize_local_file_rejected_before_offer(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(
                    server, tmp_path, limits=_limits(max_file_size_bytes=4_000)
                )
                try:
                    from ghostlink.exceptions.transfer import TransferLimitError

                    with pytest.raises(TransferLimitError, match="limit"):
                        await transfer_pair.host_mgr.send_file(
                            _make_file(tmp_path / "huge.bin", 8_000)
                        )
                finally:
                    await transfer_pair.close()

        run(scenario())

    def test_double_start_and_close_idempotence(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                transfer_pair = await make_transfer_pair(server, tmp_path)
                try:
                    from ghostlink.exceptions.transfer import TransferError

                    with pytest.raises(TransferError, match="single-use"):
                        await transfer_pair.host_mgr.start()
                    await transfer_pair.host_mgr.close()
                    await transfer_pair.host_mgr.close()
                finally:
                    await transfer_pair.close()

        run(scenario())

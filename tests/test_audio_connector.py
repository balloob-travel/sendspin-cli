from __future__ import annotations

import asyncio
from types import SimpleNamespace

import sendspin.audio_connector as audio_connector
from sendspin.audio_connector import AudioStreamHandler


class _FakeWorker:
    instances: list[_FakeWorker] = []

    def __init__(
        self,
        *,
        audio_device: object,
        use_software_volume: bool,
        volume: int,
        muted: bool,
    ) -> None:
        self.audio_device = audio_device
        self.use_software_volume = use_software_volume
        self.volume = volume
        self.muted = muted
        self.running = False
        self.submitted: list[tuple[int, bytes | bytearray, object]] = []
        _FakeWorker.instances.append(self)

    def start(self, compute_play_time: object, compute_server_time: object) -> None:
        self.running = True
        self.compute_play_time = compute_play_time
        self.compute_server_time = compute_server_time

    def is_running(self) -> bool:
        return self.running

    def submit_chunk(self, server_timestamp_us: int, audio_data: bytes | bytearray, fmt: object) -> None:
        self.submitted.append((server_timestamp_us, audio_data, fmt))

    def clear(self) -> None:
        return

    def set_volume(self, volume: int, *, muted: bool) -> None:
        self.volume = volume
        self.muted = muted

    async def stop(self) -> None:
        self.running = False


class _FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.audio_chunk_listeners: list[object] = []
        self.stream_start_listeners: list[object] = []
        self.stream_end_listeners: list[object] = []
        self.stream_clear_listeners: list[object] = []

    def compute_play_time(self, timestamp_us: int) -> int:
        return timestamp_us

    def compute_server_time(self, timestamp_us: int) -> int:
        return timestamp_us

    async def send_player_state(self, **_: object) -> None:
        return

    def add_audio_chunk_listener(self, callback: object):
        return self._add_listener(self.audio_chunk_listeners, callback)

    def add_stream_start_listener(self, callback: object):
        return self._add_listener(self.stream_start_listeners, callback)

    def add_stream_end_listener(self, callback: object):
        return self._add_listener(self.stream_end_listeners, callback)

    def add_stream_clear_listener(self, callback: object):
        return self._add_listener(self.stream_clear_listeners, callback)

    @staticmethod
    def _add_listener(callbacks: list[object], callback: object):
        callbacks.append(callback)
        return lambda: None


def _make_format() -> SimpleNamespace:
    return SimpleNamespace(
        codec=SimpleNamespace(value="pcm"),
        pcm_format=SimpleNamespace(sample_rate=48_000, bit_depth=16, channels=2),
    )


def test_audio_worker_restarts_on_stream_start_after_connection_reset(monkeypatch) -> None:
    monkeypatch.setattr(audio_connector, "_AudioSyncWorker", _FakeWorker)
    _FakeWorker.instances.clear()

    async def exercise() -> None:
        handler = AudioStreamHandler(
            audio_device=SimpleNamespace(index=0, name="Fake Device"),
            volume=10,
            muted=False,
        )
        client = _FakeClient()
        handler.attach_client(client)
        handler.set_volume(37, muted=True)
        await asyncio.sleep(0)

        await handler.reset_connection()
        assert len(_FakeWorker.instances) == 1
        assert not _FakeWorker.instances[0].running

        fmt = _make_format()
        handler._on_stream_start(object())

        assert len(_FakeWorker.instances) == 2
        restarted_worker = _FakeWorker.instances[1]
        assert restarted_worker.running
        assert restarted_worker.volume == 37
        assert restarted_worker.muted is True

        handler._on_audio_chunk(123_456, b"payload", fmt)

        assert restarted_worker.submitted == [(123_456, b"payload", fmt)]

    asyncio.run(exercise())

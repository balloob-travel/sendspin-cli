# ruff: noqa: SLF001

from __future__ import annotations

import struct
from types import SimpleNamespace

from aiosendspin.client.client import PCMFormat
import pytest
from sounddevice import CallbackFlags

from sendspin.audio import AudioPlayer, PlaybackState


def _stereo16(*frames: tuple[int, int]) -> bytes:
    flat_samples = [sample for frame in frames for sample in frame]
    return struct.pack("<" + ("h" * len(flat_samples)), *flat_samples)


def _frame_duration_us(frame_count: int, sample_rate: int) -> int:
    return (frame_count * 1_000_000) // sample_rate


@pytest.fixture
def audio_player() -> tuple[AudioPlayer, PCMFormat]:
    player = AudioPlayer(lambda timestamp_us: timestamp_us, lambda timestamp_us: timestamp_us)
    fmt = PCMFormat(sample_rate=48_000, channels=2, bit_depth=16)
    player._format = fmt
    player._reset_output_frame_scratch()
    player._playback_state = PlaybackState.PLAYING
    return player, fmt


def test_audio_callback_writes_pcm_directly_across_chunks(
    audio_player: tuple[AudioPlayer, PCMFormat],
) -> None:
    player, fmt = audio_player
    first = _stereo16((1, 2), (3, 4))
    second = _stereo16((5, 6))

    player.submit(0, first)
    player.submit(_frame_duration_us(2, fmt.sample_rate), second)
    player._playback_state = PlaybackState.PLAYING

    output = bytearray(fmt.frame_size * 4)
    player._audio_callback(
        memoryview(output),
        4,
        SimpleNamespace(outputBufferDacTime=0.0),
        CallbackFlags(),
    )

    assert bytes(output) == first + second + (b"\x00" * fmt.frame_size)
    assert player._server_ts_cursor_us == _frame_duration_us(3, fmt.sample_rate)


def test_audio_callback_drop_correction_duplicates_last_frame(
    audio_player: tuple[AudioPlayer, PCMFormat],
) -> None:
    player, fmt = audio_player
    payload = _stereo16((1, 11), (2, 12), (3, 13), (4, 14), (5, 15))

    player.submit(0, payload)
    player._playback_state = PlaybackState.PLAYING

    warmup = bytearray(fmt.frame_size)
    player._audio_callback(
        memoryview(warmup),
        1,
        SimpleNamespace(outputBufferDacTime=0.0),
        CallbackFlags(),
    )
    assert bytes(warmup) == _stereo16((1, 11))

    player._drop_every_n_frames = 1
    player._frames_until_next_drop = 1

    corrected = bytearray(fmt.frame_size * 2)
    player._audio_callback(
        memoryview(corrected),
        2,
        SimpleNamespace(outputBufferDacTime=0.0),
        CallbackFlags(),
    )

    assert bytes(corrected) == _stereo16((2, 12), (2, 12))

    player._drop_every_n_frames = 0
    player._frames_until_next_drop = 0

    tail = bytearray(fmt.frame_size)
    player._audio_callback(
        memoryview(tail),
        1,
        SimpleNamespace(outputBufferDacTime=0.0),
        CallbackFlags(),
    )

    assert bytes(tail) == _stereo16((5, 15))

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np

from sendspin.decoder import FlacDecoder


FIXTURE_FLAC = Path(__file__).resolve().parent / "fixtures" / "almost_silent.flac"


def _make_audio_format(*, sample_rate: int, channels: int, bit_depth: int, codec_header: bytes):
    return SimpleNamespace(
        pcm_format=SimpleNamespace(
            sample_rate=sample_rate,
            channels=channels,
            bit_depth=bit_depth,
        ),
        codec_header=codec_header,
    )


def _pack_i24_le(values: list[int]) -> bytes:
    return b"".join(value.to_bytes(3, "little", signed=True) for value in values)


def _reference_frame_to_pcm(frame: av.AudioFrame, bit_depth: int) -> bytes:
    src_bits = frame.format.bits
    channel_count = frame.layout.nb_channels
    total_samples = frame.samples * channel_count

    if src_bits == 16:
        dtype = np.int16
    elif src_bits == 32:
        dtype = np.int32
    else:
        raise AssertionError(f"Unexpected source bit depth: {src_bits}")

    if frame.format.is_planar:
        samples = np.empty(total_samples, dtype=dtype)
        for ch in range(channel_count):
            plane = np.frombuffer(
                memoryview(frame.planes[ch]),
                dtype=dtype,
                count=frame.samples,
            )
            samples[ch::channel_count] = plane
    else:
        samples = np.frombuffer(
            memoryview(frame.planes[0]),
            dtype=dtype,
            count=total_samples,
        ).copy()

    if src_bits == bit_depth:
        return samples.tobytes()

    if src_bits == 32 and bit_depth == 24:
        raw = (samples.astype(np.int32) >> 8).astype("<i4").view(np.uint8).reshape(-1, 4)
        return raw[:, :3].tobytes()

    if src_bits == 32 and bit_depth == 16:
        return (samples.astype(np.int32) >> 16).astype(np.int16).tobytes()

    if src_bits == 16 and bit_depth == 24:
        raw = (samples.astype(np.int32) << 8).astype("<i4").view(np.uint8).reshape(-1, 4)
        return raw[:, :3].tobytes()

    if src_bits == 16 and bit_depth == 32:
        return (samples.astype(np.int32) << 16).tobytes()

    raise AssertionError(f"Unexpected conversion: {src_bits} -> {bit_depth}")


def test_packed_s32_to_s16_uses_expected_high_words() -> None:
    decoder = FlacDecoder(
        _make_audio_format(sample_rate=48_000, channels=1, bit_depth=16, codec_header=b"")
    )
    samples = np.array([[0, 1, -2, 32767, -32768, 256]], dtype=np.int32) << 16
    frame = av.AudioFrame.from_ndarray(samples, format="s32", layout="mono")

    result = decoder._frame_to_pcm(frame)

    expected = np.array([0, 1, -2, 32767, -32768, 256], dtype="<i2").tobytes()
    assert isinstance(result, bytearray)
    assert bytes(result) == expected


def test_planar_s32_to_s24_interleaves_channels() -> None:
    decoder = FlacDecoder(
        _make_audio_format(sample_rate=48_000, channels=2, bit_depth=24, codec_header=b"")
    )
    left = np.array([1, -2, 0x7FFFFF], dtype=np.int32) << 8
    right = np.array([3, -4, -0x800000], dtype=np.int32) << 8
    frame = av.AudioFrame.from_ndarray(
        np.vstack([left, right]),
        format="s32p",
        layout="stereo",
    )

    result = decoder._frame_to_pcm(frame)

    expected = _pack_i24_le([1, 3, -2, -4, 0x7FFFFF, -0x800000])
    assert bytes(result) == expected


def test_planar_s16_to_s32_expands_samples() -> None:
    decoder = FlacDecoder(
        _make_audio_format(sample_rate=48_000, channels=2, bit_depth=32, codec_header=b"")
    )
    left = np.array([1, -2, 3], dtype=np.int16)
    right = np.array([-4, 5, -6], dtype=np.int16)
    frame = av.AudioFrame.from_ndarray(
        np.vstack([left, right]),
        format="s16p",
        layout="stereo",
    )

    result = decoder._frame_to_pcm(frame)

    interleaved = np.array([1, -4, -2, 5, 3, -6], dtype=np.int32) << 16
    assert bytes(result) == interleaved.astype("<i4").tobytes()


def test_decode_matches_reference_conversion_for_real_flac_packet() -> None:
    container = av.open(str(FIXTURE_FLAC))
    stream = container.streams.audio[0]
    packet = next(packet for packet in container.demux(stream) if packet.dts is not None)
    codec_header = b"fLaC\x80\x00\x00\x22" + (stream.codec_context.extradata or b"")
    fmt = _make_audio_format(
        sample_rate=stream.codec_context.sample_rate,
        channels=stream.codec_context.channels,
        bit_depth=16,
        codec_header=codec_header,
    )
    decoder = FlacDecoder(fmt)

    reference_codec = av.CodecContext.create("flac", "r")
    reference_codec.extradata = decoder._build_extradata()
    reference_codec.open()
    reference_frames = reference_codec.decode(av.Packet(bytes(packet)))
    expected = b"".join(_reference_frame_to_pcm(frame, 16) for frame in reference_frames)

    result = decoder.decode(bytes(packet))

    assert isinstance(result, bytearray)
    assert bytes(result) == expected

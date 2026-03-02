"""Audio decoders for compressed formats (FLAC, etc.)."""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING

import av

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat

logger = logging.getLogger(__name__)

# FLAC header layout:
# - fLaC marker: 4 bytes
# - Metadata block header: 4 bytes (last-block flag + type + 24-bit length)
# - STREAMINFO block: 34 bytes
_FLAC_HEADER_PREFIX_SIZE = 8  # fLaC marker + metadata block header


class FlacDecoder:
    """Decoder for FLAC audio frames.

    Uses a persistent PyAV codec context to decode individual FLAC frames
    to PCM samples without per-frame container overhead.
    """

    def __init__(self, audio_format: AudioFormat) -> None:
        """Initialize the FLAC decoder.

        Args:
            audio_format: Audio format from stream start, including codec_header.
        """
        self._format = audio_format
        self._sample_rate = audio_format.pcm_format.sample_rate
        self._channels = audio_format.pcm_format.channels
        self._bit_depth = audio_format.pcm_format.bit_depth
        self._codec_header = audio_format.codec_header

        # Bytes per sample for output PCM
        self._bytes_per_sample = self._bit_depth // 8
        self._frame_size = self._bytes_per_sample * self._channels

        # Track total samples decoded for debugging
        self._samples_decoded = 0

        # Create persistent codec context
        self._codec_ctx = av.CodecContext.create("flac", "r")
        self._codec_ctx.extradata = self._build_extradata()
        self._codec_ctx.open()

        # Use FFmpeg PCM encoder for packed 24-bit output.
        self._s24_encoder: av.CodecContext | None = None
        self._s24_encoder_layout: str | None = None
        self._s24_encoder_input_format: str | None = None

        # Use FFmpeg audio resampler for 16/32-bit PCM conversion.
        self._pcm_target_format = {16: "s16", 32: "s32"}.get(self._bit_depth)
        self._pcm_resampler: av.AudioResampler | None = None
        self._pcm_resampler_layout: str | None = None

    def decode(self, flac_frame: bytes) -> bytes:
        """Decode a FLAC frame to PCM samples.

        Args:
            flac_frame: Raw FLAC frame bytes.

        Returns:
            PCM audio bytes in the format specified by audio_format.
        """
        try:
            packet = av.Packet(flac_frame)
            frames = self._codec_ctx.decode(packet)  # type: ignore[attr-defined]

            pcm_bytes = bytearray()
            for frame in frames:
                pcm_bytes.extend(self._frame_to_pcm(frame))

            return bytes(pcm_bytes)

        except av.FFmpegError as e:
            logger.warning("FLAC decode error: %s", e)
            return b""

    def _build_extradata(self) -> bytes:
        """Build the 34-byte FLAC STREAMINFO for codec extradata.

        If the server provided a codec_header (fLaC + block header + STREAMINFO),
        extract the 34-byte STREAMINFO. Otherwise, generate it from params.
        """
        if self._codec_header and len(self._codec_header) >= _FLAC_HEADER_PREFIX_SIZE + 34:
            return self._codec_header[_FLAC_HEADER_PREFIX_SIZE : _FLAC_HEADER_PREFIX_SIZE + 34]

        # Fallback: generate STREAMINFO from parameters (codec_header is optional per spec)
        streaminfo = bytearray(34)
        block_size = 4096
        streaminfo[0:2] = struct.pack(">H", block_size)
        streaminfo[2:4] = struct.pack(">H", block_size)
        packed = (
            (self._sample_rate << 12) | ((self._channels - 1) << 9) | ((self._bit_depth - 1) << 4)
        )
        streaminfo[10:14] = struct.pack(">I", packed)
        return bytes(streaminfo)

    def _frame_to_pcm(self, frame: av.AudioFrame) -> bytes:
        """Convert an av.AudioFrame to PCM bytes.

        For 24-bit output, use FFmpeg's pcm_s24le encoder to produce packed
        3-byte samples. For 16/32-bit output, convert via FFmpeg resampler.
        """
        if self._bit_depth == 24:
            return self._encode_24bit(frame)
        return self._convert_with_resampler(frame)

    def _encode_24bit(self, frame: av.AudioFrame) -> bytes:
        """Encode an audio frame to packed 24-bit little-endian PCM."""
        layout_name = frame.layout.name
        input_format_name = frame.format.name

        if (
            self._s24_encoder is None
            or self._s24_encoder_layout != layout_name
            or self._s24_encoder_input_format != input_format_name
        ):
            self._s24_encoder = av.CodecContext.create("pcm_s24le", "w")
            self._s24_encoder.sample_rate = self._sample_rate  # type: ignore[attr-defined]
            self._s24_encoder.layout = frame.layout  # type: ignore[attr-defined]
            self._s24_encoder.format = "s32"  # type: ignore[attr-defined]
            self._s24_encoder.open()
            self._s24_encoder_layout = layout_name
            self._s24_encoder_input_format = input_format_name
            logger.info(
                "Initialized 24-bit PCM encoder: layout=%s input_format=%s channels=%d",
                layout_name,
                input_format_name,
                self._channels,
            )

        packets = self._s24_encoder.encode(frame)  # type: ignore[attr-defined]
        if not packets:
            return b""
        return b"".join(bytes(packet) for packet in packets)

    def _convert_with_resampler(self, frame: av.AudioFrame) -> bytes:
        """Convert a frame to packed 16/32-bit PCM via FFmpeg resampler."""
        if self._pcm_target_format is None:
            logger.warning(
                "Unsupported target bit depth for FFmpeg conversion: %d", self._bit_depth
            )
            return b""

        layout_name = frame.layout.name
        if self._pcm_resampler is None or self._pcm_resampler_layout != layout_name:
            self._pcm_resampler = av.AudioResampler(
                format=self._pcm_target_format,
                layout=frame.layout,
                rate=self._sample_rate,
            )
            self._pcm_resampler_layout = layout_name
            logger.info(
                "Initialized PCM resampler: target_format=%s layout=%s channels=%d",
                self._pcm_target_format,
                layout_name,
                self._channels,
            )

        output_frames = self._pcm_resampler.resample(frame)
        pcm_bytes = bytearray()
        for output_frame in output_frames:
            pcm_bytes.extend(self._extract_frame_bytes(output_frame))
        return bytes(pcm_bytes)

    def _extract_frame_bytes(self, frame: av.AudioFrame) -> bytes:
        """Extract interleaved PCM bytes from a frame while ignoring plane padding."""
        bytes_per_sample = self._bit_depth // 8
        actual_bytes = frame.samples * self._channels * bytes_per_sample

        if not frame.format.is_planar:
            return bytes(frame.planes[0])[:actual_bytes]

        plane_bytes = frame.samples * bytes_per_sample
        planes = [bytes(frame.planes[ch])[:plane_bytes] for ch in range(self._channels)]
        interleaved = bytearray(actual_bytes)
        for sample_idx in range(frame.samples):
            src_start = sample_idx * bytes_per_sample
            dst_base = sample_idx * self._channels * bytes_per_sample
            for channel_idx, plane in enumerate(planes):
                dst_start = dst_base + channel_idx * bytes_per_sample
                interleaved[dst_start : dst_start + bytes_per_sample] = plane[
                    src_start : src_start + bytes_per_sample
                ]
        return bytes(interleaved)

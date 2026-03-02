"""Audio decoders for compressed formats (FLAC, etc.)."""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING

import av
import numpy as np

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
        if self._bit_depth == 24:
            layout = "mono" if self._channels == 1 else "stereo"
            self._s24_encoder = av.CodecContext.create("pcm_s24le", "w")
            self._s24_encoder.sample_rate = self._sample_rate  # type: ignore[attr-defined]
            self._s24_encoder.layout = layout  # type: ignore[attr-defined]
            self._s24_encoder.format = "s32"  # type: ignore[attr-defined]
            self._s24_encoder.open()
            logger.info(
                "Initialized 24-bit PCM encoder: _s24_encoder=%r _s24_encoder.sample_rate=%r _s24_encoder.layout=%r _s24_encoder.format=%r codec=pcm_s24le channels=%d",
                self._s24_encoder,
                getattr(self._s24_encoder, "sample_rate", None),
                getattr(self._s24_encoder, "layout", None),
                getattr(self._s24_encoder, "format", None),
                self._channels,
            )

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
        3-byte samples. For other target depths, convert in numpy.
        """
        if self._bit_depth == 24:
            return self._encode_24bit(frame)

        samples_per_channel = frame.samples

        # Get source format info
        src_format = frame.format.name  # e.g., 's32', 's32p', 's16', 's16p'
        is_planar = frame.format.is_planar

        # Determine source bytes per sample from format
        # FFmpeg typically decodes FLAC to s32/s32p
        is_16bit_source = "16" in src_format
        src_bytes_per_sample = 2 if is_16bit_source else 4

        # Read samples from frame
        samples: np.ndarray[tuple[int], np.dtype[np.int16 | np.int32]]
        if is_planar:
            # Planar: each channel in separate plane, interleave them
            src_bytes_per_plane = samples_per_channel * src_bytes_per_sample
            if is_16bit_source:
                samples = np.empty(samples_per_channel * self._channels, dtype=np.int16)
                for ch in range(self._channels):
                    plane_data = np.frombuffer(
                        bytes(frame.planes[ch])[:src_bytes_per_plane], dtype=np.int16
                    )
                    samples[ch :: self._channels] = plane_data
            else:
                samples = np.empty(samples_per_channel * self._channels, dtype=np.int32)
                for ch in range(self._channels):
                    plane_data = np.frombuffer(
                        bytes(frame.planes[ch])[:src_bytes_per_plane], dtype=np.int32
                    )
                    samples[ch :: self._channels] = plane_data
        else:
            # Packed: all channels interleaved in plane 0
            total_src_bytes = samples_per_channel * self._channels * src_bytes_per_sample
            if is_16bit_source:
                samples = np.frombuffer(
                    bytes(frame.planes[0])[:total_src_bytes], dtype=np.int16
                ).copy()
            else:
                samples = np.frombuffer(
                    bytes(frame.planes[0])[:total_src_bytes], dtype=np.int32
                ).copy()

        # Convert to target bit depth
        return self._convert_bit_depth(samples, src_bytes_per_sample * 8)

    def _convert_bit_depth(self, samples: np.ndarray, src_bits: int) -> bytes:
        """Convert samples from source bit depth to target bit depth."""
        if src_bits == self._bit_depth:
            return samples.tobytes()

        # Convert from source to target bit depth
        # FFmpeg stores samples left-justified, so shift right to normalize
        if src_bits == 32 and self._bit_depth == 16:
            # 32-bit to 16-bit: shift right 16 bits
            samples_16 = (samples.astype(np.int32) >> 16).astype(np.int16)
            return samples_16.tobytes()

        if src_bits == 16 and self._bit_depth == 32:
            # 16-bit to 32-bit: shift left 16 bits
            samples_32 = samples.astype(np.int32) << 16
            return samples_32.tobytes()

        # Fallback: just return as-is (may not work correctly)
        logger.warning("Unsupported bit depth conversion: %d -> %d", src_bits, self._bit_depth)
        return samples.tobytes()

    def _encode_24bit(self, frame: av.AudioFrame) -> bytes:
        """Encode an audio frame to packed 24-bit little-endian PCM."""
        assert self._s24_encoder is not None
        packets = self._s24_encoder.encode(frame)  # type: ignore[attr-defined]
        if not packets:
            return b""
        return b"".join(bytes(packet) for packet in packets)

"""Tests for supported audio format preference ordering."""

from __future__ import annotations

from aiosendspin.models.types import AudioCodec

from sendspin.audio import detect_supported_audio_formats


def test_detect_supported_audio_formats_prefers_24bit_by_default(monkeypatch) -> None:
    """Default ordering should keep 24-bit formats first."""
    monkeypatch.setattr("sendspin.audio._check_format", lambda *_args: True)

    formats = detect_supported_audio_formats()

    flac_formats = [fmt for fmt in formats if fmt.codec == AudioCodec.FLAC]
    pcm_formats = [fmt for fmt in formats if fmt.codec == AudioCodec.PCM]

    assert flac_formats[0].bit_depth == 24
    assert pcm_formats[0].bit_depth == 24


def test_detect_supported_audio_formats_prefers_16bit_for_software_volume(
    monkeypatch,
) -> None:
    """Software-volume mode should advertise 16-bit formats first."""
    monkeypatch.setattr("sendspin.audio._check_format", lambda *_args: True)

    formats = detect_supported_audio_formats(prefer_16bit=True)

    flac_formats = [fmt for fmt in formats if fmt.codec == AudioCodec.FLAC]
    pcm_formats = [fmt for fmt in formats if fmt.codec == AudioCodec.PCM]

    assert flac_formats[0].bit_depth == 16
    assert pcm_formats[0].bit_depth == 16

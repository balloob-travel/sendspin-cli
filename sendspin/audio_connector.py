"""Audio connector for connecting audio playback to a Sendspin client."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiosendspin.models.core import StreamStartMessage
from aiosendspin.models.types import AudioCodec, ClientStateType, Roles

from sendspin.audio import AudioDevice, AudioPlayer
from sendspin.decoder import FlacDecoder
from sendspin.hardware_volume import HardwareVolumeController
from sendspin.utils import create_task

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat, SendspinClient


logger = logging.getLogger(__name__)


class _FlacDecodeWorker:
    """Decode FLAC chunks on a dedicated single worker thread."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        audio_format: AudioFormat,
        on_decoded: Callable[[int, bytes], None],
    ) -> None:
        self._loop = loop
        self._audio_format = audio_format
        self._on_decoded = on_decoded
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sendspin-flac"
        )
        self._decoder: FlacDecoder | None = None
        self._closed = False
        self._generation = 0
        self._next_sequence = 0
        self._next_expected_sequence = 0
        self._pending: dict[int, tuple[int, bytes]] = {}

    def submit(self, server_timestamp_us: int, flac_data: bytes) -> None:
        """Queue a FLAC chunk for decode."""
        if self._closed:
            return
        sequence = self._next_sequence
        self._next_sequence += 1
        generation = self._generation
        future = self._executor.submit(
            self._decode_one, generation, sequence, server_timestamp_us, flac_data
        )
        future.add_done_callback(self._on_decode_done)

    def discard_pending(self) -> None:
        """Drop queued/decoded results from previous stream timeline."""
        if self._closed:
            return
        self._generation += 1
        self._next_expected_sequence = self._next_sequence
        self._pending.clear()

    def close(self, *, wait: bool) -> None:
        """Stop the worker and prevent any further decoded delivery."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _decode_one(
        self,
        generation: int,
        sequence: int,
        server_timestamp_us: int,
        flac_data: bytes,
    ) -> tuple[int, int, int, bytes]:
        if self._decoder is None:
            self._decoder = FlacDecoder(self._audio_format)
        pcm_data = self._decoder.decode(flac_data)
        return generation, sequence, server_timestamp_us, pcm_data

    def _on_decode_done(
        self,
        future: concurrent.futures.Future[tuple[int, int, int, bytes]],
    ) -> None:
        try:
            result = future.result()
        except Exception:
            logger.exception("FLAC decode worker failed")
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver_decoded, *result)
        except RuntimeError:
            # Loop may already be closed during shutdown.
            pass

    def _deliver_decoded(
        self,
        generation: int,
        sequence: int,
        server_timestamp_us: int,
        pcm_data: bytes,
    ) -> None:
        if self._closed or generation != self._generation:
            return

        self._pending[sequence] = (server_timestamp_us, pcm_data)
        while self._next_expected_sequence in self._pending:
            next_server_ts, next_pcm = self._pending.pop(self._next_expected_sequence)
            self._next_expected_sequence += 1
            if next_pcm:
                self._on_decoded(next_server_ts, next_pcm)


class AudioStreamHandler:
    """Manages audio playback state and stream lifecycle.

    This handler connects to a SendspinClient and manages audio playback
    by listening for audio chunks, stream start/end events, and handling
    format changes. Supports PCM and FLAC codecs.

    When hardware volume is enabled, the handler owns a HardwareVolumeController
    and routes volume changes to it, keeping the software player at full volume.
    """

    def __init__(
        self,
        audio_device: AudioDevice,
        *,
        volume: int = 100,
        muted: bool = False,
        on_event: Callable[[str], None] | None = None,
        on_format_change: Callable[[str | None, int, int, int], None] | None = None,
        on_volume_change: Callable[[int, bool], None] | None = None,
        use_hardware_volume: bool = False,
    ) -> None:
        """Initialize the audio stream handler.

        Args:
            audio_device: Audio device to use for playback.
            volume: Initial volume (0-100).
            muted: Initial muted state.
            on_event: Callback for stream lifecycle events ("start" or "stop").
            on_format_change: Callback for format changes (codec, sample_rate, bit_depth, channels).
            on_volume_change: Callback for volume changes.
            use_hardware_volume: Whether to use hardware volume control if available.
        """
        self._audio_device = audio_device
        self._volume = volume
        self._muted = muted
        self._on_event = on_event
        self._on_format_change = on_format_change
        self._on_volume_change = on_volume_change
        self._client: SendspinClient | None = None
        self.audio_player: AudioPlayer | None = None
        self._current_format: AudioFormat | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flac_worker: _FlacDecodeWorker | None = None
        self._stream_active = False  # Track if stream is currently active

        self._hw_volume: HardwareVolumeController | None = None
        if use_hardware_volume:
            self._hw_volume = HardwareVolumeController()

    @property
    def volume(self) -> int:
        """Current logical volume (what the server/user sees)."""
        return self._volume

    @property
    def muted(self) -> bool:
        """Current logical muted state (what the server/user sees)."""
        return self._muted

    async def read_initial_volume(self) -> None:
        """Read the effective initial volume state.

        When hardware volume is active, reads the current system volume/mute
        state. Otherwise the constructor values are used as-is.
        """
        if self._hw_volume is None:
            return

        self._volume, self._muted = await self._hw_volume.get_state()

    async def start_volume_monitor(self) -> None:
        """Start hardware volume monitoring if applicable."""
        if self._hw_volume is not None:
            await self._hw_volume.start_monitoring(self._on_hw_volume_change)

    @property
    def use_hardware_volume(self) -> bool:
        """Whether this handler is using hardware volume control."""
        return self._hw_volume is not None

    def set_volume(self, volume: int, *, muted: bool) -> None:
        """Set the volume and muted state.

        Routes to the hardware controller when active, otherwise updates the
        software audio player directly. Notifies the server and fires the
        on_volume_change callback.

        Args:
            volume: Volume level (0-100).
            muted: Muted state.
        """
        if self._hw_volume is not None:
            create_task(self._hw_volume.set_state(volume, muted=muted))
            return
        self._volume = volume
        self._muted = muted
        if self.audio_player is not None:
            self.audio_player.set_volume(volume, muted=muted)
        self.send_player_volume()
        if self._on_volume_change is not None:
            self._on_volume_change(volume, muted)

    def _on_hw_volume_change(self, volume: int, muted: bool) -> None:
        """Handle external hardware volume changes from the controller."""
        self._volume = volume
        self._muted = muted
        self.send_player_volume()
        if self._on_volume_change is not None:
            self._on_volume_change(volume, muted)

    def send_player_volume(self) -> None:
        """Send current player volume/mute state to the server."""
        if self._client is not None and self._client.connected:
            create_task(
                self._client.send_player_state(
                    state=ClientStateType.SYNCHRONIZED,
                    volume=self._volume,
                    muted=self._muted,
                )
            )

    def attach_client(self, client: SendspinClient) -> list[Callable[[], None]]:
        """Attach to a SendspinClient and register listeners.

        Args:
            client: The Sendspin client to attach to.

        Returns:
            List of unsubscribe functions for all registered listeners.
        """
        self._client = client

        # Register listeners directly with the client
        return [
            client.add_audio_chunk_listener(self._on_audio_chunk),
            client.add_stream_start_listener(self._on_stream_start),
            client.add_stream_end_listener(self._on_stream_end),
            client.add_stream_clear_listener(self._on_stream_clear),
        ]

    def _on_audio_chunk(
        self, server_timestamp_us: int, audio_data: bytes, fmt: AudioFormat
    ) -> None:
        """Handle incoming audio chunks.

        For PCM codec, audio_data is passed directly to the player.
        For FLAC codec, audio_data is decoded to PCM on a worker thread first.
        """
        assert self._client is not None, "Received audio chunk but client is not attached"

        pcm_format = fmt.pcm_format
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop

        # Initialize or reconfigure audio player if format changed
        if self.audio_player is None or self._current_format != fmt:
            if self.audio_player is not None:
                self.audio_player.clear()

            self.audio_player = AudioPlayer(
                loop, self._client.compute_play_time, self._client.compute_server_time
            )
            self.audio_player.set_format(fmt, device=self._audio_device)
            self._current_format = fmt

            # Initialize/destroy FLAC decode worker as needed.
            self._shutdown_flac_worker(wait=False)
            if fmt.codec == AudioCodec.FLAC:
                self._flac_worker = _FlacDecodeWorker(loop, fmt, self._submit_decoded_chunk)
                logger.info(
                    "Initialized FLAC decode worker for %dHz/%d-bit/%dch",
                    pcm_format.sample_rate,
                    pcm_format.bit_depth,
                    pcm_format.channels,
                )

            if self._hw_volume is None:
                self.audio_player.set_volume(self._volume, muted=self._muted)

            if self._on_format_change is not None:
                self._on_format_change(
                    fmt.codec.value,
                    pcm_format.sample_rate,
                    pcm_format.bit_depth,
                    pcm_format.channels,
                )

        # Decode FLAC on dedicated worker thread if needed.
        if fmt.codec == AudioCodec.FLAC:
            if self._flac_worker is None:
                self._flac_worker = _FlacDecodeWorker(loop, fmt, self._submit_decoded_chunk)
                logger.info(
                    "Reinitialized FLAC decode worker for %dHz/%d-bit/%dch",
                    pcm_format.sample_rate,
                    pcm_format.bit_depth,
                    pcm_format.channels,
                )
            self._flac_worker.submit(server_timestamp_us, audio_data)
            return

        # Submit audio chunk - AudioPlayer handles timing.
        self.audio_player.async_submit(server_timestamp_us, audio_data)

    def _submit_decoded_chunk(self, server_timestamp_us: int, pcm_data: bytes) -> None:
        """Submit decoded FLAC PCM back on event loop thread."""
        if not pcm_data:
            logger.debug("FLAC decode returned empty, skipping chunk")
            return
        if self.audio_player is None:
            return
        self.audio_player.async_submit(server_timestamp_us, pcm_data)

    def _shutdown_flac_worker(self, *, wait: bool) -> None:
        """Stop and clear FLAC decode worker."""
        worker = self._flac_worker
        self._flac_worker = None
        if worker is not None:
            worker.close(wait=wait)

    def _discard_pending_flac_results(self) -> None:
        """Discard pending decoded FLAC chunks that no longer match timeline."""
        if self._flac_worker is not None:
            self._flac_worker.discard_pending()

    def _on_stream_start(self, _message: StreamStartMessage) -> None:
        """Handle stream start by clearing stale audio chunks."""
        self._discard_pending_flac_results()
        if self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream start")

        # Fire event only on transition from inactive to active
        if not self._stream_active:
            self._stream_active = True
            if self._on_event:
                self._on_event("start")

    def _on_stream_end(self, roles: list[str] | None) -> None:
        """Handle stream end by clearing audio queue."""
        # For the CLI player, we only care about the player role
        if roles is not None and Roles.PLAYER.value not in roles:
            return

        self._discard_pending_flac_results()
        if self.audio_player is not None:
            self.audio_player.clear()
            logger.debug("Cleared audio queue on stream end")

        # Fire event only on transition from active to inactive
        if self._stream_active:
            self._stream_active = False
            if self._on_event:
                self._on_event("stop")

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        """Handle stream clear by clearing audio queue (e.g., for seek operations)."""
        # For the CLI player, we only care about the player role
        if roles is None or Roles.PLAYER.value in roles:
            self._discard_pending_flac_results()
            if self.audio_player is not None:
                self.audio_player.clear()
                logger.debug("Cleared audio queue on stream clear")

    def clear_queue(self) -> None:
        """Clear the audio queue to prevent desync."""
        self._discard_pending_flac_results()
        if self.audio_player is not None:
            self.audio_player.clear()

    async def cleanup(self) -> None:
        """Stop audio player, hardware monitoring, and clear resources."""
        if self._hw_volume is not None:
            await self._hw_volume.stop_monitoring()

        # Fire stop event if stream was active
        if self._stream_active:
            self._stream_active = False
            if self._on_event:
                self._on_event("stop")

        self._shutdown_flac_worker(wait=True)

        if self.audio_player is not None:
            await self.audio_player.stop()
            self.audio_player = None
        self._current_format = None
        self._loop = None

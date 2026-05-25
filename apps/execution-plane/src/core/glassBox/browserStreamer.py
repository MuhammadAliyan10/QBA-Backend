"""BrowserStreamer - CDP Screencast with NATS Transport"""

import asyncio
import logging
import time
import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from enum import Enum

from playwright.async_api import Page, CDPSession

logger = logging.getLogger("glassBox.streamer")


@dataclass
class StreamConfig:
    """Streaming configuration."""
    format: str = "jpeg"
    quality: int = 80
    max_width: int = 1280
    max_height: int = 720
    target_fps: int = 15
    min_frame_interval_ms: int = 50
    nats_url: str = "nats://localhost:4222"
    subject_prefix: str = "bot.stream"
    skip_when_no_subscribers: bool = True
    frame_buffer_size: int = 3

    @property
    def frame_interval(self) -> float:
        return max(1.0 / self.target_fps, self.min_frame_interval_ms / 1000.0)


class StreamerState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    STREAMING = "streaming"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class NATSPublisher:
    """NATS JetStream publisher for binary frames."""

    def __init__(self, url: str = "nats://localhost:4222"):
        self.url = url
        self._nc = None
        self._connected = False

    async def connect(self):
        try:
            import nats
            self._nc = await nats.connect(self.url)
            self._connected = True
            logger.info(f"[NATS] Connected to {self.url}")
        except ImportError:
            logger.warning("[NATS] nats-py not installed")
            self._connected = False
        except Exception as e:
            logger.warning(f"[NATS] Connection failed: {e}")
            self._connected = False

    async def disconnect(self):
        if self._nc:
            await self._nc.close()
            self._connected = False

    async def publish(self, subject: str, data: bytes, headers: dict[str, str] = None):
        if not self._connected:
            return False
        try:
            await self._nc.publish(subject, data)
            return True
        except Exception as e:
            logger.warning(f"[NATS] Publish failed: {e}")
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    async def has_subscribers(self, subject: str) -> bool:
        return True  # TODO: Implement proper subscriber counting


class BrowserStreamer:
    """CDP Screencast → NATS Transport"""

    MAX_CANDIDATES = 100
    MAX_IFRAME_DEPTH = 3

    def __init__(
        self,
        page: Page,
        workflow_id: str,
        config: StreamConfig = None,
        publisher: NATSPublisher = None
    ):
        self.page = page
        self.workflow_id = workflow_id
        self.config = config or StreamConfig()
        self.publisher = publisher

        self._cdp: Optional[CDPSession] = None
        self._state = StreamerState.IDLE
        self._streaming_task: Optional[asyncio.Task] = None

        self._frame_count = 0
        self._last_frame_time = 0.0
        self._total_bytes = 0
        self._dropped_frames = 0
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.frame_buffer_size)

    @property
    def state(self) -> StreamerState:
        return self._state

    @property
    def subject(self) -> str:
        return f"{self.config.subject_prefix}.{self.workflow_id}"

    @property
    def stats(self) -> Dict:
        return {
            "state": self._state.value,
            "frame_count": self._frame_count,
            "dropped_frames": self._dropped_frames,
            "total_bytes": self._total_bytes,
        }

    async def start(self):
        if self._state != StreamerState.IDLE:
            return

        self._state = StreamerState.STARTING

        try:
            if not self.publisher:
                self.publisher = NATSPublisher(self.config.nats_url)
                await self.publisher.connect()

            self._cdp = await self.page.context.new_cdp_session(self.page)
            self._cdp.on("Page.screencastFrame", self._on_frame)

            await self._cdp.send("Page.startScreencast", {
                "format": self.config.format,
                "quality": self.config.quality,
                "maxWidth": self.config.max_width,
                "maxHeight": self.config.max_height,
                "everyNthFrame": 1
            })

            self._streaming_task = asyncio.create_task(self._process_frames())
            self._state = StreamerState.STREAMING
            logger.info(f"[Streamer] Started on {self.subject}")

        except Exception as e:
            self._state = StreamerState.ERROR
            logger.error(f"[Streamer] Failed to start: {e}")
            raise

    async def stop(self):
        if self._state not in [StreamerState.STREAMING, StreamerState.PAUSED]:
            return

        self._state = StreamerState.STOPPING

        try:
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
                await self._cdp.detach()
                self._cdp = None

            if self._streaming_task:
                self._streaming_task.cancel()
                try:
                    await self._streaming_task
                except asyncio.CancelledError:
                    pass
                self._streaming_task = None

            if self.publisher:
                await self.publisher.disconnect()

            self._state = StreamerState.STOPPED
            logger.info(f"[Streamer] Stopped. Stats: {self.stats}")

        except Exception as e:
            self._state = StreamerState.ERROR
            logger.error(f"[Streamer] Error stopping: {e}")

    async def pause(self):
        if self._state == StreamerState.STREAMING:
            self._state = StreamerState.PAUSED

    async def resume(self):
        if self._state == StreamerState.PAUSED:
            self._state = StreamerState.STREAMING

    def _on_frame(self, params: Dict):
        session_id = params.get("sessionId")

        if self._cdp and session_id:
            asyncio.create_task(self._ack_frame(session_id))

        if self._state != StreamerState.STREAMING:
            return

        now = time.time()
        if now - self._last_frame_time < self.config.frame_interval:
            self._dropped_frames += 1
            return

        try:
            self._frame_queue.put_nowait({
                "data": params.get("data"),
                "metadata": params.get("metadata", {}),
                "timestamp": now
            })
            self._last_frame_time = now
        except asyncio.QueueFull:
            self._dropped_frames += 1

    async def _ack_frame(self, session_id: int):
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def _process_frames(self):
        while self._state in [StreamerState.STREAMING, StreamerState.PAUSED]:
            try:
                frame = await asyncio.wait_for(self._frame_queue.get(), timeout=1.0)

                if self._state == StreamerState.PAUSED:
                    continue

                if self.config.skip_when_no_subscribers:
                    if not await self.publisher.has_subscribers(self.subject):
                        continue

                jpeg_data = base64.b64decode(frame["data"])

                await self.publisher.publish(self.subject, jpeg_data)

                self._frame_count += 1
                self._total_bytes += len(jpeg_data)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Streamer] Frame error: {e}")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False


async def create_streamer(page: Page, workflow_id: str, **kwargs) -> BrowserStreamer:
    config = StreamConfig(**{k: v for k, v in kwargs.items() if hasattr(StreamConfig, k)})
    streamer = BrowserStreamer(page, workflow_id, config)
    await streamer.start()
    return streamer

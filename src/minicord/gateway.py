from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import threading
from collections.abc import Callable
from enum import IntEnum, IntFlag
from typing import Any

import websockets
import websockets.exceptions

_log = logging.getLogger(__name__)

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Close codes that make resuming impossible — a fresh IDENTIFY is required.
_NON_RESUMABLE_CODES: frozenset[int] = frozenset(
    {
        4004,  # Authentication failed
        4010,  # Invalid shard
        4011,  # Sharding required
        4012,  # Invalid API version
        4013,  # Invalid intent(s)
        4014,  # Disallowed intent(s)
    }
)

EventHandler = Callable[[dict[str, Any]], None]


class GatewayOpcode(IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class Intents(IntFlag):
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    GUILD_MODERATION = 1 << 2
    GUILD_EXPRESSIONS = 1 << 3
    GUILD_INTEGRATIONS = 1 << 4
    GUILD_WEBHOOKS = 1 << 5
    GUILD_INVITES = 1 << 6
    GUILD_VOICE_STATES = 1 << 7
    GUILD_PRESENCES = 1 << 8
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    GUILD_MESSAGE_TYPING = 1 << 11
    DIRECT_MESSAGES = 1 << 12
    DIRECT_MESSAGE_REACTIONS = 1 << 13
    DIRECT_MESSAGE_TYPING = 1 << 14
    MESSAGE_CONTENT = 1 << 15
    GUILD_SCHEDULED_EVENTS = 1 << 16
    AUTO_MODERATION_CONFIGURATION = 1 << 20
    AUTO_MODERATION_EXECUTION = 1 << 21
    GUILD_MESSAGE_POLLS = 1 << 24
    DIRECT_MESSAGE_POLLS = 1 << 25

    @classmethod
    def default(cls) -> "Intents":
        """Guilds + guild messages: a minimal set requiring no privileged intents."""
        return cls.GUILDS | cls.GUILD_MESSAGES


def _get_close_code(exc: websockets.exceptions.ConnectionClosed) -> int | None:
    """Extract the WebSocket close code from a ConnectionClosed exception."""
    try:
        return exc.rcvd.code if exc.rcvd is not None else None
    except AttributeError:
        # Older websockets versions exposed .code directly.
        return getattr(exc, "code", None)


class GatewayHandler:
    """Manages a persistent Discord Gateway WebSocket connection.

    Runs an asyncio event loop in a dedicated background daemon thread so the
    full connection lifecycle — handshake, heartbeating, reconnect/resume, and
    event dispatch — never blocks the calling thread.

    All mutable session state is protected by a ``threading.Lock``, making the
    handler safe to use from multiple OS threads simultaneously.  This is
    especially important for free-threaded Python (PEP 703, CPython 3.13t)
    where the GIL is absent and unsynchronised attribute writes are data races.

    Example::

        handler = GatewayHandler(token="my_bot_token", intents=Intents.default())

        @handler.on_event("MESSAGE_CREATE")
        def on_message(data: dict) -> None:
            print("New message:", data)

        handler.connect()    # non-blocking; returns once the event loop is live
        ...
        handler.disconnect() # graceful shutdown; blocks until the thread exits
    """

    def __init__(
        self,
        token: str,
        intents: Intents = Intents.default(),
        *,
        gateway_url: str = _GATEWAY_URL,
        library_name: str = "minicord",
    ) -> None:
        self.token = token
        self.intents = intents
        self._initial_gateway_url = gateway_url
        self._library_name = library_name

        # Session state — every field here is guarded by _state_lock so that
        # reads/writes from the asyncio thread and external caller threads are
        # always consistent when the GIL is disabled.
        self._state_lock = threading.Lock()
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._heartbeat_interval: float = 41.25  # seconds; replaced on HELLO
        self._ack_received: bool = True

        # Background asyncio event loop running in _loop_thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()  # set once the loop is accepting work
        self._stop_event: asyncio.Event | None = None
        self._ws: Any = None  # current websockets ClientConnection

        # Dispatch listeners — guarded by _listeners_lock.
        self._listeners: dict[str, list[EventHandler]] = {}
        self._listeners_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_event(
        self,
        event_name: str,
        handler: EventHandler | None = None,
    ) -> Any:
        """Register a handler for a gateway dispatch event.

        Can be used directly or as a decorator::

            # direct
            gateway.on_event("MESSAGE_CREATE", my_func)

            # decorator
            @gateway.on_event("MESSAGE_CREATE")
            def my_func(data: dict) -> None: ...

        *handler* is called from the background networking thread and **must
        be thread-safe**.
        """

        def register(fn: EventHandler) -> EventHandler:
            with self._listeners_lock:
                self._listeners.setdefault(event_name.upper(), []).append(fn)
            return fn

        if handler is not None:
            return register(handler)
        return register  # used as a decorator factory

    def connect(self) -> None:
        """Start the Gateway connection in a background thread.

        Returns immediately after the event loop is running. Raises
        ``RuntimeError`` if the handler is already connected.
        """
        if self._loop_thread and self._loop_thread.is_alive():
            raise RuntimeError("GatewayHandler is already connected.")
        self._loop_ready.clear()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="minicord-gateway",
        )
        self._loop_thread.start()
        self._loop_ready.wait()  # block only until the event loop is running

    def disconnect(self) -> None:
        """Close the connection and stop the background thread gracefully.

        Blocks until the thread finishes (up to 10 seconds).
        """
        if self._loop and self._stop_event:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._loop_thread:
            self._loop_thread.join(timeout=10)

    def send_command(self, opcode: GatewayOpcode, data: Any) -> None:
        """Send a gateway payload from any OS thread.

        The payload is enqueued on the background event loop and transmitted
        asynchronously. Safe to call concurrently from multiple threads.
        """
        if self._loop is None:
            raise RuntimeError("Not connected.")
        asyncio.run_coroutine_threadsafe(
            self._send({"op": opcode.value, "d": data}),
            self._loop,
        )

    # ------------------------------------------------------------------
    # Internal: event loop bootstrap
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._gateway_lifecycle())
        finally:
            loop.close()
            self._loop = None

    # ------------------------------------------------------------------
    # Internal: connection lifecycle
    # ------------------------------------------------------------------

    async def _gateway_lifecycle(self) -> None:
        self._stop_event = asyncio.Event()
        self._loop_ready.set()
        url = self._initial_gateway_url
        resume = False

        while not self._stop_event.is_set():
            try:
                resumable = await self._run_connection(url, resume=resume) # type: ignore
            except Exception as exc:
                _log.warning("Connection error: %r — will reconnect", exc)
                resumable = False

            if self._stop_event.is_set():
                break

            with self._state_lock:
                can_resume = (
                    resumable
                    and self._session_id is not None
                    and self._resume_url is not None
                )
                url = self._resume_url if can_resume else self._initial_gateway_url

            resume = can_resume
            _log.debug("Reconnecting (resume=%s) to %s", resume, url)
            await asyncio.sleep(1.0)

    async def _run_connection(self, url: str, *, resume: bool) -> bool:
        """Open and manage one WebSocket connection.

        Returns ``True`` if the session can be resumed after this connection
        closes, or ``False`` if a fresh IDENTIFY is required.
        """
        resumable = True
        hello_event = asyncio.Event()

        async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
            self._ws = ws
            hb_task = asyncio.create_task(
                self._heartbeat_loop(hello_event), name="minicord-heartbeat"
            )
            try:
                await self._receive_loop(ws, resume=resume, hello_event=hello_event)
            except websockets.exceptions.ConnectionClosed as exc:
                code = _get_close_code(exc)
                if code in _NON_RESUMABLE_CODES:
                    _log.error("Non-resumable close code %s — clearing session", code)
                    with self._state_lock:
                        self._session_id = None
                        self._resume_url = None
                        self._sequence = None
                    resumable = False
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

        return resumable

    async def _receive_loop(
        self,
        ws: Any,
        *,
        resume: bool,
        hello_event: asyncio.Event,
    ) -> None:
        async for raw in ws:
            if self._stop_event and self._stop_event.is_set():
                await ws.close(1000, "client disconnect")
                return

            payload: dict[str, Any] = json.loads(
                raw.decode() if isinstance(raw, bytes) else raw
            )
            op = payload.get("op")
            data = payload.get("d")
            seq = payload.get("s")

            if seq is not None:
                with self._state_lock:
                    self._sequence = seq

            match op:
                case GatewayOpcode.HELLO:
                    interval_ms: float = data["heartbeat_interval"] # type: ignore
                    with self._state_lock:
                        self._heartbeat_interval = interval_ms / 1000.0
                        self._ack_received = True
                    hello_event.set()
                    if resume:
                        await self._send_resume()
                    else:
                        await self._send_identify()

                case GatewayOpcode.HEARTBEAT_ACK:
                    with self._state_lock:
                        self._ack_received = True

                case GatewayOpcode.HEARTBEAT:
                    # Discord is requesting an immediate heartbeat.
                    await self._send_heartbeat()

                case GatewayOpcode.DISPATCH:
                    self._handle_dispatch(payload.get("t"), data)

                case GatewayOpcode.RECONNECT:
                    _log.debug("Received RECONNECT — closing for resume")
                    await ws.close(4000, "reconnect requested")
                    return

                case GatewayOpcode.INVALID_SESSION:
                    await self._handle_invalid_session(ws, resumable=bool(data))
                    return

                case _:
                    _log.debug("Unhandled opcode: %s", op)

    async def _handle_invalid_session(self, ws: Any, *, resumable: bool) -> None:
        if resumable:
            _log.debug("INVALID_SESSION (resumable) — closing to reconnect")
            await ws.close(4000, "invalid session resumable")
        else:
            _log.debug("INVALID_SESSION (not resumable) — clearing state")
            with self._state_lock:
                self._session_id = None
                self._resume_url = None
                self._sequence = None
            # Discord recommends waiting 1–5 s before re-identifying.
            await asyncio.sleep(random.uniform(1.0, 5.0))
            await ws.close(4000, "invalid session")

    # ------------------------------------------------------------------
    # Internal: heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, hello_event: asyncio.Event) -> None:
        """Send heartbeats at the negotiated interval until cancelled."""
        await hello_event.wait()

        with self._state_lock:
            interval = self._heartbeat_interval

        # Initial jitter: spread reconnecting clients across the interval to
        # prevent thundering-herd bursts on Discord's servers.
        await asyncio.sleep(interval * random.random())
        await self._send_heartbeat()

        while True:
            with self._state_lock:
                interval = self._heartbeat_interval
                ack = self._ack_received

            if not ack:
                _log.warning("No heartbeat ACK received — closing zombied connection")
                if self._ws is not None:
                    await self._ws.close(4000, "zombied connection")
                return

            await asyncio.sleep(interval)
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        with self._state_lock:
            seq = self._sequence
            self._ack_received = False
        _log.debug("Sending HEARTBEAT (seq=%s)", seq)
        await self._send({"op": GatewayOpcode.HEARTBEAT.value, "d": seq})

    # ------------------------------------------------------------------
    # Internal: identify / resume
    # ------------------------------------------------------------------

    async def _send_identify(self) -> None:
        await self._send(
            {
                "op": GatewayOpcode.IDENTIFY.value,
                "d": {
                    "token": self.token,
                    "intents": int(self.intents),
                    "properties": {
                        "os": sys.platform,
                        "browser": self._library_name,
                        "device": self._library_name,
                    },
                },
            }
        )

    async def _send_resume(self) -> None:
        with self._state_lock:
            session_id = self._session_id
            seq = self._sequence
        await self._send(
            {
                "op": GatewayOpcode.RESUME.value,
                "d": {
                    "token": self.token,
                    "session_id": session_id,
                    "seq": seq,
                },
            }
        )

    # ------------------------------------------------------------------
    # Internal: send / dispatch / shutdown
    # ------------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        data = json.dumps(payload)
        # Gateway rejects payloads larger than 4096 bytes (close code 4002).
        if len(data.encode()) > 4096:
            _log.error("Payload exceeds 4096 bytes and will not be sent")
            return
        await self._ws.send(data)

    def _handle_dispatch(self, event_type: str | None, data: Any) -> None:
        if event_type == "READY":
            with self._state_lock:
                self._session_id = data.get("session_id")
                self._resume_url = data.get("resume_gateway_url")
            _log.debug("READY: session_id=%s", self._session_id)

        if event_type is None:
            return

        with self._listeners_lock:
            handlers = list(self._listeners.get(event_type, []))

        for fn in handlers:
            try:
                fn(data)
            except Exception:
                _log.exception("Unhandled exception in %s listener", event_type)

    async def _shutdown(self) -> None:
        """Signal the lifecycle loop to stop and close the current connection."""
        if self._stop_event:
            self._stop_event.set()
        if self._ws is not None:
            await self._ws.close(1000, "client disconnect")

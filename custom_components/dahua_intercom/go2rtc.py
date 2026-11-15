"""Our own go2rtc instance: turns the local RTSP stream into WebRTC for the card.

Home Assistant's built-in go2rtc only listens on TCP 18555 and announces no public
address, so it does not work from mobile data. We run a second instance of the same
binary with a config we control: a WebRTC port reachable from the internet (with a
router port forward), the public host name as an ICE candidate, and STUN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Awaitable, Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

STREAM = "intercom"


def find_binary() -> str | None:
    for path in (shutil.which("go2rtc"), "/bin/go2rtc", "/usr/local/bin/go2rtc"):
        if path and os.access(path, os.X_OK):
            return path
    return None


class Go2rtc:
    def __init__(
        self,
        config_dir: str,
        *,
        api_port: int,
        webrtc_port: int,
        public_host: str,
        rtsp_url: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self.api = f"http://127.0.0.1:{api_port}"
        self._api_port = api_port
        self._webrtc_port = webrtc_port
        self._public_host = public_host
        self._rtsp_url = rtsp_url
        self._config_path = os.path.join(config_dir, "go2rtc.yaml")
        self._session = session
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._binary = find_binary()

    def _config(self) -> str:
        candidates = ["stun:%d" % self._webrtc_port]
        if self._public_host:
            candidates.insert(0, f"{self._public_host}:{self._webrtc_port}")
        cfg = {
            "api": {"listen": f"127.0.0.1:{self._api_port}"},
            "rtsp": {"listen": ""},
            "rtmp": {"listen": ""},
            "srtp": {"listen": ""},
            "webrtc": {
                "listen": f":{self._webrtc_port}",
                "candidates": candidates,
                "ice_servers": [{"urls": ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"]}],
            },
            "streams": {STREAM: [self._rtsp_url]},
            "log": {"level": "info"},
        }
        return json.dumps(cfg, indent=2)  # JSON is valid YAML

    async def start(self) -> None:
        if not self._binary:
            raise RuntimeError("go2rtc binary not found")
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        cfg = self._config()
        await asyncio.get_running_loop().run_in_executor(None, self._write, cfg)
        self._task = asyncio.get_running_loop().create_task(self._supervise())

    def _write(self, cfg: str) -> None:
        with open(self._config_path, "w", encoding="utf-8") as f:
            f.write(cfg)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    async def _supervise(self) -> None:
        backoff = 1
        while True:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self._binary, "-c", self._config_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                _LOGGER.info("go2rtc started (pid %s, webrtc port %s)", self._proc.pid, self._webrtc_port)
                assert self._proc.stdout is not None
                async for line in self._proc.stdout:
                    text = line.decode(errors="replace").rstrip()
                    if " ERR " in text or " WRN " in text:
                        _LOGGER.warning("go2rtc: %s", text)
                    else:
                        _LOGGER.debug("go2rtc: %s", text)
                code = await self._proc.wait()
                _LOGGER.warning("go2rtc exited with %s, restarting", code)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("go2rtc failed to start: %s", err)
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(backoff)

    async def snapshot(self) -> bytes | None:
        try:
            async with self._session.get(f"{self.api}/api/frame.jpeg", params={"src": STREAM}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        return None


class WebRtcSession:
    """One browser session, signalled through go2rtc's WebSocket API (trickle ICE)."""

    def __init__(self, go2rtc: Go2rtc, session: aiohttp.ClientSession) -> None:
        self._go2rtc = go2rtc
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None

    async def start(
        self,
        offer: str,
        on_answer: Callable[[str], None],
        on_candidate: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._ws = await self._session.ws_connect(
            f"{self._go2rtc.api}/api/ws", params={"src": STREAM}, timeout=aiohttp.ClientWSTimeout(ws_close=5),
            heartbeat=20,
        )
        await self._ws.send_json({"type": "webrtc/offer", "value": offer})
        self._task = asyncio.get_running_loop().create_task(self._read(on_answer, on_candidate, on_error))

    async def _read(self, on_answer, on_candidate, on_error) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                kind, value = data.get("type"), data.get("value")
                if kind == "webrtc/answer":
                    on_answer(value)
                elif kind == "webrtc/candidate":
                    on_candidate(value)
                elif kind == "error":
                    on_error(str(value))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            on_error(str(err))

    async def add_candidate(self, candidate: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"type": "webrtc/candidate", "value": candidate})

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()


SessionFactory = Callable[[], Awaitable[WebRtcSession]]

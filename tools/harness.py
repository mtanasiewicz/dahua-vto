#!/usr/bin/env python3
"""Run the SIP agent + RTP relay + RTSP server without Home Assistant.

Usage: SIPPWD=... python3 harness.py [seconds]
Opening rtsp://127.0.0.1:8565/intercom starts a live-view call to the VTO.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkg.media import MediaRelay  # noqa: E402
from pkg.rtsp import RtspServer  # noqa: E402
from pkg.sip import SipAgent  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("harness")


async def main() -> None:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    media = MediaRelay(5068, 5070, lambda k, p: rtsp.push(k, p))
    state = {"call": None}

    def on_clients(n: int) -> None:
        log.info("viewers=%s", n)
        if n > 0 and (agent.call is None):
            asyncio.get_running_loop().create_task(agent.call_out("8001"))
        elif n == 0 and agent.call is not None:
            asyncio.get_running_loop().create_task(agent.hangup())

    def on_event(ev: str, call) -> None:
        log.info("SIP event %s", ev)
        if ev == "active" and call is not None and call.plan:
            media.begin(call.plan)
        elif ev.startswith("ended"):
            media.end()

    rtsp = RtspServer("127.0.0.1", 8565, on_clients, media.push_mic)
    agent = SipAgent(
        server="192.168.0.5", server_port=5060, username="9901#1", password=os.environ["SIPPWD"],
        realm="VDP", local_ip="192.168.0.2", local_port=5063, audio_port=5068, video_port=5070, on_event=on_event,
    )
    await media.start()
    await rtsp.start()
    await agent.start()
    ok = await agent.wait_registered(8)
    log.info("registered=%s err=%s", ok, agent.last_error)
    for _ in range(secs):
        await asyncio.sleep(1)
        if media.active:
            log.info("stats %s", media.stats)
    await agent.stop()
    await rtsp.stop()
    await media.stop()


asyncio.run(main())

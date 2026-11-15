# Dahua Intercom (SIP) for Home Assistant

Custom integration that talks SIP directly to a Dahua VTO door station (e.g. VTO2211G)
using the door station's own SIP server, and gives Home Assistant:

* ringing state and a doorbell event entity,
* answer / hang up / live view buttons,
* live H.264 video, door audio and **two-way talk** through a local RTSP stream with an
  ONVIF audio backchannel, which go2rtc / WebRTC Camera turn into WebRTC in the browser.

## Setup

1. On the VTO: enable the local SIP server, Group Call, and add a room account
   (e.g. `9901#1`) with its registration password.
2. Install via HACS (custom repository), restart, add **Dahua Intercom (SIP)**.
3. Card (WebRTC Camera):

```yaml
type: custom:webrtc-camera
url: rtsp://127.0.0.1:8564/intercom
media: video,audio,microphone
```

Opening the card answers a ringing call, or starts a live view call when idle.
Closing it hangs up after a few seconds.

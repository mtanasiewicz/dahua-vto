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

## Ring notifications

Option *Page opened from the ring notification*: the path of the dashboard view with the
intercom card (e.g. `/dashboard-domofon/domofon`). There is no default; when empty, tapping the
notification opens the app's home page. The integration adds `?intercom=live` to that path, and
the card then shows the live stream at once for a minute (even if the ring is already over),
instead of the still pictures it shows when idle.

When idle, the play button in the top-right corner of the picture does the same on demand
(live for a minute); while that live view runs, the stop button goes back to still pictures.

Ring notifications are sent by the integration to every phone registered with the Companion app.
Per phone (entities of the intercom device):

- **Ring notifications** (all phones): off / normal / important (breaks through Focus) / critical.
- **Ring sound** (iPhone): name of a sound file in the Companion app (`default` = system sound).
- **Open intercom on ring** (Android): the app opens that page by itself when it rings
  (needs the "Display over other apps" permission, asked the first time).

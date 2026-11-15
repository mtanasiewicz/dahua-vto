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

## Sidebar panel and ring notifications

The integration adds a **Domofon** panel to the sidebar (`/domofon`): the intercom card, as large
as the screen allows. It works with any dashboard layout, so ring notifications open this panel by
default (option *Notification URL*).

The built-in panel cannot be edited. To add your own buttons, create a regular dashboard
(e.g. `/dashboard-domofon`) with the `custom:dahua-intercom-card`, set *Notification URL* to it
(e.g. `/dashboard-domofon/domofon`) and turn off the option *Show the built-in "Domofon" panel*.

Ring notifications are sent by the integration to every phone registered with the Companion app.
Per phone (entities of the intercom device):

- **Ring notifications** (all phones): off / normal / important (breaks through Focus) / critical.
- **Ring sound** (iPhone): name of a sound file in the Companion app (`default` = system sound).
- **Open intercom on ring** (Android): the app opens the panel by itself when it rings
  (needs the "Display over other apps" permission, asked the first time).

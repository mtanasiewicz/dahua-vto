"""Constants for the Dahua Intercom integration."""

DOMAIN = "dahua_intercom"

CONF_SIP_PORT = "sip_port"
CONF_REALM = "realm"
CONF_LOCAL_SIP_PORT = "local_sip_port"
CONF_RTP_PORT = "rtp_port"
CONF_RTSP_PORT = "rtsp_port"
CONF_RTSP_HOST = "rtsp_host"
CONF_MONITOR_TARGET = "monitor_target"
CONF_ANSWER_ON_VIEW = "answer_on_view"
CONF_MONITOR_ON_VIEW = "monitor_on_view"
CONF_LOCAL_IP = "local_ip"
CONF_SOURCE_CAMERA = "source_camera"
CONF_MAIN_STREAM = "main_stream"
CONF_PUBLIC_HOST = "public_host"
CONF_WEBRTC_PORT = "webrtc_port"
CONF_GO2RTC_API_PORT = "go2rtc_api_port"
CONF_LOCK = "lock_entity"
CONF_NOTIFY_URL = "notify_url"

DEFAULT_SIP_PORT = 5060
DEFAULT_USERNAME = "9901#1"
DEFAULT_REALM = "VDP"
DEFAULT_LOCAL_SIP_PORT = 5062
DEFAULT_RTP_PORT = 5064
DEFAULT_RTSP_PORT = 8564
DEFAULT_RTSP_HOST = "127.0.0.1"
DEFAULT_MONITOR_TARGET = "8001"
DEFAULT_SOURCE_CAMERA = "camera.domofon"
DEFAULT_WEBRTC_PORT = 8556
DEFAULT_GO2RTC_API_PORT = 11985
UPSTREAM_IDLE_S = 30
DEFAULT_LOCK = "lock.domofon_elektrozaczep"

STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_CALLING = "calling"
STATE_TALKING = "talking"
STATE_LIVE_VIEW = "live_view"
CALL_STATES = [STATE_IDLE, STATE_RINGING, STATE_CALLING, STATE_TALKING, STATE_LIVE_VIEW]

EVENT_RING = f"{DOMAIN}_ring"
EVENT_CALL = f"{DOMAIN}_call"

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"
VIEWER_GRACE_S = 4

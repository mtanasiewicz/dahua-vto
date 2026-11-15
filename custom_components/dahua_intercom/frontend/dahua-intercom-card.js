/*
 * Dahua intercom card.
 *
 * Idle: a still picture refreshed every few seconds (little data).
 * Ringing, or a call active on any device: live picture + door audio over
 * WebRTC (Home Assistant's camera WebRTC API).
 *
 * Buttons:
 *   Odbierz  - answer the ring (this device talks)
 *   Połącz   - call the door station from here
 *   Dołącz   - join a call that another phone is already in (conference)
 *   Zakończ  - end the call (for everybody)
 *   Otwórz   - open the gate
 * The tap that answers/joins also unmutes and enables the microphone (iOS autoplay rules).
 */
const VERSION = "0.5.0";

const DEFAULTS = {
  camera: "camera.domofon_intercom",
  call_sensor: "sensor.domofon_call",
  answer: "button.domofon_answer",
  call: "button.domofon_live_view",
  hangup: "button.domofon_hang_up",
  lock: "lock.domofon_elektrozaczep",
  open: "button.domofon_open",
  title: "Domofon",
  snapshot_interval: 3,
  snapshot_width: 640,
};

const IN_CALL = ["talking", "live_view", "calling"];

const LABELS = {
  ringing: "Ktoś dzwoni",
  talking: "Rozmowa",
  calling: "Łączenie z furtką…",
  talking_elsewhere: "Rozmowa na innym urządzeniu",
  connecting: "Łączenie…",
  reconnecting: "Wznawianie połączenia…",
  error: "Brak połączenia z domofonem",
  opened: "Furtka otwarta",
};

class DahuaIntercomCard extends HTMLElement {
  static getStubConfig() {
    return { ...DEFAULTS };
  }

  setConfig(config) {
    this._config = { ...DEFAULTS, ...(config || {}) };
    if (!this.shadowRoot) this._build();
    this._els.title.textContent = this._config.title;
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const st = hass.states[this._config.call_sensor];
    const callState = st ? st.state : "unavailable";
    if (callState !== this._callState || first) {
      const prev = this._callState;
      this._callState = callState;
      this._onCallState(prev, callState);
    }
    this._render();
  }

  connectedCallback() {
    this._onVisibility = () => this._sync();
    document.addEventListener("visibilitychange", this._onVisibility);
    if (this._hass) this._sync();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stop();
    this._stopSnapshots();
  }

  // ------------------------------------------------------------------ UI

  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .head { display:flex; align-items:center; justify-content:space-between; padding:12px 16px 8px; }
        .title { font-size: 16px; font-weight: 500; }
        .status { font-size: 13px; color: var(--secondary-text-color); }
        .status.ringing { color: var(--warning-color, #f4b400); font-weight: 600; animation: blink 1s infinite; }
        .status.talking { color: var(--success-color, #43a047); font-weight: 600; }
        @keyframes blink { 50% { opacity: .4; } }
        .video { position: relative; background: #000; aspect-ratio: 16 / 9; }
        video, img { position:absolute; inset:0; width: 100%; height: 100%; object-fit: contain; display: block; background:#000; }
        video[hidden], img[hidden] { display:none; }
        .overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
                   color:#fff; font-size:14px; text-align:center; padding:16px; pointer-events:none;
                   background: rgba(0,0,0,.35); }
        .overlay[hidden] { display:none; }
        .badge { position:absolute; top:8px; right:8px; background:rgba(0,0,0,.5); color:#fff; border-radius:12px;
                 padding:2px 8px; font-size:12px; }
        .badge[hidden] { display:none; }
        .buttons { display:grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 12px 16px 16px; }
        button { font: inherit; font-size: 15px; font-weight: 600; border: none; border-radius: 12px; padding: 14px 8px;
                 color: #fff; cursor: pointer; display:flex; flex-direction:column; align-items:center; gap:4px; }
        button ha-icon { --mdc-icon-size: 26px; }
        button:disabled { opacity: .35; cursor: default; }
        .answer { background: var(--success-color, #43a047); }
        .answer.pulse:not(:disabled) { animation: pulse 1s infinite; }
        @keyframes pulse { 50% { filter: brightness(1.3); } }
        .hangup { background: var(--error-color, #db4437); }
        .open { background: var(--primary-color, #03a9f4); }
      </style>
      <ha-card>
        <div class="head"><span class="title"></span><span class="status"></span></div>
        <div class="video">
          <img alt="">
          <video playsinline autoplay muted hidden></video>
          <div class="overlay" hidden></div>
          <div class="badge" hidden></div>
        </div>
        <div class="buttons">
          <button class="answer"><ha-icon icon="mdi:phone"></ha-icon><span class="answer-label">Połącz</span></button>
          <button class="hangup"><ha-icon icon="mdi:phone-hangup"></ha-icon>Zakończ</button>
          <button class="open"><ha-icon icon="mdi:door-open"></ha-icon>Otwórz</button>
        </div>
      </ha-card>`;
    const $ = (s) => root.querySelector(s);
    this._els = {
      title: $(".title"), status: $(".status"), video: $("video"), img: $("img"), overlay: $(".overlay"),
      badge: $(".badge"), answer: $(".answer"), answerLabel: $(".answer-label"), answerIcon: $(".answer ha-icon"),
      hangup: $(".hangup"), open: $(".open"),
    };
    this._els.answer.addEventListener("click", () => this._primary());
    this._els.hangup.addEventListener("click", () => this._hangup());
    this._els.open.addEventListener("click", () => this._open());
    this._els.video.addEventListener("click", () => {
      if (this._joined) { this._els.video.muted = false; this._els.video.play().catch(() => {}); }
    });
  }

  _render() {
    if (!this._els) return;
    const s = this._callState;
    const ringing = s === "ringing";
    const inCall = IN_CALL.includes(s);
    let label;
    if (this._flash) label = this._flash;
    else if (this._error && this._pc) label = LABELS.error;
    else if (s === "calling") label = LABELS.calling;
    else if (inCall) label = this._joined ? LABELS.talking : LABELS.talking_elsewhere;
    else if (ringing) label = LABELS.ringing;
    else label = "";
    this._els.status.textContent = label;
    this._els.status.className = "status " + (ringing ? "ringing" : inCall ? "talking" : "");

    let primary = "Połącz", icon = "mdi:phone";
    if (ringing) primary = "Odbierz";
    else if (inCall && !this._joined) { primary = "Dołącz"; icon = "mdi:account-multiple-plus"; }
    this._els.answerLabel.textContent = primary;
    this._els.answerIcon.setAttribute("icon", icon);
    const sipOk = s !== "unavailable" && s !== "unknown";
    this._els.answer.disabled = !sipOk || (inCall && this._joined);
    this._els.answer.classList.toggle("pulse", ringing);
    this._els.hangup.disabled = !(ringing || inCall);
    const lock = this._hass && this._hass.states[this._config.lock];
    this._els.open.disabled = !lock || lock.state === "unavailable";

    const live = !!this._pc && !!this._els.video.srcObject;
    this._els.video.hidden = !live;
    this._els.img.hidden = live;
    let overlay = "";
    if (this._pc && this._state === "connecting" && !live) overlay = LABELS.connecting;
    else if (this._pc && this._state === "reconnecting") overlay = LABELS.reconnecting;
    this._els.overlay.textContent = overlay;
    this._els.overlay.hidden = !overlay;
    const micOn = this._micTrack && this._micTrack.enabled;
    this._els.badge.textContent = micOn ? "🎙 mikrofon" : live ? "na żywo" : "";
    this._els.badge.hidden = !this._els.badge.textContent;
  }

  _flashMessage(text) {
    this._flash = text;
    this._render();
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => { this._flash = null; this._render(); }, 3000);
  }

  // ------------------------------------------------------------- modes

  _visible() {
    return this.isConnected && document.visibilityState === "visible";
  }

  _liveWanted() {
    const s = this._callState;
    return this._visible() && (s === "ringing" || IN_CALL.includes(s) || !!this._joined);
  }

  /** Pick the mode: live stream while ringing / in a call, still pictures otherwise. */
  _sync() {
    if (!this._hass || !this._els) return;
    if (this._liveWanted()) {
      this._stopSnapshots();
      if (!this._pc) this._start();
    } else {
      if (this._pc) this._stop();
      if (this._visible()) this._startSnapshots();
      else this._stopSnapshots();
    }
    this._render();
  }

  // ---------------------------------------------------------- snapshots

  _startSnapshots() {
    if (this._snapTimer || this._snapLoading) return;
    this._nextSnapshot(0);
  }

  _stopSnapshots() {
    clearTimeout(this._snapTimer);
    this._snapTimer = null;
  }

  _nextSnapshot(delay) {
    clearTimeout(this._snapTimer);
    this._snapTimer = setTimeout(() => this._loadSnapshot(), delay);
  }

  _loadSnapshot() {
    this._snapTimer = null;
    if (!this._hass || this._pc || !this._visible()) return;
    const st = this._hass.states[this._config.camera];
    const token = st && st.attributes.access_token;
    if (!token) { this._nextSnapshot(1000); return; }
    const url = `/api/camera_proxy/${this._config.camera}?token=${token}&width=${this._config.snapshot_width}&_=${Date.now()}`;
    const loader = new Image();
    this._snapLoading = true;
    const done = () => {
      this._snapLoading = false;
      if (!this._pc && this._visible()) this._nextSnapshot(this._config.snapshot_interval * 1000);
    };
    loader.onload = () => { if (!this._pc) this._els.img.src = loader.src; done(); };
    loader.onerror = done;
    loader.src = url;
  }

  // ------------------------------------------------------------- actions

  async _primary() {
    // Everything that needs a user gesture happens synchronously first.
    const s = this._callState;
    const ringing = s === "ringing";
    const joining = IN_CALL.includes(s);
    this._joined = true;
    const v = this._els.video;
    v.muted = false;
    v.play().catch(() => {});
    if (this._micTrack) this._micTrack.enabled = true;
    else if (this._pc) this._restart("microphone");
    else this._sync();
    this._render();
    if (joining) return; // the call is already up: just take part in it
    try {
      await this._hass.callService("button", "press", { entity_id: ringing ? this._config.answer : this._config.call });
    } catch (e) {
      console.warn("intercom answer/call failed", e);
      this._flashMessage("Błąd: " + (e.message || e));
    }
  }

  async _hangup() {
    this._joined = false;
    if (this._micTrack) this._micTrack.enabled = false;
    this._els.video.muted = true;
    try {
      await this._hass.callService("button", "press", { entity_id: this._config.hangup });
    } catch (e) {
      console.warn("intercom hangup failed", e);
    }
    this._render();
  }

  async _open() {
    // the integration's Open button also stops the ringing on every device
    const entity = this._config.open && this._hass.states[this._config.open] ? this._config.open : this._config.lock;
    const domain = entity.split(".")[0];
    try {
      if (domain === "lock") await this._hass.callService("lock", "unlock", { entity_id: entity });
      else if (domain === "script") await this._hass.callService("script", "turn_on", { entity_id: entity });
      else if (domain === "button") await this._hass.callService("button", "press", { entity_id: entity });
      else await this._hass.callService("homeassistant", "turn_on", { entity_id: entity });
      this._flashMessage(LABELS.opened);
    } catch (e) {
      this._flashMessage("Błąd: " + (e.message || e));
    }
  }

  // ---------------------------------------------------------- call state

  _onCallState(prev, cur) {
    const inCall = IN_CALL.includes(cur);
    if (cur === "ringing" && !IN_CALL.includes(prev)) {
      this._joined = false;
      if (this._els) this._els.video.muted = true;
    }
    if (!inCall && cur !== "ringing") {
      // call over (or never started): back to still pictures, microphone released
      this._joined = false;
      if (this._micTrack) this._micTrack.enabled = false;
      if (this._els) this._els.video.muted = true;
    }
    if (inCall && this._micTrack) this._micTrack.enabled = !!this._joined;
    // a ring while live without a microphone: reconnect with one so answering is instant
    if (cur === "ringing" && this._pc && !this._micTrack) { this._restart("ring: add microphone"); return; }
    this._sync();
  }

  // ------------------------------------------------------------- WebRTC

  _isConnected() {
    return this._pc && ["connected", "completed"].includes(this._pc.iceConnectionState);
  }

  _restart(reason) {
    console.info("intercom: restarting stream", reason);
    this._stop(true);
    this._start(true);
  }

  async _start(reconnect = false) {
    if (this._pc || !this._hass) return;
    this._error = false;
    this._state = reconnect ? "reconnecting" : "connecting";
    const gen = (this._gen = (this._gen || 0) + 1);
    let config = {};
    try {
      config = await this._hass.callWS({ type: "camera/webrtc/get_client_config", entity_id: this._config.camera });
    } catch (e) {
      console.warn("intercom: client config", e);
    }
    if (gen !== this._gen) return;
    const pc = new RTCPeerConnection(config.configuration || {});
    this._pc = pc;
    this._render();

    // microphone in the offer (disabled) while ringing or when we take part, so talking starts at once
    const needMic = this._callState === "ringing" || this._joined;
    if (needMic && !this._micStream) {
      try {
        this._micStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (e) {
        console.warn("intercom: no microphone", e);
      }
    }
    if (gen !== this._gen) return;
    this._micTrack = this._micStream ? this._micStream.getAudioTracks()[0] : null;
    if (this._micTrack) {
      this._micTrack.enabled = !!this._joined && IN_CALL.includes(this._callState);
      pc.addTransceiver(this._micTrack, { direction: "sendonly" });
    }
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });

    const stream = new MediaStream();
    pc.ontrack = (ev) => {
      stream.addTrack(ev.track);
      const v = this._els.video;
      if (v.srcObject !== stream) v.srcObject = stream;
      v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
      this._render();
    };
    pc.oniceconnectionstatechange = () => {
      const s = pc.iceConnectionState;
      if (s === "connected" || s === "completed") {
        this._state = "connected";
        clearTimeout(this._failTimer);
      } else if (s === "disconnected") {
        clearTimeout(this._failTimer);
        this._failTimer = setTimeout(() => { if (this._pc === pc && this._liveWanted()) this._restart("disconnected"); }, 2500);
      } else if (s === "failed") {
        if (this._pc === pc && this._liveWanted()) this._restart("failed");
      }
      this._render();
    };

    const pending = [];
    let sessionId = null;
    const sendCandidate = (c) => {
      this._hass.callWS({
        type: "camera/webrtc/candidate", entity_id: this._config.camera, session_id: sessionId,
        candidate: { candidate: c.candidate, sdpMid: c.sdpMid, sdpMLineIndex: c.sdpMLineIndex },
      }).catch(() => {});
    };
    pc.onicecandidate = (ev) => {
      if (!ev.candidate || !ev.candidate.candidate) return;
      if (sessionId) sendCandidate(ev.candidate);
      else pending.push(ev.candidate);
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    if (gen !== this._gen) return;
    try {
      this._unsub = await this._hass.connection.subscribeMessage((msg) => {
        if (this._pc !== pc) return;
        if (msg.type === "session") {
          sessionId = msg.session_id;
          pending.splice(0).forEach(sendCandidate);
        } else if (msg.type === "answer") {
          pc.setRemoteDescription({ type: "answer", sdp: msg.answer }).catch((e) => console.warn("intercom answer sdp", e));
        } else if (msg.type === "candidate") {
          const c = msg.candidate || {};
          pc.addIceCandidate({
            candidate: c.candidate,
            sdpMid: c.sdpMid ?? c.sdp_mid ?? undefined,
            sdpMLineIndex: c.sdpMLineIndex ?? c.sdp_m_line_index ?? 0,
          }).catch(() => {});
        } else if (msg.type === "error") {
          console.warn("intercom webrtc error", msg);
          this._error = true;
          this._render();
          setTimeout(() => { if (this._pc === pc && this._liveWanted()) this._restart("error"); }, 3000);
        }
      }, { type: "camera/webrtc/offer", entity_id: this._config.camera, offer: offer.sdp });
    } catch (e) {
      console.warn("intercom offer failed", e);
      this._error = true;
      this._render();
      setTimeout(() => { if (this._pc === pc && this._liveWanted()) this._restart("offer"); }, 3000);
    }
    clearTimeout(this._failTimer);
    this._failTimer = setTimeout(() => { if (this._pc === pc && !this._isConnected() && this._liveWanted()) this._restart("timeout"); }, 10000);
  }

  _stop(keepMic = false) {
    this._gen = (this._gen || 0) + 1;
    clearTimeout(this._failTimer);
    if (this._unsub) { try { this._unsub(); } catch (e) { /* closed */ } this._unsub = null; }
    if (this._pc) { this._pc.ontrack = null; this._pc.oniceconnectionstatechange = null; this._pc.close(); this._pc = null; }
    if (!keepMic && this._micStream) {
      this._micStream.getTracks().forEach((t) => t.stop());
      this._micStream = null;
      this._micTrack = null;
    }
    if (this._els && !keepMic) this._els.video.srcObject = null;
    this._state = null;
    this._render();
  }
}

customElements.define("dahua-intercom-card", DahuaIntercomCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "dahua-intercom-card",
  name: "Dahua intercom",
  description: "Domofon: obraz, dźwięk, mikrofon, Odbierz / Połącz / Dołącz / Zakończ / Otwórz",
  preview: false,
});
console.info(`%c DAHUA-INTERCOM-CARD %c ${VERSION} `, "background:#43a047;color:#fff", "");

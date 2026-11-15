/*
 * Dahua intercom card: live picture + door audio + microphone over WebRTC
 * (signalled through Home Assistant's camera WebRTC API), with three buttons:
 * Odbierz (answer), Zakończ (end), Otwórz (open the gate).
 *
 * The stream starts on its own when someone rings, so the picture is there
 * before answering. "Odbierz" answers the SIP call, unmutes the door audio and
 * enables the microphone in the same tap (needed for iOS autoplay rules).
 */
const VERSION = "0.3.0";

const DEFAULTS = {
  camera: "camera.domofon_domofon",
  call_sensor: "sensor.domofon_call",
  answer: "button.domofon_answer",
  call: "button.domofon_live_view",
  hangup: "button.domofon_hang_up",
  lock: "lock.domofon_elektrozaczep",
  title: "Domofon",
};

const IN_CALL = ["talking", "live_view", "calling"];

const LABELS = {
  preview: "Podgląd na żywo",
  ringing: "Ktoś dzwoni",
  talking: "Rozmowa",
  calling: "Łączenie z furtką…",
  talking_elsewhere: "Odebrano na innym urządzeniu",
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
    this._onVisibility = () => {
      if (document.visibilityState !== "visible") {
        // app in the background: free the stream (and the microphone) unless we are in a call
        if (!IN_CALL.includes(this._callState) && this._callState !== "ringing") this._stop();
      } else if (this._wanted() && !this._isConnected()) {
        this._restart("visible");
      }
    };
    document.addEventListener("visibilitychange", this._onVisibility);
    if (this._hass && this._wanted()) this._start();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stop();
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
        .video { position: relative; background: #000; aspect-ratio: 16 / 9; cursor: pointer; }
        video { width: 100%; height: 100%; object-fit: contain; display: block; background:#000; }
        .overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
                   color:#fff; font-size:14px; text-align:center; padding:16px; pointer-events:none;
                   background: rgba(0,0,0,.35); }
        .overlay[hidden] { display:none; }
        .mic { position:absolute; top:8px; right:8px; background:rgba(0,0,0,.5); color:#fff; border-radius:12px;
               padding:2px 8px; font-size:12px; }
        .mic[hidden] { display:none; }
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
          <video playsinline autoplay muted></video>
          <div class="overlay"></div>
          <div class="mic" hidden>🎙 mikrofon</div>
        </div>
        <div class="buttons">
          <button class="answer"><ha-icon icon="mdi:phone"></ha-icon><span class="answer-label">Połącz</span></button>
          <button class="hangup"><ha-icon icon="mdi:phone-hangup"></ha-icon>Zakończ</button>
          <button class="open"><ha-icon icon="mdi:door-open"></ha-icon>Otwórz</button>
        </div>
      </ha-card>`;
    const $ = (s) => root.querySelector(s);
    this._els = {
      title: $(".title"), status: $(".status"), video: $("video"), overlay: $(".overlay"), mic: $(".mic"),
      videoBox: $(".video"), answer: $(".answer"), answerLabel: $(".answer-label"), hangup: $(".hangup"), open: $(".open"),
    };
    this._els.answer.addEventListener("click", () => this._answer());
    this._els.hangup.addEventListener("click", () => this._hangup());
    this._els.open.addEventListener("click", () => this._open());
    this._els.videoBox.addEventListener("click", () => this._tapVideo());
  }

  _render() {
    if (!this._els) return;
    const s = this._callState;
    const ringing = s === "ringing";
    const talking = IN_CALL.includes(s);
    let label;
    if (this._flash) label = this._flash;
    else if (this._error) label = LABELS.error;
    else if (s === "calling") label = LABELS.calling;
    else if (talking) label = this._answeredHere ? LABELS.talking : LABELS.talking_elsewhere;
    else if (ringing) label = LABELS.ringing;
    else if (this._pc) label = LABELS.preview;
    else label = "";
    this._els.status.textContent = label;
    this._els.status.className = "status " + (ringing ? "ringing" : talking ? "talking" : "");
    this._els.answerLabel.textContent = ringing ? "Odbierz" : "Połącz";
    const sipOk = s !== "unavailable" && s !== "unknown";
    this._els.answer.disabled = talking || !sipOk;
    this._els.answer.classList.toggle("pulse", ringing);
    this._els.hangup.disabled = !(ringing || talking);
    const lock = this._hass && this._hass.states[this._config.lock];
    this._els.open.disabled = !lock || lock.state === "unavailable";
    let overlay = "";
    if (!this._pc) overlay = LABELS.connecting;
    else if (this._state === "connecting") overlay = LABELS.connecting;
    else if (this._state === "reconnecting") overlay = LABELS.reconnecting;
    this._els.overlay.textContent = overlay;
    this._els.overlay.hidden = !overlay;
    this._els.mic.hidden = !(this._micTrack && this._micTrack.enabled);
  }

  _flashMessage(text) {
    this._flash = text;
    this._render();
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => { this._flash = null; this._render(); }, 3000);
  }

  // ------------------------------------------------------------- actions

  async _answer() {
    // Everything that needs a user gesture happens synchronously first.
    const ringing = this._callState === "ringing";
    this._answeredHere = true;
    const v = this._els.video;
    v.muted = false;
    v.play().catch(() => {});
    if (this._micTrack) this._micTrack.enabled = true;
    if (!this._pc) this._start();
    else if (!this._micTrack) this._restart("mic"); // mic was not available before
    this._render();
    try {
      await this._hass.callService("button", "press", { entity_id: ringing ? this._config.answer : this._config.call });
    } catch (e) {
      console.warn("intercom answer/call failed", e);
      this._flashMessage("Błąd: " + (e.message || e));
    }
  }

  async _hangup() {
    const hadMic = !!this._micStream;
    this._answeredHere = false;
    if (this._micTrack) this._micTrack.enabled = false;
    this._els.video.muted = true;
    if (this._callState === "ringing" || IN_CALL.includes(this._callState)) {
      try {
        await this._hass.callService("button", "press", { entity_id: this._config.hangup });
      } catch (e) {
        console.warn("intercom hangup failed", e);
      }
    }
    // keep the picture; reconnect without the microphone so it is released
    if (hadMic) { this._stop(); this._start(true); }
    this._render();
  }

  async _open() {
    const entity = this._config.lock;
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

  _tapVideo() {
    // a tap unmutes the picture during our own call; otherwise it (re)starts a stalled stream
    if (this._pc && this._answeredHere) { this._els.video.muted = false; this._els.video.play().catch(() => {}); }
    else if (!this._isConnected()) this._restart("tap");
  }

  // ---------------------------------------------------------- call state

  _wanted() {
    // always-on while the card is shown and the app is in the foreground
    return this.isConnected && document.visibilityState === "visible";
  }

  _onCallState(prev, cur) {
    if (cur === "ringing") {
      this._answeredHere = false;
      if (this._els) this._els.video.muted = true;
      if (this._pc && !this._micTrack) { this._restart("ring: add microphone"); return; }
    }
    const inCall = IN_CALL.includes(cur);
    if (inCall && this._micTrack) this._micTrack.enabled = !!this._answeredHere;
    if (!inCall && cur !== "ringing") {
      const hadMic = !!this._micStream;
      this._answeredHere = false;
      if (this._micTrack) this._micTrack.enabled = false;
      if (this._els) this._els.video.muted = true;
      // call over: reconnect without the microphone so it is released
      if (hadMic && this._pc && (IN_CALL.includes(prev) || prev === "ringing")) { this._restart("call ended"); return; }
    }
    if (this._wanted() && !this._pc && this._hass) this._start();
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
    clearTimeout(this._stopTimer);
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

    // microphone: in the offer from the start (muted) so answering is instant;
    // not requested for a plain preview
    const needMic = this._callState === "ringing" || this._answeredHere;
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
      this._micTrack.enabled = IN_CALL.includes(this._callState) && !!this._answeredHere;
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
    };
    pc.oniceconnectionstatechange = () => {
      const s = pc.iceConnectionState;
      if (s === "connected" || s === "completed") {
        this._state = "connected";
        clearTimeout(this._failTimer);
      } else if (s === "disconnected") {
        clearTimeout(this._failTimer);
        this._failTimer = setTimeout(() => { if (this._pc === pc && this._wanted()) this._restart("disconnected"); }, 2500);
      } else if (s === "failed") {
        if (this._pc === pc && this._wanted()) this._restart("failed");
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
          setTimeout(() => { if (this._pc === pc && this._wanted()) this._restart("error"); }, 3000);
        }
      }, { type: "camera/webrtc/offer", entity_id: this._config.camera, offer: offer.sdp });
    } catch (e) {
      console.warn("intercom offer failed", e);
      this._error = true;
      this._render();
      setTimeout(() => { if (this._pc === pc && this._wanted()) this._restart("offer"); }, 3000);
    }
    // watchdog: no media within 10 s -> try again
    clearTimeout(this._failTimer);
    this._failTimer = setTimeout(() => { if (this._pc === pc && !this._isConnected() && this._wanted()) this._restart("timeout"); }, 10000);
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
  description: "Domofon: obraz, dźwięk, mikrofon, Odbierz / Zakończ / Otwórz",
  preview: false,
});
console.info(`%c DAHUA-INTERCOM-CARD %c ${VERSION} `, "background:#43a047;color:#fff", "");

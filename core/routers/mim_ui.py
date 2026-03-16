from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import SpeechOutputAction, WorkspacePerceptionSource

router = APIRouter(tags=["mim-ui"])


def _known_people() -> set[str]:
    return {
        "testpilot",
        "operator",
        "alice",
        "bob",
        "charlie",
    }


def _age_seconds(now: datetime, ts: datetime | None) -> float | None:
    if ts is None:
      return None
    if ts.tzinfo is None:
      ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())


@router.get("/mim", response_class=HTMLResponse)
async def mim_ui_page() -> str:
    return """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>MIM</title>
  <style>
    :root {
      --bg: #071c2b;
      --panel: #0c2436;
      --line: #1fd5ff;
      --text: #d7efff;
      --muted: #9dc6d8;
      --ok: #2dcf6b;
      --err: #c56a2d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: radial-gradient(circle at 40% 20%, #0e3550, var(--bg));
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      padding: 20px;
      gap: 16px;
    }
    h1 {
      margin: 0;
      letter-spacing: 0.18em;
      font-weight: 600;
      color: #e5f7ff;
    }
    .mim-icon {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 10px;
    }
    .mim-icon::before {
      content: '';
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: #37596a;
      box-shadow: 0 0 0 rgba(0, 0, 0, 0);
      transition: background 160ms ease, box-shadow 160ms ease;
    }
    .mim-icon.ok::before {
      background: var(--ok);
      box-shadow: 0 0 18px rgba(45, 207, 107, 0.7);
    }
    .mim-icon.err::before {
      background: var(--err);
      box-shadow: 0 0 18px rgba(197, 106, 45, 0.72);
    }
    .panel {
      width: min(920px, 96vw);
      background: color-mix(in oklab, var(--panel) 88%, black 12%);
      border: 1px solid #16415a;
      border-radius: 12px;
      padding: 14px;
    }
    .top-right {
      position: fixed;
      top: 12px;
      right: 12px;
      z-index: 20;
      display: flex;
      gap: 8px;
    }
    .icon-btn {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      border: 1px solid #1b6a8d;
      background: #0f3b52;
      color: #d7efff;
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
    }
    .settings-panel {
      position: fixed;
      top: 52px;
      right: 12px;
      z-index: 20;
      width: min(320px, 92vw);
      background: color-mix(in oklab, var(--panel) 90%, black 10%);
      border: 1px solid #16415a;
      border-radius: 10px;
      padding: 10px;
      display: none;
    }
    .settings-panel.open { display: block; }
    .settings-title {
      font-size: 13px;
      font-weight: 600;
      color: #d7efff;
      margin-bottom: 8px;
    }
    .settings-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      margin-bottom: 10px;
    }
    .settings-row label {
      font-size: 12px;
      color: var(--muted);
    }
    .settings-row select,
    .settings-row input[type="text"] {
      width: 100%;
      background: #0a1f2d;
      color: var(--text);
      border: 1px solid #1a4f68;
      border-radius: 8px;
      padding: 8px;
      font-size: 13px;
    }
    .settings-note {
      font-size: 11px;
      color: var(--muted);
      margin-top: -4px;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .wave-wrap {
      position: relative;
      overflow: hidden;
      height: 240px;
      border-radius: 10px;
      border: 1px solid #1a4f68;
      background: linear-gradient(180deg, #072538, #081c2a);
    }
    .wave {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      opacity: 0.42;
      transform: scaleY(0.35);
      transition: transform 180ms ease, opacity 180ms ease;
    }
    .wave.speaking {
      opacity: 1;
      transform: scaleY(1);
      animation: pulseGlow 1.2s ease-in-out infinite;
    }
    .bar {
      width: 4px;
      height: 110px;
      border-radius: 4px;
      background: linear-gradient(180deg, transparent 0%, var(--line) 40%, transparent 100%);
      animation: bounce 1.4s ease-in-out infinite;
      animation-play-state: paused;
    }
    .wave.speaking .bar { animation-play-state: running; }
    .bar:nth-child(3n) { animation-duration: 1.1s; }
    .bar:nth-child(4n) { animation-duration: 1.8s; }
    .bar:nth-child(5n) { animation-duration: 1.3s; }
    .status {
      margin-top: 10px;
      font-size: 14px;
      color: var(--muted);
      min-height: 20px;
    }
    .mic-event {
      margin-top: 6px;
      font-size: 13px;
      color: #9de8ff;
      min-height: 18px;
    }
    .controls {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      margin-top: 12px;
    }
    input {
      width: 100%;
      background: #0a1f2d;
      color: var(--text);
      border: 1px solid #1a4f68;
      border-radius: 8px;
      padding: 10px;
      font-size: 14px;
    }
    button {
      background: #0f3b52;
      color: var(--text);
      border: 1px solid #1b6a8d;
      border-radius: 8px;
      padding: 10px 12px;
      cursor: pointer;
      font-size: 14px;
    }
    button:hover { filter: brightness(1.12); }
    .small {
      font-size: 12px;
      color: var(--muted);
      margin-top: 8px;
    }
    .debug-log {
      margin-top: 8px;
      font-size: 11px;
      color: #a7deef;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid #15475e;
      border-radius: 8px;
      background: #091b27;
      padding: 8px;
      min-height: 84px;
    }
    @keyframes bounce {
      0%, 100% { transform: scaleY(0.22); }
      50% { transform: scaleY(1); }
    }
    @keyframes pulseGlow {
      0%, 100% { box-shadow: inset 0 0 0 rgba(31,213,255,0.0); }
      50% { box-shadow: inset 0 0 120px rgba(31,213,255,0.16); }
    }
  </style>
</head>
<body>
  <div class="top-right">
    <button id="settingsBtn" class="icon-btn" title="Voice settings" aria-label="Voice settings">⚙</button>
  </div>

  <div id="settingsPanel" class="settings-panel" role="dialog" aria-label="MIM settings">
    <div class="settings-title">Voice Settings</div>

    <div class="settings-row">
      <label for="voiceSelect">Fixed Voice</label>
      <select id="voiceSelect"></select>
      <div class="settings-note">This stays fixed until you change it.</div>
    </div>

    <div class="settings-row">
      <label for="defaultLang">Default Listen Language</label>
      <input id="defaultLang" type="text" value="en-US" placeholder="en-US" />
    </div>

    <div class="settings-row">
      <label for="micSelect">Microphone Input</label>
      <select id="micSelect"></select>
      <div class="settings-note">If you have multiple mics, choose the one MIM should use.</div>
    </div>

    <div class="settings-row toggle-row">
      <input id="autoLangToggle" type="checkbox" checked />
      <label for="autoLangToggle">Speak in detected input language</label>
    </div>

    <div class="settings-row toggle-row">
      <input id="naturalVoiceToggle" type="checkbox" checked />
      <label for="naturalVoiceToggle">Natural Voice preset (smoother)</label>
    </div>

    <div class="settings-row">
      <label for="voiceRate">Voice Speed (<span id="voiceRateValue">1.00</span>)</label>
      <input id="voiceRate" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
    </div>

    <div class="settings-row">
      <label for="voicePitch">Voice Tone (<span id="voicePitchValue">1.00</span>)</label>
      <input id="voicePitch" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
    </div>

    <div class="settings-row">
      <label for="voiceDepth">Voice Depth (<span id="voiceDepthValue">0</span>)</label>
      <input id="voiceDepth" type="range" min="0" max="100" step="5" value="0" />
      <div class="settings-note">Higher depth lowers perceived pitch.</div>
    </div>

    <div class="settings-row">
      <label for="voiceVolume">Voice Volume (<span id="voiceVolumeValue">1.00</span>)</label>
      <input id="voiceVolume" type="range" min="0.40" max="1.00" step="0.05" value="1.00" />
    </div>
  </div>

  <h1 id="mimIcon" class="mim-icon">MIM</h1>
  <div id="buildTag" class="small" style="text-align:center; margin-top:-8px; margin-bottom:8px;">Build: objective-22-provider-degrade</div>

  <div class=\"panel\">
    <div class=\"wave-wrap\">
      <div id=\"wave\" class=\"wave\"></div>
    </div>
    <div id=\"status\" class=\"status\">Listening...</div>
    <div id="micEvent" class="mic-event">Mic event: waiting...</div>
    <div id=\"micDiag\" class=\"small\">Mic: detecting devices...</div>
    <div id=\"micDebug\" class=\"debug-log\">Mic debug: starting...</div>
    <div id=\"camera\" class=\"small\">Camera: waiting for observations</div>
    <div id=\"inquiry\" class=\"small\"></div>

    <div class=\"controls\">
      <input id=\"sayInput\" placeholder=\"Type what MIM should say\" value=\"Hello, I am MIM.\" />
      <button id=\"speakBtn\">Speak</button>
      <button id=\"listenBtn\">Listen</button>
    </div>

    <div class=\"controls\" style=\"grid-template-columns: 1fr auto; margin-top: 10px;\">
      <input id=\"cameraInput\" placeholder=\"Who is in view? (e.g. unknown, person, alice)\" value=\"unknown\" />
      <button id=\"cameraBtn\">Send Camera Event</button>
    </div>
  </div>

  <script>
    const wave = document.getElementById('wave');
    const statusEl = document.getElementById('status');
    const micEventEl = document.getElementById('micEvent');
    const micDiagEl = document.getElementById('micDiag');
    const micDebugEl = document.getElementById('micDebug');
    const cameraEl = document.getElementById('camera');
    const inquiryEl = document.getElementById('inquiry');
    const sayInput = document.getElementById('sayInput');
    const cameraInput = document.getElementById('cameraInput');
    const listenBtn = document.getElementById('listenBtn');
    const mimIcon = document.getElementById('mimIcon');
    const settingsBtn = document.getElementById('settingsBtn');
    const settingsPanel = document.getElementById('settingsPanel');
    const voiceSelect = document.getElementById('voiceSelect');
    const micSelect = document.getElementById('micSelect');
    const defaultLangInput = document.getElementById('defaultLang');
    const autoLangToggle = document.getElementById('autoLangToggle');
    const naturalVoiceToggle = document.getElementById('naturalVoiceToggle');
    const voiceRateInput = document.getElementById('voiceRate');
    const voicePitchInput = document.getElementById('voicePitch');
    const voiceDepthInput = document.getElementById('voiceDepth');
    const voiceVolumeInput = document.getElementById('voiceVolume');
    const voiceRateValueEl = document.getElementById('voiceRateValue');
    const voicePitchValueEl = document.getElementById('voicePitchValue');
    const voiceDepthValueEl = document.getElementById('voiceDepthValue');
    const voiceVolumeValueEl = document.getElementById('voiceVolumeValue');

    window.addEventListener('error', (event) => {
      const msg = String(event?.message || 'unknown_js_error');
      if (micEventEl) {
        micEventEl.textContent = `Mic event: js-error:${msg}`;
      }
      statusEl.textContent = `UI error: ${msg}`;
    });

    let micAutoMode = false;
    let micListening = false;
    let recognition = null;
    let motionInterval = null;
    let lastSpokenOutputId = Number(localStorage.getItem('mim_last_spoken_output_id') || 0);
    let availableVoices = [];
    let availableMics = [];
    let selectedVoiceURI = localStorage.getItem('mim_voice_uri') || '';
    let selectedVoiceName = localStorage.getItem('mim_voice_name') || '';
    let selectedMicDeviceId = localStorage.getItem('mim_mic_device_id') || '';
    let selectedMicLabel = localStorage.getItem('mim_mic_device_label') || '';
    let voiceRate = Number(localStorage.getItem('mim_voice_rate') || 0.96);
    let voicePitch = Number(localStorage.getItem('mim_voice_pitch') || 0.98);
    let voiceDepth = Number(localStorage.getItem('mim_voice_depth') || 0);
    let voiceVolume = Number(localStorage.getItem('mim_voice_volume') || 0.92);
    const healthState = {
      backendOk: true,
      micOk: true,
      micAvailable: true,
      cameraOk: true,
      voicesOk: true,
      voicesLoaded: false,
    };
    let backendFailureStreak = 0;
    let backendSuccessStreak = 0;
    let micErrorStreak = 0;
    let micRetryTimer = null;
    let micHardErrorStreak = 0;
    let micLastErrorCode = '';
    let micRecoveryMode = false;
    let micRecoveryReason = '';
    let micCooldownUntil = 0;
    let micEndTimestamps = [];
    let micRecentErrorAt = 0;
    let micConsecutiveOnend = 0;
    let micLastActiveAt = 0;
    let micStartInFlight = false;
    let micRestartPending = false;
    let micStartTimeoutTimer = null;
    let micLastLifecycleEventAt = 0;
    let micStartAttemptStreak = 0;
    let micStartTimeoutStreak = 0;
    let micStartFailureStreak = 0;
    let micSessionStartedAt = 0;
    let micShortRunStreak = 0;
    let micUnstableCycleCount = 0;
    let micLastEvent = '';
    let micLastEventAt = 0;
    let micDebugLines = [];
    let micFallbackNoSpeechTimer = null;
    let micFallbackCaptureInFlight = false;
    let micFallbackInterval = null;
    let micLastSpeechEventAt = 0;
    let micLastResultAt = 0;
    let voiceRecoveryInterval = null;
    let voiceRecoveryAttempts = 0;
    const MIC_FLAP_WINDOW_MS = 12000;
    const MIC_FLAP_THRESHOLD = 5;
    const MIC_FLAP_COOLDOWN_MS = 5000;
    const MIC_SHORT_RUN_MS = 1200;
    const MIC_SHORT_RUN_LIMIT = 4;
    const MIC_UNSTABLE_MAX_CYCLES = 3;
    const MIC_EVENT_MIN_INTERVAL_SECONDS = 0;
    const MIC_EVENT_DUPLICATE_WINDOW_SECONDS = 2;
    const MIC_EVENT_CONFIDENCE_FLOOR = 0.2;
    const FORCE_FALLBACK_STT = true;
    const STARTUP_IDENTITY_INQUIRY = "I can see someone. Hi there — who are you? What's your name?";
    const WEAK_IDENTITY_WORDS = new Set(['there', 'here', 'their', 'theyre', 'unknown', 'person', 'human', 'visitor']);
    let startupInquiryIssued = false;
    let lastInquiryPromptSpoken = '';
    let weakIdentityClarifyCooldownUntil = 0;
    let weakIdentityLastPromptKey = '';
    let lastLocalTtsError = '';
    let micPermissionState = 'unknown';
    let micPermissionStream = null;
    const SYSTEM_DEFAULT_LANG = 'en-US';
    let defaultListenLang = localStorage.getItem('mim_default_listen_lang') || SYSTEM_DEFAULT_LANG;
    let autoLanguageMode = localStorage.getItem('mim_auto_lang_mode') !== '0';
    let naturalVoicePreset = localStorage.getItem('mim_voice_natural_preset') !== '0';
    let currentConversationLang = localStorage.getItem('mim_current_lang') || defaultListenLang;
    let activeVisualIdentity = '';
    let lastVisualIdentity = '';
    let interactionMemory = {};
    let greetingCooldownByIdentity = {};

    try {
      interactionMemory = JSON.parse(localStorage.getItem('mim_identity_language_memory') || '{}') || {};
    } catch (_) {
      interactionMemory = {};
    }
    try {
      greetingCooldownByIdentity = JSON.parse(localStorage.getItem('mim_identity_greeting_cooldown') || '{}') || {};
    } catch (_) {
      greetingCooldownByIdentity = {};
    }

    for (let i = 0; i < 90; i += 1) {
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = `${40 + Math.abs(45 - i) * 1.6}px`;
      bar.style.animationDelay = `${(i % 12) * 0.07}s`;
      wave.appendChild(bar);
    }

    function setSpeaking(on) {
      wave.classList.toggle('speaking', !!on);
      statusEl.textContent = on ? 'MIM is speaking...' : 'MIM is listening...';
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function stopMicPermissionStream() {
      if (!micPermissionStream) return;
      try {
        for (const track of micPermissionStream.getTracks()) {
          try {
            track.stop();
          } catch (_) {
          }
        }
      } catch (_) {
      }
      micPermissionStream = null;
    }

    function addMicDebug(label, detail = '') {
      const time = new Date().toLocaleTimeString();
      const detailText = String(detail || '').trim();
      const line = detailText ? `[${time}] ${label} :: ${detailText}` : `[${time}] ${label}`;
      micDebugLines.push(line);
      if (micDebugLines.length > 10) {
        micDebugLines = micDebugLines.slice(-10);
      }
      if (micDebugEl) {
        const lineBreak = String.fromCharCode(10);
        micDebugEl.textContent = `Mic debug:${lineBreak}${micDebugLines.join(lineBreak)}`;
      }
    }

    function syncVoiceControlAvailability() {
      const manualMode = !naturalVoicePreset;
      voiceRateInput.disabled = !manualMode;
      voicePitchInput.disabled = !manualMode;
      voiceDepthInput.disabled = !manualMode;
      voiceVolumeInput.disabled = !manualMode;
    }

    function clearMicFallbackTimer() {
      if (micFallbackNoSpeechTimer) {
        clearTimeout(micFallbackNoSpeechTimer);
        micFallbackNoSpeechTimer = null;
      }
    }

    function stopMicFallbackLoop() {
      clearMicFallbackTimer();
      if (micFallbackInterval) {
        clearInterval(micFallbackInterval);
        micFallbackInterval = null;
      }
    }

    function startMicFallbackLoop() {
      stopMicFallbackLoop();
      micFallbackNoSpeechTimer = setTimeout(() => {
        if (!micAutoMode) return;
        noteMicEvent('fallback', 'scheduled-start');
        captureFallbackTranscription();
      }, 1400);
      micFallbackInterval = setInterval(() => {
        if (!micAutoMode) return;
        captureFallbackTranscription();
      }, 9000);
    }

    function writeAscii(view, offset, value) {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    }

    function encodeWavBlob(floatChunks, sampleRate) {
      let totalSamples = 0;
      for (const chunk of floatChunks) {
        totalSamples += chunk.length;
      }
      const pcmBuffer = new ArrayBuffer(44 + totalSamples * 2);
      const view = new DataView(pcmBuffer);

      writeAscii(view, 0, 'RIFF');
      view.setUint32(4, 36 + totalSamples * 2, true);
      writeAscii(view, 8, 'WAVE');
      writeAscii(view, 12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeAscii(view, 36, 'data');
      view.setUint32(40, totalSamples * 2, true);

      let offset = 44;
      for (const chunk of floatChunks) {
        for (let index = 0; index < chunk.length; index += 1) {
          const sample = Math.max(-1, Math.min(1, chunk[index]));
          const value = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
          view.setInt16(offset, value, true);
          offset += 2;
        }
      }
      return new Blob([view], { type: 'audio/wav' });
    }

    function blobToBase64(blob) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => {
          const text = String(reader.result || '');
          const base64 = text.includes(',') ? text.split(',')[1] : text;
          resolve(base64);
        };
        reader.onerror = () => reject(reader.error || new Error('read_failed'));
        reader.readAsDataURL(blob);
      });
    }

    async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || 12000));
      try {
        return await fetch(url, {
          ...options,
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timer);
      }
    }

    async function captureFallbackTranscription() {
      if (micFallbackCaptureInFlight) return;
      micFallbackCaptureInFlight = true;
      clearMicFallbackTimer();
      const captureStartedAt = Date.now();
      addMicDebug('fallback:start', `lang=${defaultListenLang}`);

      try {
        const preferredMic = resolvePreferredMicDevice();
        const fallbackAudioConstraints = {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        };
        if (preferredMic?.deviceId && preferredMic.deviceId !== 'default' && preferredMic.deviceId !== 'communications') {
          fallbackAudioConstraints.deviceId = { exact: preferredMic.deviceId };
        }

        const stream = await navigator.mediaDevices.getUserMedia({
          audio: fallbackAudioConstraints,
          video: false,
        });
        addMicDebug('fallback:getUserMedia', 'ok');

        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor) {
          noteMicEvent('fallback-error', 'AudioContext unavailable');
          for (const track of stream.getTracks()) {
            track.stop();
          }
          micFallbackCaptureInFlight = false;
          return;
        }

        const audioContext = new AudioContextCtor();
        const fallbackSampleRate = Math.max(8000, Math.round(Number(audioContext.sampleRate || 16000)));
        const sourceNode = audioContext.createMediaStreamSource(stream);
        const processorNode = audioContext.createScriptProcessor(4096, 1, 1);
        const floatChunks = [];

        processorNode.onaudioprocess = (event) => {
          const input = event.inputBuffer.getChannelData(0);
          floatChunks.push(new Float32Array(input));
        };

        sourceNode.connect(processorNode);
        processorNode.connect(audioContext.destination);
        noteMicEvent('fallback', 'capturing-audio');
        addMicDebug('fallback:capture', `sampleRate=${fallbackSampleRate}`);

        await new Promise((resolve) => setTimeout(resolve, 3200));

        try {
          processorNode.disconnect();
          sourceNode.disconnect();
        } catch (_) {
        }
        try {
          await audioContext.close();
        } catch (_) {
        }
        for (const track of stream.getTracks()) {
          track.stop();
        }

        if (!floatChunks.length) {
          noteMicEvent('fallback-empty', 'no-audio-chunks');
          addMicDebug('fallback:empty', 'no-audio-chunks');
          micFallbackCaptureInFlight = false;
          return;
        }

        const wavBlob = encodeWavBlob(floatChunks, fallbackSampleRate);
        const audioBase64 = await blobToBase64(wavBlob);
        addMicDebug('fallback:wav-ready', `bytes≈${Math.round((audioBase64.length * 3) / 4)}`);
        noteMicEvent('fallback', 'transcribe-request');
        const transcribeStartedAt = Date.now();
        const transcribeRes = await fetchWithTimeout('/gateway/perception/mic/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            audio_wav_base64: audioBase64,
            language: defaultListenLang,
            debug: true,
          }),
        }, 12000);

        if (!transcribeRes.ok) {
          let detail = '';
          let traceId = '';
          let debugLogPath = '';
          let rawErrorPayload = null;
          try {
            const errPayload = await transcribeRes.json();
            rawErrorPayload = errPayload;
            if (errPayload && typeof errPayload.detail === 'object' && errPayload.detail !== null) {
              detail = String(errPayload.detail.message || '').trim();
              traceId = String(errPayload.detail.trace_id || '').trim();
              debugLogPath = String(errPayload.detail.debug_log_path || '').trim();
            } else {
              detail = String(errPayload?.detail || '').trim();
              traceId = String(errPayload?.trace_id || '').trim();
            }
          } catch (_) {
          }
          const detailText = detail || transcribeRes.statusText || '-';
          const detailLower = detailText.toLowerCase();
          const isProviderForbidden = detailLower.includes('recognition request failed: forbidden') || detailLower.includes('forbidden');
          const isProviderError = isProviderForbidden || detailLower.includes('speech request failed') || detailLower.includes('recognition request failed');
          const isUpstreamUnavailable = Number(transcribeRes.status || 0) >= 500;
          const traceSuffix = traceId ? ` trace=${traceId}` : '';
          if (isProviderError || isUpstreamUnavailable) {
            noteMicEvent('fallback-degraded', `provider-unavailable${traceSuffix}`);
            statusEl.textContent = 'Listening... (speech provider unavailable)';
          } else {
            noteMicEvent('fallback-error', `http-${transcribeRes.status}:${detailText}${traceSuffix}`);
          }
          addMicDebug(
            'fallback:transcribe-http',
            `status=${transcribeRes.status} statusText=${transcribeRes.statusText || '-'} detail=${detailText} trace=${traceId || '-'} debugLog=${debugLogPath || '-'} providerError=${isProviderError} body=${rawErrorPayload ? JSON.stringify(rawErrorPayload).slice(0, 420) : '-'}`,
          );
          micFallbackCaptureInFlight = false;
          return;
        }

        const payload = await transcribeRes.json();
        noteMicEvent('fallback', 'transcribe-response');
        addMicDebug('fallback:transcribe-ok', `${Date.now() - transcribeStartedAt}ms`);
        if (payload && payload.ok === false && String(payload.reason || '') === 'provider_unavailable') {
          noteMicEvent('fallback-degraded', 'provider-unavailable');
          statusEl.textContent = 'Listening... (speech provider unavailable)';
          micFallbackCaptureInFlight = false;
          return;
        }
        const transcript = String(payload?.transcript || '').trim();
        if (!transcript) {
          const reason = String(payload?.reason || 'no-transcript').trim() || 'no-transcript';
          noteMicEvent('fallback-empty', reason);
          addMicDebug('fallback:no-transcript', `reason=${reason}`);
          micFallbackCaptureInFlight = false;
          return;
        }

        noteMicEvent('fallback-result', transcript.slice(0, 48));
        if (isLikelyLowValueTranscript(transcript)) {
          noteMicEvent('fallback-short', transcript.slice(0, 24));
          addMicDebug('fallback:short-transcript-skipped', transcript);
          micFallbackCaptureInFlight = false;
          return;
        }
        statusEl.textContent = `Heard: ${transcript}`;
        const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
        if (!handledWeakIdentity) {
          await maybeHandleIdentityIntroduction(transcript);
        }

        const micSync = await submitMicTranscript(transcript, Number(payload?.confidence || 0.74), 'fallback_audio');
        if (!micSync.ok) {
          noteMicEvent('fallback-sync-error', micSync.status);
          addMicDebug('fallback:event-sync', `status=${micSync.status}`);
        } else if (!micSync.accepted) {
          noteMicEvent('fallback-sync-skip', micSync.status);
          addMicDebug('fallback:event-sync', `skipped=${micSync.status}`);
        } else {
          addMicDebug('fallback:event-sync', `ok total=${Date.now() - captureStartedAt}ms`);
        }
        refreshState();
      } catch (error) {
        const errorName = String(error?.name || '').trim();
        if (errorName === 'AbortError') {
          noteMicEvent('fallback-error', 'transcribe-timeout');
          addMicDebug('fallback:error', 'AbortError/transcribe-timeout');
        } else {
          noteMicEvent('fallback-error', String(errorName || error?.message || 'unknown'));
          addMicDebug('fallback:error', String(errorName || error?.message || 'unknown'));
        }
      } finally {
        micFallbackCaptureInFlight = false;
        if (micAutoMode) {
          micLastSpeechEventAt = Date.now();
          micFallbackNoSpeechTimer = setTimeout(() => {
            if (!micAutoMode) return;
            captureFallbackTranscription();
          }, 7000);
        }
      }
    }

    function updateMicDiagnostics() {
      if (!availableMics.length) {
        if (micLastEvent) {
          micDiagEl.textContent = `Mic: no audio input devices detected yet. Last event: ${micLastEvent}`;
        } else {
          micDiagEl.textContent = 'Mic: no audio input devices detected yet.';
        }
        return;
      }

      let selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (!selected) {
        selected = availableMics.find((d) => d.deviceId === 'default') || availableMics[0];
      }

      const label = String(selected?.label || selectedMicLabel || 'Default microphone');
      const eventSuffix = micLastEvent ? ` · ${micLastEvent}` : '';
      if (availableMics.length > 1) {
        micDiagEl.textContent = `Mic: ${label} (${availableMics.length} detected)${eventSuffix}`;
      } else {
        micDiagEl.textContent = `Mic: ${label}${eventSuffix}`;
      }
    }

    function noteMicEvent(eventLabel, detail = '') {
      const time = new Date().toLocaleTimeString();
      const detailText = String(detail || '').trim();
      micLastEventAt = Date.now();
      micLastEvent = detailText ? `${eventLabel}:${detailText} @ ${time}` : `${eventLabel} @ ${time}`;
      if (micEventEl) {
        micEventEl.textContent = `Mic event: ${micLastEvent}`;
      }
      addMicDebug(`event:${eventLabel}`, detailText);
      listenBtn.textContent = micAutoMode ? 'Listening On' : 'Listening Off';
      updateMicDiagnostics();
    }

    function resolvePreferredMicDevice() {
      if (!availableMics.length) return null;

      const explicit = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (explicit && selectedMicDeviceId && selectedMicDeviceId !== 'default' && selectedMicDeviceId !== 'communications') {
        return explicit;
      }

      const candidates = availableMics.filter((d) => d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications');
      if (!candidates.length) {
        return availableMics.find((d) => d.deviceId === 'default') || availableMics[0] || null;
      }

      const scored = candidates.map((device) => {
        const label = String(device.label || '').toLowerCase();
        let score = 0;
        if (/(fduce|usb|headset|microphone|mic|pro audio|analog)/.test(label)) score += 30;
        if (/(camera|webcam|emeet|s600)/.test(label)) score -= 40;
        return { device, score };
      });

      scored.sort((a, b) => b.score - a.score);
      return scored[0]?.device || candidates[0];
    }

    async function enumerateMicDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        availableMics = [];
        micSelect.innerHTML = '';
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'Microphone listing unavailable';
        micSelect.appendChild(option);
        updateMicDiagnostics();
        return;
      }

      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        availableMics = devices.filter((d) => d.kind === 'audioinput');
      } catch (_) {
        availableMics = [];
      }

      micSelect.innerHTML = '';
      if (!availableMics.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No microphones detected';
        micSelect.appendChild(option);
        updateMicDiagnostics();
        return;
      }

      const preferred = resolvePreferredMicDevice();
      let selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (!selected) {
        selected = preferred || availableMics.find((d) => d.deviceId === 'default') || availableMics[0];
      }

      for (let index = 0; index < availableMics.length; index += 1) {
        const mic = availableMics[index];
        const option = document.createElement('option');
        option.value = mic.deviceId;
        option.textContent = mic.label || `Microphone ${index + 1}`;
        micSelect.appendChild(option);
      }

      selectedMicDeviceId = selected?.deviceId || '';
      selectedMicLabel = selected?.label || '';
      micSelect.value = selectedMicDeviceId;
      localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
      localStorage.setItem('mim_mic_device_label', selectedMicLabel);
      updateMicDiagnostics();
    }

    function syncVoiceControlLabels() {
      voiceRateValueEl.textContent = Number(voiceRate).toFixed(2);
      voicePitchValueEl.textContent = Number(voicePitch).toFixed(2);
      voiceDepthValueEl.textContent = String(Math.round(voiceDepth));
      voiceVolumeValueEl.textContent = Number(voiceVolume).toFixed(2);
    }

    function effectivePitchValue() {
      const depthLowering = (voiceDepth / 100) * 0.45;
      return clamp(voicePitch - depthLowering, 0.5, 2.0);
    }

    function hasAnyHealthError() {
      return !healthState.backendOk || !healthState.micOk || !healthState.cameraOk || !healthState.voicesOk;
    }

    function hasCriticalHealthError() {
      return !healthState.backendOk || !healthState.micAvailable || !healthState.micOk;
    }

    function isMicEffectivelyActive() {
      if (micListening || micStartInFlight || micRestartPending) return true;
      return (Date.now() - micLastActiveAt) < 4000;
    }

    function applyStatusFromHealth() {
      if (!healthState.backendOk) {
        statusEl.textContent = 'Backend unreachable. Retrying...';
        return;
      }
      if (!healthState.micAvailable) {
        statusEl.textContent = 'Mic recognition API unavailable in this runtime.';
        return;
      }
      if (micRecoveryMode) {
        const remainingMs = Math.max(0, micCooldownUntil - Date.now());
        const remainingSec = Math.ceil(remainingMs / 1000);
        statusEl.textContent = remainingSec > 0
          ? `Mic stabilizing (${remainingSec}s)...`
          : 'Mic stabilization complete. Reconnecting...';
        return;
      }
      if (!healthState.micOk) {
        if (micLastErrorCode) {
          statusEl.textContent = `Mic recovering (${micLastErrorCode})...`;
        } else {
          statusEl.textContent = 'Mic recovering from errors...';
        }
        return;
      }
      if (!healthState.voicesOk) {
        statusEl.textContent = 'Voice list unavailable. Using system default voice.';
        return;
      }
      if (!micAutoMode && isMicEffectivelyActive()) {
        statusEl.textContent = 'Listening...';
        return;
      }
      if (micAutoMode && !isMicEffectivelyActive()) {
        if (micErrorStreak > 0) {
          statusEl.textContent = 'Mic reconnecting...';
        } else {
          statusEl.textContent = 'Starting always-listen mic...';
        }
        return;
      }
      if (micAutoMode && isMicEffectivelyActive()) {
        statusEl.textContent = 'Always listening...';
        return;
      }
      if (!micAutoMode) {
        statusEl.textContent = 'Listening paused.';
      }
    }

    function updateIconGlow() {
      mimIcon.classList.remove('ok', 'err');
      if (hasCriticalHealthError()) {
        mimIcon.classList.add('err');
        applyStatusFromHealth();
        return;
      }
      mimIcon.classList.add('ok');
      applyStatusFromHealth();
    }

    function enterMicRecovery(reason) {
      micRecoveryMode = true;
      micRecoveryReason = String(reason || 'restart-flap');
      const cooldownMs = micRecoveryReason.includes('short-run-flap') ? 12000 : MIC_FLAP_COOLDOWN_MS;
      micCooldownUntil = Date.now() + cooldownMs;
      micListening = false;
      micStartInFlight = false;
      micRestartPending = true;
      micErrorStreak = 0;
      micHardErrorStreak = 0;
      micLastErrorCode = '';
      healthState.micOk = true;

      if (recognition) {
        try {
          recognition.stop();
        } catch (_) {
        }
      }

      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
      }
      micRetryTimer = setTimeout(() => {
        micRetryTimer = null;
        micRecoveryMode = false;
        micRecoveryReason = '';
        micRestartPending = false;
        micEndTimestamps = [];
        if (micUnstableCycleCount >= MIC_UNSTABLE_MAX_CYCLES) {
          micAutoMode = false;
          listenBtn.textContent = 'Listening Off';
          statusEl.textContent = 'Mic paused after repeated unstable starts. Press Listen to retry.';
          updateIconGlow();
          return;
        }
        listenOnce();
      }, cooldownMs);

      updateIconGlow();
    }

    function noteMicCycleAndMaybeRecover(reason) {
      if (!String(reason || '').startsWith('hard-error:')) {
        return false;
      }
      const now = Date.now();
      micEndTimestamps.push(now);
      micEndTimestamps = micEndTimestamps.filter((ts) => now - ts <= MIC_FLAP_WINDOW_MS);
      if (micEndTimestamps.length >= MIC_FLAP_THRESHOLD) {
        enterMicRecovery(reason || 'restart-flap');
        return true;
      }
      return false;
    }

    function scheduleMicRetry(delayMs) {
      if (!micAutoMode) return;
      micRestartPending = true;
      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
      }
      micRetryTimer = setTimeout(() => {
        micRetryTimer = null;
        micRestartPending = false;
        listenOnce();
      }, Math.max(150, Number(delayMs) || 350));
    }

    function clearMicStartTimeout() {
      if (micStartTimeoutTimer) {
        clearTimeout(micStartTimeoutTimer);
        micStartTimeoutTimer = null;
      }
    }

    function pauseMicAuto(reasonText) {
      micAutoMode = false;
      listenBtn.textContent = 'Listening Off';
      micListening = false;
      micStartInFlight = false;
      micRestartPending = false;
      stopMicFallbackLoop();
      clearMicStartTimeout();
      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
        micRetryTimer = null;
      }
      statusEl.textContent = reasonText || 'Mic auto-listen paused.';
      updateIconGlow();
    }

    function noteMicLifecycleEvent() {
      micLastLifecycleEventAt = Date.now();
    }

    function resetRecognitionInstance() {
      clearMicStartTimeout();
      if (recognition) {
        try {
          recognition.onstart = null;
          recognition.onresult = null;
          recognition.onerror = null;
          recognition.onend = null;
          recognition.stop();
        } catch (_) {
        }
      }
      recognition = null;
      micListening = false;
      micStartInFlight = false;
      micSessionStartedAt = 0;
    }

    function markBackendReachability(ok) {
      if (ok) {
        backendFailureStreak = 0;
        backendSuccessStreak += 1;
        if (backendSuccessStreak >= 1) {
          healthState.backendOk = true;
        }
        return;
      }

      backendSuccessStreak = 0;
      backendFailureStreak += 1;
      if (backendFailureStreak >= 3) {
        healthState.backendOk = false;
      }
    }

    async function submitMicTranscript(transcript, confidence, mode = 'always_listening') {
      const safeTranscript = String(transcript || '').trim();
      if (!safeTranscript) {
        return { ok: false, accepted: false, status: 'empty_transcript' };
      }

      const safeConfidence = clamp(Number(confidence || 0.72), 0.0, 1.0);
      try {
        const micEventRes = await fetchWithTimeout('/gateway/perception/mic/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_id: 'mim-ui-mic',
            source_type: 'microphone',
            session_id: 'mim-ui-session',
            is_remote: false,
            transcript: safeTranscript,
            confidence: safeConfidence,
            min_interval_seconds: MIC_EVENT_MIN_INTERVAL_SECONDS,
            duplicate_window_seconds: MIC_EVENT_DUPLICATE_WINDOW_SECONDS,
            transcript_confidence_floor: MIC_EVENT_CONFIDENCE_FLOOR,
            discard_low_confidence: false,
            metadata_json: { source: 'mim_ui_sketch', mode },
          }),
        }, 8000);

        if (!micEventRes.ok) {
          markBackendReachability(false);
          return {
            ok: false,
            accepted: false,
            status: `http_${micEventRes.status}`,
          };
        }

        markBackendReachability(true);
        let payload = {};
        try {
          payload = await micEventRes.json();
        } catch (_) {
          payload = {};
        }
        return {
          ok: true,
          accepted: payload?.accepted !== false,
          status: String(payload?.status || 'accepted'),
        };
      } catch (_) {
        markBackendReachability(false);
        return { ok: false, accepted: false, status: 'network_error' };
      }
    }

    function isLikelyLowValueTranscript(transcript) {
      const text = String(transcript || '').trim().toLowerCase();
      if (!text) return true;

      const compact = text.replace(/[^a-z]/g, '');
      if (compact.length >= 3) return false;

      const allowedShortUtterances = new Set(['hi', 'ok', 'no', 'yo']);
      return !allowedShortUtterances.has(compact);
    }

    function speakLocally(text, interrupt = true) {
      const phrase = String(text || '').trim();
      const smoothedPhrase = phrase.replace(/\s*[—-]\s*/g, ', ').replace(/\s{2,}/g, ' ').trim();
      if (!phrase) return false;
      if (!window.speechSynthesis) {
        lastLocalTtsError = 'speechSynthesis API unavailable';
        statusEl.textContent = 'Local TTS unavailable in this runtime.';
        return false;
      }

      try {
        if (window.speechSynthesis.resume) {
          window.speechSynthesis.resume();
        }
        if (interrupt) {
          window.speechSynthesis.cancel();
        }

        let started = false;
        let retriedBare = false;
        const utteranceText = naturalVoicePreset ? (smoothedPhrase || phrase) : phrase;
        const utterance = new SpeechSynthesisUtterance(utteranceText);
        const preferredLang = getPreferredInteractionLanguage();
        utterance.lang = preferredLang;

        const chosenVoice = resolveVoiceForLanguage(preferredLang);
        if (chosenVoice) {
          utterance.voice = chosenVoice;
        }

        const appliedRate = naturalVoicePreset
          ? clamp(voiceRate, 0.86, 1.08)
          : clamp(voiceRate, 0.1, 10.0);
        const appliedPitch = naturalVoicePreset
          ? clamp(effectivePitchValue(), 0.82, 1.08)
          : effectivePitchValue();
        const appliedVolume = naturalVoicePreset
          ? clamp(voiceVolume, 0.65, 1.0)
          : clamp(voiceVolume, 0.0, 1.0);
        utterance.rate = appliedRate;
        utterance.pitch = appliedPitch;
        utterance.volume = appliedVolume;
        utterance.onstart = () => {
          started = true;
          lastLocalTtsError = '';
          setSpeaking(true);
        };
        utterance.onend = () => setSpeaking(false);
        utterance.onerror = (event) => {
          lastLocalTtsError = String(event?.error || 'unknown_tts_error');
          setSpeaking(false);
          statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
        };
        window.speechSynthesis.speak(utterance);

        const tryBareRetry = () => {
          if (started || retriedBare) return;
          retriedBare = true;
          try {
            window.speechSynthesis.cancel();
            const fallbackUtterance = new SpeechSynthesisUtterance(utteranceText);
            fallbackUtterance.rate = appliedRate;
            fallbackUtterance.pitch = appliedPitch;
            fallbackUtterance.volume = appliedVolume;
            fallbackUtterance.onstart = () => {
              started = true;
              lastLocalTtsError = '';
              setSpeaking(true);
            };
            fallbackUtterance.onend = () => setSpeaking(false);
            fallbackUtterance.onerror = (event) => {
              lastLocalTtsError = String(event?.error || 'fallback_tts_error');
              setSpeaking(false);
              statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
            };
            window.speechSynthesis.speak(fallbackUtterance);
          } catch (_) {
          }
        };

        setTimeout(() => {
          if (!started) {
            tryBareRetry();
          }
        }, 1200);

        setTimeout(() => {
          if (!started) {
            const voices = window.speechSynthesis.getVoices ? window.speechSynthesis.getVoices() : [];
            const voiceCount = Array.isArray(voices) ? voices.length : 0;
            lastLocalTtsError = `tts_not_started voices=${voiceCount}`;
            statusEl.textContent = `Voice playback did not start (voices=${voiceCount}).`;
          }
        }, 2500);

        return true;
      } catch (_) {
        lastLocalTtsError = 'exception_during_tts';
        statusEl.textContent = 'Local voice playback failed before start.';
        return false;
      }
    }

    function maybeSpeakFromState(data) {
      const outputId = Number(data.latest_output_action_id || 0);
      const text = String(data.latest_output_text || '').trim();
      const allowed = Boolean(data.latest_output_allowed);
      if (!allowed || !text || outputId <= 0) return;
      if (outputId <= lastSpokenOutputId) return;

      if (speakLocally(text, true)) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
      }
    }

    function persistIdentityMemory() {
      localStorage.setItem('mim_identity_language_memory', JSON.stringify(interactionMemory));
    }

    function persistGreetingCooldowns() {
      localStorage.setItem('mim_identity_greeting_cooldown', JSON.stringify(greetingCooldownByIdentity));
    }

    function normalizeIdentityLabel(raw) {
      const label = String(raw || '').trim().toLowerCase();
      if (!label) return '';
      if (['unknown', 'person', 'human', 'visitor', 'activity'].includes(label)) return '';
      return label;
    }

    function getPreferredInteractionLanguage() {
      if (!autoLanguageMode) {
        return normalizeLangCode(defaultListenLang || SYSTEM_DEFAULT_LANG);
      }

      const identity = normalizeIdentityLabel(activeVisualIdentity);
      if (identity) {
        const remembered = interactionMemory?.[identity]?.lang;
        if (remembered) {
          return normalizeLangCode(remembered);
        }
      }

      return normalizeLangCode(currentConversationLang || defaultListenLang || SYSTEM_DEFAULT_LANG);
    }

    function normalizeLangCode(raw) {
      const text = String(raw || '').trim();
      if (!text) return 'en-US';
      if (text.includes('-')) return text;
      if (text.length === 2) {
        const lower = text.toLowerCase();
        if (lower === 'en') return 'en-US';
        if (lower === 'es') return 'es-ES';
        if (lower === 'fr') return 'fr-FR';
        if (lower === 'de') return 'de-DE';
        if (lower === 'it') return 'it-IT';
        if (lower === 'pt') return 'pt-BR';
        if (lower === 'ja') return 'ja-JP';
        if (lower === 'ko') return 'ko-KR';
        if (lower === 'zh') return 'zh-CN';
      }
      return text;
    }

    function detectExplicitLanguageOverride(transcript) {
      const lower = ` ${String(transcript || '').toLowerCase()} `;
      const rules = [
        { lang: 'en-US', patterns: [' speak english ', ' use english ', ' english only '] },
        { lang: 'fr-FR', patterns: [' speak french ', ' use french ', ' en français ', ' francais '] },
        { lang: 'es-ES', patterns: [' speak spanish ', ' use spanish ', ' en español ', ' espanol '] },
        { lang: 'de-DE', patterns: [' speak german ', ' use german ', ' auf deutsch '] },
        { lang: 'it-IT', patterns: [' speak italian ', ' use italian ', ' in italiano '] },
        { lang: 'pt-BR', patterns: [' speak portuguese ', ' use portuguese ', ' em português ', ' portugues '] },
        { lang: 'ja-JP', patterns: [' speak japanese ', ' use japanese ', ' 日本語で '] },
        { lang: 'ko-KR', patterns: [' speak korean ', ' use korean ', ' 한국어로 '] },
        { lang: 'zh-CN', patterns: [' speak chinese ', ' use chinese ', ' 中文 '] },
      ];

      for (const rule of rules) {
        if (rule.patterns.some((pattern) => lower.includes(pattern))) {
          return rule.lang;
        }
      }

      return '';
    }

    function greetingForLanguage(identity, langCode) {
      const name = identity ? identity.charAt(0).toUpperCase() + identity.slice(1) : 'there';
      const lang = normalizeLangCode(langCode).toLowerCase();
      if (lang.startsWith('fr')) return `Bonjour ${name} — ravi de vous revoir.`;
      if (lang.startsWith('es')) return `Hola ${name}, me alegra verte.`;
      if (lang.startsWith('de')) return `Hallo ${name}, schön dich wiederzusehen.`;
      if (lang.startsWith('it')) return `Ciao ${name}, felice di rivederti.`;
      if (lang.startsWith('pt')) return `Olá ${name}, bom te ver novamente.`;
      if (lang.startsWith('ja')) return `${name}さん、またお会いできてうれしいです。`;
      if (lang.startsWith('ko')) return `${name}님, 다시 만나서 반가워요.`;
      if (lang.startsWith('zh')) return `${name}，很高兴再次见到你。`;
      return `Hello ${name}, great to see you again.`;
    }

    function extractIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const patterns = [
        /\bmy name is\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
        /\bi am\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
        /\bi'm\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
      ];

      let candidate = '';
      for (const pattern of patterns) {
        const match = text.match(pattern);
        if (match && match[1]) {
          candidate = String(match[1]).trim();
          break;
        }
      }
      if (!candidate) return '';

      candidate = candidate.replace(/[^a-z'\-\s]/gi, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
      if (!candidate || WEAK_IDENTITY_WORDS.has(candidate)) return '';
      return candidate;
    }

    function extractWeakIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const patterns = [
        /\bmy name is\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
        /\bi am\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
        /\bi'm\s+([a-z][a-z'\-]{1,24}(?:\s+[a-z][a-z'\-]{1,24})?)/i,
      ];

      for (const pattern of patterns) {
        const match = text.match(pattern);
        if (!match || !match[1]) continue;
        const candidate = String(match[1]).replace(/[^a-z'\-\s]/gi, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
        if (candidate && WEAK_IDENTITY_WORDS.has(candidate)) {
          return candidate;
        }
      }

      return '';
    }

    async function maybeHandleWeakIdentityIntroduction(transcript) {
      const weakIdentity = extractWeakIntroducedIdentity(transcript);
      if (!weakIdentity) return false;

      const now = Date.now();
      const promptKey = `weak:${weakIdentity}`;
      if (now < weakIdentityClarifyCooldownUntil && weakIdentityLastPromptKey === promptKey) {
        return true;
      }

      const clarification = 'I may have misheard your name. Please say: my name is, then your name.';
      startupInquiryIssued = true;
      inquiryEl.textContent = clarification;
      speakLocally(clarification, false);
      lastInquiryPromptSpoken = clarification;
      weakIdentityLastPromptKey = promptKey;
      weakIdentityClarifyCooldownUntil = now + 20000;
      return true;
    }

    async function maybeHandleIdentityIntroduction(transcript) {
      const spokenIdentity = extractIntroducedIdentity(transcript);
      if (!spokenIdentity) return false;

      const normalized = normalizeIdentityLabel(spokenIdentity);
      if (!normalized || isUnknownOrMissingIdentity(normalized)) return false;

      const displayName = normalized.charAt(0).toUpperCase() + normalized.slice(1);
      startupInquiryIssued = true;
      inquiryEl.textContent = `Nice to meet you, ${displayName}.`;
      speakLocally(`Nice to meet you, ${displayName}.`, false);
      lastInquiryPromptSpoken = `identity:${normalized}`;

      activeVisualIdentity = normalized;
      interactionMemory[normalized] = {
        lang: currentConversationLang || defaultListenLang,
        updated_at: new Date().toISOString(),
        source: 'verbal_identity_intro',
      };
      persistIdentityMemory();

      cameraInput.value = normalized;
      try {
        await fetch('/gateway/perception/camera/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_id: 'mim-ui-camera',
            source_type: 'camera',
            session_id: 'mim-ui-session',
            is_remote: false,
            observations: [{ object_label: normalized, confidence: 0.92, zone: 'front-center' }],
            metadata_json: { source: 'mim_ui_sketch', reason: 'verbal_identity_intro' },
          }),
        });
      } catch (_) {
      }

      return true;
    }

    async function maybeGreetRecognizedIdentity(identity) {
      const normalized = normalizeIdentityLabel(identity);
      if (!normalized) return;
      const rememberedLang = interactionMemory?.[normalized]?.lang;
      if (!rememberedLang) return;

      const now = Date.now();
      const lastAt = Number(greetingCooldownByIdentity?.[normalized] || 0);
      if (now - lastAt < 60000) return;

      greetingCooldownByIdentity[normalized] = now;
      persistGreetingCooldowns();

      const message = greetingForLanguage(normalized, rememberedLang);
      try {
        await fetch('/gateway/voice/output', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message,
            voice_profile: 'default',
            channel: 'ui',
            priority: 'normal',
            metadata_json: {
              source: 'mim_ui_sketch',
              reason: 'identity_language_greeting',
              identity: normalized,
              language: rememberedLang,
            },
          }),
        });
      } catch (_) {
      }
    }

    function isUnknownOrMissingIdentity(labelRaw) {
      const label = String(labelRaw || '').trim().toLowerCase();
      if (!label || label === '(none)') return true;
      return ['unknown', 'person', 'human', 'visitor', 'activity'].includes(label);
    }

    async function maybeIssueStartupIdentityInquiry(data) {
      if (startupInquiryIssued) return;

      const backendPrompt = String(data?.inquiry_prompt || '').trim();
      if (backendPrompt) {
        startupInquiryIssued = true;
        inquiryEl.textContent = backendPrompt;
        speakLocally(backendPrompt, false);
        lastInquiryPromptSpoken = backendPrompt;
        return;
      }

      if (!isUnknownOrMissingIdentity(data?.camera_last_label)) {
        startupInquiryIssued = true;
        return;
      }

      startupInquiryIssued = true;
      inquiryEl.textContent = STARTUP_IDENTITY_INQUIRY;
      speakLocally(STARTUP_IDENTITY_INQUIRY, false);
      lastInquiryPromptSpoken = STARTUP_IDENTITY_INQUIRY;

      try {
        await fetch('/gateway/voice/output', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: STARTUP_IDENTITY_INQUIRY,
            voice_profile: 'default',
            channel: 'ui',
            priority: 'normal',
            metadata_json: {
              source: 'mim_ui_sketch',
              reason: 'startup_identity_inquiry',
            },
          }),
        });
      } catch (_) {
      }
    }

    function detectLanguageFromTranscript(transcript) {
      const t = String(transcript || '').trim();
      if (!t) return defaultListenLang;

      if (/[\u3040-\u30ff]/.test(t)) return 'ja-JP';
      if (/[\uac00-\ud7af]/.test(t)) return 'ko-KR';
      if (/[\u4e00-\u9fff]/.test(t)) return 'zh-CN';
      if (/[а-яА-ЯЁё]/.test(t)) return 'ru-RU';

      const lower = t.toLowerCase();
      if (/[¿¡]/.test(t) || /( hola | gracias | por favor | buenos )/.test(` ${lower} `)) return 'es-ES';
      if (/( bonjour | merci | s'il vous plaît | salut )/.test(` ${lower} `)) return 'fr-FR';
      if (/( hallo | danke | bitte | guten )/.test(` ${lower} `)) return 'de-DE';
      if (/( olá | obrigado | obrigada | por favor )/.test(` ${lower} `)) return 'pt-BR';
      if (/( ciao | grazie | per favore )/.test(` ${lower} `)) return 'it-IT';

      return defaultListenLang;
    }

    function scoreVoice(voice, langPrefix) {
      const name = String(voice?.name || '').toLowerCase();
      const lang = String(voice?.lang || '').toLowerCase();
      let score = 0;

      if (lang.startsWith(langPrefix)) score += 80;
      else if (langPrefix === 'en' && lang.startsWith('en')) score += 50;

      if (voice?.default) score += 12;
      if (voice?.localService) score += 8;

      if (/(neural|natural|enhanced|premium|wavenet|studio)/.test(name)) score += 22;
      if (/(siri|samantha|victoria|daniel|karen|moira|zira|aria)/.test(name)) score += 12;
      if (/(espeak|compact|robot|test|default voice)/.test(name)) score -= 20;

      return score;
    }

    function resolveVoiceForLanguage(langCode) {
      if (!window.speechSynthesis) return null;
      const lang = normalizeLangCode(langCode).toLowerCase();
      const langPrefix = lang.split('-')[0];

      if (selectedVoiceURI) {
        const byUri = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
        if (byUri && byUri.lang && byUri.lang.toLowerCase().startsWith(langPrefix)) {
          return byUri;
        }
      }

      if (selectedVoiceName) {
        const byName = availableVoices.find((v) => v.name === selectedVoiceName && String(v.lang || '').toLowerCase().startsWith(langPrefix));
        if (byName) {
          return byName;
        }
      }

      const ranked = [...availableVoices].sort((a, b) => scoreVoice(b, langPrefix) - scoreVoice(a, langPrefix));
      if (ranked.length) {
        return ranked[0];
      }

      if (selectedVoiceURI) {
        const fallbackUri = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
        if (fallbackUri) return fallbackUri;
      }
      if (selectedVoiceName) {
        const fallbackName = availableVoices.find((v) => v.name === selectedVoiceName);
        if (fallbackName) return fallbackName;
      }

      return availableVoices[0] || null;
    }

    function applyVoiceSettings() {
      defaultListenLang = normalizeLangCode(defaultLangInput.value || defaultListenLang);
      defaultLangInput.value = defaultListenLang;
      localStorage.setItem('mim_default_listen_lang', defaultListenLang);

      autoLanguageMode = Boolean(autoLangToggle.checked);
      localStorage.setItem('mim_auto_lang_mode', autoLanguageMode ? '1' : '0');

      naturalVoicePreset = Boolean(naturalVoiceToggle.checked);
      localStorage.setItem('mim_voice_natural_preset', naturalVoicePreset ? '1' : '0');
      syncVoiceControlAvailability();

      if (!autoLanguageMode) {
        currentConversationLang = defaultListenLang;
        localStorage.setItem('mim_current_lang', currentConversationLang);
      }

      const uri = String(voiceSelect.value || '').trim();
      if (uri) {
        const matched = availableVoices.find((v) => v.voiceURI === uri);
        if (matched) {
          selectedVoiceURI = matched.voiceURI;
          selectedVoiceName = matched.name;
          localStorage.setItem('mim_voice_uri', selectedVoiceURI);
          localStorage.setItem('mim_voice_name', selectedVoiceName);
        }
      }

      const nextRate = Number(voiceRateInput.value || voiceRate);
      const nextPitch = Number(voicePitchInput.value || voicePitch);
      const nextDepth = Number(voiceDepthInput.value || voiceDepth);
      const nextVolume = Number(voiceVolumeInput.value || voiceVolume);

      voiceRate = clamp(Number.isFinite(nextRate) ? nextRate : 1.0, 0.7, 1.35);
      voicePitch = clamp(Number.isFinite(nextPitch) ? nextPitch : 1.0, 0.7, 1.35);
      voiceDepth = clamp(Number.isFinite(nextDepth) ? nextDepth : 0, 0, 100);
      voiceVolume = clamp(Number.isFinite(nextVolume) ? nextVolume : 1.0, 0.4, 1.0);

      voiceRateInput.value = voiceRate.toFixed(2);
      voicePitchInput.value = voicePitch.toFixed(2);
      voiceDepthInput.value = String(Math.round(voiceDepth));
      voiceVolumeInput.value = voiceVolume.toFixed(2);
      syncVoiceControlLabels();

      localStorage.setItem('mim_voice_rate', String(voiceRate));
      localStorage.setItem('mim_voice_pitch', String(voicePitch));
      localStorage.setItem('mim_voice_depth', String(voiceDepth));
      localStorage.setItem('mim_voice_volume', String(voiceVolume));

      if (recognition) {
        recognition.lang = defaultListenLang;
      }

      updateIconGlow();
    }

    async function ensureMicPermission(options = {}) {
      const keepStreamAlive = Boolean(options?.keepStreamAlive);
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        micPermissionState = 'unavailable';
        noteMicEvent('permission', 'mediaDevices unavailable');
        return false;
      }

      const audioConstraints = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      };

      const preferredMic = resolvePreferredMicDevice();
      const preferredCandidates = [];
      if (preferredMic?.deviceId) {
        preferredCandidates.push(preferredMic);
      }
      if (selectedMicDeviceId && selectedMicDeviceId !== 'default' && selectedMicDeviceId !== 'communications') {
        const selectedExplicit = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
        if (selectedExplicit && !preferredCandidates.some((d) => d.deviceId === selectedExplicit.deviceId)) {
          preferredCandidates.push(selectedExplicit);
        }
      }
      for (const mic of availableMics) {
        if (mic.deviceId && mic.deviceId !== 'default' && mic.deviceId !== 'communications' && !preferredCandidates.some((d) => d.deviceId === mic.deviceId)) {
          preferredCandidates.push(mic);
        }
      }

      try {
        let stream = null;
        let lastError = null;

        for (const candidate of preferredCandidates) {
          try {
            noteMicEvent('permission-route', candidate.label || candidate.deviceId);
            stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
                deviceId: { exact: candidate.deviceId },
              },
              video: false,
            });
            selectedMicDeviceId = candidate.deviceId;
            selectedMicLabel = candidate.label || selectedMicLabel || 'Microphone';
            localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
            localStorage.setItem('mim_mic_device_label', selectedMicLabel);
            break;
          } catch (candidateError) {
            lastError = candidateError;
          }
        }

        if (!stream) {
          try {
            noteMicEvent('permission-fallback', 'trying default');
            stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
              },
              video: false,
            });
            selectedMicDeviceId = 'default';
            selectedMicLabel = 'Default microphone';
            localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
            localStorage.setItem('mim_mic_device_label', selectedMicLabel);
          } catch (fallbackError) {
            throw (fallbackError || lastError || new Error('mic_open_failed'));
          }
        }

        if (!stream) {
          throw new Error('mic_stream_unavailable');
        }

        noteMicEvent('permission', 'granted');
        if (keepStreamAlive) {
          stopMicPermissionStream();
          micPermissionStream = stream;
          noteMicEvent('permission-stream', 'active');
        } else {
          try {
            for (const track of stream.getTracks()) {
              track.stop();
            }
          } catch (_) {
          }
          stopMicPermissionStream();
        }
        micPermissionState = 'granted';
        await enumerateMicDevices();
        updateMicDiagnostics();
        return true;
      } catch (error) {
        noteMicEvent('permission-error', String(error?.name || error?.message || 'unknown'));
        micPermissionState = 'denied';
        statusEl.textContent = 'Mic permission blocked. Allow microphone access for MIM Desktop.';
        healthState.micOk = false;
        updateIconGlow();
        return false;
      }
    }

    function buildVoiceOptions() {
      if (!window.speechSynthesis) return;
      availableVoices = window.speechSynthesis.getVoices() || [];
      availableVoices.sort((a, b) => {
        const aEn = String(a.lang || '').toLowerCase().startsWith('en') ? 0 : 1;
        const bEn = String(b.lang || '').toLowerCase().startsWith('en') ? 0 : 1;
        if (aEn !== bEn) return aEn - bEn;

        const langCompare = String(a.lang || '').localeCompare(String(b.lang || ''));
        if (langCompare !== 0) return langCompare;
        return String(a.name || '').localeCompare(String(b.name || ''));
      });
      voiceSelect.innerHTML = '';

      if (!availableVoices.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'System default voice (list unavailable)';
        voiceSelect.appendChild(option);
        healthState.voicesLoaded = false;
        healthState.voicesOk = false;
        updateIconGlow();
        return;
      }

      for (const voice of availableVoices) {
        const option = document.createElement('option');
        option.value = voice.voiceURI;
        option.textContent = `${voice.name} (${voice.lang})`;
        voiceSelect.appendChild(option);
      }
      healthState.voicesLoaded = true;
      healthState.voicesOk = true;

      let selected = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
      if (!selected && selectedVoiceName) {
        selected = availableVoices.find((v) => v.name === selectedVoiceName);
      }
      if (!selected) {
        selected = resolveVoiceForLanguage(defaultListenLang) || availableVoices.find((v) => String(v.lang || '').toLowerCase().startsWith('en')) || availableVoices[0];
      }

      selectedVoiceURI = selected.voiceURI;
      selectedVoiceName = selected.name;
      voiceSelect.value = selected.voiceURI;
      localStorage.setItem('mim_voice_uri', selectedVoiceURI);
      localStorage.setItem('mim_voice_name', selectedVoiceName);
      updateIconGlow();
    }

    function startVoiceRecoveryLoop() {
      if (voiceRecoveryInterval) return;
      voiceRecoveryInterval = setInterval(() => {
        if (!window.speechSynthesis || healthState.voicesLoaded) {
          if (healthState.voicesLoaded && voiceRecoveryInterval) {
            clearInterval(voiceRecoveryInterval);
            voiceRecoveryInterval = null;
          }
          return;
        }
        voiceRecoveryAttempts += 1;
        buildVoiceOptions();
        if (healthState.voicesLoaded && voiceRecoveryInterval) {
          clearInterval(voiceRecoveryInterval);
          voiceRecoveryInterval = null;
          return;
        }
        if (voiceRecoveryAttempts >= 45) {
          clearInterval(voiceRecoveryInterval);
          voiceRecoveryInterval = null;
        }
      }, 1000);
    }

    async function refreshState() {
      try {
        const res = await fetch('/mim/ui/state');
        if (!res.ok) {
          markBackendReachability(false);
          updateIconGlow();
          return;
        }
        markBackendReachability(true);
        const data = await res.json();
        setSpeaking(Boolean(data.speaking));
        maybeSpeakFromState(data);
        maybeIssueStartupIdentityInquiry(data);

        const cameraLabel = data.camera_last_label || '(none)';
        const cameraConfidence = Number(data.camera_last_confidence || 0).toFixed(2);
        cameraEl.textContent = `Camera: ${cameraLabel} (confidence ${cameraConfidence})`;
        const inquiryPrompt = String(data.inquiry_prompt || '').trim();
        if (inquiryPrompt) {
          inquiryEl.textContent = inquiryPrompt;
          if (inquiryPrompt !== lastInquiryPromptSpoken) {
            speakLocally(inquiryPrompt, false);
            lastInquiryPromptSpoken = inquiryPrompt;
          }
        } else if (!startupInquiryIssued) {
          inquiryEl.textContent = '';
          lastInquiryPromptSpoken = '';
        }

        activeVisualIdentity = normalizeIdentityLabel(cameraLabel);
        if (activeVisualIdentity && activeVisualIdentity !== lastVisualIdentity) {
          maybeGreetRecognizedIdentity(activeVisualIdentity);
        }
        lastVisualIdentity = activeVisualIdentity;

        updateIconGlow();
      } catch (_) {
        markBackendReachability(false);
        updateIconGlow();
      }
    }

    async function speakNow() {
      const message = sayInput.value.trim();
      if (!message) return;
      const localSpoken = speakLocally(message, true);
      try {
        await fetch('/gateway/voice/output', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message,
            voice_profile: 'default',
            channel: 'ui',
            priority: 'normal',
            metadata_json: { source: 'mim_ui_sketch' },
          }),
        });
      } catch (_) {
      }
      if (!localSpoken) {
        const detail = lastLocalTtsError ? ` (${lastLocalTtsError})` : '';
        statusEl.textContent = `Speak requested, but local TTS is unavailable${detail}.`;
      }
      refreshState();
    }

    async function sendCameraEvent() {
      const label = (cameraInput.value || '').trim() || 'unknown';
      await fetch('/gateway/perception/camera/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: 'mim-ui-camera',
          source_type: 'camera',
          session_id: 'mim-ui-session',
          is_remote: false,
          observations: [{ object_label: label, confidence: 0.82, zone: 'front-center' }],
          metadata_json: { source: 'mim_ui_sketch' },
        }),
      });
      refreshState();
    }

    async function listenOnce() {
      const hardMicErrors = new Set([
        'not-allowed',
        'service-not-allowed',
        'audio-capture',
        'bad-grammar',
        'language-not-supported',
      ]);
      const softMicErrors = new Set([
        'aborted',
        'no-speech',
        'network',
      ]);

      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        healthState.micAvailable = false;
        healthState.micOk = false;
        micAutoMode = false;
        updateIconGlow();
        return;
      }

      const micReady = await ensureMicPermission({ keepStreamAlive: false });
      if (!micReady) {
        micAutoMode = false;
        listenBtn.textContent = 'Listening Off';
        return;
      }

      if (FORCE_FALLBACK_STT) {
        micListening = true;
        micStartInFlight = false;
        micRestartPending = false;
        micLastActiveAt = Date.now();
        healthState.micAvailable = true;
        healthState.micOk = true;
        micLastErrorCode = '';
        statusEl.textContent = 'Always listening...';
        noteMicEvent('fallback', 'forced-mode-active');
        startMicFallbackLoop();
        updateIconGlow();
        return;
      }

      if (micRecoveryMode) {
        if (Date.now() < micCooldownUntil) {
          updateIconGlow();
          return;
        }
        micRecoveryMode = false;
        micRecoveryReason = '';
      }

      healthState.micAvailable = true;

      resetRecognitionInstance();
      if (!recognition) {
        recognition = new SpeechRecognition();
        recognition.lang = defaultListenLang;
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.continuous = true;

        recognition.onstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onstart');
          stopMicPermissionStream();
          startMicFallbackLoop();
          clearMicStartTimeout();
          micStartInFlight = false;
          micRestartPending = false;
          micListening = true;
          micStartAttemptStreak = 0;
          micStartTimeoutStreak = 0;
          micStartFailureStreak = 0;
          micSessionStartedAt = Date.now();
          micLastActiveAt = Date.now();
          micLastSpeechEventAt = Date.now();
          healthState.micOk = true;
          micLastErrorCode = '';
          statusEl.textContent = 'Always listening...';
          updateIconGlow();
        };

        recognition.onaudiostart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onaudiostart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onaudioend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onaudioend');
        };

        recognition.onsoundstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onsoundstart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onsoundend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onsoundend');
        };

        recognition.onspeechstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onspeechstart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onspeechend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onspeechend');
        };

        recognition.onnomatch = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onnomatch');
        };

        recognition.onresult = async (event) => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onresult');
          micLastSpeechEventAt = Date.now();
          micLastResultAt = Date.now();
          const last = event.results?.[event.results.length - 1]?.[0];
          const transcript = (last?.transcript || '').trim();
          const confidence = Number(last?.confidence || 0.8);
          if (!transcript) return;

          micConsecutiveOnend = 0;
          micErrorStreak = 0;
          micHardErrorStreak = 0;
          micStartInFlight = false;
          micRestartPending = false;
          micLastActiveAt = Date.now();
          healthState.micOk = true;
          micLastErrorCode = '';

          const explicitOverrideLang = detectExplicitLanguageOverride(transcript);
          if (explicitOverrideLang) {
            currentConversationLang = normalizeLangCode(explicitOverrideLang);
            localStorage.setItem('mim_current_lang', currentConversationLang);

            if (activeVisualIdentity) {
              interactionMemory[activeVisualIdentity] = {
                lang: currentConversationLang,
                updated_at: new Date().toISOString(),
                source: 'verbal_override',
              };
              persistIdentityMemory();
            }
          } else if (autoLanguageMode) {
            currentConversationLang = detectLanguageFromTranscript(transcript);
            localStorage.setItem('mim_current_lang', currentConversationLang);

            if (activeVisualIdentity) {
              interactionMemory[activeVisualIdentity] = {
                lang: currentConversationLang,
                updated_at: new Date().toISOString(),
                source: 'detected_input',
              };
              persistIdentityMemory();
            }
          }

          if (isLikelyLowValueTranscript(transcript)) {
            noteMicEvent('recognition-short', transcript.slice(0, 24));
            addMicDebug('recognition:short-transcript-skipped', transcript);
            return;
          }

          statusEl.textContent = `Heard: ${transcript}`;
          const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
          if (!handledWeakIdentity) {
            await maybeHandleIdentityIntroduction(transcript);
          }

          const micSync = await submitMicTranscript(transcript, confidence, 'always_listening');
          if (!micSync.ok) {
            statusEl.textContent = `Heard: ${transcript} (backend sync delayed)`;
          } else if (!micSync.accepted) {
            statusEl.textContent = `Heard: ${transcript} (${micSync.status})`;
          }
          refreshState();
        };

        recognition.onerror = (event) => {
          noteMicLifecycleEvent();
          stopMicPermissionStream();
          stopMicFallbackLoop();
          clearMicStartTimeout();
          micListening = false;
          micStartInFlight = false;
          micSessionStartedAt = 0;
          micRecentErrorAt = Date.now();
          const errorCode = String(event?.error || 'unknown');
          const errorMessage = String(event?.message || '').trim();
          const detail = errorMessage && errorMessage !== errorCode ? `${errorCode}:${errorMessage}` : errorCode;
          noteMicEvent('recognition-error', detail);
          addMicDebug('recognition:onerror', `code=${errorCode} message=${errorMessage || '-'} autoMode=${micAutoMode} listening=${micListening}`);
          micLastErrorCode = errorCode;
          const isHardError = hardMicErrors.has(errorCode);

          if (isHardError) {
            micHardErrorStreak += 1;
            healthState.micOk = false;
            micErrorStreak += 1;
          } else if (softMicErrors.has(errorCode)) {
            micHardErrorStreak = 0;
            healthState.micOk = true;
            micLastErrorCode = '';
            micErrorStreak = 0;
          } else {
            micHardErrorStreak += 1;
            healthState.micOk = micHardErrorStreak < 3;
            micErrorStreak += 1;
          }
          updateIconGlow();
          if (isHardError) {
            if (noteMicCycleAndMaybeRecover(`hard-error:${errorCode}`)) {
              return;
            }
          }
          if (micAutoMode) {
            const backoffMs = softMicErrors.has(errorCode)
              ? 450
              : Math.min(20000, 2500 + micErrorStreak * 1200);
            scheduleMicRetry(backoffMs);
          } else {
            micRestartPending = false;
            statusEl.textContent = 'Listening failed. Try again.';
          }
        };

        recognition.onend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onend');
          stopMicPermissionStream();
          stopMicFallbackLoop();
          clearMicStartTimeout();
          const sessionDuration = micSessionStartedAt > 0 ? (Date.now() - micSessionStartedAt) : 0;
          micSessionStartedAt = 0;
          micListening = false;
          micStartInFlight = false;
          micConsecutiveOnend += 1;
          if (micHardErrorStreak === 0) {
            healthState.micOk = true;
          }

          if (sessionDuration > 0 && sessionDuration < MIC_SHORT_RUN_MS) {
            micShortRunStreak += 1;
          } else {
            micShortRunStreak = 0;
          }

          if (micShortRunStreak >= MIC_SHORT_RUN_LIMIT && micAutoMode) {
            micUnstableCycleCount += 1;
            micShortRunStreak = 0;
            enterMicRecovery('short-run-flap');
            return;
          }

          if (micConsecutiveOnend <= 2) {
            micLastActiveAt = Date.now();
          }
          updateIconGlow();
          if (micAutoMode) {
            captureFallbackTranscription();
            const backoffMs = micErrorStreak > 0 ? Math.min(15000, 1500 + micErrorStreak * 800) : 350;
            scheduleMicRetry(backoffMs);
          } else {
            micRestartPending = false;
            listenBtn.textContent = 'Listening Off';
            statusEl.textContent = 'Listening paused. Press Listen to start.';
          }
        };
      }

      if (micListening || micStartInFlight) return;
      try {
        micStartInFlight = true;
        micRestartPending = false;
        micStartAttemptStreak += 1;
        noteMicLifecycleEvent();
        clearMicStartTimeout();
        micStartTimeoutTimer = setTimeout(() => {
          if (!micStartInFlight && micListening) return;
          noteMicEvent('recognition-timeout', 'start');
          stopMicPermissionStream();
          micStartInFlight = false;
          micListening = false;
          micLastErrorCode = 'start-timeout';
          micErrorStreak += 1;
          micStartTimeoutStreak += 1;
          healthState.micOk = micErrorStreak < 3;
          statusEl.textContent = 'Mic startup timed out.';
          resetRecognitionInstance();
          updateIconGlow();
          if (micStartTimeoutStreak >= 2) {
            pauseMicAuto('Mic startup unstable. Auto-listen paused; press Listen to retry.');
            return;
          }
          if (micAutoMode) {
            scheduleMicRetry(Math.min(12000, 1800 + micErrorStreak * 900));
          } else {
            listenBtn.textContent = 'Listening Off';
            statusEl.textContent = 'Mic did not start. Press Listen to retry.';
          }
        }, 2600);
        recognition.start();
        micLastActiveAt = Date.now();
        healthState.micOk = true;
        micLastErrorCode = '';
        micRecoveryMode = false;
        micRecoveryReason = '';
        micErrorStreak = 0;
        micHardErrorStreak = 0;
        micShortRunStreak = 0;
        micUnstableCycleCount = 0;
        statusEl.textContent = 'Starting microphone...';
        updateIconGlow();
      } catch (error) {
        stopMicPermissionStream();
        clearMicStartTimeout();
        micStartInFlight = false;
        micRestartPending = true;
        micLastErrorCode = 'start-failed';
        micHardErrorStreak += 1;
        micStartFailureStreak += 1;
        healthState.micOk = micHardErrorStreak < 3;
        micErrorStreak += 1;
        if (micStartAttemptStreak >= 3) {
          resetRecognitionInstance();
          micStartAttemptStreak = 0;
        }
        statusEl.textContent = `Mic start failed (${String(error?.name || 'unknown')}).`;
        updateIconGlow();
        if (micStartFailureStreak >= 2) {
          pauseMicAuto('Mic failed to start repeatedly. Auto-listen paused; press Listen to retry.');
          return;
        }
        if (micAutoMode) {
          scheduleMicRetry(Math.min(12000, 1200 + micErrorStreak * 900));
        } else {
          listenBtn.textContent = 'Listening Off';
        }
      }
    }

    async function startCameraWatcher() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        cameraEl.textContent = 'Camera: browser camera API not available';
        healthState.cameraOk = false;
        updateIconGlow();
        return;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        const video = document.createElement('video');
        video.srcObject = stream;
        video.muted = true;
        video.playsInline = true;
        await video.play();

        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        const width = 96;
        const height = 72;
        canvas.width = width;
        canvas.height = height;

        let lastFrame = null;
        let lastSentAt = 0;

        const postCameraActivity = async (activityScore) => {
          await fetch('/gateway/perception/camera/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              device_id: 'mim-ui-camera',
              source_type: 'camera',
              session_id: 'mim-ui-session',
              is_remote: false,
              observations: [{ object_label: 'activity', confidence: Math.max(0.5, Math.min(0.98, activityScore)), zone: 'front-center' }],
              metadata_json: { source: 'mim_ui_sketch', mode: 'always_watching' },
            }),
          });
          refreshState();
        };

        if (motionInterval) clearInterval(motionInterval);
        motionInterval = setInterval(async () => {
          if (!ctx || video.readyState < 2) return;
          ctx.drawImage(video, 0, 0, width, height);
          const frame = ctx.getImageData(0, 0, width, height).data;

          if (!lastFrame) {
            lastFrame = new Uint8ClampedArray(frame);
            return;
          }

          let delta = 0;
          const stride = 16;
          for (let i = 0; i < frame.length; i += stride) {
            delta += Math.abs(frame[i] - lastFrame[i]);
          }
          const samples = Math.floor(frame.length / stride);
          const avgDelta = samples > 0 ? delta / samples : 0;
          const normalized = Math.max(0, Math.min(1, avgDelta / 40));

          lastFrame.set(frame);
          const now = Date.now();
          if (normalized >= 0.18 && now - lastSentAt >= 1200) {
            lastSentAt = now;
            cameraEl.textContent = `Camera: activity detected (${normalized.toFixed(2)})`;
            await postCameraActivity(normalized);
          }
        }, 900);

        cameraEl.textContent = 'Camera: always watching for activity';
        healthState.cameraOk = true;
        updateIconGlow();
      } catch (_) {
        cameraEl.textContent = 'Camera permission denied or unavailable';
        healthState.cameraOk = false;
        updateIconGlow();
      }
    }

    document.getElementById('speakBtn').addEventListener('click', speakNow);
    document.getElementById('cameraBtn').addEventListener('click', sendCameraEvent);
    settingsBtn.addEventListener('click', () => {
      settingsPanel.classList.toggle('open');
    });
    voiceSelect.addEventListener('change', applyVoiceSettings);
    micSelect.addEventListener('change', async () => {
      selectedMicDeviceId = String(micSelect.value || '').trim();
      const selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      selectedMicLabel = selected?.label || selectedMicLabel;
      localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
      localStorage.setItem('mim_mic_device_label', selectedMicLabel || '');
      micPermissionState = 'unknown';
      updateMicDiagnostics();

      resetRecognitionInstance();
      await ensureMicPermission();
      if (micAutoMode) {
        listenOnce();
      }
    });
    defaultLangInput.addEventListener('change', applyVoiceSettings);
    autoLangToggle.addEventListener('change', applyVoiceSettings);
    naturalVoiceToggle.addEventListener('change', applyVoiceSettings);
    voiceRateInput.addEventListener('input', applyVoiceSettings);
    voicePitchInput.addEventListener('input', applyVoiceSettings);
    voiceDepthInput.addEventListener('input', applyVoiceSettings);
    voiceVolumeInput.addEventListener('input', applyVoiceSettings);
    listenBtn.addEventListener('click', () => {
      if (micAutoMode || micListening || micStartInFlight) {
        micAutoMode = false;
        listenBtn.textContent = 'Listening Off';
        micListening = false;
        micStartInFlight = false;
        micRestartPending = false;
        stopMicFallbackLoop();
        if (micRetryTimer) {
          clearTimeout(micRetryTimer);
          micRetryTimer = null;
        }
        clearMicStartTimeout();
        stopMicFallbackLoop();
        stopMicPermissionStream();
        if (recognition && micListening) {
          recognition.stop();
        }
        statusEl.textContent = 'Listening paused.';
        updateIconGlow();
        return;
      }

      micAutoMode = true;
      listenBtn.textContent = 'Listening On';
      micRestartPending = false;
      micStartTimeoutStreak = 0;
      micStartFailureStreak = 0;
      micShortRunStreak = 0;
      micUnstableCycleCount = 0;
      listenOnce();
    });

    updateIconGlow();
    addMicDebug('ui-boot', 'objective-22-debug-pane');
    if (micEventEl) {
      micEventEl.textContent = `Mic event: ui-boot @ ${new Date().toLocaleTimeString()}`;
    }
    defaultLangInput.value = normalizeLangCode(defaultListenLang || SYSTEM_DEFAULT_LANG);
    autoLangToggle.checked = autoLanguageMode;
    naturalVoiceToggle.checked = naturalVoicePreset;
    voiceRateInput.value = clamp(voiceRate, 0.7, 1.35).toFixed(2);
    voicePitchInput.value = clamp(voicePitch, 0.7, 1.35).toFixed(2);
    voiceDepthInput.value = String(Math.round(clamp(voiceDepth, 0, 100)));
    voiceVolumeInput.value = clamp(voiceVolume, 0.4, 1.0).toFixed(2);
    syncVoiceControlAvailability();
    syncVoiceControlLabels();
    enumerateMicDevices();
    buildVoiceOptions();
    startVoiceRecoveryLoop();
    if (window.speechSynthesis) {
      window.speechSynthesis.onvoiceschanged = () => {
        buildVoiceOptions();
        applyVoiceSettings();
      };
    }
    applyVoiceSettings();
    refreshState();
    listenBtn.textContent = 'Listening Off';
    statusEl.textContent = 'Listening paused. Press Listen to start.';
    ensureMicPermission().then(() => enumerateMicDevices());
    startCameraWatcher();
    setInterval(refreshState, 2000);
  </script>
</body>
</html>
"""


@router.get("/mim/ui/state")
async def mim_ui_state(db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)

    speech_row = (
        (
            await db.execute(
                select(SpeechOutputAction).order_by(SpeechOutputAction.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )

    speaking = False
    if speech_row and speech_row.created_at:
        age_seconds = (now - speech_row.created_at.astimezone(timezone.utc)).total_seconds()
        speaking = age_seconds <= 8

    camera_row = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == "camera")
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    camera_payload = camera_row.last_event_payload_json if camera_row and isinstance(camera_row.last_event_payload_json, dict) else {}
    label_raw = str(camera_payload.get("object_label", "")).strip()
    label = label_raw.lower()
    confidence = float(camera_payload.get("confidence", 0.0) or 0.0)

    unknown_person = False
    if label:
        if label in {"person", "human", "unknown", "visitor"}:
            unknown_person = True
        elif "person" in label and label not in _known_people():
            unknown_person = True

    inquiry_prompt = ""
    if unknown_person:
        inquiry_prompt = "I can see someone. Hi there — who are you? What's your name?"

    return {
        "speaking": speaking,
        "camera_last_label": label_raw,
        "camera_last_confidence": confidence,
        "inquiry_prompt": inquiry_prompt,
      "latest_output_action_id": int(speech_row.id) if speech_row else 0,
      "latest_output_text": str(speech_row.requested_text or "") if speech_row else "",
      "latest_output_allowed": bool(str(speech_row.delivery_status or "") == "queued") if speech_row else False,
    }


@router.get("/mim/ui/health")
async def mim_ui_health(db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)

    camera_stale_seconds = 30.0
    mic_stale_seconds = 30.0
    speech_stale_seconds = 90.0

    db_ok = True

    try:
      speech_row = (
        (
          await db.execute(
            select(SpeechOutputAction).order_by(SpeechOutputAction.id.desc()).limit(1)
          )
        )
        .scalars()
        .first()
      )

      camera_row = (
        (
          await db.execute(
            select(WorkspacePerceptionSource)
            .where(WorkspacePerceptionSource.source_type == "camera")
            .order_by(WorkspacePerceptionSource.id.desc())
            .limit(1)
          )
        )
        .scalars()
        .first()
      )

      mic_row = (
        (
          await db.execute(
            select(WorkspacePerceptionSource)
            .where(WorkspacePerceptionSource.source_type == "microphone")
            .order_by(WorkspacePerceptionSource.id.desc())
            .limit(1)
          )
        )
        .scalars()
        .first()
      )
    except Exception:
      db_ok = False
      speech_row = None
      camera_row = None
      mic_row = None

    speech_age = _age_seconds(now, speech_row.created_at if speech_row else None)
    camera_age = _age_seconds(now, camera_row.last_seen_at if camera_row else None)
    mic_age = _age_seconds(now, mic_row.last_seen_at if mic_row else None)

    speech_ok = (speech_age is None) or (speech_age <= speech_stale_seconds)
    camera_ok = (camera_age is not None) and (camera_age <= camera_stale_seconds)
    mic_ok = (mic_age is not None) and (mic_age <= mic_stale_seconds)

    overall_ok = bool(db_ok and camera_ok and mic_ok and speech_ok)
    overall_status = "healthy" if overall_ok else "degraded"

    return {
      "generated_at": now.isoformat().replace("+00:00", "Z"),
      "status": overall_status,
      "ok": overall_ok,
      "checks": {
        "backend": {"ok": True, "status": "healthy"},
        "database": {
          "ok": db_ok,
          "status": "healthy" if db_ok else "error",
        },
        "camera": {
          "ok": camera_ok,
          "status": "healthy" if camera_ok else "stale",
          "age_seconds": camera_age,
          "stale_threshold_seconds": camera_stale_seconds,
          "source_health": str(camera_row.health_status or "") if camera_row else "",
          "source_status": str(camera_row.status or "") if camera_row else "",
        },
        "microphone": {
          "ok": mic_ok,
          "status": "healthy" if mic_ok else "stale",
          "age_seconds": mic_age,
          "stale_threshold_seconds": mic_stale_seconds,
          "source_health": str(mic_row.health_status or "") if mic_row else "",
          "source_status": str(mic_row.status or "") if mic_row else "",
        },
        "speech_output": {
          "ok": speech_ok,
          "status": "healthy" if speech_ok else "stale",
          "age_seconds": speech_age,
          "stale_threshold_seconds": speech_stale_seconds,
          "delivery_status": str(speech_row.delivery_status or "") if speech_row else "",
        },
      },
      "latest": {
        "camera": {
          "source_id": int(camera_row.id) if camera_row else None,
          "device_id": str(camera_row.device_id or "") if camera_row else "",
          "last_seen_at": camera_row.last_seen_at.isoformat().replace("+00:00", "Z") if camera_row and camera_row.last_seen_at else None,
        },
        "microphone": {
          "source_id": int(mic_row.id) if mic_row else None,
          "device_id": str(mic_row.device_id or "") if mic_row else "",
          "last_seen_at": mic_row.last_seen_at.isoformat().replace("+00:00", "Z") if mic_row and mic_row.last_seen_at else None,
        },
        "speech_output": {
          "action_id": int(speech_row.id) if speech_row else None,
          "created_at": speech_row.created_at.isoformat().replace("+00:00", "Z") if speech_row and speech_row.created_at else None,
        },
      },
    }
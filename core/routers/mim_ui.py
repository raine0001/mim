from datetime import datetime, timezone
from hashlib import sha256
import re

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import InputEvent, MemoryEntry, SpeechOutputAction, WorkspaceInquiryQuestion, WorkspacePerceptionSource, WorkspaceStrategyGoal

router = APIRouter(tags=["mim-ui"])

MIC_PROMPT_MIN_CONFIDENCE = 0.66
MIC_PROMPT_MAX_AGE_SECONDS = 25.0


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


def _parse_payload_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_sentence(raw: str, *, max_len: int = 180) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _tokenize(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    return {token for token in cleaned.split() if token}


def _looks_like_direct_question(text: str) -> bool:
    prompt = str(text or "").strip().lower()
    if not prompt:
      return False
    if "?" in prompt:
      return True
    question_starts = (
      "what ", "why ", "how ", "when ", "where ", "who ", "which ",
      "does ", "do ", "can ", "could ", "is ", "are ", "will ", "would ",
    )
    return prompt.startswith(question_starts)


def _is_clarifier_prompt_text(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
      return False
    return (
      "missing one detail" in normalized
      or "options: 1)" in normalized
      or "i am still missing" in normalized
    )


def _plain_answer_from_context(
    *,
    latest_mic_transcript: str,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
    ) -> str:
    question = str(latest_mic_transcript or "").strip()
    ql = question.lower()
    question_stub = _compact_sentence(question, max_len=72)
    if "task 75" in ql and ("what" in ql or "does" in ql):
      return "Task 75 checks whether MIM and TOD stay synchronized without drift."

    if goal_summary:
      return _compact_sentence(f"{goal_summary.rstrip('.')}.", max_len=180)
    if memory_summary:
      return _compact_sentence(f"{memory_summary.rstrip('.')}.", max_len=180)
    if environment_now:
      if environment_now.startswith("camera has no clear"):
        return f"For '{question_stub}', I do not have enough current state to answer directly yet."
      return _compact_sentence(f"For '{question_stub}', current state is {environment_now.rstrip('.')}.", max_len=180)
    return f"For '{question_stub}', I do not have enough current state to answer directly yet."


def _apply_anti_drift_rewrite(
    *,
    text: str,
    latest_mic_transcript: str,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
    ) -> str:
    candidate = str(text or "").strip()
    if not candidate:
      return ""

    lowered = candidate.lower()
    drift_openers = (
      "what you're really asking",
      "what you are really asking",
      "at a high level",
      "in broad terms",
      "more generally",
    )
    if lowered.startswith(drift_openers):
      first_sentence = candidate.split(".", 1)[0].strip()
      candidate = first_sentence if first_sentence else candidate

    if _looks_like_direct_question(latest_mic_transcript):
      user_tokens = _tokenize(latest_mic_transcript)
      reply_tokens = _tokenize(candidate)
      overlap = len(user_tokens.intersection(reply_tokens))
      if overlap < 2:
        direct = _plain_answer_from_context(
          latest_mic_transcript=latest_mic_transcript,
          environment_now=environment_now,
          goal_summary=goal_summary,
          memory_summary=memory_summary,
        )
        return direct
    return _compact_sentence(candidate, max_len=220)


def _is_low_quality_learning_entry(entry: MemoryEntry) -> bool:
    meta = entry.metadata_json if isinstance(entry.metadata_json, dict) else {}
    signal = str(meta.get("preference_signal", "")).strip().lower()
    value = str(meta.get("preference_value", "")).strip().lower()
    try:
        confidence = float(meta.get("confidence", 0.0) or 0.0)
    except Exception:
        confidence = 0.0

    if confidence and confidence < 0.68:
        return True

    if signal == "call_me":
        low_value_tokens = {
            "what",
            "that",
            "there",
            "here",
            "hello",
            "hi",
            "hey",
            "him",
            "you",
            "see",
        }
        if not value or len(value) < 3 or value in low_value_tokens:
            return True

    return False


def _looks_like_identity_prompt(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return False
    return (
        "what should i call you" in text
        or "what's your name" in text
        or "tell me your name" in text
    )


def _rewrite_state_output_text(
    raw_text: str,
    *,
    needs_identity_prompt: bool,
    open_question_summary: str,
    goal_summary: str,
    latest_mic_transcript: str,
    environment_now: str,
    memory_summary: str,
) -> str:
  text = str(raw_text or "").strip()
  if not text:
    return ""

  normalized = " ".join(text.lower().split())
  if normalized in {"hello, i am mim.", "hello i am mim.", "hello i am mim"}:
    return ""

  if needs_identity_prompt:
    return text

  if _looks_like_identity_prompt(text):
    if open_question_summary:
      return f"Before I proceed, I need one decision: {open_question_summary}"
    if goal_summary:
      return f"I am tracking this goal: {goal_summary}. Tell me what you want me to do next."
    return ""

  return _apply_anti_drift_rewrite(
      text=text,
      latest_mic_transcript=latest_mic_transcript,
      environment_now=environment_now,
      goal_summary=goal_summary,
      memory_summary=memory_summary,
  )


def _choose_phrase(options: list[str], key: str) -> str:
    phrases = [item.strip() for item in options if str(item or "").strip()]
    if not phrases:
        return ""
    digest = sha256(str(key or "seed").encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(phrases)
    return phrases[idx]


def _build_curiosity_prompt(
    *,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
    latest_mic_transcript: str,
    learning_summary: str,
    clarification_budget_exhausted: bool = False,
) -> str:
    env = _compact_sentence(environment_now, max_len=96)
    goal = _compact_sentence(goal_summary, max_len=110)
    memory = _compact_sentence(memory_summary, max_len=110)
    mic = _compact_sentence(latest_mic_transcript, max_len=90)
    learning = _compact_sentence(learning_summary, max_len=110)

    if mic:
      if _looks_like_direct_question(mic):
        return _plain_answer_from_context(
          latest_mic_transcript=mic,
          environment_now=env,
          goal_summary=goal,
          memory_summary=memory,
        )
      if clarification_budget_exhausted:
        return f"For '{mic}', I still need one detail. Options: 1) answer, 2) plan, 3) action."
      return (
        f"For '{mic}', I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"
      )

    if learning:
      return _choose_phrase(
        [
          f"Current preference signal: {learning}.",
          f"Stored interaction pattern: {learning}.",
          f"Recent preference memory: {learning}.",
        ],
        key=f"learn:{learning}|env:{env}",
      )

    if goal and env:
      return _choose_phrase(
        [
          f"Current scene: {env}. Active goal: {goal}.",
          f"I can see {env}. I am tracking {goal}.",
          f"Context: {env}. Goal in play: {goal}.",
        ],
        key=f"goal-env:{goal}|{env}",
      )

    if goal:
      return _choose_phrase(
        [
          f"I am tracking this goal: {goal}.",
          f"Goal status: {goal}.",
        ],
        key=f"goal:{goal}",
      )

    if memory:
      return _choose_phrase(
        [
          f"From memory: {memory}.",
          f"I remember: {memory}.",
        ],
        key=f"memory:{memory}",
      )

    return _choose_phrase(
      [
        "I am ready. Choose one: answer a question, suggest a plan, or take an action.",
        "I am ready. Options: answer, plan, or action.",
        "I am available. Pick one path: answer, plan, or action.",
      ],
      key="fallback-curiosity",
    )

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
    .settings-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 10px;
    }
    .settings-tab {
      background: #0a2c3f;
      color: var(--muted);
      border: 1px solid #1b6a8d;
      border-radius: 8px;
      padding: 7px 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }
    .settings-tab.active {
      color: #e8f7ff;
      background: #12506f;
      border-color: #2aa6d4;
    }
    .settings-view { display: none; }
    .settings-view.active { display: block; }
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
    .camera-preview {
      width: 100%;
      height: 150px;
      border-radius: 8px;
      border: 1px solid #1a4f68;
      background: #081a25;
      object-fit: cover;
    }
    .camera-preview.inactive {
      opacity: 0.55;
      filter: grayscale(0.15);
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
    <button id="settingsBtn" class="icon-btn" title="MIM settings" aria-label="MIM settings">⚙</button>
  </div>

  <div id="settingsPanel" class="settings-panel" role="dialog" aria-label="MIM settings">
    <div class="settings-title">MIM Settings</div>
    <div class="settings-tabs">
      <button id="settingsTabVoice" class="settings-tab active" type="button">Voice</button>
      <button id="settingsTabCamera" class="settings-tab" type="button">Camera</button>
    </div>

    <div id="settingsViewVoice" class="settings-view active">
      <div class="settings-row">
        <label for="voiceSelect">Fixed Voice</label>
        <select id="voiceSelect"></select>
        <div class="settings-note">This stays fixed until you change it.</div>
      </div>

      <div class="settings-row toggle-row">
        <input id="serverTtsToggle" type="checkbox" checked />
        <label for="serverTtsToggle">Use Neural Server TTS (recommended)</label>
      </div>

      <div class="settings-row">
        <label for="serverTtsVoiceSelect">Neural Server Voice</label>
        <select id="serverTtsVoiceSelect"></select>
        <div class="settings-note">Higher quality voice rendered by backend TTS.</div>
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

    <div id="settingsViewCamera" class="settings-view">
      <div class="settings-row">
        <label for="cameraSelect">Camera Device</label>
        <select id="cameraSelect"></select>
      </div>
      <div class="settings-row">
        <video id="cameraPreview" class="camera-preview inactive" autoplay muted playsinline></video>
        <div id="cameraSettingsStatus" class="settings-note">Camera preview is idle.</div>
      </div>
      <div class="settings-row">
        <button id="cameraRefreshBtn" type="button">Refresh Camera List</button>
      </div>
      <div class="settings-row">
        <button id="cameraToggleBtn" type="button">Start Camera Preview</button>
      </div>
      <div class="settings-note">Use this panel to verify framing and permissions for MIM camera sensing.</div>
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
    const serverTtsToggle = document.getElementById('serverTtsToggle');
    const serverTtsVoiceSelect = document.getElementById('serverTtsVoiceSelect');
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
    const settingsTabVoice = document.getElementById('settingsTabVoice');
    const settingsTabCamera = document.getElementById('settingsTabCamera');
    const settingsViewVoice = document.getElementById('settingsViewVoice');
    const settingsViewCamera = document.getElementById('settingsViewCamera');
    const cameraSelect = document.getElementById('cameraSelect');
    const cameraPreview = document.getElementById('cameraPreview');
    const cameraSettingsStatus = document.getElementById('cameraSettingsStatus');
    const cameraRefreshBtn = document.getElementById('cameraRefreshBtn');
    const cameraToggleBtn = document.getElementById('cameraToggleBtn');

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
    let availableCameras = [];
    let selectedCameraDeviceId = localStorage.getItem('mim_camera_device_id') || '';
    let cameraStream = null;
    let cameraWatcherVideo = null;
    let cameraWatcherCanvas = null;
    let cameraWatcherCtx = null;
    let cameraLastFrame = null;
    let cameraLastSentAt = 0;
    let lastSpokenOutputId = Number(localStorage.getItem('mim_last_spoken_output_id') || 0);
    let availableVoices = [];
    let availableMics = [];
    let selectedVoiceURI = localStorage.getItem('mim_voice_uri') || '';
    let selectedVoiceName = localStorage.getItem('mim_voice_name') || '';
    let selectedMicDeviceId = localStorage.getItem('mim_mic_device_id') || '';
    let selectedMicLabel = localStorage.getItem('mim_mic_device_label') || '';
    let voiceRate = Number(localStorage.getItem('mim_voice_rate') || 0.90);
    let voicePitch = Number(localStorage.getItem('mim_voice_pitch') || 0.90);
    let voiceDepth = Number(localStorage.getItem('mim_voice_depth') || 22);
    let voiceVolume = Number(localStorage.getItem('mim_voice_volume') || 0.95);
    const VOICE_PROFILE_MIGRATION_VERSION = 'voice-natural-v2';
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
    const MIC_POCKETSPHINX_CONFIDENCE_MIN = 0.55;
    const MIC_FALLBACK_CAPTURE_MS = 3600;
    const MIC_FALLBACK_INTERVAL_MS = 5200;
    const MIC_LOCAL_PROVIDER_BACKOFF_MS = 300000;
    const MIC_POST_TTS_SUPPRESS_MS = 1100;
    const MIC_ECHO_MATCH_WINDOW_MS = 20000;
    const MIC_ECHO_MIN_SIGNATURE_LEN = 6;
    const STATE_POLL_SPEAK_ENABLED = false;
    const WAKE_WORD_REQUIRED_FOR_LIVE_REPLY = true;
    const LOW_VALUE_CLARIFY_COOLDOWN_MS = 15000;
    const LOW_VALUE_SPEAK_COOLDOWN_MS = 180000;
    const SPOKEN_PHRASE_DEDUPE_MS = 2500;
    const GREETING_CLARIFY_COOLDOWN_MS = 12000;
    const SPOKEN_DUPLICATE_COOLDOWN_MS = 45000;
    const BACKEND_INQUIRY_SPEAK_COOLDOWN_MS = 25000;
    const FORCE_FALLBACK_STT = false;
    const PIN_TO_SYSTEM_DEFAULT_MIC = true;
    const WEAK_IDENTITY_WORDS = new Set(['there', 'here', 'their', 'theyre', 'unknown', 'person', 'human', 'visitor']);
    let startupInquiryIssued = false;
    let latestUiState = null;
    let lastInquiryPromptSpoken = '';
    let weakIdentityClarifyCooldownUntil = 0;
    let weakIdentityLastPromptKey = '';
    let lowValueClarifyCooldownUntil = 0;
    let lowValueSpeakCooldownUntil = 0;
    let lowValueClarifyLastCompact = '';
    let greetingClarifyCooldownUntil = 0;
    let startupFeedbackCooldownUntil = 0;
    let startupFeedbackLastCompact = '';
    let suppressBackendInquiryUntil = 0;
    let backendInquirySpeakCooldownUntil = 0;
    let lastBackendInquirySignature = '';
    let locallyAcceptedIdentity = '';
    let lastLocalTtsError = '';
    let micPermissionState = 'unknown';
    let micPermissionStream = null;
    let micKeepAliveAudioContext = null;
    let micKeepAliveSourceNode = null;
    let micKeepAliveGainNode = null;
    let micKeepAliveProcessorNode = null;
    let micKeepAliveRecorder = null;
    let micProviderLocalBackoffUntil = 0;
    const SYSTEM_DEFAULT_LANG = 'en-US';
    let defaultListenLang = localStorage.getItem('mim_default_listen_lang') || SYSTEM_DEFAULT_LANG;
    let autoLanguageMode = localStorage.getItem('mim_auto_lang_mode') !== '0';
    let naturalVoicePreset = localStorage.getItem('mim_voice_natural_preset') !== '0';
    let currentConversationLang = localStorage.getItem('mim_current_lang') || defaultListenLang;
    let activeVisualIdentity = '';
    let lastVisualIdentity = '';
    let interactionMemory = {};
    let greetingCooldownByIdentity = {};
    let lastSpokenSignature = localStorage.getItem('mim_last_spoken_signature') || '';
    let lastSpokenSignatureAt = Number(localStorage.getItem('mim_last_spoken_signature_at') || 0);
    let serverTtsEnabled = localStorage.getItem('mim_server_tts_enabled') !== '0';
    let selectedServerTtsVoice = localStorage.getItem('mim_server_tts_voice') || 'en-US-EmmaMultilingualNeural';
    let activeServerTtsAudio = null;
    let activeServerTtsUrl = '';
    let speechRequestSeq = 0;
    let speechInFlight = false;
    let speechPlaybackActive = false;
    let activeSpeechOwner = '';
    let micSuppressedUntil = 0;
    let recentSpokenUtterances = [];
    let localTtsPlaybackToken = 0;
    let lastSpokenPhraseCompact = '';
    let lastSpokenPhraseAt = 0;
    let refreshInFlight = false;
    let refreshPending = false;

    const SERVER_TTS_VOICES = [
      { value: 'en-US-EmmaMultilingualNeural', label: 'Emma (en-US, multilingual)' },
      { value: 'en-US-AvaMultilingualNeural', label: 'Ava (en-US, multilingual)' },
      { value: 'en-US-AriaNeural', label: 'Aria (en-US)' },
      { value: 'en-US-JennyNeural', label: 'Jenny (en-US)' },
      { value: 'en-GB-SoniaNeural', label: 'Sonia (en-GB)' },
      { value: 'es-ES-ElviraNeural', label: 'Elvira (es-ES)' },
      { value: 'fr-FR-DeniseNeural', label: 'Denise (fr-FR)' },
      { value: 'de-DE-SeraphinaMultilingualNeural', label: 'Seraphina (de-DE, multilingual)' },
      { value: 'it-IT-ElsaNeural', label: 'Elsa (it-IT)' },
      { value: 'pt-BR-FranciscaNeural', label: 'Francisca (pt-BR)' },
    ];

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

    const appliedVoiceMigration = localStorage.getItem('mim_voice_profile_migration') || '';
    if (appliedVoiceMigration !== VOICE_PROFILE_MIGRATION_VERSION) {
      voiceRate = 0.90;
      voicePitch = 0.90;
      voiceDepth = 22;
      voiceVolume = 0.95;
      naturalVoicePreset = true;
      localStorage.setItem('mim_voice_rate', String(voiceRate));
      localStorage.setItem('mim_voice_pitch', String(voicePitch));
      localStorage.setItem('mim_voice_depth', String(voiceDepth));
      localStorage.setItem('mim_voice_volume', String(voiceVolume));
      localStorage.setItem('mim_voice_natural_preset', '1');
      localStorage.setItem('mim_voice_profile_migration', VOICE_PROFILE_MIGRATION_VERSION);
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

    function chooseDialogVariant(options, key = '') {
      const variants = Array.isArray(options) ? options.filter(Boolean) : [];
      if (!variants.length) return '';
      const seedText = `${String(key || '')}|${Date.now()}`;
      let hash = 0;
      for (let i = 0; i < seedText.length; i += 1) {
        hash = ((hash << 5) - hash + seedText.charCodeAt(i)) | 0;
      }
      const index = Math.abs(hash) % variants.length;
      return String(variants[index]);
    }

    function normalizeDialogSnippet(raw, maxLen = 120) {
      const text = String(raw || '').replace(/\s+/g, ' ').trim();
      if (!text) return '';
      if (text.length <= maxLen) return text;
      return `${text.slice(0, maxLen - 3).trim()}...`;
    }

    function getConversationContext() {
      const context = latestUiState?.conversation_context;
      return context && typeof context === 'object' ? context : {};
    }

    function shouldAskForNameNow() {
      return Boolean(getConversationContext().needs_identity_prompt);
    }

    function buildContextLead() {
      const context = getConversationContext();
      const snippets = [];
      const environmentNow = normalizeDialogSnippet(context.environment_now, 90);
      const activeGoal = normalizeDialogSnippet(context.active_goal, 95);
      const openQuestion = normalizeDialogSnippet(context.open_question, 95);
      const memoryHint = normalizeDialogSnippet(context.memory_hint, 95);

      if (environmentNow) snippets.push(`Right now ${environmentNow}.`);
      if (activeGoal) snippets.push(`Current goal: ${activeGoal}.`);
      if (openQuestion) {
        snippets.push(`Open decision: ${openQuestion}.`);
      } else if (memoryHint) {
        snippets.push(`From memory: ${memoryHint}.`);
      }

      return snippets.join(' ').trim();
    }

    function isIdentityInquiryText(textRaw) {
      const text = String(textRaw || '').toLowerCase();
      if (!text.trim()) return false;
      return text.includes('what should i call you')
        || text.includes("what's your name")
        || text.includes('tell me your name');
    }

    function normalizeSpeechSignature(textRaw) {
      return String(textRaw || '')
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    }

    function shortSpeechSignature(textRaw) {
      const signature = normalizeSpeechSignature(textRaw);
      if (!signature) return '-';
      let hash = 2166136261;
      for (let i = 0; i < signature.length; i += 1) {
        hash ^= signature.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
      }
      return `h${(hash >>> 0).toString(16).padStart(8, '0')}:${signature.slice(0, 24)}`;
    }

    function suppressionWindowMs() {
      return Math.max(0, micSuppressedUntil - Date.now());
    }

    function addSpeechDebug(stage, detail = '') {
      const detailText = String(detail || '').trim();
      addMicDebug(`speech:${stage}`, detailText);
      try {
        console.debug(`[mim:speech] ${stage}${detailText ? ` ${detailText}` : ''}`);
      } catch (_) {
      }
    }

    function logTranscriptDrop(reason, transcript, mode = 'unknown', detail = '') {
      const preview = String(transcript || '').slice(0, 48);
      const signature = shortSpeechSignature(transcript);
      const suffix = detail ? ` ${detail}` : '';
      addMicDebug(
        'transcript-drop',
        `reason=${reason} mode=${mode} sig=${signature} token=${localTtsPlaybackToken} suppressMs=${suppressionWindowMs()} text=${preview}${suffix}`,
      );
    }

    function setMicSuppression(durationMs, reason = '') {
      const until = Date.now() + Math.max(0, Number(durationMs) || 0);
      if (until > micSuppressedUntil) {
        micSuppressedUntil = until;
      }
      if (reason) {
        addMicDebug('mic-suppress', `${reason} until=${micSuppressedUntil}`);
      }
    }

    function isMicSuppressedNow() {
      return speechInFlight || speechPlaybackActive || Date.now() < micSuppressedUntil;
    }

    function rememberSpokenUtterance(text, sourceTag = 'unknown') {
      const signature = normalizeSpeechSignature(text);
      if (!signature || signature.length < MIC_ECHO_MIN_SIGNATURE_LEN) return;
      recentSpokenUtterances.push({ signature, sourceTag, at: Date.now() });
      if (recentSpokenUtterances.length > 14) {
        recentSpokenUtterances = recentSpokenUtterances.slice(-14);
      }
    }

    function isLikelyEchoTranscript(transcript) {
      const signature = normalizeSpeechSignature(transcript);
      if (!signature || signature.length < MIC_ECHO_MIN_SIGNATURE_LEN) return false;
      const now = Date.now();
      recentSpokenUtterances = recentSpokenUtterances.filter((item) => (now - Number(item.at || 0)) <= MIC_ECHO_MATCH_WINDOW_MS);
      return recentSpokenUtterances.some((item) => {
        if (!item || !item.signature) return false;
        if (item.signature === signature) return true;
        return item.signature.includes(signature) || signature.includes(item.signature);
      });
    }

    function hasWakePhrase(transcript) {
      const text = ` ${String(transcript || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
      if (!text.trim()) return false;
      return text.includes(' mim ') || text.includes(' hey mim ') || text.includes(' okay mim ') || text.includes(' ok mim ');
    }

    function shouldSpeakBackendInquiryPrompt(inquiryPrompt, conversationContext = {}) {
      const prompt = String(inquiryPrompt || '').trim();
      if (!prompt) return false;

      const now = Date.now();
      const signature = normalizeSpeechSignature(prompt);
      const needsIdentityPrompt = Boolean(conversationContext?.needs_identity_prompt);
      const hasOpenQuestion = Boolean(String(conversationContext?.open_question || '').trim());

      if (needsIdentityPrompt || hasOpenQuestion) {
        backendInquirySpeakCooldownUntil = now + 6000;
        lastBackendInquirySignature = signature;
        return true;
      }

      if (signature && signature === lastBackendInquirySignature && now < backendInquirySpeakCooldownUntil) {
        return false;
      }

      if (now < backendInquirySpeakCooldownUntil) {
        return false;
      }

      backendInquirySpeakCooldownUntil = now + BACKEND_INQUIRY_SPEAK_COOLDOWN_MS;
      lastBackendInquirySignature = signature;
      return true;
    }

    function rewriteQueuedOutputText(textRaw, data = {}) {
      let text = String(textRaw || '').replace(/\s+/g, ' ').trim();
      if (!text) return '';

      const context = (data && typeof data.conversation_context === 'object')
        ? data.conversation_context
        : getConversationContext();
      const needsIdentityPrompt = Boolean(context?.needs_identity_prompt);
      const openQuestion = normalizeDialogSnippet(context?.open_question || '', 140);
      const activeGoal = normalizeDialogSnippet(context?.active_goal || '', 140);

      // Drop stale identity asks when context says identity is no longer required.
      if (!needsIdentityPrompt && isIdentityInquiryText(text)) {
        if (openQuestion) {
          return `Before I proceed, I need one decision: ${openQuestion}`;
        }
        if (activeGoal) {
          return `I am tracking this goal: ${activeGoal}. Tell me what you want me to do next.`;
        }
        return '';
      }

      text = text
        .replace(/^i\s+can\s+see\s+someone\.\s*/i, '')
        .replace(/^hi\s+there[,\s]*/i, '');

      const signature = normalizeSpeechSignature(text);
      const cannedAckOnly = new Set([
        'ok', 'okay', 'got it', 'understood', 'noted', 'thanks', 'thank you',
        'all right', 'alright', 'copy that', 'hello i am mim', 'hello i am mim.',
      ]);
      if (cannedAckOnly.has(signature)) {
        return '';
      }

      return text;
    }

    function buildDialogPrompt(kind, context = {}) {
      const name = String(context?.name || '').trim();
      const transcript = String(context?.transcript || '').trim();
      const contextLead = buildContextLead();
      const askForName = shouldAskForNameNow();
      if (kind === 'startup_identity') {
        if (askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}Hi there. What should I call you?`,
            `${contextLead ? `${contextLead} ` : ''}I can continue right away, and I only need the name you prefer.`,
            `${contextLead ? `${contextLead} ` : ''}Before we continue, what name do you want me to use?`,
          ], transcript || contextLead || 'startup-name');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}I am listening. What do you want to work on right now?`,
          `${contextLead ? `${contextLead} ` : ''}I am here with full context. Tell me the next thing you want to do.`,
          `${contextLead ? `${contextLead} ` : ''}We can continue from where we are. What should I do now?`,
        ], transcript || contextLead || 'startup-open');
      }
      if (kind === 'low_value') {
        if (askForName) {
          return chooseDialogVariant([
            'I only caught part of that. Please say just your name once.',
            'I heard fragments. Please tell me the name you want me to use.',
            'I missed part of that. Could you repeat your name clearly?',
          ], transcript || contextLead || 'low-value-name');
        }
        return chooseDialogVariant([
          'I only caught part of that. Say your request again in one sentence.',
          'I heard fragments. Please repeat what you want me to do now.',
          'I am missing part of your intent. Tell me the next action clearly.',
        ], transcript || contextLead || 'low-value-action');
      }
      if (kind === 'greeting_only') {
        if (askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}Hi. What should I call you?`,
            `${contextLead ? `${contextLead} ` : ''}Hello. Share the name you want me to use and we can continue.`,
            `${contextLead ? `${contextLead} ` : ''}Hey. I am ready. What name should I address you by?`,
          ], transcript || contextLead || 'greeting-name');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}Hi. What do you want to do next?`,
          `${contextLead ? `${contextLead} ` : ''}Hello. Tell me your next request and I will act on it.`,
          `${contextLead ? `${contextLead} ` : ''}Hey. I am ready for the next step.`,
        ], transcript || contextLead || 'greeting-action');
      }
      if (kind === 'uncertain_name') {
        if (!askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}I heard you, but I am not certain about the request. Say the next action in one clear sentence.`,
            `${contextLead ? `${contextLead} ` : ''}I may have misheard. Please restate exactly what you want me to do now.`,
            `${contextLead ? `${contextLead} ` : ''}I am uncertain about your intent. Give me one concise instruction.`,
          ], transcript || contextLead || 'uncertain-action');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}I heard you, but I am not fully sure about the name. Please say only your name once.`,
          `${contextLead ? `${contextLead} ` : ''}I may have misheard the name. Please say just your name clearly.`,
          `${contextLead ? `${contextLead} ` : ''}I am uncertain about the name. Please repeat only your name, one word if possible.`,
        ], transcript || contextLead || 'uncertain-name');
      }
      if (kind === 'identity_ack') {
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}Nice to meet you, ${name}. What should we tackle first?`,
          `${contextLead ? `${contextLead} ` : ''}Great to meet you, ${name}. What is the next step you want?`,
          `${contextLead ? `${contextLead} ` : ''}Thanks, ${name}. I am ready when you are.`,
        ], name || transcript || contextLead || 'identity-ack');
      }
      return '';
    }

    function stopMicPermissionStream() {
      stopMicKeepAliveMonitor();
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

    function stopMicKeepAliveMonitor() {
      if (micKeepAliveRecorder) {
        try {
          if (micKeepAliveRecorder.state !== 'inactive') {
            micKeepAliveRecorder.stop();
          }
        } catch (_) {
        }
        micKeepAliveRecorder.ondataavailable = null;
        micKeepAliveRecorder.onerror = null;
      }
      if (micKeepAliveProcessorNode) {
        try {
          micKeepAliveProcessorNode.disconnect();
        } catch (_) {
        }
        micKeepAliveProcessorNode.onaudioprocess = null;
      }
      if (micKeepAliveSourceNode) {
        try {
          micKeepAliveSourceNode.disconnect();
        } catch (_) {
        }
      }
      if (micKeepAliveGainNode) {
        try {
          micKeepAliveGainNode.disconnect();
        } catch (_) {
        }
      }
      if (micKeepAliveAudioContext) {
        try {
          micKeepAliveAudioContext.close();
        } catch (_) {
        }
      }
      micKeepAliveAudioContext = null;
      micKeepAliveSourceNode = null;
      micKeepAliveGainNode = null;
      micKeepAliveProcessorNode = null;
      micKeepAliveRecorder = null;
    }

    function startMicKeepAliveMonitor() {
      if (!micPermissionStream || !micPermissionStream.active) return;
      if (micKeepAliveRecorder && micKeepAliveRecorder.state !== 'inactive') return;
      if (micKeepAliveAudioContext && micKeepAliveSourceNode) return;

      // Prefer MediaRecorder keepalive because desktop audio stacks treat it as
      // an explicit ongoing capture session and keep mic indicators lit.
      if (typeof window.MediaRecorder === 'function') {
        try {
          micKeepAliveRecorder = new MediaRecorder(micPermissionStream, { mimeType: 'audio/webm;codecs=opus' });
          micKeepAliveRecorder.ondataavailable = () => {};
          micKeepAliveRecorder.onerror = (event) => {
            addMicDebug('keepalive-recorder-error', String(event?.error?.message || event?.message || 'unknown'));
          };
          micKeepAliveRecorder.start(2000);
          addMicDebug('keepalive', 'recorder-active');
          return;
        } catch (_) {
          // Fall back to AudioContext pipeline below.
          micKeepAliveRecorder = null;
        }
      }

      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextCtor) {
        addMicDebug('keepalive', 'AudioContext unavailable');
        return;
      }

      try {
        micKeepAliveAudioContext = new AudioContextCtor();
        micKeepAliveSourceNode = micKeepAliveAudioContext.createMediaStreamSource(micPermissionStream);
        micKeepAliveProcessorNode = micKeepAliveAudioContext.createScriptProcessor(1024, 1, 1);
        micKeepAliveProcessorNode.onaudioprocess = () => {};
        micKeepAliveGainNode = micKeepAliveAudioContext.createGain();
        // Keep the stream active while producing effectively silent output.
        micKeepAliveGainNode.gain.value = 0.00001;
        micKeepAliveSourceNode.connect(micKeepAliveProcessorNode);
        micKeepAliveProcessorNode.connect(micKeepAliveGainNode);
        micKeepAliveGainNode.connect(micKeepAliveAudioContext.destination);
        if (micKeepAliveAudioContext.state === 'suspended') {
          micKeepAliveAudioContext.resume().catch(() => {});
        }
        addMicDebug('keepalive', 'active');
      } catch (error) {
        addMicDebug('keepalive-error', String(error?.message || 'failed'));
        stopMicKeepAliveMonitor();
      }
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

    function setSettingsTab(tabName) {
      const isCamera = tabName === 'camera';
      settingsTabVoice.classList.toggle('active', !isCamera);
      settingsTabCamera.classList.toggle('active', isCamera);
      settingsViewVoice.classList.toggle('active', !isCamera);
      settingsViewCamera.classList.toggle('active', isCamera);
    }

    function updateCameraSettingsUi() {
      const active = Boolean(cameraStream && cameraStream.active);
      cameraToggleBtn.textContent = active ? 'Stop Camera Preview' : 'Start Camera Preview';
      cameraPreview.classList.toggle('inactive', !active);
      if (active) {
        cameraSettingsStatus.textContent = 'Camera preview is live.';
      } else if (!cameraSettingsStatus.textContent.trim()) {
        cameraSettingsStatus.textContent = 'Camera preview is idle.';
      }
    }

    function syncVoiceControlAvailability() {
      const manualMode = !naturalVoicePreset;
      voiceRateInput.disabled = !manualMode;
      voicePitchInput.disabled = !manualMode;
      voiceDepthInput.disabled = !manualMode;
      voiceVolumeInput.disabled = !manualMode;
      serverTtsVoiceSelect.disabled = !serverTtsEnabled;
    }

    function buildServerTtsVoiceOptions() {
      serverTtsVoiceSelect.innerHTML = '';
      for (const voice of SERVER_TTS_VOICES) {
        const option = document.createElement('option');
        option.value = voice.value;
        option.textContent = voice.label;
        serverTtsVoiceSelect.appendChild(option);
      }

      const hasSelected = SERVER_TTS_VOICES.some((voice) => voice.value === selectedServerTtsVoice);
      if (!hasSelected) {
        selectedServerTtsVoice = 'en-US-EmmaMultilingualNeural';
      }
      serverTtsVoiceSelect.value = selectedServerTtsVoice;
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
      }, 900);
      micFallbackInterval = setInterval(() => {
        if (!micAutoMode) return;
        captureFallbackTranscription();
      }, MIC_FALLBACK_INTERVAL_MS);
    }

    function writeAscii(view, offset, value) {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    }

    function downsampleChunksToRate(floatChunks, sourceRate, targetRate) {
      if (!Array.isArray(floatChunks) || !floatChunks.length) return [];

      const source = [];
      for (const chunk of floatChunks) {
        for (let index = 0; index < chunk.length; index += 1) {
          source.push(chunk[index]);
        }
      }

      const safeSourceRate = Math.max(8000, Math.round(Number(sourceRate || 16000)));
      const safeTargetRate = Math.max(8000, Math.round(Number(targetRate || 16000)));
      if (safeTargetRate >= safeSourceRate) {
        return [new Float32Array(source)];
      }

      const ratio = safeSourceRate / safeTargetRate;
      const outputLength = Math.max(1, Math.floor(source.length / ratio));
      const output = new Float32Array(outputLength);

      let outputIndex = 0;
      let inputIndex = 0;
      while (outputIndex < outputLength) {
        const nextInputIndex = Math.min(source.length, Math.floor((outputIndex + 1) * ratio));
        let sum = 0;
        let count = 0;
        for (let idx = inputIndex; idx < nextInputIndex; idx += 1) {
          sum += source[idx];
          count += 1;
        }
        output[outputIndex] = count > 0 ? sum / count : source[Math.min(inputIndex, source.length - 1)] || 0;
        outputIndex += 1;
        inputIndex = nextInputIndex;
      }

      return [output];
    }

    function getMicTranscribeProvider() {
      if (Date.now() < micProviderLocalBackoffUntil) {
        return 'local';
      }
      return 'auto';
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

    function extractFirstUrl(rawText) {
      const text = String(rawText || '');
      const match = text.match(/https?:\/\/[^\s)]+/i);
      return match ? String(match[0]).trim() : '';
    }

    async function handleWebSummaryCommand(url, sourceMode = 'ui') {
      const targetUrl = String(url || '').trim();
      if (!targetUrl) return false;

      inquiryEl.textContent = `Summarizing website: ${targetUrl}`;
      statusEl.textContent = `Fetching website summary (${sourceMode})...`;

      try {
        const res = await fetchWithTimeout('/gateway/web/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: targetUrl,
            timeout_seconds: 12,
            max_summary_sentences: 4,
          }),
        }, 16000);

        let payload = {};
        try {
          payload = await res.json();
        } catch (_) {
          payload = {};
        }

        if (!res.ok) {
          const detail = String(payload?.detail || '').trim();
          let message = `I could not summarize that website (${detail || `http_${res.status}`}).`;
          if (detail.includes('web_access_disabled')) {
            message = 'Web access is currently disabled. Set ALLOW_WEB_ACCESS=true to enable website summaries.';
          }
          inquiryEl.textContent = message;
          statusEl.textContent = message;
          await speakLocally(message, true, `web_summary_error:${sourceMode}`);
          addMicDebug('web-summary-failed', `mode=${sourceMode} detail=${detail || `http_${res.status}`}`);
          return true;
        }

        const title = String(payload?.title || '').trim();
        const summary = String(payload?.summary || '').trim();
        const spoken = summary || 'I fetched the page, but there was no useful summary text.';
        const display = title ? `Web summary (${title}): ${spoken}` : `Web summary: ${spoken}`;
        inquiryEl.textContent = display;
        statusEl.textContent = `Website summarized (${sourceMode}).`;
        await speakLocally(display, true, `web_summary_result:${sourceMode}`);
        addMicDebug('web-summary-ok', `mode=${sourceMode} url=${targetUrl}`);
        return true;
      } catch (error) {
        const message = `I could not summarize that website right now (${String(error?.message || 'network_error')}).`;
        inquiryEl.textContent = message;
        statusEl.textContent = message;
        await speakLocally(message, true, `web_summary_exception:${sourceMode}`);
        addMicDebug('web-summary-error', `mode=${sourceMode} error=${String(error?.message || 'unknown')}`);
        return true;
      }
    }

    async function handleCapabilitiesCommand(sourceMode = 'ui') {
      try {
        const res = await fetchWithTimeout('/manifest', {}, 8000);
        if (!res.ok) {
          const message = `I could not read my manifest right now (http_${res.status}).`;
          inquiryEl.textContent = message;
          statusEl.textContent = message;
          await speakLocally(message, true, `capabilities_error:${sourceMode}`);
          return true;
        }
        const manifest = await res.json();
        const capabilities = Array.isArray(manifest?.capabilities) ? manifest.capabilities : [];
        const hasWebSummary = capabilities.includes('web_page_summarization');
        const message = hasWebSummary
          ? `I currently expose ${capabilities.length} capabilities. Web page summarization is available through gateway web summarize.`
          : `I currently expose ${capabilities.length} capabilities. You can inspect them through the manifest endpoint.`;
        inquiryEl.textContent = message;
        statusEl.textContent = `Capabilities summary ready (${sourceMode}).`;
        await speakLocally(message, true, `capabilities_result:${sourceMode}`);
        addMicDebug('capabilities-summary', `mode=${sourceMode} count=${capabilities.length}`);
        return true;
      } catch (error) {
        const message = `I could not retrieve capability details right now (${String(error?.message || 'network_error')}).`;
        inquiryEl.textContent = message;
        statusEl.textContent = message;
        await speakLocally(message, true, `capabilities_exception:${sourceMode}`);
        addMicDebug('capabilities-summary-error', `mode=${sourceMode} error=${String(error?.message || 'unknown')}`);
        return true;
      }
    }

    async function maybeHandleWebOrCapabilityCommand(transcript, sourceMode = 'ui') {
      const text = String(transcript || '').trim();
      if (!text) return false;

      const lowered = text.toLowerCase();
      const askedWebsiteSummary =
        lowered.includes('summarize this website')
        || lowered.includes('summary of this website')
        || lowered.includes('summarize this url')
        || lowered.includes('summarize this site')
        || (lowered.includes('summarize') && lowered.includes('http'));

      if (askedWebsiteSummary) {
        const url = extractFirstUrl(text);
        if (!url) {
          const prompt = 'Please include a full http or https URL so I can summarize the website.';
          inquiryEl.textContent = prompt;
          statusEl.textContent = prompt;
          await speakLocally(prompt, true, `web_summary_prompt:${sourceMode}`);
          return true;
        }
        return await handleWebSummaryCommand(url, sourceMode);
      }

      const askedCapabilities =
        lowered.includes('capabilities')
        || lowered.includes('what can you do')
        || lowered.includes('access the capabilities')
        || lowered.includes('application capabilities');

      if (askedCapabilities) {
        return await handleCapabilitiesCommand(sourceMode);
      }

      return false;
    }

    async function captureFallbackTranscription() {
      if (micFallbackCaptureInFlight) return;
      if (isMicSuppressedNow()) {
        noteMicEvent('fallback-suppressed', 'tts-active');
        return;
      }
      micFallbackCaptureInFlight = true;
      clearMicFallbackTimer();
      const captureStartedAt = Date.now();
      addMicDebug('fallback:start', `lang=${defaultListenLang}`);

      try {
        let stream = null;
        let ownsStream = false;

        if (micPermissionStream && micPermissionStream.active) {
          stream = micPermissionStream;
          addMicDebug('fallback:getUserMedia', 'reuse-shared-stream');
        } else {
          addMicDebug('fallback:getUserMedia', `new-stream required active=${Boolean(micPermissionStream && micPermissionStream.active)}`);
          const preferredMic = resolvePreferredMicDevice();
          const fallbackAudioConstraints = {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          };
          if (preferredMic?.deviceId && preferredMic.deviceId !== 'default' && preferredMic.deviceId !== 'communications') {
            fallbackAudioConstraints.deviceId = { exact: preferredMic.deviceId };
          }

          stream = await navigator.mediaDevices.getUserMedia({
            audio: fallbackAudioConstraints,
            video: false,
          });
          ownsStream = true;
          addMicDebug('fallback:getUserMedia', 'ok');
        }

        const activeTrackCount = (stream && typeof stream.getAudioTracks === 'function')
          ? stream.getAudioTracks().filter((track) => track.readyState === 'live').length
          : 0;
        addMicDebug('fallback:stream-state', `owns=${ownsStream} activeTracks=${activeTrackCount}`);

        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor) {
          noteMicEvent('fallback-error', 'AudioContext unavailable');
          if (stream && ownsStream) {
            for (const track of stream.getTracks()) {
              track.stop();
            }
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

        await new Promise((resolve) => setTimeout(resolve, MIC_FALLBACK_CAPTURE_MS));

        try {
          processorNode.disconnect();
          sourceNode.disconnect();
        } catch (_) {
        }
        try {
          await audioContext.close();
        } catch (_) {
        }
        if (stream && ownsStream) {
          for (const track of stream.getTracks()) {
            track.stop();
          }
        }

        if (!floatChunks.length) {
          noteMicEvent('fallback-empty', 'no-audio-chunks');
          addMicDebug('fallback:empty', 'no-audio-chunks');
          micFallbackCaptureInFlight = false;
          return;
        }

        const targetSampleRate = 16000;
        const normalizedChunks = downsampleChunksToRate(floatChunks, fallbackSampleRate, targetSampleRate);
        const wavBlob = encodeWavBlob(normalizedChunks, targetSampleRate);
        const audioBase64 = await blobToBase64(wavBlob);
        addMicDebug('fallback:wav-ready', `bytes≈${Math.round((audioBase64.length * 3) / 4)}`);
        noteMicEvent('fallback', 'transcribe-request');
        const transcribeStartedAt = Date.now();
        const transcribeProvider = getMicTranscribeProvider();
        const transcribeRes = await fetchWithTimeout('/gateway/perception/mic/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            audio_wav_base64: audioBase64,
            language: defaultListenLang,
            provider: transcribeProvider,
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
          if (isProviderForbidden) {
            micProviderLocalBackoffUntil = Date.now() + MIC_LOCAL_PROVIDER_BACKOFF_MS;
            addMicDebug('fallback:provider-backoff', `local-for=${MIC_LOCAL_PROVIDER_BACKOFF_MS}ms`);
          }
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
        addMicDebug('fallback:provider', `${String(payload?.provider || 'unknown')} conf=${Number(payload?.confidence || 0).toFixed(2)}`);
        if (payload && payload.ok === false && String(payload.reason || '') === 'provider_unavailable') {
          const providerDetailLower = String(payload?.detail || '').toLowerCase();
          if (providerDetailLower.includes('forbidden')) {
            micProviderLocalBackoffUntil = Date.now() + MIC_LOCAL_PROVIDER_BACKOFF_MS;
            addMicDebug('fallback:provider-backoff', `local-for=${MIC_LOCAL_PROVIDER_BACKOFF_MS}ms`);
          }
          noteMicEvent('fallback-degraded', 'provider-unavailable');
          statusEl.textContent = 'Listening... (speech provider unavailable)';
          await submitMicTranscript('', 0.0, 'fallback_audio_heartbeat_provider_unavailable', true);
          micFallbackCaptureInFlight = false;
          return;
        }
        const transcript = String(payload?.transcript || '').trim();
        const fallbackProvider = String(payload?.provider || '').toLowerCase();
        const fallbackConfidence = Number(payload?.confidence || 0.74);
        if (fallbackProvider.includes('pocketsphinx') && fallbackConfidence < MIC_POCKETSPHINX_CONFIDENCE_MIN) {
          noteMicEvent('fallback-low-confidence', `${fallbackProvider}:${fallbackConfidence.toFixed(2)}`);
          addMicDebug('fallback:low-confidence-drop', `provider=${fallbackProvider} conf=${fallbackConfidence.toFixed(2)} transcript=${transcript.slice(0, 48)}`);
          statusEl.textContent = 'Listening... (low-confidence speech capture, please repeat)';
          await submitMicTranscript('', fallbackConfidence, 'fallback_audio_heartbeat_low_confidence', true);
          micFallbackCaptureInFlight = false;
          return;
        }
        if (!transcript) {
          const reason = String(payload?.reason || 'no-transcript').trim() || 'no-transcript';
          noteMicEvent('fallback-empty', reason);
          addMicDebug('fallback:no-transcript', `reason=${reason}`);
          await submitMicTranscript('', 0.0, 'fallback_audio_heartbeat_no_transcript', true);
          micFallbackCaptureInFlight = false;
          return;
        }

        if (isMicSuppressedNow()) {
          noteMicEvent('fallback-drop', 'tts-suppressed');
          addMicDebug('fallback:drop-suppressed', transcript.slice(0, 48));
          logTranscriptDrop('suppressed', transcript, 'fallback_audio');
          micFallbackCaptureInFlight = false;
          return;
        }
        if (isLikelyEchoTranscript(transcript)) {
          noteMicEvent('fallback-echo-drop', transcript.slice(0, 24));
          addMicDebug('fallback:echo-drop', transcript.slice(0, 48));
          logTranscriptDrop('echo', transcript, 'fallback_audio');
          micFallbackCaptureInFlight = false;
          return;
        }

        noteMicEvent('fallback-result', transcript.slice(0, 48));
        const isLowValueFallback = isLikelyLowValueTranscript(transcript);
        const micSync = await submitMicTranscript(
          transcript,
          isLowValueFallback ? Math.min(fallbackConfidence, 0.33) : fallbackConfidence,
          isLowValueFallback ? 'fallback_audio_short' : 'fallback_audio',
        );
        if (!micSync.ok) {
          noteMicEvent('fallback-sync-error', micSync.status);
          addMicDebug('fallback:event-sync', `status=${micSync.status}`);
        } else if (!micSync.accepted) {
          noteMicEvent('fallback-sync-skip', micSync.status);
          addMicDebug('fallback:event-sync', `skipped=${micSync.status}`);
        } else {
          addMicDebug('fallback:event-sync', `ok total=${Date.now() - captureStartedAt}ms`);
        }

        if (isLowValueFallback) {
          noteMicEvent('fallback-short', transcript.slice(0, 24));
          addMicDebug('fallback:short-transcript-forwarded', transcript);
          logTranscriptDrop('low_value', transcript, 'fallback_audio');
          await maybeHandleLowValueTranscript(transcript, 'fallback_audio');
          refreshState();
          return;
        }

        const handledWebOrCapabilityFallback = await maybeHandleWebOrCapabilityCommand(transcript, 'fallback_audio');
        if (handledWebOrCapabilityFallback) {
          refreshState();
          return;
        }

        statusEl.textContent = `Heard: ${transcript}`;
        const wakePresent = hasWakePhrase(transcript);
        if (WAKE_WORD_REQUIRED_FOR_LIVE_REPLY && !wakePresent) {
          addMicDebug('wake-gate-drop', `mode=fallback transcript=${transcript.slice(0, 40)}`);
          logTranscriptDrop('no_wake', transcript, 'fallback_audio');
          statusEl.textContent = 'Listening... (wake word required: "MIM")';
          refreshState();
          return;
        }
        const handledGreetingOnly = await maybeHandleGreetingWithoutIntent(transcript);
        if (!handledGreetingOnly) {
          const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
          if (!handledWeakIdentity) {
            const handledUnparsedIdentityIntent = await maybeHandleUnparsedIdentityIntent(transcript);
            if (!handledUnparsedIdentityIntent) {
              const handledIdentity = await maybeHandleIdentityIntroduction(transcript);
              if (!handledIdentity) {
                const handledStandaloneName = await maybeHandleStandaloneNameDuringStartup(transcript);
                if (!handledStandaloneName) {
                  await maybeHandleStartupUncertainTranscript(transcript);
                }
              }
            }
          }
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

      if (PIN_TO_SYSTEM_DEFAULT_MIC) {
        const defaultDevice = availableMics.find((d) => d.deviceId === 'default');
        if (defaultDevice) {
          return defaultDevice;
        }
      }

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

    async function enumerateCameraDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        availableCameras = [];
        cameraSelect.innerHTML = '';
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'Camera listing unavailable';
        cameraSelect.appendChild(option);
        cameraSettingsStatus.textContent = 'Camera listing unavailable in this runtime.';
        updateCameraSettingsUi();
        return;
      }

      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        availableCameras = devices.filter((d) => d.kind === 'videoinput');
      } catch (_) {
        availableCameras = [];
      }

      cameraSelect.innerHTML = '';
      if (!availableCameras.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No cameras detected';
        cameraSelect.appendChild(option);
        cameraSettingsStatus.textContent = 'No camera devices detected.';
        updateCameraSettingsUi();
        return;
      }

      let selected = availableCameras.find((d) => d.deviceId === selectedCameraDeviceId);
      if (!selected) {
        selected = availableCameras[0];
      }

      for (let index = 0; index < availableCameras.length; index += 1) {
        const camera = availableCameras[index];
        const option = document.createElement('option');
        option.value = camera.deviceId;
        option.textContent = camera.label || `Camera ${index + 1}`;
        cameraSelect.appendChild(option);
      }

      selectedCameraDeviceId = selected?.deviceId || '';
      cameraSelect.value = selectedCameraDeviceId;
      localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
      if (!(cameraStream && cameraStream.active)) {
        cameraSettingsStatus.textContent = `Selected camera: ${selected?.label || 'default camera'}`;
      }
      updateCameraSettingsUi();
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

    async function submitMicTranscript(transcript, confidence, mode = 'always_listening', allowEmpty = false) {
      const safeTranscript = String(transcript || '').trim();
      if (!safeTranscript && !allowEmpty) {
        return { ok: false, accepted: false, status: 'empty_transcript' };
      }

      if (safeTranscript && isLikelyEchoTranscript(safeTranscript)) {
        logTranscriptDrop('echo', safeTranscript, mode);
        return { ok: true, accepted: false, status: 'echo_suppressed' };
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
      if (!compact) return true;
      if (compact.length <= 2) return true;

      const normalized = text.replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      const tokens = normalized ? normalized.split(' ').filter(Boolean) : [];
      if (!tokens.length) return true;
      if (tokens.length >= 2 && tokens.every((token) => token.length <= 2)) {
        return true;
      }

      const fillerTokens = new Set(['um', 'uh', 'hmm', 'mm', 'erm', 'ah', 'eh']);
      if (tokens.every((token) => fillerTokens.has(token))) {
        return true;
      }

      return compact.length < 5;
    }

    async function maybeHandleLowValueTranscript(transcript, sourceMode = 'mic') {
      if (!isLikelyLowValueTranscript(transcript)) {
        return false;
      }

      logTranscriptDrop('low_value', transcript, sourceMode);

      const compact = String(transcript || '').toLowerCase().replace(/[^a-z]/g, '');
      const now = Date.now();
      if (now < lowValueClarifyCooldownUntil && compact && compact === lowValueClarifyLastCompact) {
        return true;
      }

      if (now < lowValueSpeakCooldownUntil) {
        addMicDebug('low-value-suppressed', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
        return true;
      }

      if (compact.length < 3) {
        addMicDebug('low-value-muted', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
        lowValueClarifyLastCompact = compact;
        lowValueClarifyCooldownUntil = now + LOW_VALUE_CLARIFY_COOLDOWN_MS;
        return true;
      }

      lowValueClarifyLastCompact = compact;
      lowValueClarifyCooldownUntil = now + LOW_VALUE_CLARIFY_COOLDOWN_MS;
      lowValueSpeakCooldownUntil = now + LOW_VALUE_SPEAK_COOLDOWN_MS;

      const clarify = buildDialogPrompt('low_value', { transcript });

      statusEl.textContent = `Low-confidence input ignored (${sourceMode}).`;
      inquiryEl.textContent = clarify;
      addMicDebug('low-value-clarify', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
      return true;
    }

    function isGreetingOnlyTranscript(transcript) {
      const text = String(transcript || '').toLowerCase();
      if (!text.trim()) return false;
      if (text.includes('my name is') || text.includes("i am") || text.includes("i'm")) return false;

      const normalized = text.replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!normalized) return false;
      return /^(hello|hi|hey)(\s+(ma'?am|mam|maam|sir|mim))*$/.test(normalized);
    }

    async function maybeHandleGreetingWithoutIntent(transcript) {
      if (!isGreetingOnlyTranscript(transcript)) {
        return false;
      }

      const now = Date.now();
      if (now < greetingClarifyCooldownUntil) {
        return true;
      }

      greetingClarifyCooldownUntil = now + GREETING_CLARIFY_COOLDOWN_MS;
      startupInquiryIssued = true;
      const prompt = buildDialogPrompt('greeting_only', { transcript });
      inquiryEl.textContent = prompt;
      await speakLocally(prompt, true, 'greeting_only');
      lastInquiryPromptSpoken = prompt;
      addMicDebug('greeting-clarify', String(transcript || '').slice(0, 32));
      return true;
    }

    async function maybeHandleStandaloneNameDuringStartup(transcript) {
      if (!startupInquiryIssued || !shouldAskForNameNow()) return false;
      const text = String(transcript || '').toLowerCase().replace(/[^a-z'\-\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!text) return false;
      if (text.includes('my name is') || text.includes("i'm") || text.includes('i am')) return false;

      const filler = new Set(['hello', 'hi', 'hey', 'it', 'had', 'a', 'the', 'is', 'name', 'my', 'maam', 'mam', 'sir', 'there']);
      const parts = text.split(' ').map((s) => s.trim()).filter(Boolean);
      if (!parts.length || parts.length > 2) return false;

      const candidates = parts.filter((part) => part.length >= 2 && part.length <= 24 && !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
      if (!candidates.length) return false;

      const candidate = candidates[candidates.length - 1];
      addMicDebug('identity-standalone', `candidate=${candidate}`);
      return await acknowledgeIntroducedIdentity(candidate);
    }

    function isLikelyIdentityAttemptTranscript(transcript) {
      const text = String(transcript || '').toLowerCase().replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!text) return false;
      if (text.includes('my name is') || text.includes('name is') || text.includes('i am') || text.includes("i'm")) return true;

      const parts = text.split(' ').map((s) => s.trim()).filter(Boolean);
      if (parts.length >= 1 && parts.length <= 2) {
        return parts.every((part) => part.length >= 2 && part.length <= 24);
      }

      return false;
    }

    async function maybeHandleStartupUncertainTranscript(transcript) {
      if (!startupInquiryIssued || !shouldAskForNameNow()) return false;
      if (!isLikelyIdentityAttemptTranscript(transcript)) return false;
      const compact = String(transcript || '').toLowerCase().replace(/[^a-z]/g, '');
      if (!compact) return false;

      const now = Date.now();
      if (now < startupFeedbackCooldownUntil) {
        return true;
      }

      startupFeedbackCooldownUntil = now + 45000;
      startupFeedbackLastCompact = compact;
      const prompt = buildDialogPrompt('uncertain_name', { transcript });
      inquiryEl.textContent = prompt;
      await speakLocally(prompt, true, 'startup_uncertain_name');
      lastInquiryPromptSpoken = prompt;
      addMicDebug('startup-uncertain', String(transcript || '').slice(0, 36));
      return true;
    }

    function stopServerTtsPlayback() {
      if (activeServerTtsAudio) {
        try {
          activeServerTtsAudio.pause();
          activeServerTtsAudio.src = '';
        } catch (_) {
        }
      }
      activeServerTtsAudio = null;
      if (activeServerTtsUrl) {
        try {
          URL.revokeObjectURL(activeServerTtsUrl);
        } catch (_) {
        }
      }
      activeServerTtsUrl = '';
      speechPlaybackActive = false;
      setSpeaking(false);
    }

    function speakWithBrowserTts(text, interrupt = true) {
      const phrase = String(text || '').trim();
      const smoothedPhrase = phrase.replace(/\s*[—-]\s*/g, ', ').replace(/\s{2,}/g, ' ').trim();
      if (!phrase) return false;
      if (!window.speechSynthesis) {
        lastLocalTtsError = 'speechSynthesis API unavailable';
        statusEl.textContent = 'Local TTS unavailable in this runtime.';
        return false;
      }

      try {
        const playbackToken = ++localTtsPlaybackToken;
        activeSpeechOwner = 'browser_tts';
        rememberSpokenUtterance(phrase, 'browser_tts');
        addSpeechDebug('queued', `source=browser_tts path=local sig=${shortSpeechSignature(phrase)} interrupt=${Boolean(interrupt)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setMicSuppression(2500, 'browser_tts_start');
        if (window.speechSynthesis.resume) {
          window.speechSynthesis.resume();
        }
        if (interrupt) {
          addSpeechDebug('canceled', `source=browser_tts reason=interrupt token=${playbackToken}`);
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
          ? clamp(voiceRate, 0.88, 1.00)
          : clamp(voiceRate, 0.1, 10.0);
        const appliedPitch = naturalVoicePreset
          ? clamp(effectivePitchValue(), 0.78, 0.98)
          : effectivePitchValue();
        const appliedVolume = naturalVoicePreset
          ? clamp(voiceVolume, 0.75, 1.0)
          : clamp(voiceVolume, 0.0, 1.0);
        utterance.rate = appliedRate;
        utterance.pitch = appliedPitch;
        utterance.volume = appliedVolume;
        utterance.onstart = () => {
          if (playbackToken !== localTtsPlaybackToken) return;
          started = true;
          lastLocalTtsError = '';
          speechPlaybackActive = true;
          activeSpeechOwner = 'browser_tts';
          setMicSuppression(2500, 'browser_tts_onstart');
          addSpeechDebug('started', `source=browser_tts path=local sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(true);
        };
        utterance.onend = () => {
          if (playbackToken !== localTtsPlaybackToken) return;
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'browser_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_onend');
          addSpeechDebug('ended', `source=browser_tts path=local token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
        };
        utterance.onerror = (event) => {
          if (playbackToken !== localTtsPlaybackToken) return;
          lastLocalTtsError = String(event?.error || 'unknown_tts_error');
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'browser_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_onerror');
          addSpeechDebug('canceled', `source=browser_tts reason=error:${lastLocalTtsError} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
        };
        window.speechSynthesis.speak(utterance);

        const tryBareRetry = () => {
          if (playbackToken !== localTtsPlaybackToken || started || retriedBare) return;
          retriedBare = true;
          try {
            window.speechSynthesis.cancel();
            const fallbackUtterance = new SpeechSynthesisUtterance(utteranceText);
            fallbackUtterance.rate = appliedRate;
            fallbackUtterance.pitch = appliedPitch;
            fallbackUtterance.volume = appliedVolume;
            fallbackUtterance.onstart = () => {
              if (playbackToken !== localTtsPlaybackToken) return;
              started = true;
              lastLocalTtsError = '';
              speechPlaybackActive = true;
              activeSpeechOwner = 'browser_tts';
              setMicSuppression(2500, 'browser_tts_fallback_onstart');
              addSpeechDebug('started', `source=browser_tts path=local-retry sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(true);
            };
            fallbackUtterance.onend = () => {
              if (playbackToken !== localTtsPlaybackToken) return;
              speechPlaybackActive = false;
              if (activeSpeechOwner === 'browser_tts') {
                activeSpeechOwner = '';
              }
              setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_fallback_onend');
              addSpeechDebug('ended', `source=browser_tts path=local-retry token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(false);
            };
            fallbackUtterance.onerror = (event) => {
              if (playbackToken !== localTtsPlaybackToken) return;
              lastLocalTtsError = String(event?.error || 'fallback_tts_error');
              speechPlaybackActive = false;
              if (activeSpeechOwner === 'browser_tts') {
                activeSpeechOwner = '';
              }
              setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_fallback_onerror');
              addSpeechDebug('canceled', `source=browser_tts path=local-retry reason=error:${lastLocalTtsError} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(false);
              statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
            };
            window.speechSynthesis.speak(fallbackUtterance);
          } catch (_) {
          }
        };

        setTimeout(() => {
          if (playbackToken === localTtsPlaybackToken && !started) {
            tryBareRetry();
          }
        }, 1200);

        setTimeout(() => {
          if (playbackToken === localTtsPlaybackToken && !started) {
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

    async function speakWithServerTts(text, interrupt = true) {
      const phrase = String(text || '').trim();
      if (!phrase || !serverTtsEnabled) return false;

      try {
        const playbackToken = localTtsPlaybackToken;
        activeSpeechOwner = 'server_tts';
        rememberSpokenUtterance(phrase, 'server_tts');
        addSpeechDebug('queued', `source=server_tts path=server sig=${shortSpeechSignature(phrase)} interrupt=${Boolean(interrupt)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setMicSuppression(2500, 'server_tts_start');
        if (interrupt) {
          addSpeechDebug('canceled', `source=server_tts reason=interrupt token=${playbackToken}`);
          stopServerTtsPlayback();
        }
        if (window.speechSynthesis && window.speechSynthesis.cancel) {
          window.speechSynthesis.cancel();
        }

        const res = await fetch('/gateway/voice/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: phrase,
            language: getPreferredInteractionLanguage(),
            voice: selectedServerTtsVoice,
          }),
        });
        if (!res.ok) {
          lastLocalTtsError = `server_tts_http_${res.status}`;
          addSpeechDebug('suppressed', `source=server_tts reason=http_${res.status} sig=${shortSpeechSignature(phrase)} token=${playbackToken}`);
          return false;
        }

        const audioBlob = await res.blob();
        if (!audioBlob || audioBlob.size < 256) {
          lastLocalTtsError = 'server_tts_empty_audio';
          addSpeechDebug('suppressed', `source=server_tts reason=empty-audio sig=${shortSpeechSignature(phrase)} token=${playbackToken}`);
          return false;
        }

        stopServerTtsPlayback();
        activeServerTtsUrl = URL.createObjectURL(audioBlob);
        activeServerTtsAudio = new Audio(activeServerTtsUrl);
        activeServerTtsAudio.preload = 'auto';
        speechPlaybackActive = true;
        addSpeechDebug('started', `source=server_tts path=server sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setSpeaking(true);

        activeServerTtsAudio.onended = () => {
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'server_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'server_tts_onend');
          addSpeechDebug('ended', `source=server_tts path=server token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          stopServerTtsPlayback();
        };
        activeServerTtsAudio.onerror = () => {
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'server_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'server_tts_onerror');
          addSpeechDebug('canceled', `source=server_tts path=server reason=playback-error token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          stopServerTtsPlayback();
        };

        const playPromise = activeServerTtsAudio.play();
        if (playPromise && typeof playPromise.then === 'function') {
          await playPromise;
        }

        lastLocalTtsError = '';
        return true;
      } catch (error) {
        lastLocalTtsError = String(error?.message || 'server_tts_failed');
        addSpeechDebug('suppressed', `source=server_tts reason=${lastLocalTtsError} sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        stopServerTtsPlayback();
        setSpeaking(false);
        return false;
      }
    }

    async function speakLocally(text, interrupt = true, sourceTag = 'unspecified') {
      const phrase = String(text || '').trim();
      if (!phrase) return false;

      const compact = phrase.toLowerCase().replace(/[^a-z0-9]/g, '');
      const now = Date.now();
      if (compact && compact === lastSpokenPhraseCompact && (now - lastSpokenPhraseAt) < SPOKEN_PHRASE_DEDUPE_MS) {
        addSpeechDebug('suppressed', `source=${sourceTag} reason=dedupe sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        return false;
      }

      if ((speechInFlight || speechPlaybackActive) && !interrupt) {
        addSpeechDebug('suppressed', `source=${sourceTag} reason=busy_no_interrupt sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        return false;
      }

      if ((speechInFlight || speechPlaybackActive) && interrupt) {
        addSpeechDebug('canceled', `source=${sourceTag} reason=interrupt-active-owner owner=${activeSpeechOwner || '-'} token=${localTtsPlaybackToken}`);
        stopServerTtsPlayback();
        localTtsPlaybackToken += 1;
        if (window.speechSynthesis && window.speechSynthesis.cancel) {
          window.speechSynthesis.cancel();
        }
      }

      if (compact) {
        lastSpokenPhraseCompact = compact;
        lastSpokenPhraseAt = now;
      }

      const requestId = ++speechRequestSeq;
      speechInFlight = true;
      addSpeechDebug('queued', `source=${sourceTag} route=auto sig=${shortSpeechSignature(phrase)} request=${requestId} token=${localTtsPlaybackToken} suppressMs=${suppressionWindowMs()}`);
      try {
        const serverSpoken = await speakWithServerTts(phrase, interrupt);
        if (serverSpoken) {
          return true;
        }
        return speakWithBrowserTts(phrase, interrupt);
      } finally {
        if (requestId === speechRequestSeq) {
          speechInFlight = false;
        }
      }
    }

    async function maybeSpeakFromState(data) {
      if (!STATE_POLL_SPEAK_ENABLED) return false;
      const outputId = Number(data.latest_output_action_id || 0);
      const text = String(data.latest_output_text || '').trim();
      const allowed = Boolean(data.latest_output_allowed);
      if (!allowed || !text || outputId <= 0) return false;
      if (outputId <= lastSpokenOutputId) return false;

      const rewritten = rewriteQueuedOutputText(text, data);
      if (!rewritten) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        return false;
      }

      const signature = normalizeSpeechSignature(rewritten);
      const now = Date.now();
      if (signature && signature === lastSpokenSignature && (now - lastSpokenSignatureAt) < SPOKEN_DUPLICATE_COOLDOWN_MS) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        return false;
      }

      if (await speakLocally(rewritten, true, 'state_poll_output')) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        lastSpokenSignature = signature;
        lastSpokenSignatureAt = now;
        localStorage.setItem('mim_last_spoken_signature', lastSpokenSignature);
        localStorage.setItem('mim_last_spoken_signature_at', String(lastSpokenSignatureAt));
        return true;
      }
      return false;
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

    function extractNameAfterLeadIns(textRaw, leadIns) {
      const text = String(textRaw || '').toLowerCase();
      for (const leadIn of leadIns) {
        const idx = text.indexOf(leadIn);
        if (idx < 0) continue;
        const tail = text.slice(idx + leadIn.length)
          .replace(/[^a-z'\-\s]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
        if (tail) return tail;
      }
      return '';
    }

    function extractIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const candidate = extractNameAfterLeadIns(text, ['my name is ', "i'm ", 'i am ']);
      if (!candidate) return '';

      const filler = new Set(['hello', 'hi', 'hey', 'maam', 'mam', 'sir', 'there']);
      const pieces = candidate.split(' ').map((s) => s.trim()).filter(Boolean);
      const filtered = pieces.filter((part) => !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
      if (!filtered.length) return '';

      const preferredSingle = filtered.find((part) => part.length >= 2 && part.length <= 24);
      if (preferredSingle && !WEAK_IDENTITY_WORDS.has(preferredSingle)) {
        return preferredSingle;
      }

      const merged = filtered.slice(0, 2).join(' ').trim();
      if (!merged || WEAK_IDENTITY_WORDS.has(merged)) return '';
      return merged;
    }

    function extractWeakIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const candidate = extractNameAfterLeadIns(text, ['my name is ', "i'm ", 'i am ']);
      if (!candidate) return '';
      const parts = candidate.split(' ').map((s) => s.trim()).filter(Boolean);
      const weakPart = parts.find((part) => WEAK_IDENTITY_WORDS.has(part));
      if (weakPart) {
        return weakPart;
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

      const clarification = buildDialogPrompt('low_value', { transcript });
      startupInquiryIssued = true;
      inquiryEl.textContent = clarification;
      await speakLocally(clarification, false, 'weak_identity_clarify');
      lastInquiryPromptSpoken = clarification;
      weakIdentityLastPromptKey = promptKey;
      weakIdentityClarifyCooldownUntil = now + 20000;
      return true;
    }

    async function acknowledgeIntroducedIdentity(identityRaw) {
      const normalized = normalizeIdentityLabel(identityRaw);
      if (!normalized || isUnknownOrMissingIdentity(normalized)) return false;

      const displayName = normalized.charAt(0).toUpperCase() + normalized.slice(1);
      const greeting = buildDialogPrompt('identity_ack', { name: displayName });
      startupInquiryIssued = true;
      locallyAcceptedIdentity = normalized;
      suppressBackendInquiryUntil = Date.now() + 120000;
      inquiryEl.textContent = greeting;
      await speakLocally(greeting, true, 'identity_ack');
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

    async function maybeHandleUnparsedIdentityIntent(transcript) {
      const text = String(transcript || '').toLowerCase();
      if (!text.includes('my name is')) {
        return false;
      }

      const parsed = extractIntroducedIdentity(transcript);
      if (parsed) {
        addMicDebug('identity-direct', `parsed=${parsed}`);
        return await acknowledgeIntroducedIdentity(parsed);
      }

      const tail = extractNameAfterLeadIns(String(transcript || ''), ['my name is ']);
      if (tail) {
        const filler = new Set(['hello', 'hi', 'hey', 'maam', 'mam', 'sir', 'there']);
        const parts = tail.split(' ').map((s) => s.trim()).filter(Boolean);
        const candidate = parts.find((part) => part.length >= 2 && part.length <= 24 && !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
        if (candidate) {
          addMicDebug('identity-recovery', `candidate=${candidate}`);
          return await acknowledgeIntroducedIdentity(candidate);
        }
      }

      const now = Date.now();
      const promptKey = 'unparsed-name-intent';
      if (now < weakIdentityClarifyCooldownUntil && weakIdentityLastPromptKey === promptKey) {
        return true;
      }

      const clarification = buildDialogPrompt('uncertain_name', { transcript });
      startupInquiryIssued = true;
      inquiryEl.textContent = clarification;
      await speakLocally(clarification, true, 'identity_unparsed_clarify');
      lastInquiryPromptSpoken = clarification;
      weakIdentityLastPromptKey = promptKey;
      weakIdentityClarifyCooldownUntil = now + 20000;
      return true;
    }

    async function maybeHandleIdentityIntroduction(transcript) {
      const spokenIdentity = extractIntroducedIdentity(transcript);
      if (!spokenIdentity) return false;
      return await acknowledgeIntroducedIdentity(spokenIdentity);
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
        lastInquiryPromptSpoken = backendPrompt;
        return;
      }

      if (!isUnknownOrMissingIdentity(data?.camera_last_label) && !shouldAskForNameNow()) {
        startupInquiryIssued = true;
        return;
      }

      startupInquiryIssued = true;
      const startupPrompt = buildDialogPrompt('startup_identity');
      inquiryEl.textContent = startupPrompt;
      lastInquiryPromptSpoken = startupPrompt;
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

      if (/(neural|natural|enhanced|premium|wavenet|studio|online|hq)/.test(name)) score += 30;
      if (/(siri|samantha|victoria|daniel|karen|moira|zira|aria|alloy|nova)/.test(name)) score += 15;
      if (/(espeak|compact|robot|test|default voice|mbrola|festival)/.test(name)) score -= 38;

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

      serverTtsEnabled = Boolean(serverTtsToggle.checked);
      localStorage.setItem('mim_server_tts_enabled', serverTtsEnabled ? '1' : '0');

      selectedServerTtsVoice = String(serverTtsVoiceSelect.value || selectedServerTtsVoice || '').trim() || 'en-US-EmmaMultilingualNeural';
      localStorage.setItem('mim_server_tts_voice', selectedServerTtsVoice);

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
          startMicKeepAliveMonitor();
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
      if (refreshInFlight) {
        refreshPending = true;
        return;
      }

      refreshInFlight = true;
      try {
        const res = await fetch('/mim/ui/state');
        if (!res.ok) {
          markBackendReachability(false);
          updateIconGlow();
          return;
        }
        markBackendReachability(true);
        const data = await res.json();
        latestUiState = data;
        setSpeaking(Boolean(data.speaking));
        const spokeFromState = await maybeSpeakFromState(data).catch(() => false);
        maybeIssueStartupIdentityInquiry(data);

        const cameraLabel = data.camera_last_label || '(none)';
        const cameraConfidence = Number(data.camera_last_confidence || 0).toFixed(2);
        cameraEl.textContent = `Camera: ${cameraLabel} (confidence ${cameraConfidence})`;
        const inquiryPrompt = String(data.inquiry_prompt || '').trim();
        const conversationContext = (data && typeof data.conversation_context === 'object') ? data.conversation_context : {};
        const cameraIdentityKnown = !isUnknownOrMissingIdentity(cameraLabel);
        const shouldSuppressInquiryReplay = Date.now() < suppressBackendInquiryUntil && (Boolean(locallyAcceptedIdentity) || cameraIdentityKnown);
        if (inquiryPrompt && !shouldSuppressInquiryReplay && !spokeFromState) {
          inquiryEl.textContent = inquiryPrompt;
          lastInquiryPromptSpoken = inquiryPrompt;
        } else if (shouldSuppressInquiryReplay && isIdentityInquiryText(inquiryEl.textContent)) {
          inquiryEl.textContent = locallyAcceptedIdentity
            ? `Nice to meet you, ${locallyAcceptedIdentity.charAt(0).toUpperCase()}${locallyAcceptedIdentity.slice(1)}.`
            : inquiryEl.textContent;
        } else if (!startupInquiryIssued) {
          inquiryEl.textContent = '';
          lastInquiryPromptSpoken = '';
        }

        activeVisualIdentity = normalizeIdentityLabel(cameraLabel);
        if (activeVisualIdentity) {
          locallyAcceptedIdentity = activeVisualIdentity;
        }
        if (activeVisualIdentity && activeVisualIdentity !== lastVisualIdentity) {
          maybeGreetRecognizedIdentity(activeVisualIdentity);
        }
        lastVisualIdentity = activeVisualIdentity;

        updateIconGlow();
      } catch (_) {
        markBackendReachability(false);
        updateIconGlow();
      } finally {
        refreshInFlight = false;
        if (refreshPending) {
          refreshPending = false;
          setTimeout(() => {
            refreshState();
          }, 0);
        }
      }
    }

    async function speakNow() {
      const message = sayInput.value.trim();
      if (!message) return;

      const handledWebOrCapabilityText = await maybeHandleWebOrCapabilityCommand(message, 'typed_input');
      if (handledWebOrCapabilityText) {
        refreshState();
        return;
      }

      const localSpoken = await speakLocally(message, true, 'typed_input');
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

      const micReady = await ensureMicPermission({ keepStreamAlive: FORCE_FALLBACK_STT });
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
        startMicKeepAliveMonitor();
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
          stopMicFallbackLoop();
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
          if (isMicSuppressedNow()) {
            noteMicEvent('recognition-drop', 'tts-suppressed');
            addMicDebug('recognition:drop-suppressed', transcript.slice(0, 48));
            logTranscriptDrop('suppressed', transcript, 'always_listening');
            return;
          }
          if (isLikelyEchoTranscript(transcript)) {
            noteMicEvent('recognition-echo-drop', transcript.slice(0, 24));
            addMicDebug('recognition:echo-drop', transcript.slice(0, 48));
            logTranscriptDrop('echo', transcript, 'always_listening');
            return;
          }

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

          const isLowValueRecognition = isLikelyLowValueTranscript(transcript);
          const micSync = await submitMicTranscript(
            transcript,
            isLowValueRecognition ? Math.min(confidence, 0.33) : confidence,
            isLowValueRecognition ? 'always_listening_short' : 'always_listening',
          );
          if (!micSync.ok) {
            statusEl.textContent = `Heard: ${transcript} (backend sync delayed)`;
          } else if (!micSync.accepted) {
            statusEl.textContent = `Heard: ${transcript} (${micSync.status})`;
          }

          if (isLowValueRecognition) {
            noteMicEvent('recognition-short', transcript.slice(0, 24));
            addMicDebug('recognition:short-transcript-forwarded', transcript);
            logTranscriptDrop('low_value', transcript, 'always_listening');
            await maybeHandleLowValueTranscript(transcript, 'always_listening');
            refreshState();
            return;
          }

          const handledWebOrCapabilityRecognition = await maybeHandleWebOrCapabilityCommand(transcript, 'always_listening');
          if (handledWebOrCapabilityRecognition) {
            refreshState();
            return;
          }

          statusEl.textContent = `Heard: ${transcript}`;
          const wakePresent = hasWakePhrase(transcript);
          if (WAKE_WORD_REQUIRED_FOR_LIVE_REPLY && !wakePresent) {
            addMicDebug('wake-gate-drop', `mode=recognition transcript=${transcript.slice(0, 40)}`);
            logTranscriptDrop('no_wake', transcript, 'always_listening');
            statusEl.textContent = 'Listening... (wake word required: "MIM")';
            refreshState();
            return;
          }
          const handledGreetingOnly = await maybeHandleGreetingWithoutIntent(transcript);
          if (!handledGreetingOnly) {
            const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
            if (!handledWeakIdentity) {
              const handledUnparsedIdentityIntent = await maybeHandleUnparsedIdentityIntent(transcript);
              if (!handledUnparsedIdentityIntent) {
                const handledIdentity = await maybeHandleIdentityIntroduction(transcript);
                if (!handledIdentity) {
                  const handledStandaloneName = await maybeHandleStandaloneNameDuringStartup(transcript);
                  if (!handledStandaloneName) {
                    await maybeHandleStartupUncertainTranscript(transcript);
                  }
                }
              }
            }
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
            const shouldUseFallback = micErrorStreak > 0 || micHardErrorStreak > 0 || micConsecutiveOnend > 2;
            if (shouldUseFallback) {
              captureFallbackTranscription();
            }
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

    function stopCameraWatcher() {
      if (motionInterval) {
        clearInterval(motionInterval);
        motionInterval = null;
      }

      if (cameraWatcherVideo) {
        try {
          cameraWatcherVideo.pause();
        } catch (_) {
        }
        cameraWatcherVideo.srcObject = null;
      }

      if (cameraStream) {
        try {
          for (const track of cameraStream.getTracks()) {
            try {
              track.stop();
            } catch (_) {
            }
          }
        } catch (_) {
        }
      }

      cameraStream = null;
      cameraWatcherVideo = null;
      cameraWatcherCanvas = null;
      cameraWatcherCtx = null;
      cameraLastFrame = null;
      cameraLastSentAt = 0;

      if (cameraPreview) {
        cameraPreview.srcObject = null;
      }
      updateCameraSettingsUi();
    }

    async function startCameraWatcher() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        cameraEl.textContent = 'Camera: browser camera API not available';
        cameraSettingsStatus.textContent = 'Camera API is unavailable in this runtime.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
        return;
      }

      stopCameraWatcher();

      try {
        const videoConstraints = { facingMode: 'user' };
        if (selectedCameraDeviceId) {
          videoConstraints.deviceId = { exact: selectedCameraDeviceId };
        }
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: false });
        const firstTrack = cameraStream.getVideoTracks ? cameraStream.getVideoTracks()[0] : null;
        if (firstTrack) {
          const settings = firstTrack.getSettings ? firstTrack.getSettings() : {};
          const resolvedDeviceId = String(settings?.deviceId || selectedCameraDeviceId || '').trim();
          if (resolvedDeviceId) {
            selectedCameraDeviceId = resolvedDeviceId;
            localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
          }
        }

        cameraWatcherVideo = document.createElement('video');
        cameraWatcherVideo.srcObject = cameraStream;
        cameraWatcherVideo.muted = true;
        cameraWatcherVideo.playsInline = true;
        await cameraWatcherVideo.play();

        cameraPreview.srcObject = cameraStream;
        try {
          await cameraPreview.play();
        } catch (_) {
        }

        cameraWatcherCanvas = document.createElement('canvas');
        cameraWatcherCtx = cameraWatcherCanvas.getContext('2d', { willReadFrequently: true });
        const width = 96;
        const height = 72;
        cameraWatcherCanvas.width = width;
        cameraWatcherCanvas.height = height;

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

        motionInterval = setInterval(async () => {
          if (!cameraWatcherCtx || !cameraWatcherVideo || cameraWatcherVideo.readyState < 2) return;
          cameraWatcherCtx.drawImage(cameraWatcherVideo, 0, 0, width, height);
          const frame = cameraWatcherCtx.getImageData(0, 0, width, height).data;

          if (!cameraLastFrame) {
            cameraLastFrame = new Uint8ClampedArray(frame);
            return;
          }

          let delta = 0;
          const stride = 16;
          for (let i = 0; i < frame.length; i += stride) {
            delta += Math.abs(frame[i] - cameraLastFrame[i]);
          }
          const samples = Math.floor(frame.length / stride);
          const avgDelta = samples > 0 ? delta / samples : 0;
          const normalized = Math.max(0, Math.min(1, avgDelta / 40));

          cameraLastFrame.set(frame);
          const now = Date.now();
          if (normalized >= 0.18 && now - cameraLastSentAt >= 1200) {
            cameraLastSentAt = now;
            cameraEl.textContent = `Camera: activity detected (${normalized.toFixed(2)})`;
            await postCameraActivity(normalized);
          }
        }, 900);

        await enumerateCameraDevices();

        const activeLabel = firstTrack?.label ? ` (${firstTrack.label})` : '';
        cameraSettingsStatus.textContent = `Camera preview live${activeLabel}.`;
        cameraEl.textContent = 'Camera: always watching for activity';
        healthState.cameraOk = true;
        updateCameraSettingsUi();
        updateIconGlow();
      } catch (_) {
        stopCameraWatcher();
        cameraEl.textContent = 'Camera permission denied or unavailable';
        cameraSettingsStatus.textContent = 'Unable to start camera. Check permission and selected device.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
      }
    }

    document.getElementById('speakBtn').addEventListener('click', speakNow);
    document.getElementById('cameraBtn').addEventListener('click', sendCameraEvent);
    settingsBtn.addEventListener('click', () => {
      settingsPanel.classList.toggle('open');
    });
    settingsTabVoice.addEventListener('click', () => setSettingsTab('voice'));
    settingsTabCamera.addEventListener('click', () => setSettingsTab('camera'));
    cameraSelect.addEventListener('change', async () => {
      selectedCameraDeviceId = String(cameraSelect.value || '').trim();
      localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
      cameraSettingsStatus.textContent = 'Switching camera...';
      await startCameraWatcher();
    });
    cameraRefreshBtn.addEventListener('click', async () => {
      cameraSettingsStatus.textContent = 'Refreshing camera list...';
      await enumerateCameraDevices();
    });
    cameraToggleBtn.addEventListener('click', async () => {
      if (cameraStream && cameraStream.active) {
        stopCameraWatcher();
        cameraEl.textContent = 'Camera: preview stopped by user';
        healthState.cameraOk = false;
        cameraSettingsStatus.textContent = 'Camera preview stopped.';
        updateIconGlow();
        return;
      }
      cameraSettingsStatus.textContent = 'Starting camera preview...';
      await startCameraWatcher();
    });
    voiceSelect.addEventListener('change', applyVoiceSettings);
    serverTtsToggle.addEventListener('change', applyVoiceSettings);
    serverTtsVoiceSelect.addEventListener('change', applyVoiceSettings);
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
    serverTtsToggle.checked = serverTtsEnabled;
    naturalVoiceToggle.checked = naturalVoicePreset;
    voiceRateInput.value = clamp(voiceRate, 0.7, 1.35).toFixed(2);
    voicePitchInput.value = clamp(voicePitch, 0.7, 1.35).toFixed(2);
    voiceDepthInput.value = String(Math.round(clamp(voiceDepth, 0, 100)));
    voiceVolumeInput.value = clamp(voiceVolume, 0.4, 1.0).toFixed(2);
    syncVoiceControlAvailability();
    syncVoiceControlLabels();
    enumerateMicDevices();
    enumerateCameraDevices();
    buildVoiceOptions();
    buildServerTtsVoiceOptions();
    startVoiceRecoveryLoop();
    if (window.speechSynthesis) {
      window.speechSynthesis.onvoiceschanged = () => {
        buildVoiceOptions();
        applyVoiceSettings();
      };
    }
    setSettingsTab('voice');
    updateCameraSettingsUi();
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

    latest_goal = (
      (
        await db.execute(
          select(WorkspaceStrategyGoal)
          .order_by(WorkspaceStrategyGoal.id.desc())
          .limit(1)
        )
      )
      .scalars()
      .first()
    )

    open_question = (
      (
        await db.execute(
          select(WorkspaceInquiryQuestion)
          .where(WorkspaceInquiryQuestion.status == "open")
          .order_by(WorkspaceInquiryQuestion.id.desc())
          .limit(1)
        )
      )
      .scalars()
      .first()
    )

    latest_memory = (
      (
        await db.execute(
          select(MemoryEntry)
          .order_by(MemoryEntry.id.desc())
          .limit(1)
        )
      )
      .scalars()
      .first()
    )

    latest_interaction_learning = (
      (
        await db.execute(
          select(MemoryEntry)
          .where(MemoryEntry.memory_class == "interaction_learning")
          .order_by(MemoryEntry.id.desc())
          .limit(1)
        )
      )
      .scalars()
      .first()
    )

    goal_summary = ""
    if latest_goal:
      goal_summary = _compact_sentence(
        latest_goal.reasoning_summary
        or latest_goal.success_criteria
        or latest_goal.evidence_summary
        or latest_goal.strategy_type.replace("_", " "),
        max_len=160,
      )

    open_question_summary = ""
    should_surface_open_question = False
    if open_question:
      urgency = str(open_question.urgency or "").strip().lower()
      priority = str(open_question.priority or "").strip().lower()
      age_seconds = (now - open_question.created_at.astimezone(timezone.utc)).total_seconds() if open_question.created_at else 0.0
      urgent_flag = urgency in {"critical", "high", "urgent"} or priority in {"critical", "high", "urgent"}
      open_question_summary = _compact_sentence(
        open_question.waiting_decision
        or open_question.why_answer_matters
        or open_question.safe_default_if_unanswered,
        max_len=170,
      )

    memory_summary = ""
    if latest_memory:
      memory_summary = _compact_sentence(
        latest_memory.summary or latest_memory.content,
        max_len=140,
      )

    learning_summary = ""
    if latest_interaction_learning and not _is_low_quality_learning_entry(latest_interaction_learning):
      learning_summary = _compact_sentence(
        latest_interaction_learning.summary or latest_interaction_learning.content,
        max_len=140,
      )

    mic_payload = mic_row.last_event_payload_json if mic_row and isinstance(mic_row.last_event_payload_json, dict) else {}
    mic_confidence = float(mic_payload.get("confidence", 0.0) or 0.0)
    mic_timestamp = _parse_payload_timestamp(mic_payload.get("timestamp"))
    mic_age_seconds = _age_seconds(now, mic_timestamp)
    mic_transcript_raw = str(mic_payload.get("transcript", "")).strip()
    latest_mic_transcript = ""
    if (
      mic_transcript_raw
      and mic_confidence >= MIC_PROMPT_MIN_CONFIDENCE
      and (mic_age_seconds is None or mic_age_seconds <= MIC_PROMPT_MAX_AGE_SECONDS)
    ):
      latest_mic_transcript = _compact_sentence(mic_transcript_raw, max_len=120)

    latest_input_event = (
      (
        await db.execute(
          select(InputEvent)
          .where(InputEvent.source.in_(["text", "voice", "ui", "api"]))
          .order_by(InputEvent.id.desc())
          .limit(1)
        )
      )
      .scalars()
      .first()
    )
    latest_input_text = ""
    if latest_input_event:
      latest_input_text = _compact_sentence(str(latest_input_event.raw_input or "").strip(), max_len=120)

    latest_user_input = latest_mic_transcript or latest_input_text

    recent_speech_actions = (
      (
        await db.execute(
          select(SpeechOutputAction)
          .order_by(SpeechOutputAction.id.desc())
          .limit(6)
        )
      )
      .scalars()
      .all()
    )
    clarification_budget_exhausted = any(
      _is_clarifier_prompt_text(str(row.requested_text or ""))
      for row in recent_speech_actions
    )

    if open_question_summary and open_question:
      urgency = str(open_question.urgency or "").strip().lower()
      priority = str(open_question.priority or "").strip().lower()
      age_seconds = (now - open_question.created_at.astimezone(timezone.utc)).total_seconds() if open_question.created_at else 0.0
      critical_interrupt = urgency in {"critical", "urgent", "emergency"} or priority in {"critical", "urgent", "emergency"}
      elevated_priority = urgency in {"high", "critical", "urgent", "emergency"} or priority in {"high", "critical", "urgent", "emergency"}
      conversational_signal = bool(latest_mic_transcript or learning_summary)
      should_surface_open_question = bool(
        critical_interrupt
        or (not conversational_signal and (elevated_priority or age_seconds <= 1200))
      )

    environment_now = ""
    if unknown_person:
      environment_now = "there is an unidentified person in view"
    elif label_raw:
      environment_now = f"{label_raw} is visible on camera with confidence {confidence:.2f}"
    else:
      environment_now = "camera has no clear person in view"

    needs_identity_prompt = bool(unknown_person and not goal_summary and not (open_question_summary and should_surface_open_question))

    inquiry_prompt = ""
    if open_question_summary and should_surface_open_question:
      inquiry_prompt = f"If you want me to continue this workflow, I need one decision: {open_question_summary}"
    elif needs_identity_prompt:
      inquiry_prompt = "I can see someone nearby. What should I call you?"
    else:
      inquiry_prompt = _build_curiosity_prompt(
          environment_now=environment_now,
          goal_summary=goal_summary,
          memory_summary=memory_summary,
            latest_mic_transcript=latest_user_input,
          learning_summary=learning_summary,
            clarification_budget_exhausted=clarification_budget_exhausted,
      )

    latest_output_text = _rewrite_state_output_text(
        str(speech_row.requested_text or "") if speech_row else "",
        needs_identity_prompt=needs_identity_prompt,
        open_question_summary=open_question_summary,
        goal_summary=goal_summary,
        latest_mic_transcript=latest_user_input,
      environment_now=environment_now,
      memory_summary=memory_summary,
    )

    return {
        "speaking": speaking,
        "camera_last_label": label_raw,
        "camera_last_confidence": confidence,
        "inquiry_prompt": inquiry_prompt,
      "conversation_context": {
        "environment_now": environment_now,
        "active_goal": goal_summary,
        "open_question": open_question_summary,
        "memory_hint": memory_summary,
        "recent_user_input": latest_user_input,
        "interaction_learning": learning_summary,
        "needs_identity_prompt": needs_identity_prompt,
      },
      "latest_output_action_id": int(speech_row.id) if speech_row else 0,
      "latest_output_text": latest_output_text,
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
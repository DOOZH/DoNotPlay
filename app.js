/* ══════════════════════════════════════════
   STATE
   ══════════════════════════════════════════ */
const S = {
  status: 'init',
  phoneAlertActive: false,
  sessionStart: Date.now(),
  focusMs: 0, distMs: 0, awayMs: 0,
  phaseStart: Date.now(),
  phaseFocusMs: 0, phaseDistMs: 0, phaseAwayMs: 0, phasePhoneMs: 0, curPhaseStreakMs: 0,
  blinks: 0, eyeOpen: true, lastBlink: 0,
  lastFace: Date.now(), lastChange: Date.now(),
  yaw: 0, pitch: 0,
  focusHist: [],
  tlData: Array(29).fill(null),
  curMinStart: Date.now(),
  breakShown: false,
  events: [], camOn: false, lastStateSwitch: 0,
  paused: false,

  /* Calibration */
  calibrated: false,
  calYaw: 0, calPitch: 0, calNoseY: 0, calFaceH: 0, calNeckLength: 0, calEAR: 0, calPostureRatio: 0,
  calibSamples: [], calibSamplesNeck: [], calibSamplesEAR: [], calibSamplesPRatio: [],
  slouchStartMs: 0, drowsyStartMs: 0,
  eyeHistory: [], pitchHistory: [],
  yawnCount: 0, lastYawnTime: 0,
  nodCount: 0, lastNodTime: 0,
  fatigueScore: 0, lastEyeUpdate: 0, lastPitchUpdate: 0,

  /* Posture */
  noseYHistory: [],
  faceHeightHistory: [],
  postureBaseline: null,
  postureBad: false,
  postureBadSince: 0,
  postureAlertShown: false,
  lastPostureSound: 0,
  postureWasGood: true,
  distractedSince: 0,
  lastTickSound: 0,
  lookingAwaySince: 0,

  /* Drowsiness */
  earHistory: [],
  blinkTimestamps: [],
  longBlinkCount: 0,
  blinkStartTime: 0,
  drowsyAlertShown: false,

  /* Streaks */
  curStreakStart: 0,
  curStreakMs: 0,
  bestStreakMs: 0,
  streakCount: 0,
  inStreak: false,

  /* Tab visibility */
  tabVisibleMs: 0, tabHiddenMs: 0, tabSwitches: 0,
  tabLastChange: Date.now(), tabVisible: true,

  /* Audio & Settings */
  soundOn: false,
  goalMin: 120,
  theme: 'dark',

  /* Per-hour focus tracking for heatmap */
  hourFocusMs: {},
};

function applyTheme() {
  document.documentElement.setAttribute('data-theme', S.theme);
  const btn = document.getElementById('themeBtn');
  if (btn) btn.textContent = S.theme === 'dark' ? '主题: 深色' : '主题: 浅色';
}

function toggleTheme() {
  S.theme = S.theme === 'dark' ? 'light' : 'dark';
  applyTheme();
  saveStore();
}

/* ══════════════════════════════════════════
   CONSTANTS
   ══════════════════════════════════════════ */
const AWAY_T = 1500;
const MIRROR_VIDEO = false;
const ROTATE_VIDEO = 0;

const SENS_PRESETS = [
  { name: 'Tight', yaw: 20, pitch: 18, delay: 5000, desc: '1 monitor, strict' },
  { name: 'Normal', yaw: 35, pitch: 30, delay: 8000, desc: '1-2 monitors' },
  { name: 'Wide', yaw: 55, pitch: 40, delay: 10000, desc: '2-3 monitors' },
  { name: 'Very Wide', yaw: 75, pitch: 50, delay: 12000, desc: '3+ monitors / relaxed' },
];
let sensIdx = 1;
let toastTimer = null;

/* ══════════════════════════════════════════
   PERSISTENCE — localStorage
   ══════════════════════════════════════════ */
const STORE_KEY = 'focus_monitor_data';

function loadStore() {
  try {
    const d = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    if (!d.days) d.days = {};
    if (!d.allTimeBestStreak) d.allTimeBestStreak = 0;
    if (!d.goalMin) d.goalMin = 120;
    if (!d.theme) d.theme = 'dark';
    if (!d.pomo) d.pomo = { work: 25, short: 5, long: 15, rounds: 4 };
    return d;
  } catch {
    return { days: {}, allTimeBestStreak: 0, goalMin: 120, sessGoalMin: 60, theme: 'dark', pomo: { work: 25, short: 5, long: 15, rounds: 4 } };
  }
}

function saveStore() {
  const d = loadStore();
  const key = todayKey();
  d.goalMin = parseInt(document.getElementById('goalInput').value) || 120;
  d.sessGoalMin = parseInt(document.getElementById('sessionGoalInput').value) || 60;
  d.theme = document.body.classList.contains('light') ? 'light' : 'dark';
  d.pomo = {
    work: parseInt(document.getElementById('pomoWorkInput').value) || 25,
    short: parseInt(document.getElementById('pomoShortInput').value) || 5,
    long: parseInt(document.getElementById('pomoLongInput').value) || 15,
    rounds: parseInt(document.getElementById('pomoRoundsInput').value) || 4
  };
  if (!d.days[key]) d.days[key] = { focusMs: 0, sessions: 0, bestStreak: 0, hourly: {} };
  const today = d.days[key];
  today.focusMs = S.focusMs;
  if (S.curStreakMs > today.bestStreak) today.bestStreak = S.curStreakMs;
  if (today.bestStreak > d.allTimeBestStreak) d.allTimeBestStreak = today.bestStreak;
  localStorage.setItem(STORE_KEY, JSON.stringify(d));
}

function backfillUI() {
  const d = loadStore();
  const key = todayKey();
  const today = d.days[key] || { focusMs: 0, bestStreak: 0 };
  document.getElementById('goalInput').value = d.goalMin;
  if (document.getElementById('sessionGoalInput')) document.getElementById('sessionGoalInput').value = d.sessGoalMin || 60;
  document.getElementById('pomoWorkInput').value = d.pomo.work;
  document.getElementById('pomoShortInput').value = d.pomo.short;
  document.getElementById('pomoLongInput').value = d.pomo.long;
  document.getElementById('pomoRoundsInput').value = d.pomo.rounds;
  if (d.theme === 'light') document.body.classList.add('light'); else document.body.classList.remove('light');
  S.focusMs = today.focusMs || 0;
  S.bestStreakMs = today.bestStreak || 0;
  if (document.getElementById('allTimeBest')) document.getElementById('allTimeBest').textContent = fmtMs(d.allTimeBestStreak || 0);
  POMO.workMin = d.pomo.work;
  POMO.shortMin = d.pomo.short;
  POMO.longMin = d.pomo.long;
  POMO.rounds = d.pomo.rounds;
  POMO.remainMs = POMO.workMin * 60 * 1000;
  pomoRenderUI();
}

function todayKey() { return new Date().toISOString().slice(0, 10); }

/* ══════════════════════════════════════════
   AUDIO CUES
   ══════════════════════════════════════════ */
const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;

function playTone(freq, duration, type = 'sine', vol = 0.08) {
  if (!S.soundOn) return;
  if (!audioCtx) audioCtx = new AudioCtx();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = type; osc.frequency.value = freq;
  gain.gain.setValueAtTime(vol, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
  osc.connect(gain); gain.connect(audioCtx.destination);
  osc.start(); osc.stop(audioCtx.currentTime + duration);
}

function playFocusIn() { playTone(523.25, 0.1); setTimeout(() => playTone(659.25, 0.15), 120); } // C5 then E5 (柔和悦耳的提示音)
function playDistractedTick() { playTone(300, 0.15, 'triangle', 0.08); setTimeout(() => playTone(250, 0.2, 'triangle', 0.08), 200); } // 温和的双音分心警告
function playAlert() { playTone(800, 0.1, 'square', 0.15); setTimeout(() => playTone(800, 0.1, 'square', 0.15), 120); setTimeout(() => playTone(800, 0.1, 'square', 0.15), 240); } // 刺耳警告连击
function playPostureWarn() { playTone(300, 0.25, 'triangle', 0.1); setTimeout(() => playTone(250, 0.3, 'triangle', 0.08), 200); }
function playPostureGood() { playTone(600, 0.1, 'sine', 0.06); setTimeout(() => playTone(800, 0.1, 'sine', 0.05), 80); }

function playPostureAlert() {
  playTone(300, 0.2, 'triangle', 0.1);
  setTimeout(() => playTone(300, 0.2, 'triangle', 0.1), 300);
}

function playDrowsyAlert() {
  playTone(400, 0.4, 'sine', 0.1);
  setTimeout(() => playTone(500, 0.4, 'sine', 0.1), 400);
  setTimeout(() => playTone(600, 0.4, 'sine', 0.1), 800);
}

/**
 * 播放舒缓的提示音 (正弦波)
 */
function playSoftAlert() {
  if (!S.soundOn) return;
  if (!audioCtx) audioCtx = new AudioCtx();
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'sine';
  osc.frequency.setValueAtTime(440, audioCtx.currentTime); // A4 440Hz
  gain.gain.setValueAtTime(0, audioCtx.currentTime);
  gain.gain.linearRampToValueAtTime(0.1, audioCtx.currentTime + 0.1);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.8);
  osc.connect(gain);
  gain.connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 1.0);
}

function toggleSound() {
  S.soundOn = !S.soundOn;
  document.getElementById('soundBtn').textContent = '声音: ' + (S.soundOn ? '开启' : '关闭');
  document.getElementById('soundBtn').classList.toggle('active', S.soundOn);
  if (S.soundOn) { if (!audioCtx) audioCtx = new AudioCtx(); playTone(700, 0.1, 'sine', 0.04); }
  showToast(S.soundOn ? '音频提示已开启' : '音频提示已关闭', 'info');
}

/* ══════════════════════════════════════════
   CALIBRATION
   ══════════════════════════════════════════ */
let calibTimer = null;
let isCalibrating = false;

function startCalibration() {
  if (!S.camOn) { showToast('Start camera first', 'info'); return; }
  isCalibrating = true;
  S.calibSamples = [];
  S.calibSamplesNeck = [];
  S.calibSamplesEAR = [];
  S.calibSamplesPRatio = [];

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ action: 'calibrate' }));
  }

  const overlay = document.getElementById('calibOverlay');
  overlay.classList.add('on');
  let count = 3;
  document.getElementById('calibCountdown').textContent = count;
  addEv('正在校准，请保持端正坐姿并注视屏幕 3 秒...', 'i');
  showToast('正在校准，请保持端正坐姿并注视屏幕 3 秒...', 'info');

  clearInterval(calibTimer);
  calibTimer = setInterval(() => {
    count--;
    if (count > 0) {
      document.getElementById('calibCountdown').textContent = count;
    } else {
      clearInterval(calibTimer);
      if (S.calibSamples.length > 0) {
        S.calNoseY = S.calibSamples.reduce((sum, s) => sum + s.noseY, 0) / S.calibSamples.length;
        S.calFaceH = S.calibSamples.reduce((sum, s) => sum + s.faceH, 0) / S.calibSamples.length;
      }
      if (S.calibSamplesNeck.length > 0) {
        S.calNeckLength = S.calibSamplesNeck.reduce((sum, val) => sum + val, 0) / S.calibSamplesNeck.length;
      }
      if (S.calibSamplesEAR.length > 0) {
        S.calEAR = S.calibSamplesEAR.reduce((sum, val) => sum + val, 0) / S.calibSamplesEAR.length;
      }
      if (S.calibSamplesPRatio.length > 0) {
        S.calPostureRatio = S.calibSamplesPRatio.reduce((sum, val) => sum + val, 0) / S.calibSamplesPRatio.length;
      }
      isCalibrating = false;
      S.calibrated = true;
      S.calibSamples = [];
      S.calibSamplesNeck = [];
      S.calibSamplesEAR = [];
      S.calibSamplesPRatio = [];
      overlay.classList.remove('on');
      document.getElementById('calibStatus').textContent = '已校准';
      document.getElementById('calibBtn').classList.add('active');
      playTone(800, 0.2, 'sine', 0.1);
    }
  }, 1000);
}

/* ══════════════════════════════════════════
   POMODORO
   ══════════════════════════════════════════ */
const POMO = {
  workMin: 25, shortMin: 5, longMin: 15, rounds: 4,
  state: 'idle', running: false, remainMs: 25 * 60 * 1000, round: 1, completed: 0, lastTick: 0
};

function pomoToggle() {
  if (POMO.state === 'idle') { POMO.state = 'work'; POMO.running = true; POMO.lastTick = Date.now(); }
  else { POMO.running = !POMO.running; if (POMO.running) POMO.lastTick = Date.now(); }
  pomoRenderUI();
}
function pomoReset() {
  POMO.state = 'idle';
  POMO.running = false;
  POMO.round = 1;
  POMO.remainMs = POMO.workMin * 60 * 1000;
  pomoRenderUI();
}
function pomoSkip() { pomoPhaseComplete(); }
function pomoPhaseComplete() {
  playAlert();
  if (POMO.state === 'work') {
    POMO.completed++;
    if (POMO.completed > 0 && POMO.completed % POMO.rounds === 0) {
      POMO.state = 'long';
      POMO.remainMs = POMO.longMin * 60 * 1000;
    } else {
      POMO.state = 'short';
      POMO.remainMs = POMO.shortMin * 60 * 1000;
    }
  } else {
    POMO.state = 'work';
    POMO.round = Math.floor(POMO.completed / POMO.rounds) + 1;
    POMO.remainMs = POMO.workMin * 60 * 1000;
  }
  pomoRenderUI();
}
function pomoTick() {
  if (!POMO.running || POMO.state === 'idle') return;
  const now = Date.now(); const dt = now - POMO.lastTick; POMO.lastTick = now;
  POMO.remainMs -= dt;
  if (POMO.remainMs <= 0) { POMO.remainMs = 0; pomoPhaseComplete(); return; }
  pomoRenderTime();
}
function pomoRenderTime() {
  const m = Math.floor(POMO.remainMs / 60000); const s = Math.floor((POMO.remainMs % 60000) / 1000);
  document.getElementById('pomoTime').textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

  let totalPhaseMs = POMO.workMin * 60 * 1000;
  if (POMO.state === 'short') totalPhaseMs = POMO.shortMin * 60 * 1000;
  else if (POMO.state === 'long') totalPhaseMs = POMO.longMin * 60 * 1000;

  const pct = Math.max(0, Math.min(1, POMO.remainMs / totalPhaseMs));
  const dashoffset = 465 * (1 - pct);
  document.getElementById('pomoArc').style.strokeDashoffset = dashoffset;
}
function pomoRenderUI() {
  const sec = document.getElementById('pomoSec');
  sec.className = `pomo-sec ${POMO.state}`;

  const startBtn = document.getElementById('pomoStartBtn');
  const skipBtn = document.getElementById('pomoSkipBtn');
  const resetBtn = document.getElementById('pomoResetBtn');
  const phaseLabel = document.getElementById('pomoPhase');

  if (POMO.state === 'idle') {
    startBtn.textContent = '开始专注';
    skipBtn.style.display = 'none';
    resetBtn.style.display = 'none';
    phaseLabel.textContent = '准备就绪';
  } else {
    startBtn.textContent = POMO.running ? '暂停' : '继续';
    skipBtn.style.display = 'inline-flex';
    resetBtn.style.display = 'inline-flex';

    if (POMO.state === 'work') phaseLabel.textContent = '工作中';
    else if (POMO.state === 'short') phaseLabel.textContent = '短休息';
    else if (POMO.state === 'long') phaseLabel.textContent = '长休息';
  }

  pomoRenderTime();
  document.getElementById('pomoCycle').textContent = `${(POMO.completed % POMO.rounds) + 1} / ${POMO.rounds}`;
  document.getElementById('pomoCompleted').textContent = POMO.completed;
}

/* ══════════════════════════════════════════
   LOGS & UI
   ══════════════════════════════════════════ */
function addEv(msg, type = 'i') {
  const list = document.getElementById('evList');
  if (!list) return;
  const t = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const el = document.createElement('div');
  el.className = 'ev';
  el.innerHTML = `<span class="ev-t">${t}</span><span class="ev-${type}">${msg}</span>`;
  list.prepend(el);
  if (list.children.length > 50) list.lastChild.remove();
}

function updatePose(yaw, pitch) {
  const yBar = document.getElementById('yBar');
  const pBar = document.getElementById('pBar');
  if (yBar) { yBar.style.left = '50%'; yBar.style.width = Math.min(Math.abs(yaw), 50) + '%'; yBar.style.transform = yaw > 0 ? 'scaleX(1)' : 'scaleX(-1)'; yBar.style.transformOrigin = 'left'; }
  if (pBar) { pBar.style.left = '50%'; pBar.style.width = Math.min(Math.abs(pitch), 50) + '%'; pBar.style.transform = pitch > 0 ? 'scaleX(1)' : 'scaleX(-1)'; pBar.style.transformOrigin = 'left'; }
  const yZone = document.getElementById('yZone');
  if (yZone) yZone.textContent = Math.abs(yaw) < 35 ? '注视屏幕' : (yaw > 0 ? '向右看' : '向左看');
  const pZone = document.getElementById('pZone');
  if (pZone) pZone.textContent = Math.abs(pitch) < 30 ? '注视屏幕' : (pitch > 0 ? '向下看' : '向上看');
}

function updatePosture(noseY, faceH) {
  if (!S.calibrated) return;
  const drift = noseY - S.calNoseY;
  const tag = document.getElementById('postureTag');
  if (drift > 0.08) {
    if (tag) { tag.textContent = '驼背'; tag.className = 'posture-indicator bad'; tag.style.display = 'block'; }
  } else {
    if (tag) { tag.textContent = '良好姿势'; tag.className = 'posture-indicator'; tag.style.display = 'block'; }
  }
}

function updateHeatmap() {
  const container = document.getElementById('heatmap');
  if (!container) return;
  const d = loadStore();
  const days = d.days || {};
  let html = '';
  // 生成过去 28 天的数据块
  for (let i = 27; i >= 0; i--) {
    const date = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    const focusMs = days[date] ? days[date].focusMs : 0;
    const hours = focusMs / 3600000;

    let bg = 'var(--bg3)'; // 默认底色
    if (hours >= 4) bg = 'var(--green)';
    else if (hours >= 2) bg = 'var(--green-d)';
    else if (hours > 0.1) bg = 'rgba(129, 201, 149, 0.5)';

    html += `<div class="hm-cell" style="background:${bg}; border-radius: 4px; border: 1px solid var(--border)" title="${date}: 专注 ${hours.toFixed(1)} 小时"></div>`;
  }
  container.innerHTML = html;
}

function updateTimeline(now) {
  if (now - S.curMinStart >= 60000) {
    S.curMinStart = now;
    // 取当前状态作为这一分钟的代表状态
    S.tlData.shift();
    S.tlData.push(S.status);
    renderTimeline();
  }
}

function renderTimeline() {
  const container = document.getElementById('tlBars');
  if (!container) return;
  container.innerHTML = '';
  S.tlData.forEach(st => {
    const el = document.createElement('div');
    el.className = 'tl-bar';
    if (st === 'active') { el.style.background = 'var(--green)'; el.style.height = '100%'; }
    else if (st === 'distracted' || st === 'phone') { el.style.background = 'var(--amber)'; el.style.height = '60%'; }
    else if (st === 'away') { el.style.background = 'var(--red-d)'; el.style.height = '30%'; }
    else { el.style.background = 'transparent'; }
    container.appendChild(el);
  });
}


function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  clearTimeout(toastTimer);
  el.textContent = msg; el.className = 'toast ' + type + ' show';
  toastTimer = setTimeout(() => el.classList.remove('show'), 2000);
}

function setStatus(st) {
  if (S.status === st) return;
  S.status = st;
  const cfg = {
    init: { bg: 'var(--bg3)', tc: 'var(--muted)', lbl: 'INIT' },
    active: { bg: 'var(--green-bg)', tc: 'var(--green)', lbl: '专注中' },
    distracted: { bg: 'var(--amber-bg)', tc: 'var(--amber)', lbl: '分心' },
    away: { bg: 'var(--red-bg)', tc: 'var(--red-d)', lbl: '离开' },
    phone: { bg: 'var(--purple-bg)', tc: 'var(--purple)', lbl: '玩手机!' },
  };
  const c = cfg[st] || cfg.init;
  const pill = document.getElementById('sPill');
  pill.style.background = c.bg; pill.style.color = c.tc;
  document.getElementById('sTxt').textContent = c.lbl;
  document.getElementById('sBadge').textContent = c.lbl;
}

function fmtMs(ms) {
  const s = Math.floor(ms / 1000); const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}
function fmtMsShort(ms) { return Math.floor(ms / 1000) + 's'; }

function fmtMsLong(ms) {
  const totS = Math.floor(ms / 1000);
  const h = Math.floor(totS / 3600);
  const m = Math.floor((totS % 3600) / 60);
  const s = totS % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function resetPhase() {
  S.phaseStart = Date.now();
  S.phaseFocusMs = 0;
  S.phaseDistMs = 0;
  S.phaseAwayMs = 0;
  S.phasePhoneMs = 0;
  S.curPhaseStreakMs = 0;
  S.fatigueScore = 0;
  S.nodCount = 0;
  S.yawnCount = 0;
  S.eyeHistory = [];
  S.pitchHistory = [];
  addEv('已开启新阶段', 'g');
  showToast('新阶段已开启', 'info');
}

/* ══════════════════════════════════════════
   WebSocket & Communication
   ══════════════════════════════════════════ */
let _ws = null;
function connectWebSocket() {
  if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) return;
  _ws = new WebSocket('ws://localhost:8765');
  _ws.onopen = () => {
    addEv('已连接到后端', 'g'); S.camOn = true; setStatus('active'); sendSensUpdate();
  };
  _ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === 'config_sync') {
        sensIdx = data.sens_idx;
        document.getElementById('sensBtn').textContent = '灵敏度: ' + SENS_PRESETS[sensIdx].name;
      } else { handleBackendData(data); }
    } catch (e) { }
  };
  _ws.onclose = () => { S.camOn = false; setTimeout(connectWebSocket, 5000); };
}

function sendSensUpdate() {
  if (_ws && _ws.readyState === WebSocket.OPEN) _ws.send(JSON.stringify({ cmd: 'set_sensitivity', idx: sensIdx }));
}

function tick() {
  const now = Date.now();
  document.getElementById('clk').textContent = new Date().toLocaleTimeString();

  const phaseElapsed = now - S.phaseStart;
  if (document.getElementById('phaseElapsed')) document.getElementById('phaseElapsed').textContent = fmtMsLong(phaseElapsed);
  if (document.getElementById('phaseFocus')) document.getElementById('phaseFocus').textContent = fmtMsLong(S.phaseFocusMs);
  if (document.getElementById('curPhaseStreak')) document.getElementById('curPhaseStreak').textContent = fmtMsLong(S.curPhaseStreakMs);
  if (document.getElementById('phasePhone')) document.getElementById('phasePhone').textContent = fmtMsLong(S.phasePhoneMs);
  if (document.getElementById('phaseDist')) document.getElementById('phaseDist').textContent = fmtMsLong(S.phaseDistMs);
  if (document.getElementById('phaseAway')) document.getElementById('phaseAway').textContent = fmtMsLong(S.phaseAwayMs);

  const mS = document.getElementById('mSession'); if (mS) mS.textContent = fmtMs(now - S.sessionStart);
  const mF = document.getElementById('mFocus'); if (mF) mF.textContent = fmtMs(S.focusMs);
  const goalMin = parseInt(document.getElementById('goalInput').value) || 120;
  const curMin = S.focusMs / 60000;
  const pct = Math.min(Math.round((curMin / goalMin) * 100), 100);
  document.getElementById('goalArc').style.strokeDashoffset = 163.36 * (1 - pct / 100);
  document.getElementById('goalPct').textContent = pct + '%';
  document.getElementById('goalProgress').textContent = `${Math.floor(curMin)}m / ${goalMin}m 专注`;

  // --- Session Goal Logic ---
  const sessGoalMin = parseInt(document.getElementById('sessionGoalInput').value) || 60;
  const sessCurMin = S.phaseFocusMs / 60000;
  const sessPct = Math.min(Math.round((sessCurMin / sessGoalMin) * 100), 100);
  if (document.getElementById('sessionGoalArc')) document.getElementById('sessionGoalArc').style.strokeDashoffset = 163.36 * (1 - sessPct / 100);
  if (document.getElementById('sessionGoalPct')) document.getElementById('sessionGoalPct').textContent = sessPct + '%';
  if (document.getElementById('sessionGoalProgress')) document.getElementById('sessionGoalProgress').textContent = `${Math.floor(sessCurMin)}m / ${sessGoalMin}m 专注`;

  pomoTick();

  // 1. 计算会话平均专注分数
  const totalActive = S.focusMs + S.distMs + S.awayMs;
  const fPct = totalActive > 0 ? Math.round((S.focusMs / totalActive) * 100) : 0;
  const fScoreEl = document.getElementById('fScore');
  const fFillEl = document.getElementById('fFill');
  if (fScoreEl) fScoreEl.textContent = fPct + '%';
  if (fFillEl) fFillEl.style.width = fPct + '%';

  // 2. 计算最近 5 分钟的专注分数
  const fiveMinsAgo = now - 5 * 60 * 1000;
  const recentHist = S.focusHist.filter(x => x.t > fiveMinsAgo);
  const rPct = recentHist.length > 0 ? Math.round((recentHist.filter(x => x.f).length / recentHist.length) * 100) : 0;
  const rScoreEl = document.getElementById('rScore');
  const rFillEl = document.getElementById('rFill');
  if (rScoreEl) rScoreEl.textContent = rPct + '%';
  if (rFillEl) rFillEl.style.width = rPct + '%';

  // 3. 更新今日会话数
  const d = loadStore();
  const key = todayKey();
  const todaySessions = (d.days && d.days[key]) ? (d.days[key].sessions || 1) : 1;
  const goalTodayEl = document.getElementById('goalToday');
  if (goalTodayEl) goalTodayEl.textContent = `今日会话: ${todaySessions}`;

  // 4. 驱动时间线更新
  updateTimeline(now);

  // 5. 检测人脸存在 (离开预警逻辑)
  checkUserPresence(now);
}

/**
 * 人脸离开预警实时检测
 */
function checkUserPresence(now) {
  if (S.paused || !S.camOn) return;

  // 超过阈值检测不到人脸触发预警
  const isAway = (now - S.lastFace > AWAY_T);

  if (isAway && !S.phoneAlertActive) {
    // 触发弹窗
    if (!S.awayAlertActive) {
      document.getElementById('awayWarningPopup').classList.add('show');
      S.awayAlertActive = true;
      addEv('离开预警 — 请返回办公桌', 'b');
    }
    // 每 2 秒播放一次舒缓提示音
    if (!S.lastSoftAlert || now - S.lastSoftAlert > 2000) {
      playSoftAlert();
      S.lastSoftAlert = now;
    }
  } else {
    // 检测到人脸回归或手动关闭
    if (S.awayAlertActive) {
      document.getElementById('awayWarningPopup').classList.remove('show');
      S.awayAlertActive = false;
      addEv('已检测到人脸，预警解除', 'g');
    }
  }
}

function handleBackendData(data) {

  if (S.paused) return;

  // 核心逻辑：只要收到了来自后端的数据流，就隐藏“未连接”提示
  const noCamEl = document.getElementById('noCam');
  if (noCamEl) noCamEl.style.display = 'none';

  const now = Date.now();
  const {
    status, yaw, pitch, blinks, ear, noseY, faceH, faceDetected, handsVisible,
    phoneAlertActive, neckLength, mar, postureRatio, leftShoulderY, rightShoulderY,
    neck_length, face_height, shoulder_diff
  } = data;

  if (S.lastChange === 0) S.lastChange = now;

  const personVisible = faceDetected || handsVisible;

  // 只要检测到人脸或人手，就更新最后可见时间
  if (personVisible) {
    S.lastFace = now;
  }

  if (faceDetected) {
    if (isCalibrating) {
      S.calibSamples.push({ yaw, pitch, noseY, faceH });
      if (neckLength) S.calibSamplesNeck.push(neckLength);
      if (ear) S.calibSamplesEAR.push(ear / 100);
      if (postureRatio) S.calibSamplesPRatio.push(postureRatio);
    }

    const dt = now - S.lastChange;
    S.lastChange = now;

    // 1. 专注时长与连续专注逻辑重构 (v0.3)
    if (status === 'active') {
      if (S.status !== 'active') {
        playFocusIn();
        addEv('开始专注', 'g');
        S.inStreak = true;
        S.curStreakStart = now;
      }
      S.focusMs += dt;
      S.phaseFocusMs += dt;
      S.curPhaseStreakMs += dt;
      if (S.curPhaseStreakMs > S.bestStreakMs) S.bestStreakMs = S.curPhaseStreakMs;
      saveStore();
    } else {
      if (S.status === 'active') {
        addEv(status === 'phone' ? '检测到手机' : (status === 'away' ? '人脸离开' : '注意力分散'), 'w');
      }
      S.curPhaseStreakMs = 0;   // 状态异常立即清零
      S.inStreak = false;

      if (status === 'phone') S.phasePhoneMs += dt;
      else if (status === 'distracted') S.phaseDistMs += dt;
      else if (status === 'away') S.phaseAwayMs += dt;
    }

    // 2. 眨眼率计算 (前端 60s 窗口)
    if (blinks > S.blinks) {
      S.blinkTimestamps.push(now);
      S.blinks = blinks;
    }
    S.blinkTimestamps = S.blinkTimestamps.filter(t => now - t <= 60000);

    // [New] 瞬目率更新
    const blinkRateEl = document.getElementById('cBlinkRate');
    if (blinkRateEl) blinkRateEl.innerText = S.blinkTimestamps.length + ' 次/分';

    // 记录历史供计算近期分数
    S.focusHist.push({ t: now, f: (status === 'active') });
    if (S.focusHist.length > 600) S.focusHist.shift();

    setStatus(status);
    updatePosture(noseY, faceH);

    // [New] 颈椎负荷更新
    const neckStrainEl = document.getElementById('cNeckStrain');
    if (neckStrainEl) {
      if (S.calNeckLength > 0 && neck_length !== undefined && neck_length > 0) {
        let strain = (1 - neck_length / S.calNeckLength) * 100;
        if (strain < 10) neckStrainEl.innerText = '正常';
        else if (strain <= 25) neckStrainEl.innerText = '轻度前倾';
        else neckStrainEl.innerText = '重度压迫';
      } else {
        neckStrainEl.innerText = '请先校准';
      }
    }

    // [New] 视线状态更新
    const gazeStateEl = document.getElementById('cGazeState');
    if (gazeStateEl) {
      if (Math.abs(yaw) < 15 && Math.abs(pitch) < 15) {
        gazeStateEl.innerText = '专注屏幕';
      } else {
        gazeStateEl.innerText = '注意力游离';
      }
    }

    // [New] 肩颈平衡更新
    const shoulderSymEl = document.getElementById('cShoulderSymmetry');
    if (shoulderSymEl) {
      if (shoulder_diff !== undefined && neck_length > 0) {
        if (shoulder_diff < 0.05) shoulderSymEl.innerText = '端正';
        else shoulderSymEl.innerText = '侧倾警告';
      } else {
        shoulderSymEl.innerText = '-';
      }
    }

    // Streaks
    if (status === 'active') {
      if (!S.inStreak) { S.inStreak = true; S.curStreakStart = now; }
      S.curStreakMs = now - S.curStreakStart;
      if (S.curStreakMs > S.bestStreakMs) { S.bestStreakMs = S.curStreakMs; saveStore(); }
    } else { S.inStreak = false; S.curStreakMs = 0; }
    document.getElementById('curStreak').textContent = fmtMs(S.curStreakMs);
    document.getElementById('bestStreak').textContent = fmtMs(S.bestStreakMs);
    if (document.getElementById('allTimeBest')) {
      const d = loadStore();
      document.getElementById('allTimeBest').textContent = fmtMs(d.allTimeBestStreak || 0);
    }

    // Drowsiness (综合疲劳预警)
    const EAR_THRESH = S.calibrated && S.calEAR > 0 ? (S.calEAR * 0.7) : 0.2;

    // 1. 闭眼频率 (PERCLOS)
    if (now - S.lastEyeUpdate > 1000) {
      S.eyeHistory.push((ear / 100) < EAR_THRESH ? 1 : 0);
      if (S.eyeHistory.length > 60) S.eyeHistory.shift();
      S.lastEyeUpdate = now;

      // [New] 闭眼率更新
      const closedCount = S.eyeHistory.filter(x => x === 1).length;
      const perclosRatio = Math.round((closedCount / S.eyeHistory.length) * 100) || 0;
      const perclosEl = document.getElementById('cPerclos');
      if (perclosEl) {
        perclosEl.innerText = perclosRatio + '%';
        if (perclosRatio > 20) {
          perclosEl.style.color = 'var(--red-d)';
        } else {
          perclosEl.style.color = '';
        }
      }
    }

    // 2. 打哈欠检测
    if (mar > 0.5 && now - S.lastYawnTime > 5000) {
      S.yawnCount += 1;
      S.lastYawnTime = now;
      addEv('检测到打哈欠', 'w');
    }

    // 3. 点头检测
    if (now - S.lastPitchUpdate > 500) {
      S.pitchHistory.push({ t: now, p: pitch });
      if (S.pitchHistory.length > 20) S.pitchHistory.shift();
      S.lastPitchUpdate = now;

      if (S.pitchHistory.length >= 4) {
        const oldP = S.pitchHistory[S.pitchHistory.length - 4].p;
        if (pitch - oldP > 15 && now - S.lastNodTime > 5000) {
          S.nodCount += 1;
          S.lastNodTime = now;
          addEv('检测到打瞌睡点头', 'w');
        }
      }
    }

    // 4. 计算综合疲劳分数
    let newScore = 0;

    // PERCLOS 得分
    if (S.eyeHistory.length >= 30) {
      const closedCount = S.eyeHistory.filter(x => x === 1).length;
      const closedRatio = closedCount / S.eyeHistory.length;
      if (closedRatio > 0.3) newScore += 60;
      else if (closedRatio > 0.2) newScore += 40;
    }

    // 清理过期的哈欠和点头
    if (now - S.lastYawnTime > 60000) S.yawnCount = 0;
    if (now - S.lastNodTime > 60000) S.nodCount = 0;

    newScore += (S.yawnCount * 20);
    newScore += (S.nodCount * 30);

    // 平滑衰减
    if (newScore > S.fatigueScore) S.fatigueScore = newScore;
    else S.fatigueScore = Math.max(0, S.fatigueScore - 0.5);

    // UI 更新
    const drowsyFillEl = document.getElementById('drowsyFill');
    if (drowsyFillEl) drowsyFillEl.style.width = Math.min(S.fatigueScore, 100) + '%';
    const alertScoreEl = document.getElementById('alertScore');
    if (alertScoreEl) alertScoreEl.textContent = Math.round(S.fatigueScore) + '分';

    // 触发疲劳警告
    if (S.fatigueScore > 60) {
      if (!S.drowsyWarningActive) {
        S.drowsyWarningActive = true;
        document.getElementById('drowsyWarningPopup').classList.add('show');
        playDrowsyAlert();
      }
    } else {
      if (S.drowsyWarningActive && S.fatigueScore < 20) {
        S.drowsyWarningActive = false;
        document.getElementById('drowsyWarningPopup').classList.remove('show');
      }
    }

    // Posture Slouch Check & 驼背防抖与声音触发逻辑
    if (S.calibrated) {
      const drift = Math.abs(noseY - S.calNoseY);
      const tag = document.getElementById('postureTag');
      if (drift > 0.1) {
        tag.className = 'posture-indicator bad'; tag.textContent = '姿势不正';
      } else { tag.className = 'posture-indicator'; tag.textContent = '姿势良好'; }

      if (S.calPostureRatio > 0 && postureRatio) {
        if (postureRatio < S.calPostureRatio * 0.85) {
          if (S.slouchStartMs === 0) S.slouchStartMs = now;
          if (now - S.slouchStartMs > 5000) {
            if (!S.postureWarningActive) {
              S.postureWarningActive = true;
              document.getElementById('postureWarningPopup').classList.add('show');
              playPostureAlert();
            }
          }
        } else {
          S.slouchStartMs = 0;
          if (S.postureWarningActive) {
            S.postureWarningActive = false;
            document.getElementById('postureWarningPopup').classList.remove('show');
          }
        }
      }
    }
  } else {
    // 如果没有检测到人脸，且超时没有检测到任何人(包括手)，则判定为离开
    if (now - S.lastFace > AWAY_T && S.status !== 'away') { setStatus('away'); addEv('检测不到人型', 'b'); }
    if (S.status === 'away') {
      const dtAway = now - S.lastChange;
      S.awayMs += dtAway;
      S.phaseAwayMs += dtAway;
      S.curPhaseStreakMs = 0;
      S.focusHist.push({ t: now, f: false });
      if (S.focusHist.length > 600) S.focusHist.shift();
    }
  }
  S.lastChange = now;

  // =========== (Legacy MD3 Stats Update Removed) ===========

  // ══════════════════════════════════════════
  // 全局弹窗控制 (基于最终的 S.status 和 phoneAlertActive)
  // ══════════════════════════════════════════

  // 1. 玩手机：优先级最高，最激烈的警报 (每秒一次)
  if (phoneAlertActive) {
    if (!S.phoneAlertActive) { triggerDoNotPlay(); }
    if (!S.lastPhoneSound) S.lastPhoneSound = 0;
    if (now - S.lastPhoneSound > 1000) {
      playAlert();
      S.lastPhoneSound = now;
    }
  } else {
    if (S.phoneAlertActive) { dismissDoNotPlay(); }
  }

  // 2. 分心弹窗：当状态为 distracted 且没在玩手机时显示
  if (S.status === 'distracted' && !phoneAlertActive) {
    if (!S.distractedAlertActive) { triggerDistractedWarning(); S.distractedAlertActive = true; }
  } else {
    if (S.distractedAlertActive) { dismissDistractedWarning(); S.distractedAlertActive = false; }
  }

  // 3. 声音：分心警告音 (离开提示音已在 checkUserPresence 中处理)
  if (S.status === 'distracted' && !phoneAlertActive) {
    if (!S.lastTickSound) S.lastTickSound = 0;
    if (now - S.lastTickSound > 2000) {
      playDistractedTick();
      S.lastTickSound = now;
    }
  }
}

function triggerDoNotPlay() { S.phoneAlertActive = true; document.getElementById('doNotPlayPopup').classList.add('show'); }
function dismissDoNotPlay() { S.phoneAlertActive = false; document.getElementById('doNotPlayPopup').classList.remove('show'); }

function triggerDistractedWarning() { document.getElementById('distractedWarningPopup').classList.add('show'); }
function dismissDistractedWarning() { document.getElementById('distractedWarningPopup').classList.remove('show'); S.distractedAlertActive = false; }

function triggerAwayWarning() { document.getElementById('awayWarningPopup').classList.add('show'); }
function dismissAwayWarning() { document.getElementById('awayWarningPopup').classList.remove('show'); S.awayAlertActive = false; }

function initCam() {
  const vidStream = document.getElementById('vid_stream');
  if (vidStream) {
    // 增加时间戳防止浏览器缓存“加载失败”状态
    vidStream.src = 'http://localhost:5001/video_feed?t=' + Date.now();
    vidStream.onerror = () => {
      console.warn('Video stream failed, retrying in 3s...');
      document.getElementById('noCam').style.display = 'flex';
      setTimeout(initCam, 3000); // 3秒后尝试自动重连
    };
  }
  connectWebSocket();
}

/* ══════════════════════════════════════════
   BOOT
   ══════════════════════════════════════════ */
window.addEventListener('load', () => {
  backfillUI();
  updateHeatmap();
  initCam();
  setInterval(tick, 1000);
});

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('newPhaseBtn')?.addEventListener('click', resetPhase);
  document.getElementById('sensBtn').addEventListener('click', () => {
    sensIdx = (sensIdx + 1) % SENS_PRESETS.length;
    document.getElementById('sensBtn').textContent = '灵敏度: ' + SENS_PRESETS[sensIdx].name;
    sendSensUpdate();
  });
  document.getElementById('calibBtn').addEventListener('click', startCalibration);
  document.getElementById('soundBtn').addEventListener('click', toggleSound);
  document.getElementById('themeBtn').addEventListener('click', toggleTheme);
  document.getElementById('pomoStartBtn').addEventListener('click', pomoToggle);
  document.getElementById('pomoSkipBtn').addEventListener('click', pomoSkip);
  document.getElementById('pomoResetBtn').addEventListener('click', pomoReset);
  document.getElementById('doNotPlayDismissBtn').addEventListener('click', dismissDoNotPlay);
  document.getElementById('distractedDismissBtn')?.addEventListener('click', dismissDistractedWarning);
  document.getElementById('awayDismissBtn')?.addEventListener('click', dismissAwayWarning);

  document.getElementById('brkDismissBtn')?.addEventListener('click', () => document.getElementById('brkBanner').classList.remove('on'));

  document.getElementById('drowsyDismissBtn')?.addEventListener('click', () => {
    document.getElementById('drowsyWarningPopup').classList.remove('show');
    S.drowsyWarningActive = false;
    S.fatigueScore = 0;
    S.nodCount = 0;
    S.yawnCount = 0;
    S.eyeHistory = [];
    S.pitchHistory = [];
  });

  document.getElementById('postureDismissBtn')?.addEventListener('click', () => {
    document.getElementById('postureWarningPopup').classList.remove('show');
    S.postureWarningActive = false;
    S.slouchStartMs = 0;
  });

  ['pomoWorkInput', 'pomoShortInput', 'pomoLongInput', 'pomoRoundsInput', 'goalInput', 'sessionGoalInput'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      saveStore();
      if (id.startsWith('pomo')) {
        POMO.workMin = parseInt(document.getElementById('pomoWorkInput').value) || 25;
        POMO.shortMin = parseInt(document.getElementById('pomoShortInput').value) || 5;
        POMO.longMin = parseInt(document.getElementById('pomoLongInput').value) || 15;
        POMO.rounds = parseInt(document.getElementById('pomoRoundsInput').value) || 4;
        if (POMO.state === 'idle') {
          POMO.remainMs = POMO.workMin * 60 * 1000;
        }
        pomoRenderUI();
      }
    });
  });

  applyTheme();
});

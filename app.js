/* ════════════════════════════════════════════════════════════════════
   ARCHITECTURE OVERVIEW - 最终统一架构 (2026.04 Final)
   ════════════════════════════════════════════════════════════════════
   
   1. 配置 (Config)：/api/load-config (加载) & /api/save-config (持久化)
      - 处理：主题、声音、灵敏度、校准基数、摄像头索引。
   2. 业务数据 (Data)：/api/load-data (初始化) & /api/save-data (定时 30s)
      - 处理：days, top3, hour_focus_ms, all_time_best_streak, events。
   3. 唯一来源：前端内存状态 S 对象。

   废弃机制说明：
   - 已彻底移除 localStorage (不再读写)。
   - 已移除 loadStore(), saveStore(), syncToBackend() 等过渡期函数。
   
   ════════════════════════════════════════════════════════════════════ */

// 自动捕获未捕获的致命异常
window.addEventListener('error', function (e) {
  console.error(`[FATAL ERROR] ${e.message} at ${e.filename}:${e.lineno}`);
});
window.addEventListener('unhandledrejection', function (e) {
  console.error(`[PROMISE ERROR] ${e.reason}`);
});

function toggleTheme() {
  if (S.theme === 'auto') S.theme = 'dark';
  else if (S.theme === 'dark') S.theme = 'light';
  else S.theme = 'auto';
  applyTheme();
  // 同步配置到后端
  saveConfigToBackend();
}

async function refreshApp() {
  // 记录当前的连续专注时间（如果有）
  if (S.inStreak && S.curPhaseStreakMs > 5000) {
    recordStreakInterruption(S.curPhaseStreakMs, S.curStreakStart, Date.now());
  }

  showToast('🔄 正在同步数据并重置系统状态...', 'info');
  
  // —— [同步核心] 在重启前确保所有数据已安全落盘 ——
  try {
    syncDataToBackend(); 
    saveConfigToBackend();
    console.log('[REFRESH] 数据同步请求已发出');
  } catch(e) {
    console.warn('[REFRESH] 同步阶段发生非致命错误:', e);
  }

  // 给同步网络请求留出一点点喘息时间（500ms）
  await new Promise(r => setTimeout(r, 500));

  // 发送“全刷新”命令给后端，触发进程重启以加载服务端代码更改
  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ cmd: 'refresh' }));
    console.log('[REFRESH] 已向发送后端重启指令 (cmd: refresh)');
    
    // 给后端接收指令并开始重启的时间，然后前端重载代码
    setTimeout(() => {
      window.location.reload();
    }, 1200);
  } else {
    // 如果 WS 断开了，直接强制本地刷新
    console.warn('[REFRESH] WebSocket 已断开，直接执行本地重载');
    window.location.reload();
  }
}

// Listen for system theme changes
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  if (S.theme === 'auto') {
    applyTheme();
  }
});

/* ====================================================================
   CONSTANTS
   ==================================================================== */
const AWAY_T = 15000;
const MIRROR_VIDEO = false;
const ROTATE_VIDEO = 0;

const SENS_PRESETS = [
  { name: '严格', yaw: 25, pitch: 18, delay: 2000, desc: '极速响应，2秒离席' },
  { name: '正常', yaw: 45, pitch: 30, delay: 2000, desc: '2秒离席判定' },
  { name: '宽容', yaw: 65, pitch: 40, delay: 3000, desc: '3秒离席判定' },
  { name: '放纵', yaw: 85, pitch: 50, delay: 5000, desc: '5秒离席判定' },
];
let sensIdx = 1;
let toastTimer = null;

/* ====================================================================
   PERSISTENCE - Backend-Driven Only
   ==================================================================== */

// 全局状态对象 — 单一数据源 (Single Source of Truth)
const S = {
  // 启动控制
  booted: false,
  camOn: false,
  paused: false,
  status: 'active',
  theme: 'auto',
  soundOn: false,

  // 时间戳
  sessionStart: Date.now(),
  phaseStart: Date.now(),
  lastChange: 0,
  lastWsMsgTime: 0,
  lastFace: Date.now(),
  warmupUntil: 0,
  curMinStart: Date.now(),
  curStreakStart: 0,

  // 累计时间 (毫秒)
  focusMs: 0,
  distMs: 0,
  awayMs: 0,
  phoneMs: 0,
  phaseFocusMs: 0,
  phaseDistMs: 0,
  phaseAwayMs: 0,
  phasePhoneMs: 0,
  curPhaseStreakMs: 0,
  bestStreakMs: 0,
  allTimeBestStreak: 0,

  // 专注连续记录
  inStreak: false,
  interruptionStartMs: 0,
  distractedSince: 0,

  // 弹窗状态
  phoneAlertActive: false,
  awayAlertActive: false,
  breakShown: false,
  waterAlertShown: false,
  top3Celebrated: false,

  // 弹窗冷却
  _phoneDismissUntil: 0,
  _distractedDismissUntil: 0,
  _postureDismissUntil: 0,
  _drowsyDismissUntil: 0,
  _fatigueDisabledForSession: false,
  _slouchSince: 0,

  // 实时传感器数据
  eyeFatigue: 0,
  gazeAngle: { yaw: 0, pitch: 0, roll: 0 },
  neckStrain: null,
  shoulderSymmetry: null,
  hydrationMinutes: -1,
  lastDrinkTime: 0,
  curBlinkRate: 0,
  _lastDrinkingNow: false,

  // 传感器更新时间
  lastFatigueUpdate: 0,
  lastGazeUpdate: 0,
  lastNeckUpdate: 0,
  lastShoulderUpdate: 0,
  lastHydrationUpdate: 0,
  lastMetricUIUpdate: 0,

  // 历史缓冲
  gazeHistory: [],
  neckHistory: [],
  shoulderHistory: [],
  focusHist: [],
  minBucket: [],
  tlData: [],

  // 校准
  calibrated: false,
  calibSamples: [],
  calibSamplesNeck: [],
  calibSamplesEAR: [],
  calibSamplesPRatio: [],
  calYaw: 0,
  calPitch: 0,
  calEAR: 0.28,
  calNeckLength: 0,
  calNoseY: 0,

  // 摄像头
  cameraIndex: 0,
  cameraRotation: 0,
  cameraFlip: true,

  // 持久化数据
  days: {},
  top3: [],
  hourFocusMs: {},
  events: [],

  // 指标卡片持久化
  metricPersistence: {},

  // 日志辅助
  lastLoggedStatus: '',
  _lastDiagLog: 0,
};
function getSystemTheme() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * 将配置数据持久化到后端
 * 包含：主题、声音、灵敏度、手动指定的摄像头索引、校准基准值
 */
function saveConfigToBackend() {
  const configPayload = {
    theme: S.theme || 'auto',
    sound_on: S.soundOn || false,
    sens_idx: sensIdx !== undefined ? sensIdx : 1,
    camera_index: S.cameraIndex || 0,
    camera_rotation: S.cameraRotation || 0,
    camera_flip: S.cameraFlip !== undefined ? S.cameraFlip : true,
    // 校准数据
    baseline_yaw: S.calYaw || 0,
    baseline_pitch: S.calPitch || 0,
    baseline_ear: S.calEAR || 0.28,
    baseline_neck_length: S.calNeckLength || 0
  };

  fetch('/api/save-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(configPayload)
  })
    .then(res => {
      if (res.ok) console.log('[CONFIG] 成功保存配置到后端');
      else console.warn('[CONFIG] 保存配置到后端失败:', res.status);
    })
    .catch(err => console.warn('[CONFIG] 保存配置网络错误:', err));
}

/**
 * 从后端加载配置和业务数据
 * 用于页面初始化时恢复所有数据
 */
async function loadFromBackend() {
  console.log('[API] 🚀 开始从后端加载配置和业务数据...');
  try {
    // 1. 加载配置
    console.log('[API-REQ] 正在请求配置信息: /api/load-config');
    const cfgRes = await fetch('/api/load-config');
    console.log(`[API-RES] 配置请求响应状态: ${cfgRes.status} ${cfgRes.statusText}`);

    if (cfgRes.ok) {
      const cfg = await cfgRes.json();
      console.log('[API-DATA] 配置内容解析成功');
      if (cfg) {
        // 更新全局状态
        S.theme = cfg.theme || 'auto';
        S.soundOn = cfg.sound_on || false;
        sensIdx = cfg.sens_idx !== undefined ? cfg.sens_idx : 1;
        S.cameraIndex = cfg.camera_index || 0;
        S.cameraRotation = cfg.camera_rotation || 0;
        S.cameraFlip = cfg.camera_flip !== undefined ? cfg.camera_flip : true;

        // 更新校准数据
        if (cfg.baseline_yaw !== undefined) {
          S.calYaw = cfg.baseline_yaw;
          S.calPitch = cfg.baseline_pitch;
          S.calEAR = cfg.baseline_ear || 0.28;
          S.calNeckLength = cfg.baseline_neck_length || 0;
          S.calibrated = true;
          const calBtn = document.getElementById('calibBtn');
          if (calBtn) {
            calBtn.classList.add('active');
            const calStatus = document.getElementById('calibStatus');
            if (calStatus) calStatus.textContent = '已校准 (继承)';
          }
          console.log('[API-INIT] 已从后端同步校准基准值');
        }

        // 目标值使用硬编码默认值（不从后端加载）
        // goalInput、sessionGoalInput、breakInput 维持初始值，仅在会话内有效

        // 更新引擎标签（来自后端）
        if (cfg.engine_label) {
          updateEngineLabel(cfg.engine_label);
        } else {
          updateEngineLabel();
        }

        console.log('[API-OK] 后端配置应用完成');
      }
    } else {
      console.warn('[API-WARN] 后端配置加载失败，服务器返回了非 200 状态');
    }

    // 2. 加载业务数据
    console.log('[API-REQ] 正在请求历史业务数据: /api/load-data');
    const dataRes = await fetch('/api/load-data');
    console.log(`[API-RES] 数据请求响应状态: ${dataRes.status} ${dataRes.statusText}`);

    if (dataRes.ok) {
      const backendData = await dataRes.json();
      console.log('[API-DATA] 历史数据解析成功');
      if (backendData) {
        // 直接更新状态对象 S
        S.days = backendData.days || {};
        S.top3 = backendData.top3 || [];
        S.hourFocusMs = backendData.hour_focus_ms || {};
        S.allTimeBestStreak = backendData.all_time_best_streak || 0;
        S.events = backendData.events || [];

        // 更新今日数据到 S 对象
        const today = ensureTodayStats();
        S.focusMs = today.focusMs; // 核心：让运行时衔接持久化的每日累计时间
        today.sessions++; // 每次加载/刷新页面，视为一次新会话开启

        // 恢复历史笔痕统计等 (可选)
        renderTimeline();
        updateHeatmap();
        renderLeaderboard();

        console.log(`[API-INIT] 已恢复今日累计专注: ${fmtMs(S.focusMs)}, 今日会话数: ${today.sessions}`);
        console.log('[API-OK] 后端业务数据加载成功，共包含 ' + Object.keys(S.days).length + ' 天记录');

        // 同步当前主题到后端
        sendThemeSync();

        return backendData;
      }
    } else {
      console.warn('[API-WARN] 后端业务数据加载失败，服务器响应异常');
    }
  } catch (err) {
    console.error('[API-FATAL] 与后端进行 API 通讯时发生崩溃:', err);
  }
  return null;
}

function backfillUI() {
  // 从 S 对象读取数据并更新 UI
  // 核心职责：将 S 中的状态同步到 DOM 元素

  // 恢复应用设置（从 S 对象）
  S.theme = S.theme || 'auto';
  const activeTheme = S.theme === 'auto' ? getSystemTheme() : S.theme;
  document.documentElement.setAttribute('data-theme', activeTheme);
  applyTheme();

  // 恢复音量和灵敏度
  S.soundOn = S.soundOn !== undefined ? S.soundOn : false;
  sensIdx = sensIdx || 1;
  const soundBtn = document.getElementById('soundBtn');
  if (soundBtn) soundBtn.textContent = '声音: ' + (S.soundOn ? '开启' : '关闭');

  const sensBtn = document.getElementById('sensBtn');
  if (sensBtn) sensBtn.textContent = '灵敏度: ' + SENS_PRESETS[sensIdx].name;

  const rotateBtn = document.getElementById('rotateBtn');
  if (rotateBtn) rotateBtn.textContent = '旋转: ' + S.cameraRotation + '°';

  // 恢复排行榜 (防御性调用)
  try {
    renderLeaderboard();
  } catch (e) {
    console.warn('[BACKFILL] 排行榜加载失败:', e);
  }

  try {
    renderTimeline();
  } catch (e) {
    console.warn('[BACKFILL] 时间线加载失败:', e);
  }
}

function todayKey() {
  const d = new Date();
  // 补偿本地时区差，确保 toISOString 输出的是本地日期
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 10);
}

/**
 * 核心：确保获取当日唯一的统计对象
 * 解决了“刷新页面后每日进度重置”的问题
 */
function ensureTodayStats() {
  const key = todayKey();
  if (!S.days) S.days = {};
  if (!S.days[key]) {
    S.days[key] = {
      focusMs: 0,
      distMs: 0,
      awayMs: 0,
      phoneMs: 0,
      sessions: 0,
      bestStreak: 0,
      tlData: Array(29).fill('init')
    };
  }
  return S.days[key];
}

/**
 * 从后端获取并设置运行时元数据
 * 包括引擎标签和运行模式
 */
function updateEngineLabel(engineLabel = null) {
  // 如果后端没有返回 ENGINE_LABEL，使用默认值
  const label = engineLabel || 'YOLO26 + Pose + FaceMesh';

  const el = document.getElementById('mMetaEngine');
  if (el) {
    el.textContent = label;
    console.log(`[RUNTIME] 引擎标签已设置: ${label}`);
  }
}

/**
 * 初始化 UI 事件绑定
 * 将事件处理从 HTML 内联 onclick 移动到 JavaScript
 */
function initEventBindings() {
}

/* ====================================================================
   AUDIO CUES
   ==================================================================== */
const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;
let audioUnlocked = false;

// 解锁音频引擎：现代浏览器策略要求必须有用户交互（如点击）才能播放声音
document.addEventListener('click', () => {
  if (!audioUnlocked) {
    audioCtx = new AudioCtx();
    // 播放一个极短的静音音元，使引擎进入运行状态
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    gain.gain.value = 0;
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.01);
    audioUnlocked = true;
    console.log('[APP] 音频引擎已由用户交互合法解锁');
  }
}, { once: true });

function playTone(freq, duration, type = 'sine', vol = 0.08) {
  if (!S.soundOn || !audioUnlocked) return;
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
function playDistractedTick() { playTone(300, 0.15, 'triangle', 0.08); setTimeout(() => playTone(250, 0.2, 'triangle', 0.08), 200); } // 温和的双音分心警报
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
  // 同步配置到后端
  saveConfigToBackend();
}

/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   CALIBRATION
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
let calibTimer = null;
let isCalibrating = false;

function startCalibration() {
  if (!S.camOn) { showToast('请先启动摄像头', 'info'); return; }
  isCalibrating = true;
  S.calibSamples = [];
  S.calibSamplesNeck = [];
  S.calibSamplesEAR = [];
  S.calibSamplesPRatio = [];

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ action: 'calibrate' }));
  }

  const overlay = document.getElementById('calibOverlay');
  if (!overlay) { showToast('校准组件加载失败', 'error'); return; }
  overlay.classList.add('on');
  let count = 3;
  const countdownEl = document.getElementById('calibCountdown');
  if (countdownEl) countdownEl.textContent = count;
  addEv('正在校准，请保持端正坐姿并注视屏幕 3 秒...', 'i');
  showToast('正在校准，请保持端正坐姿并注视屏幕 3 秒...', 'info');

  clearInterval(calibTimer);
  calibTimer = setInterval(() => {
    count--;
    if (count > 0) {
      if (countdownEl) countdownEl.textContent = count;
    } else {
      clearInterval(calibTimer);
      isCalibrating = false;
      S.calibrated = true;
      S.calibSamples = [];
      S.calibSamplesNeck = [];
      S.calibSamplesEAR = [];
      S.calibSamplesPRatio = [];
      overlay.classList.remove('on');

      // 后端已完成30帧采样并保存到 config.json，从后端拉取权威基准值
      fetch('/api/load-config')
        .then(r => r.ok ? r.json() : null)
        .then(cfg => {
          if (cfg) {
            S.calYaw = cfg.baseline_yaw || 0;
            S.calPitch = cfg.baseline_pitch || 0;
            S.calEAR = cfg.baseline_ear || 0.28;
            S.calNeckLength = cfg.baseline_neck_length || 0;
            console.log(`[CALIB] 已从后端同步基准: Yaw=${S.calYaw}, Pitch=${S.calPitch}, EAR=${S.calEAR}, Neck=${S.calNeckLength}`);
          }
        })
        .catch(e => console.warn('[CALIB] 拉取后端基准失败，使用默认值:', e));

      const calStatus = document.getElementById('calibStatus');
      if (calStatus) calStatus.textContent = '已校准';
      document.getElementById('calibBtn')?.classList.add('active');
      playTone(800, 0.2, 'sine', 0.1);
      addEv('校准完成', 'i');
    }
  }, 1000);
}

/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   POMODORO
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   BREAK MODULE
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
let breakTimerObj = {
  active: false,
  endTime: 0,
  totalMs: 0,
};

function toggleBreak() {
  if (!breakTimerObj.active) {
    // 记录当前的连续专注时间（如果有）
    if (S.inStreak && S.curPhaseStreakMs > 5000) {
      recordStreakInterruption(S.curPhaseStreakMs, S.curStreakStart, Date.now());
    }
    S.curPhaseStreakMs = 0;
    S.inStreak = false;
    S.top3Celebrated = false;

    const mins = parseInt(document.getElementById('breakInput').value) || 15;
    breakTimerObj.totalMs = mins * 60 * 1000;
    breakTimerObj.endTime = Date.now() + breakTimerObj.totalMs;
    breakTimerObj.active = true;
    S.paused = true;
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ cmd: 'pause_monitoring', paused: true }));
    }
    S.breakShown = false; // 重置休息提醒标记

    document.querySelector('#breakStartBtn .md3-btn-text').textContent = '停止';
    document.querySelector('#breakStartBtn .md3-btn-icon').textContent = '🔔';
    document.getElementById('breakStartBtn').classList.add('danger');
    document.getElementById('breakStatus').textContent = '休息中...';
    addEv('开始休息，监控已暂停', 'i');
    // 显示休息遮罩
    document.getElementById('pauseOverlay')?.classList.add('on');
    // 关闭所有警告弹窗（休息期间不需要告警）
    ['doNotPlayPopup', 'postureWarningPopup', 'drowsyWarningPopup', 'distractedWarningPopup', 'awayWarningPopup', 'waterWarningPopup'].forEach(id => {
      document.getElementById(id)?.classList.remove('show');
    });
    S.awayAlertActive = false;
  } else {
    endBreak();
  }
}

function updateBreakUI() {
  if (!breakTimerObj.active) return;
  try {
    const now = Date.now();
    const remain = Math.max(0, breakTimerObj.endTime - now);

    // Update Timer Text
    const m = Math.floor(remain / 60000);
    const s = Math.floor((remain % 60000) / 1000);
    const timeStr = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;

    const mainTimer = document.getElementById('breakTimer');
    if (mainTimer) mainTimer.textContent = timeStr;

    // Update Overlay Countdowns
    const elapsed = Math.max(0, breakTimerObj.totalMs - remain);
    const em = Math.floor(elapsed / 60000);
    const es = Math.floor((elapsed % 60000) / 1000);
    const elapsedStr = `${String(em).padStart(2, '0')}:${String(es).padStart(2, '0')}`;

    const restEl = document.getElementById('overlayRestTime');
    const remainEl = document.getElementById('overlayRemainTime');
    if (restEl) restEl.textContent = elapsedStr;
    if (remainEl) remainEl.textContent = timeStr;

    // Update Arc
    const arcEl = document.getElementById('breakArc');
    if (arcEl && breakTimerObj.totalMs > 0) {
      const pct = remain / breakTimerObj.totalMs;
      arcEl.style.strokeDashoffset = 163.36 * (1 - pct);
    }

    if (remain <= 0) {
      endBreak();
      playAlert();
      showToast('休息结束，恢复监控...', 'info');
    }
  } catch (err) {
    console.error('UpdateBreakUI Error:', err);
  }
}

function endBreak() {
  breakTimerObj.active = false;
  S.paused = false;

  // 核心消抖：设置 4 秒预热期，给硬件启动和模型载入留出时间
  const now = Date.now();
  S.warmupUntil = now + 4000;
  S.lastFace = now + 2000; // 同时也向后推迟 lastFace 判定点

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ cmd: 'pause_monitoring', paused: false }));
  }
  document.querySelector('#breakStartBtn .md3-btn-text').textContent = '开始';
  document.querySelector('#breakStartBtn .md3-btn-icon').textContent = '☕';
  document.getElementById('breakStartBtn').classList.remove('danger');
  document.getElementById('breakStatus').textContent = '请设定休息时间';
  document.getElementById('breakTimer').textContent = '00:00';
  document.getElementById('breakArc').style.strokeDashoffset = 163.36;
  document.getElementById('pauseOverlay')?.classList.remove('on');
  S.lastChange = Date.now(); // 防止休息结束后 dt 突变
  addEv('休息结束，硬件重启中...', 'i');
}


/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   LOGS & UI
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
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
  const days = S.days || {};
  let html = '';

  // 生成最近 28 天的数据
  for (let i = 27; i >= 0; i--) {
    const dObj = new Date();
    dObj.setMinutes(dObj.getMinutes() - dObj.getTimezoneOffset());
    const date = new Date(dObj.getTime() - i * 86400000).toISOString().slice(0, 10);
    const dayData = days[date];
    const focusMs = dayData ? dayData.focusMs : 0;
    const hours = focusMs / 3600000;

    let bg = 'var(--bg3)'; // 默认底色
    if (hours >= 4) bg = 'var(--green)';
    else if (hours >= 2) bg = 'var(--green-d)';
    else if (hours > 0.1) bg = 'rgba(129, 201, 149, 0.5)';

    const bestStreakMin = dayData && dayData.bestStreak ? Math.round(dayData.bestStreak / 60000) : 0;
    html += `<div class="hm-cell" style="background:${bg}; border-radius: 4px; border: 1px solid var(--border)" title="${date}: 专注 ${hours.toFixed(1)}h, 最长连续 ${bestStreakMin}m"></div>`;
  }
  container.innerHTML = html;
}

function recordStreakInterruption(streakMs, streakStart, now) {
  if (streakMs <= 5000) return;

  const today = ensureTodayStats();
  let wasNewDailyBest = false;

  // 1. 更新今日最长记录
  if (streakMs > today.bestStreak) {
    today.bestStreak = streakMs;
    wasNewDailyBest = true;
    S.bestStreakMs = streakMs;
  }

  // 2. 检查并更新 TOP3
  const newRecord = { ms: streakMs, start: streakStart, end: now, label: new Date(streakStart).toLocaleTimeString() };
  if (!S.top3) S.top3 = [];
  const combined = [...S.top3, newRecord];
  combined.sort((a, b) => b.ms - a.ms);
  const prevTop3Str = JSON.stringify(S.top3);
  S.top3 = combined.slice(0, 3);

  const isNewTop3 = prevTop3Str !== JSON.stringify(S.top3);

  // 3. 如果进入前三，触发庆祝弹窗
  if (isNewTop3 && !S.top3Celebrated) {
    S.top3Celebrated = true;
    const celebrationPopup = document.getElementById('top3CelebrationPopup');
    if (celebrationPopup) {
      celebrationPopup.classList.add('show');
      if (S.soundOn) playTone(1000, 0.5, 'sine', 0.15);
    }
  }

  // 业务数据持久化由定时器 syncDataToBackend() 处理
  renderLeaderboard();
  updateHeatmap();
}

function updateTimeline(now) {
  if (now - S.curMinStart >= 60000) {
    const timeStr = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    S.curMinStart = now;

    // 聚合统计本分钟内各状态的秒数
    const counts = { active: 0, distracted: 0, phone: 0, away: 0 };
    S.minBucket.forEach(st => {
      if (counts.hasOwnProperty(st)) counts[st]++;
      else if (st === 'init') counts.active++;
    });

    // 过滤掉 <= 5s 的噪音状态，计算“有效状态”列表
    let validStatuses = Object.keys(counts).filter(k => counts[k] > 5);

    // 如果没有任何状态超过 5s（特殊情况），取持续最长的那个作为降级显示
    if (validStatuses.length === 0) {
      const maxSt = Object.keys(counts).reduce((a, b) => counts[a] > counts[b] ? a : b);
      validStatuses = [maxSt];
    }

    S.tlData.shift();
    S.tlData.push({
      validStatuses,
      time: timeStr,
      counts: { ...counts }
    });

    S.minBucket = []; // 清空缓冲区，开始下一分钟记录
    // 业务数据持久化由定时器 syncDataToBackend() 处理
    renderTimeline();
  }
}

function renderTimeline() {
  const container = document.getElementById('tlBars');
  if (!container) return;
  container.innerHTML = '';

  // 配色优化：柔和但区分度高的色调 + 不同高度区分状态
  const cfg = {
    init: { bg: 'var(--bg3)', lbl: '未开始', h: 0 },
    active: { bg: '#41af6a', lbl: '专注中', h: 100 },
    distracted: { bg: '#f4b451', lbl: '走神', h: 55 },
    away: { bg: '#90a4ae', lbl: '离开', h: 30 },
    phone: { bg: '#e53935', lbl: '玩手机', h: 80 }
  };

  S.tlData.forEach(item => {
    const el = document.createElement('div');
    el.className = 'tl-bar';

    if (item && typeof item === 'object' && item.validStatuses) {
      const valids = item.validStatuses;
      const count = valids.length;

      // 计算背景色：单一状态用纯色，多状态用渐变平分
      if (count === 1) {
        el.style.background = cfg[valids[0]].bg;
      } else {
        const step = 100 / count;
        let grad = `linear-gradient(to top`;
        valids.forEach((st, i) => {
          const color = cfg[st].bg;
          grad += `, ${color} ${i * step}%, ${color} ${(i + 1) * step}%`;
        });
        grad += `)`;
        el.style.background = grad;
      }

      // 高度取有效状态中的最大者，保证视觉丰满
      const maxH = Math.max(...valids.map(st => cfg[st].h));
      el.style.height = maxH + '%';
      if (maxH === 0) el.style.background = 'transparent';

      const det = item.counts;
      const parts = [];
      // 修复了模板字符串缺失反引号以及乱码的问题
      if (det.active > 0) parts.push(`专注 ${det.active}秒`);
      if (det.distracted > 0) parts.push(`走神 ${det.distracted}秒`);
      if (det.phone > 0) parts.push(`手机 ${det.phone}秒`);
      if (det.away > 0) parts.push(`离开 ${det.away}秒`);

      el.title = `时间: ${item.time}\n统计: ${parts.join(', ')}`;
    } else {
      // 兼容旧数据或初始化数据
      el.style.background = 'transparent';
      el.style.height = '0';
      el.title = '未开始';
    }

    container.appendChild(el);
  });
}

/**
 * 渲染排行榜领奖台
 */
function renderLeaderboard() {
  const t3 = S.top3;

  // 更新三个排名位置
  [1, 2, 3].forEach(rank => {
    const valEl = document.getElementById(`pod${rank}Val`);
    const barEl = document.getElementById(`pod${rank}Bar`);
    if (!valEl || !barEl) return;

    const data = t3[rank - 1]; // 排名从1开始，数组索引从0开始

    if (data && data.ms > 0) {
      valEl.textContent = fmtMs(data.ms);
      const startStr = data.start ? new Date(data.start).toLocaleTimeString() : '历史';
      const endStr = data.end ? new Date(data.end).toLocaleTimeString() : '存量';
      barEl.title = `记录时间段: ${startStr} - ${endStr}\n时长: ${fmtMsLong(data.ms)}`;
    } else {
      valEl.textContent = '-';
      barEl.title = '暂无记录';
    }
  });

  updateGaps();
}

function updateGaps() {
  // 【关键修复】：安全获取季军成绩，防止新用户数据不足导致数组越界崩溃
  const bronzeMs = (S.top3 && S.top3.length > 2) ? S.top3[2].ms : 0;

  const updateLabel = (id, curMs) => {
    const el = document.getElementById(id);
    if (!el) return;

    // 修复了乱码以及缺失单引号的问题
    if (curMs >= bronzeMs && bronzeMs > 0) {
      el.textContent = '🌟 已登榜';
      el.style.color = 'var(--green)';
    } else if (bronzeMs > 0) {
      const diff = bronzeMs - curMs;
      el.textContent = `离季军还差 ${fmtMsShort(diff)}`;
      el.style.color = 'var(--hint)';
    } else {
      el.textContent = '快创造第一个记录吧！';
    }
  };

  updateLabel('curGap', S.curPhaseStreakMs); // 使用统一的 StreakMs
  updateLabel('bestGap', S.bestStreakMs);
}




function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  if (!el) return;
  clearTimeout(toastTimer);
  el.textContent = msg; el.className = 'toast ' + type + ' show';
  // 5秒后自动隐藏
  toastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
function setStatus(st) {
  // 1. 避免重复执行
  if (S.status === st) return;
  S.status = st;

  // 2. 状态配置字典 (已修复乱码和缺少的引号)
  const cfg = {
    init: { bg: 'var(--bg3)', tc: 'var(--muted)', lbl: '初始化' },
    active: { bg: 'var(--green-bg)', tc: 'var(--green)', lbl: '运行中' },
    distracted: { bg: 'var(--amber-bg)', tc: 'var(--amber)', lbl: '注意力分散' },
    away: { bg: 'var(--red-bg)', tc: 'var(--red-d)', lbl: '检测不到面部' },
    phone: { bg: 'var(--red-bg)', tc: 'var(--red-d)', lbl: '违规玩手机' }
  };

  // 3. 获取当前状态配置，如果没有匹配项则默认使用 init
  const c = cfg[st] || cfg.init;

  // 4. 更新 UI 元素
  const pill = document.getElementById('sPill');
  const txt = document.getElementById('sTxt');

  if (pill) {
    pill.style.background = c.bg;
    pill.style.color = c.tc;
  }
  if (txt) {
    txt.textContent = c.lbl;
  }

  // 5. 打印控制台日志 (已修复乱码)
  console.info(`[STATUS] 状态切换 -> ${st}`);
}

function setMetricState(id, res) {
  if (!res) return;
  const nextState = res.state || '';
  if (!S.metricPersistence) S.metricPersistence = {};
  if (!S.metricPersistence[id]) S.metricPersistence[id] = { cur: '', start: 0 };

  const m = S.metricPersistence[id];
  const card = document.getElementById(id);
  if (!card) return;

  const now = Date.now();

  // 如果状态发生改变，重置计时器并清理所有状态 class
  if (nextState !== m.cur) {
    m.cur = nextState;
    m.start = now;
    card.classList.remove('t-warn', 't-danger', 'warn', 'danger', 'strong-warn', 't-unavailable', 't-stale');
  }

  const elapsed = now - m.start;

  // 基础状态处理
  if (nextState === 'unavailable') {
    card.classList.add('t-unavailable');
    return;
  }
  if (nextState === 'stale') {
    card.classList.add('t-stale');
    return;
  }

  // 告警状态处理
  if (!nextState) return;

  // 即时状态反馈：根据指标严重度立即添加对应类
  if (nextState === 'danger') card.classList.add('t-danger');
  else if (nextState === 'warn') card.classList.add('t-warn');

  // 根据持续时间动态升级警告级别
  if (elapsed >= 60000) {
    card.classList.add('danger');
  } else if (elapsed >= 30000) {
    card.classList.add('warn');
  }
}

/**
 * 指标解释层：负责平滑计算、文案映射与状态驱动
 */
const MetricInterpreter = {
  // 平均值计算工具
  avg: (arr) => arr.length === 0 ? 0 : arr.reduce((a, b) => a + b, 0) / arr.length,

  // 数据时效性检查 (10秒阈值，给网络波动留点余地)
  isStale: (lastUpdate) => {
    if (!lastUpdate || lastUpdate === 0) return true;
    return (Date.now() - lastUpdate) > 10000;
  },

  interpretFatigue: () => {
    try {
      const available = !!S.lastFatigueUpdate && S.lastFatigueUpdate > 0;
      const stale = available && (Date.now() - S.lastFatigueUpdate > 12000);
      
      if (!available) return { text: '未检测到', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '传感器离线', detail: 'Stale', state: 'stale', available: true, stale: true };

      const val = S.eyeFatigue || 0;
      let text = '良好';
      let state = 'normal';
      if (val > 70) { text = '极度疲劳'; state = 'danger'; }
      else if (val > 35) { text = '轻微疲劳'; state = 'warn'; }
      
      return { text: `${text} (${Math.round(val)}%)`, detail: Math.round(val), state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Fatigue Error:', e);
      return { text: '指标故障', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretGaze: () => {
    try {
      const available = !!S.lastGazeUpdate && S.lastGazeUpdate > 0;
      const stale = available && (Date.now() - S.lastGazeUpdate > 8000);
      
      if (!available) return { text: '未锁定', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '跟踪丢失', detail: 'Stale', state: 'stale', available: true, stale: true };

      const angle = S.gazeAngle || { yaw: 0, pitch: 0 };
      const preset = SENS_PRESETS[sensIdx] || SENS_PRESETS[1];
      const offX = Math.abs(angle.yaw) / (preset.yaw || 1);
      const offY = Math.abs(angle.pitch) / (preset.pitch || 1);
      const ratio = Math.max(offX, offY);

      S.gazeHistory.push(ratio);
      if (S.gazeHistory.length > 5) S.gazeHistory.shift();
      const smoothedRatio = MetricInterpreter.avg(S.gazeHistory);

      let text = '实时专注';
      let state = 'normal';
      if (smoothedRatio >= 1.0) { text = '视线游离'; state = 'danger'; }
      else if (smoothedRatio >= 0.6) { text = '注意力偏转'; state = 'warn'; }

      return { text, detail: smoothedRatio.toFixed(1), state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Gaze Error:', e);
      return { text: '指标故障', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretNeck: () => {
    try {
      const available = !!S.lastNeckUpdate && S.lastNeckUpdate > 0;
      const stale = available && (Date.now() - S.lastNeckUpdate > 8000);
      
      if (!available) return { text: '等待数据', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '姿态丢失', detail: 'Stale', state: 'stale', available: true, stale: true };

      const pct = S.neckStrain?.pct || (S.neckStrain?.ratio ? S.neckStrain.ratio * 100 : 100);
      S.neckHistory.push(pct);
      if (S.neckHistory.length > 5) S.neckHistory.shift();
      const smoothed = MetricInterpreter.avg(S.neckHistory);

      let text = '颈部挺直';
      let state = 'normal';
      if (smoothed < 70) { text = '严重前倾'; state = 'danger'; }
      else if (smoothed < 85) { text = '颈部压迫'; state = 'warn'; }

      return { text: `${text} (${Math.round(smoothed)}%)`, detail: smoothed, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Neck Error:', e);
      return { text: '指标故障', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretShoulder: () => {
    try {
      const available = !!S.lastShoulderUpdate && S.lastShoulderUpdate > 0;
      const stale = available && (Date.now() - S.lastShoulderUpdate > 8000);
      
      if (!available) return { text: '扫描中', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '骨架丢失', detail: 'Stale', state: 'stale', available: true, stale: true };

      const tilt = S.shoulderSymmetry?.tilt !== undefined ? S.shoulderSymmetry.tilt : 0;
      S.shoulderHistory.push(tilt);
      if (S.shoulderHistory.length > 5) S.shoulderHistory.shift();
      const smoothed = MetricInterpreter.avg(S.shoulderHistory);
      const absTilt = Math.abs(smoothed);

      let text = '肩部平衡';
      let state = 'normal';
      if (absTilt > 10.0) { text = '严重侧倾'; state = 'danger'; }
      else if (absTilt > 5.0) { text = '轻度不均'; state = 'warn'; }

      const sign = smoothed > 0 ? '右倾' : (smoothed < 0 ? '左倾' : '');
      return { text: `${text} (${sign}${absTilt.toFixed(1)}°)`, detail: smoothed, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Shoulder Error:', e);
      return { text: '指标故障', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretHydration: () => {
    try {
      const stale = MetricInterpreter.isStale(S.lastHydrationUpdate);
      const available = !!S.lastHydrationUpdate;
      if (!available) return { text: '暂无数据', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '数据过期', detail: '-', state: 'stale', available: true, stale: true };

      const mins = Math.round(S.hydrationMinutes);
      let text, state = '';
      if (mins < 0) {
        text = '还没喝水';
      } else if (mins === 0) {
        text = '刚才';
      } else {
        text = `${mins} 分钟前`;
      }
      if (mins > 60) state = 'warn';
      if (mins > 120) state = 'danger';

      return { text, detail: mins, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Hydration Error:', e);
      return { text: '指标异常', detail: '-', state: 'error', available: false, stale: false };
    }
  }
};


function renderThrottledMetrics() {
  const now = Date.now();
  if (now - (S.lastMetricUIUpdate || 0) < 500) return;
  S.lastMetricUIUpdate = now;

  const metrics = [
    { id: 'cFatigue', interpret: () => MetricInterpreter.interpretFatigue() },
    { id: 'cGazeState', interpret: () => MetricInterpreter.interpretGaze() },
    { id: 'cNeckStrain', interpret: () => MetricInterpreter.interpretNeck() },
    { id: 'cShoulderSymmetry', interpret: () => MetricInterpreter.interpretShoulder() },
    { id: 'cHydration', interpret: () => MetricInterpreter.interpretHydration() }
  ];

  metrics.forEach(m => {
    try {
      const res = m.interpret();
      const el = document.getElementById(m.id);
      if (el) el.innerText = res.text || '暂无数据';
    } catch (e) {
      console.error(`[RENDER] Failed to update metric ${m.id}:`, e);
      const el = document.getElementById(m.id);
      if (el) el.innerText = '指标异常';
    }
  });
}

function fmtMs(ms) {
  const s = Math.floor(ms / 1000); const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')} `;
}
function fmtMsShort(ms) { return Math.floor(ms / 1000) + 's'; }

function fmtMsLong(ms) {
  const totS = Math.floor(ms / 1000);
  const h = Math.floor(totS / 3600);
  const m = Math.floor((totS % 3600) / 60);
  const s = totS % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')} `;
}


/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   WebSocket & Communication
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
let _ws = null;
function connectWebSocket() {
  if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) {
    console.log(`[WS-SKIP] 已存在活跃或连接中的 WebSocket，跳过本次重连 (当前状态: ${_ws.readyState})`);
    return;
  }

  const wsHost = (location.hostname && location.hostname !== '') ? location.hostname : '127.0.0.1';
  const wsUrl = `ws://${wsHost}:8765`;
  console.info(`[WS-INIT] 🛰️ 正在发起 WebSocket 连接: ${wsUrl}`);

  try {
    _ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('[WS-FATAL] 创建 WebSocket 对象失败:', e);
    return;
  }

  _ws.onopen = () => {
    console.log('%c[WS-OK] ✅ 成功与后端建立实时通讯链路', 'color: #00ff00; font-weight: bold');
    addEv('已连接到后端', 'g');
    S.camOn = true;
    S.warmupUntil = Date.now() + 5000; // 启动预热：5秒内不触发弹窗
    setStatus('active');
    sendSensUpdate();
    // 关键修复：WS 连接成功即隐藏 noCam 遮罩（比 onload/handleBackendData 更可靠）
    const noCamEl = document.getElementById('noCam');
    if (noCamEl) noCamEl.style.display = 'none';
  };

  _ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === 'config_sync') {
        sensIdx = data.sens_idx;
        const sBtn = document.getElementById('sensBtn');
        if (sBtn) sBtn.textContent = '灵敏度: ' + SENS_PRESETS[sensIdx].name;
        console.log('[WS-DATA] 收到后端下发的配置同步指令');
      } else if (data.type === 'soft_refresh_done') {
        console.log('[WS-DATA] 后端通知刷新已完成，正在重启页面...');
        window.location.reload();
      } else {
        // 实时监控数据吞吐
        handleBackendData(data);
        // 为了防止日志爆炸，仅在状态变化时详细记录数据包
        if (data.status !== S.lastLoggedStatus) {
          console.log(`[WS-DATA] 核心状态变更: ${S.lastLoggedStatus} -> ${data.status}`);
          S.lastLoggedStatus = data.status;
        }
      }
    } catch (e) {
      console.error('[WS-ERROR] 消息解析或处理异常:', e);
      console.debug('[WS-DEBUG] 原始异常数据包:', ev.data);
    }
  };

  _ws.onerror = (err) => {
    console.error('[WS-ERR] ❌ 通讯链路发生错误 (Error Event)');
    console.warn('[WS-HINT] 这通常意味着 8765 端口未监听或被防火墙拦截，请检查 server.py 窗口');
    S.camOn = false;
  };

  _ws.onclose = (ev) => {
    S.camOn = false;
    console.warn(`[WS-RE] 🔌 连接已断开 (Code: ${ev.code}, Reason: ${ev.reason || 'None'})，2秒后尝试自动恢复...`);
    setTimeout(connectWebSocket, 2000);
  };
}

function sendSensUpdate() {
  if (_ws && _ws.readyState === WebSocket.OPEN) _ws.send(JSON.stringify({ cmd: 'set_sensitivity', idx: sensIdx }));
}

function tick() {
  // 0. 休息模块更新
  updateBreakUI();

  // 采样当前状态用于分钟聚合
  if (!S.paused) {
    S.minBucket.push(S.status);
  } else {
    S.minBucket.push('away'); // 休息或暂停计为离开
  }

  const now = Date.now();

  if (!S.paused) {
    S.focusHist.push({ t: now, f: S.status === 'active' });
    if (S.focusHist.length > 300) S.focusHist.shift(); // 维持5分钟长度
  }

  if (S.status === 'active' && S.curPhaseStreakMs > 0) {
    if (S.curPhaseStreakMs > S.bestStreakMs) {
      S.bestStreakMs = S.curPhaseStreakMs;
    }
  }

  const clkEl = document.getElementById('clk');
  if (clkEl) clkEl.textContent = new Date().toLocaleTimeString();

  const phaseElapsed = now - S.phaseStart;
  if (document.getElementById('phaseElapsed')) document.getElementById('phaseElapsed').textContent = fmtMsLong(phaseElapsed);
  if (document.getElementById('phaseFocus')) document.getElementById('phaseFocus').textContent = fmtMsLong(S.phaseFocusMs);
  if (document.getElementById('phaseAway')) document.getElementById('phaseAway').textContent = fmtMsLong(S.phaseAwayMs);
  if (document.getElementById('curPhaseStreak')) document.getElementById('curPhaseStreak').textContent = fmtMsLong(S.curPhaseStreakMs);
  if (document.getElementById('phasePhone')) document.getElementById('phasePhone').textContent = fmtMsLong(S.phasePhoneMs);
  if (document.getElementById('phaseDist')) document.getElementById('phaseDist').textContent = fmtMsLong(S.phaseDistMs);

  updateGaps(); // 实时刷新上榜差距

  const mS = document.getElementById('mMetaDur'); if (mS) mS.textContent = fmtMs(now - S.sessionStart);
  const mF = document.getElementById('mFocus'); if (mF) mF.textContent = fmtMs(S.focusMs);

  // 计算每日目标进度（基于当日累计 focusMs）
  const todayStats = ensureTodayStats();
  const gInput = document.getElementById('goalInput');
  const goalMin = gInput ? (parseInt(gInput.value) || 120) : 120;
  const curMin = todayStats.focusMs / 60000;
  const pct = Math.min(Math.round((curMin / goalMin) * 100), 100);

  const gArc = document.getElementById('goalArc');
  if (gArc) gArc.style.strokeDashoffset = 163.36 * (1 - pct / 100);
  const gPct = document.getElementById('goalPct');
  if (gPct) gPct.textContent = pct + '%';

  // 排行榜与实时监控数据固化刷新
  const curStreakEl = document.getElementById('curStreak');
  if (curStreakEl) curStreakEl.textContent = fmtMs(S.curPhaseStreakMs); // 使用统一的 StreakMs
  const bestStreakEl = document.getElementById('bestStreak');
  if (bestStreakEl) bestStreakEl.textContent = fmtMs(S.bestStreakMs);

  // 核心修复：确保文档标题随着监测状态更新，不再卡在 [LOADING]
  if (S.status === 'active') {
    document.title = "DoNotPlay - 正在监测中";
  } else if (S.status === 'away') {
    document.title = "⚠️ 请回到办公桌";
  } else {
    document.title = "DoNotPlay";
  }

  // v0.29: 统一指标告警驱动体系
  const isWarmedUp = (now - S.phaseStart > 3000); // 预热阶段缩短到 3s
  if (isWarmedUp) {
    const cards = [
      { id: 'cardFatigue', interpret: () => MetricInterpreter.interpretFatigue() },
      { id: 'cardGaze', interpret: () => MetricInterpreter.interpretGaze() },
      { id: 'cardNeck', interpret: () => MetricInterpreter.interpretNeck() },
      { id: 'cardShoulder', interpret: () => MetricInterpreter.interpretShoulder() },
      { id: 'cardHydration', interpret: () => MetricInterpreter.interpretHydration() }
    ];

    cards.forEach(c => {
      try {
        if (document.getElementById(c.id)) {
          setMetricState(c.id, c.interpret());
        }
      } catch (e) {
        console.error(`[CARD-UI] Refresh failed for ${c.id}:`, e);
      }
    });
  }

  renderThrottledMetrics();
  const gProg = document.getElementById('goalProgress');
  if (gProg) gProg.textContent = `${Math.floor(curMin)}m / ${goalMin}m 专注`;

  // --- Session Goal Logic ---
  const sgInput = document.getElementById('sessionGoalInput');
  const sessGoalMin = sgInput ? (parseInt(sgInput.value) || 60) : 60;
  const sessCurMin = S.phaseFocusMs / 60000;
  const sessPct = Math.min(Math.round((sessCurMin / sessGoalMin) * 100), 100);

  if (document.getElementById('sessionGoalArc')) document.getElementById('sessionGoalArc').style.strokeDashoffset = 163.36 * (1 - sessPct / 100);
  if (document.getElementById('sessionGoalPct')) document.getElementById('sessionGoalPct').textContent = sessPct + '%';
  if (document.getElementById('sessionGoalProgress')) document.getElementById('sessionGoalProgress').textContent = `${Math.floor(sessCurMin)}m / ${sessGoalMin}m 专注`;

  // 休息提醒逻辑
  if (sessCurMin >= sessGoalMin && !S.breakShown) {
    document.getElementById('brkBanner')?.classList.add('on');
    S.breakShown = true;
    playAlert();
  }

  updateBreakUI();

  // 1. 计算会话平均专注分数 (修复漏洞：将手机时间计入分母以防止分数虚高)
  const totalActive = S.focusMs + S.distMs + S.awayMs + S.phoneMs;
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

  // 3. 更新今日会话数（从持久化对象读取）
  const todaySessions = ensureTodayStats().sessions || 1;
  const goalTodayEl = document.getElementById('goalToday');
  if (goalTodayEl) goalTodayEl.textContent = `今日会话: ${todaySessions}`;

  // 4. 驱动时间线更新
  updateTimeline(now);
  // 5. 检测人脸存在 (备注：离开预警的主要显示逻辑现由 handleBackendData 的状态机驱动)
  checkUserPresence(now);

  // 【备注】之前在这里的冗余弹窗逻辑已移除，现统一由 handleBackendData 和 checkUserPresence 驱动
  // 注：业务数据持久化由后端通过 /api/save-data 异步处理，不再在此进行
}

/**
 * 人脸离开预警实时检测
 */
function checkUserPresence(now) {
  if (S.paused || !S.camOn) return;

  // 当 WebSocket 数据流正常 (<2s) 时，离开检测完全由 handleBackendData 驱动，避免冲突
  if (now - S.lastWsMsgTime < 2000) return;

  // WebSocket 数据中断时的客户端降级检测
  const delay = (SENS_PRESETS[sensIdx].delay || 2000);
  const isPhysicallyMissing = (now - S.lastFace > delay);

  if (isPhysicallyMissing) {
    if (!S.awayAlertActive && S.status !== 'break' && S.camOn) {
      S.awayAlertActive = true;
      document.getElementById('awayWarningPopup')?.classList.add('show');
      console.info('[UI-POPUP] 离开检测触发 (极简模式：人脸丢失 > 2s)');
    }
  } else {
    // 2. 瞬间返回：人脸一旦出现（或后端状态解除），立即清除弹窗
    if (S.awayAlertActive) {
      S.awayAlertActive = false;
      document.getElementById('awayWarningPopup')?.classList.remove('show');
      console.info('[UI-POPUP] 离开预警清除 (极简模式：人脸回归)');
    }
  }
}

function handleBackendData(data) {
  if (S.paused) return;
  const now = Date.now();
  S.lastWsMsgTime = now;

  const noCamEl = document.getElementById('noCam');
  if (noCamEl) noCamEl.style.display = 'none';

  // 结构化读取支撑 (Null-safe)
  const status = data.status || 'active';
  const faceDetected = !!data.faceDetected;
  const phoneAlertActive = !!data.phoneAlertActive;
  const isSlouching = !!data.is_slouching;

  // 优先读取结构化模块，若缺失则降级到 legacy 字段
  const hydration = data.hydration || null;
  const eye_fatigue = data.eye_fatigue || data.fatigue || null;
  const gaze_angle = data.gaze_angle || null;
  const neck_strain = data.neck_strain || null;
  const shoulder_symmetry = data.shoulder_symmetry || null;

  // 同步后端数据到全局状态对象 S 并记录更新时间
  if (eye_fatigue) {
    // 使用 max(composite_score, perclos) 消除 score 的 15% PERCLOS 死区，提供连续反馈
    const rawScore = eye_fatigue.score || 0;
    const rawPerclos = eye_fatigue.perclos !== undefined ? eye_fatigue.perclos : 0;
    S.eyeFatigue = Math.max(rawScore, rawPerclos);
    S.curBlinkRate = eye_fatigue.blinkRate || eye_fatigue.blink_rate || 0;
    S.lastFatigueUpdate = now;
  }
  if (gaze_angle) { S.gazeAngle = gaze_angle; S.lastGazeUpdate = now; }
  if (neck_strain) { S.neckStrain = neck_strain; S.lastNeckUpdate = now; }
  if (shoulder_symmetry) { S.shoulderSymmetry = shoulder_symmetry; S.lastShoulderUpdate = now; }

  // 周期性诊断日志 (每 5 秒一次)
  if (!S._lastDiagLog || now - S._lastDiagLog > 5000) {
    S._lastDiagLog = now;
    console.log(`[DIAG] status=${status} face=${faceDetected} phone=${phoneAlertActive} slouch=${isSlouching} fatigue_score=${eye_fatigue?.score} perclos=${eye_fatigue?.perclos} neck_pct=${neck_strain?.pct} shoulder_tilt=${shoulder_symmetry?.tilt} hydration_min=${hydration?.minutes_since} gaze_yaw=${gaze_angle?.yaw} gaze_pitch=${gaze_angle?.pitch}`);
  }
  if (hydration) {
    S.hydrationMinutes = (hydration.minutes_since !== undefined) ? hydration.minutes_since : -1;
    S.lastHydrationUpdate = now;
    // 检测饮水事件：drinking_now 上升沿触发重置
    if (hydration.drinking_now && !S._lastDrinkingNow) {
      S.lastDrinkTime = now;
      S.waterAlertShown = false;
      addEv('检测到饮水，计时器已重置', 'g');
    }
    S._lastDrinkingNow = !!hydration.drinking_now;
  }

  if (S.lastChange === 0) S.lastChange = now;
  let dt = now - S.lastChange;
  if (dt > 2000) { dt = 1000; }
  S.lastChange = now;

  // 1. 同步物理探测位（仅 faceDetected 更新 lastFace，避免 personDetected/poseValid 误判离席）
  if (faceDetected) {
    S.lastFace = now;
  }

  // 2. 状态驱动计时
  if (status === 'active') {
    const today = ensureTodayStats();
    if (S.status !== 'active') { playFocusIn(); addEv('开始专注', 'g'); }
    S.focusMs += dt; today.focusMs += dt; S.phaseFocusMs += dt; S.curPhaseStreakMs += dt;
    if (S.curPhaseStreakMs > S.bestStreakMs) S.bestStreakMs = S.curPhaseStreakMs;
    if (S.curPhaseStreakMs > 5000 && !S.inStreak) {
      S.inStreak = true;
      S.curStreakStart = now - S.curPhaseStreakMs;
    }
    if (S.inStreak) S.interruptionStartMs = 0;
  }
  else if (status === 'distracted') {
    const today = ensureTodayStats();
    if (S.status !== 'distracted') { addEv('注意力分散', 'w'); }
    S.distMs += dt; today.distMs += dt; S.phaseDistMs += dt;
  }
  else if (status === 'phone') {
    const today = ensureTodayStats();
    if (S.status !== 'phone') { addEv('检测到手机使用', 'w'); }
    S.phoneMs += dt; today.phoneMs += dt; S.phasePhoneMs += dt;
  }
  else if (status === 'away') {
    const today = ensureTodayStats();
    if (S.status !== 'away') { addEv('用户离开', 'w'); }
    S.awayMs += dt; today.awayMs += dt; S.phaseAwayMs += dt;
  }

  // 3. 连续专注中断逻辑
  if (S.inStreak && status !== 'active') {
    if (S.interruptionStartMs === 0) S.interruptionStartMs = now;
    if (now - S.interruptionStartMs > 3000) {
      const reason = (status === 'phone') ? '检测到手机' : (status === 'away' ? '离开座位' : '注意力分散');
      addEv(reason, 'w');
      recordStreakInterruption(S.curPhaseStreakMs, S.curStreakStart, now);
      S.curPhaseStreakMs = 0; S.inStreak = false; S.interruptionStartMs = 0;
    }
  }

  // 4. UI 遥测更新
  if (eye_fatigue && eye_fatigue.perclos !== undefined) {
    const elEar = document.getElementById('mMetaEAR');
    if (elEar) elEar.textContent = eye_fatigue.perclos.toFixed(2);
  }
  if (gaze_angle && gaze_angle.yaw !== undefined) {
    const elPose = document.getElementById('mMetaPose');
    if (elPose) elPose.textContent = gaze_angle.yaw.toFixed(0) + ',' + gaze_angle.pitch.toFixed(0);
  }

  // 5. 统一弹窗激活逻辑 (Popup State Sync)
  // A. 手机预警（支持手动关闭后 5 秒冷却期）
  const phonePop = document.getElementById('doNotPlayPopup');
  if (phonePop) {
    if (phoneAlertActive && now > (S._phoneDismissUntil || 0) && now > (S.warmupUntil || 0)) {
      if (!phonePop.classList.contains('show')) {
        phonePop.classList.add('show');
        playAlert();
        console.info('[UI-POPUP] 手机检测激活');
      }
    } else if (!phoneAlertActive) {
      phonePop.classList.remove('show');
    }
  }

  // B. 分心预警（使用独立计时器 S.distractedSince，而非 streak 计时器）
  const distPop = document.getElementById('distractedWarningPopup');
  if (distPop) {
    if (status === 'distracted' && now > (S.warmupUntil || 0)) {
      if (S.status !== 'distracted') S.distractedSince = now;
      if (!distPop.classList.contains('show') && S.distractedSince > 0 && (now - S.distractedSince > 5000) && now > (S._distractedDismissUntil || 0)) {
        distPop.classList.add('show');
        playDistractedTick();
        console.info('[UI-POPUP] 分心预警激活');
      }
    } else {
      distPop.classList.remove('show');
      S.distractedSince = 0;
    }
  }

  // C. 离开预警 (完全由后端 status 字段驱动，消除 personDetected/poseValid 导致的误判)
  const awayPop = document.getElementById('awayWarningPopup');
  if (status === 'away' && now > (S.warmupUntil || 0)) {
    if (awayPop && !awayPop.classList.contains('show')) {
      awayPop.classList.add('show');
      S.awayAlertActive = true;
      playSoftAlert();
      console.info('[UI-POPUP] 离开预警激活 (后端状态驱动)');
    }
  } else if (status !== 'away') {
    if (awayPop && awayPop.classList.contains('show')) {
      awayPop.classList.remove('show');
      S.awayAlertActive = false;
      S.lastFace = now;
      console.info('[UI-POPUP] 离开预警清除：用户已返回');
    }
  }

  // D. 姿态预警 (驼背) — 需持续 10 秒驼背才弹窗，避免偶尔低头误触
  const postPop = document.getElementById('postureWarningPopup');
  if (postPop) {
    if (isSlouching && status !== 'away' && now > (S.warmupUntil || 0)) {
      if (!S._slouchSince) S._slouchSince = now;
      const slouchDuration = now - S._slouchSince;
      if (slouchDuration > 10000 && now > (S._postureDismissUntil || 0)) {
        if (!postPop.classList.contains('show')) {
          postPop.classList.add('show');
          playPostureWarn();
          console.info('[UI-POPUP] 姿势预警激活 (持续驼背 >10s)');
        }
      }
    } else {
      S._slouchSince = 0;
      postPop.classList.remove('show');
    }
  }

  // E. 疲劳预警 (基于 Perclos) — 仅在用户在场 (非 away) 且预热结束后弹出
  const drowsyPop = document.getElementById('drowsyWarningPopup');
  if (drowsyPop) {
    if (S.eyeFatigue > 80 && status !== 'away' && now > (S.warmupUntil || 0) && now > (S._drowsyDismissUntil || 0) && !S._fatigueDisabledForSession) { 
      if (!drowsyPop.classList.contains('show')) {
        drowsyPop.classList.add('show');
        playDrowsyAlert();
        console.info('[UI-POPUP] 疲劳预警激活');
      }
    } else if (S.eyeFatigue < 40 || status === 'away') {
      drowsyPop.classList.remove('show');
    }
  }

  // F. 饮水提醒 — 仅在用户在场 (非 away) 时提醒
  const waterPop = document.getElementById('waterWarningPopup');
  if (S.hydrationMinutes > 60 && status !== 'away' && now > (S.warmupUntil || 0)) {
    if (!waterPop.classList.contains('show') && !S.waterAlertShown) {
      waterPop.classList.add('show');
      S.waterAlertShown = true;
      playSoftAlert();
      console.info('[UI-POPUP] 饮水预警激活');
    }
  } else {
    waterPop.classList.remove('show');
    S.waterAlertShown = false;
  }

  // 6. 状态同步到仪表盘
  setStatus(status);

  // 7. 诊断信息处理
  const postTag = document.getElementById('postureTag');
  if (postTag) {
    if (data.is_stuck) { postTag.textContent = '检测器响应缓慢，请点击刷新'; postTag.style.color = '#f44336'; }
    else if (!faceDetected && (data.personDetected || data.poseValid)) { postTag.textContent = '检测到身体，请确保头部在画面中央'; postTag.style.color = '#ff9800'; }
    else if (faceDetected) {
      if (postTag.textContent.includes('检测') || postTag.textContent.includes('响应')) { postTag.textContent = '良好姿势'; postTag.style.color = ''; }
    }
  }
}

function initCam() {
  const vidStream = document.getElementById('vid_stream');
  if (vidStream) {
    vidStream.src = '/video_feed?t=' + Date.now();
    vidStream.onload = () => { if (document.getElementById('noCam')) document.getElementById('noCam').style.display = 'none'; };
  }
  // WebSocket 连接已在 start() 中建立，不在此重复调用
}

const on = (id, evt, fn) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener(evt, fn);
  else console.warn(`[DOM-MISSING] ${id}`);
};

document.addEventListener('DOMContentLoaded', () => {
  on('sensBtn', 'click', () => {
    sensIdx = (sensIdx + 1) % 4;
    const btn = document.getElementById('sensBtn');
    if (btn) btn.textContent = '灵敏度: ' + SENS_PRESETS[sensIdx].name;
    sendSensUpdate();
    saveConfigToBackend(); // 同步到后端
  });
  on('camBtn', 'click', () => { connectWebSocket(); initCam(); });
  on('calibBtn', 'click', startCalibration);
  on('soundBtn', 'click', toggleSound);
  on('themeBtn', 'click', toggleTheme);
  on('refreshBtn', 'click', refreshApp);
  on('doNotPlayDismissBtn', 'click', () => { S.phoneAlertActive = false; S._phoneDismissUntil = Date.now() + 5000; document.getElementById('doNotPlayPopup')?.classList.remove('show'); });
  on('breakStartBtn', 'click', toggleBreak);
  on('rotateBtn', 'click', toggleRotation);

  // Missing Listeners for Popups and Banners
  on('brkDismissBtn', 'click', () => document.getElementById('brkBanner')?.classList.remove('on'));
  on('phoneDismissBtn', 'click', () => document.getElementById('phoneBanner')?.classList.remove('on'));
  on('postureDismissBtn', 'click', () => { S._postureDismissUntil = Date.now() + 30000; document.getElementById('postureWarningPopup')?.classList.remove('show'); });
  on('drowsyDismissBtn', 'click', () => { S._drowsyDismissUntil = Date.now() + 60000; document.getElementById('drowsyWarningPopup')?.classList.remove('show'); });
  on('drowsyRestBtn', 'click', () => {
    document.getElementById('drowsyWarningPopup')?.classList.remove('show');
    toggleBreak();
  });
  on('drowsyDisableBtn', 'click', () => {
    S._fatigueDisabledForSession = true;
    document.getElementById('drowsyWarningPopup')?.classList.remove('show');
    showToast('本次已关闭疲惫监测', 'info');
  });
  on('distractedDismissBtn', 'click', () => { S._distractedDismissUntil = Date.now() + 5000; document.getElementById('distractedWarningPopup')?.classList.remove('show'); });
  on('awayDismissBtn', 'click', () => { 
    S.awayAlertActive = false; 
    S.lastFace = Date.now() + 5000; // 关键修复：手动点击时给 5 秒宽限期，防止即时闪现
    document.getElementById('awayWarningPopup')?.classList.remove('show'); 
    console.log('[UI-POPUP] 用户手动确认返回，开启 5s 宽限期');
  });
  on('celebDismissBtn', 'click', () => document.getElementById('top3CelebrationPopup')?.classList.remove('show'));
  on('waterDismissBtn', 'click', () => document.getElementById('waterWarningPopup')?.classList.remove('show'));
  on('resumeFromOverlayBtn', 'click', endBreak);
  on('errorDismissBtn', 'click', () => document.getElementById('errorPopup')?.classList.remove('show'));
  on('copyErrorBtn', 'click', () => {
    const msg = document.getElementById('errorMsg')?.textContent;
    if (msg) navigator.clipboard.writeText(msg).then(() => showToast('错误信息已复制', 'success'));
  });

  applyTheme();
});

async function start() {
  if (S.booted) {
    console.log('[BOOT] 应用已经在运行中，跳过重复启动');
    return;
  }
  S.booted = true;
  console.log('[APP] 开始初始化应用...');

  // 建立 WebSocket 连接
  try {
    connectWebSocket();
    console.log('[APP] WebSocket 连接已启动');
  } catch (e) {
    console.error('[APP] WebSocket 连接错误:', e);
  }

  // 从后端加载配置和业务数据（使用统一后端接口）
  try {
    await loadFromBackend();
    console.log('[APP] 后端配置和业务数据加载完成');
    // 配置加载后立即应用主题
    applyTheme();
  } catch (e) {
    console.error('[APP] 后端数据加载错误:', e);
  }

  // 初始化 UI
  try {
    backfillUI();
    renderLeaderboard();
    updateHeatmap();

    // 启动视频流
    initCam();

    // 核心修复：刷新后立即清除“脚本已加载”状态并同步 UI
    if (S.camOn) setStatus('active');
    document.title = "DoNotPlay - 专注监测中";

    console.log('[APP] UI 初始化和摄像头初始化完成');
  } catch (e) {
    console.error('[APP] UI/摄像头初始化错误:', e);
  }

  // 初始化事件绑定（UI 控制）
  initEventBindings();

  // 启动主循环
  setInterval(tick, 1000);

  // 定期同步业务数据到后端（每 30 秒）
  // 替代废弃的 saveSessionSnapshot 机制
  setInterval(() => {
    syncDataToBackend();
  }, 30000);

  console.log('[APP] 应用初始化完成，进入主循环');
}

/**
 * 将内存中的业务数据同步到后端
 * 调用 /api/save-data 接口
 */
function syncDataToBackend() {
  try {
    const payload = {
      days: S.days || {},
      top3: S.top3 || [],
      hour_focus_ms: S.hourFocusMs || {},
      all_time_best_streak: S.allTimeBestStreak || 0,
      events: S.events || []
    };

    fetch('/api/save-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(err => console.warn('[SYNC] 业务数据同步失败:', err));
  } catch (e) {
    console.warn('[SYNC] 准备数据时出错:', e);
  }
}

function toggleRotation() {
  S.cameraRotation = (S.cameraRotation + 90) % 360;
  const btn = document.getElementById('rotateBtn');
  if (btn) btn.textContent = '旋转: ' + S.cameraRotation + '°';

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({
      cmd: 'set_camera_rotation',
      rotation: S.cameraRotation,
      flip: S.cameraFlip
    }));
  }
  saveConfigToBackend(); // 核心修复：更新旋转方向时立即持久化到后端
  showToast(`画面已旋转至 ${S.cameraRotation} 度`, 'info');
}

// 健壮的启动逻辑：兼容 DOM 已加载或未加载的情况
if (document.readyState === 'complete' || document.readyState === 'interactive') {
  console.log('[BOOT] 检测到 DOM 已就绪，立即启动...');
  start();
} else {
  console.log('[BOOT] 等待 window.load 事件启动...');
  window.addEventListener('load', start);
}
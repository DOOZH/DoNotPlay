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

function applyTheme() {
  const root = document.documentElement;
  const btn = document.getElementById('themeBtn');
  let effective = S.theme || 'auto';

  if (effective === 'auto') {
    // 跟随系统
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
    if (btn) btn.textContent = T('theme_auto');
  } else {
    root.setAttribute('data-theme', effective);
    if (btn) btn.textContent = effective === 'dark' ? T('theme_dark') : T('theme_light');
  }
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
  { name: '严格', yaw: 25, pitch: 18, delay: 2000, desc: '最严格专注判定，视线稍偏即触发' },
  { name: '正常', yaw: 40, pitch: 25, delay: 3000, desc: '标准专注判定，3秒走神触发' },
  { name: '双屏', yaw: 65, pitch: 35, delay: 5000, desc: '双屏模式，允许查看副屏' },
  { name: '多屏', yaw: 85, pitch: 50, delay: 8000, desc: '多屏模式，宽松专注判定' },
];
let sensIdx = (() => {
  const saved = localStorage.getItem('donotplay_sensIdx');
  const idx = saved !== null ? parseInt(saved, 10) : 1;
  return (idx >= 0 && idx <= 3) ? idx : 1;
})();
let toastTimer = null;

/* ====================================================================
   PERSISTENCE - Backend-Driven Only
   ==================================================================== */

// 全局状态对象 — 单一数据源 (Single Source of Truth)
// ── i18n ──────────────────────────────────────────────────────
let _lang = localStorage.getItem('dnp_lang') || 'zh';

const LANGS = {
  zh: {
    sens_btn:'灵敏度: 正常', tt_sens:'专注灵敏度 [S]', tt_sound:'切换音频提示 [M]',
    tt_theme:'切换主题 [T]', tt_rotate:'旋转摄像头视角 [V]', tt_refresh:'刷新 [R]',
    tt_calib:'校准视线焦点区间 [C]', calib_btn:'校准',
    sound_off:'声音: 关闭', sound_on:'声音: 开启',
    theme_auto:'主题: 自动', theme_dark:'主题: 深色', theme_light:'主题: 浅色',
    rotate:'视角旋转', refresh:'刷新', about:'关于',
    st_init:'初始化', st_running:'运行中', st_distracted:'注意力分散',
    st_away:'检测不到面部', st_phone:'违规玩手机', st_unknown:'未知',
    st_focus:'专注', st_distracted_short:'走神', st_phone_short:'手机', st_away_short:'离开',
    posture_good:'良好姿势', posture_bad:'请挺直腰背',
    cam_required:'需要摄像头', cam_desc:'允许摄像头访问以开始跟踪。所有处理都在本地运行。', cam_enable:'启用摄像头',
    lbl_eye_usage:'连续用眼', lbl_neck:'颈部前倾', lbl_shoulder:'肩颈平衡', lbl_hydration:'上次饮水',
    lbl_duration:'总时长', lbl_streak:'当前连续专注', lbl_focus:'累计专注',
    lbl_phone:'累计玩手机', lbl_dist:'累计走神', lbl_away:'累计离开',
    win_current:'当前窗口', win_waiting:'等待检测...', win_today_focus:'今日专注',
    goal_daily:'每日目标', goal_session:'本次目标', goal_min_lbl:'分钟目标',
    goal_session_progress:'本次目标进度',
    break_title:'休息一下', break_set:'设定时间并休息', break_min:'分钟', break_session_hint:'本次休息设置', start:'开始',
    lb_title:'排行榜', lb_cur_streak:'当前连续专注', lb_1st:'冠军', lb_2nd:'亚军',
    lb_3rd:'季军', lb_best_today:'今日最高连续专注', lb_top3_note:'历史 TOP 3 专注',
    tl_title:'时间轴', heatmap_title:'热力图',
    wl_title:'窗口日志', wl_usage:'使用记录', wl_rules:'规则管理',
    wl_proc_rules:'程序名单', wl_title_rules:'页面名单',
    wl_focus_apps:'✅ 专注程序', wl_dist_apps:'🚫 走神程序',
    wl_focus_pages:'✅ 专注页面', wl_dist_pages:'🚫 走神页面',
    wl_ph_proc_white:'程序名 (如 winword)', wl_ph_proc_black:'程序名 (如 game)',
    wl_ph_title_white:'标题关键词 (如 GitHub)', wl_ph_title_black:'标题关键词 (如 bilibili)',
    wl_browser_note:'💡 页面名单仅匹配浏览器标题（Chrome / Edge / Firefox 等）',
    add:'添加', dismiss:'忽略', ev_title:'事件日志', ev_waiting:'等待摄像头...',
    brk_banner:'该休息了！', phone_banner:'⚠️ 警告：正在使用手机！请立刻放下，返回专注状态。',
    phone_put_down:'我已放下',
    dp_label:'分心', dp_sub:'离开专注的时间',
    popup_phone_sub:'检测到手机使用 — 请立刻放下手机', popup_phone_btn:'我已放下手机',
    popup_posture_title:'请挺直腰背！', popup_posture_sub:'检测到您持续驼背，请注意保护颈椎',
    popup_posture_btn:'我已坐直',
    popup_eye_title:'用眼时间较长', popup_eye_sub:'需要放松一下', popup_gotit:'我知道了',
    popup_rest_now:'立即休息', popup_disable_session:'本次关闭提醒',
    popup_dist_title:'你好像不在状态', popup_dist_sub:'系统检测到您当前分心，请收回注意力',
    popup_dist_btn:'我已恢复专注',
    popup_away_title:'请返回办公桌', popup_away_sub:'检测到您离开已久，请恢复到工作状态',
    popup_away_btn:'我已回来',
    popup_top3_title:'荣耀时刻', popup_top3_sub:'恭喜！您的本次专注已成功杀入历史 TOP 3',
    popup_top3_btn:'太棒了',
    popup_water_title:'该喝水了！', popup_water_sub:'您已经超过 60 分钟没有饮水了，请补充水分',
    popup_water_btn:'我已饮水',
    kh_sens:'灵敏度', kh_calib:'校准', kh_mute:'静音', kh_theme:'主题', kh_refresh:'刷新',    pause_label:'正在休息中', pause_quote:'放松一下，稍后回来。',
    pause_elapsed:'已休息时长', pause_remain:'剩余目标时长', pause_resume:'恢复监控',
    err_title:'系统发生异常', err_copy:'复制详细错误', err_dismiss:'忽略并继续',
    about_version:'版本', about_build:'构建时间', about_close:'关闭',
    about_qq:'点击加入 QQ 群', about_tg:'点击加入 Telegram Group',
    calib_inherited:'已校准 (继承)', calib_done_status:'已校准',
    calib_start_toast:'四角校准已开始，请依次注视高亮角落', calib_cam_first:'请先启动摄像头',
    calib_load_fail:'校准组件加载失败', calib_done_toast:'校准完成！专注区间已设定',
    calib_gaze_at:'请注视', calib_move_gaze:'移动视线到角落…',
    calib_stay_still:'请保持不动，正在采集…', calib_processing:'处理中…',
    calib_next_corner:'请转向下一角落', calib_acquired:'已采集！',
    calib_intro_title:'校准向导',
    calib_intro_msg:'系统将引导你依次注视屏幕四个角落。到达每个角落后，保持视线稳定 1-2 秒，系统会自动采集。',
    calib_corner_0:'左上角', calib_corner_1:'左下角', calib_corner_2:'右下角', calib_corner_3:'右上角',
    calib_timeout_note:'自动采集超时，强制采集…',
    eye_relax:'👀 看向远方放松眼睛',
    break_stop:'停止', break_on:'休息中...', break_set_time:'请设定休息时间',
    break_started_ev:'开始休息，监控已暂停', break_end_toast:'休息结束，恢复监控...',
    break_start_label:'开始', break_end_ev:'休息结束，硬件重启中...',
    posture_bad:'驼背',
    slot_morning:'上午', slot_afternoon:'下午', slot_evening:'晚上',
    hm_focus:'专注',
    tl_notstarted:'未开始', tl_focused:'专注中', tl_distracted_lbl:'走神',
    tl_away_lbl:'离开', tl_phone_lbl:'玩手机',
    tl_time:'时间:', tl_stats:'统计:', tl_period:'记录时间段:', tl_duration:'时长:',
    tl_norecord:'暂无记录',
    lb_gap_to_3rd:'离季军还差', lb_no_record:'快创造第一个记录吧！',
    mins_ago:'分钟前', mins_unit:'分钟',
    goal_focus:'专注', goal_today_sessions:'今日会话:',
    rotate_label:'旋转:', rotate_degrees:'度', rotate_toast:'画面已旋转至',
    err_copied:'错误信息已复制', fatigue_disabled:'本次已关闭疲惫监测',
    ev_calib_start:'开始四角专注区间校准...', ev_calib_done:'四角专注区间校准完成',
    sens_0:'严格', sens_1:'正常', sens_2:'双屏', sens_3:'多屏',
    ev_focus_in:'开始专注', ev_dist:'注意力分散', ev_phone:'检测到手机使用',
    ev_away:'用户离开', ev_back:'用户回来', ev_break_end:'休息结束，监控恢复',
  },
  en: {
    sens_btn:'Sensitivity: Normal', tt_sens:'Gaze Sensitivity [S]', tt_sound:'Toggle Sound [M]',
    tt_theme:'Toggle Theme [T]', tt_rotate:'Rotate Camera [V]', tt_refresh:'Refresh [R]',
    tt_calib:'Calibrate Gaze Focus Zone [C]', calib_btn:'Calibrate',
    sound_off:'Sound: Off', sound_on:'Sound: On',
    theme_auto:'Theme: Auto', theme_dark:'Theme: Dark', theme_light:'Theme: Light',
    rotate:'Rotate', refresh:'Refresh', about:'About',
    st_init:'Initializing', st_running:'Running', st_distracted:'Distracted',
    st_away:'Face Not Found', st_phone:'Phone Alert', st_unknown:'Unknown',
    st_focus:'Focus', st_distracted_short:'Dist.', st_phone_short:'Phone', st_away_short:'Away',
    posture_good:'Good Posture', posture_bad:'Sit Up Straight',
    cam_required:'Camera Required', cam_desc:'Allow camera access to start tracking. All processing runs locally.', cam_enable:'Enable Camera',
    lbl_eye_usage:'Eye Usage', lbl_neck:'Neck Strain', lbl_shoulder:'Shoulder Balance', lbl_hydration:'Last Drink',
    lbl_duration:'Duration', lbl_streak:'Current Streak', lbl_focus:'Total Focus',
    lbl_phone:'Phone Time', lbl_dist:'Distracted', lbl_away:'Away Time',
    win_current:'Current Window', win_waiting:'Waiting...', win_today_focus:'Today Focus',
    goal_daily:'Daily Goal', goal_session:'Session Goal', goal_min_lbl:'min goal',
    goal_session_progress:'Session progress',
    break_title:'Take a Break', break_set:'Set time and rest', break_min:'min', break_session_hint:'Break settings', start:'Start',
    lb_title:'Leaderboard', lb_cur_streak:'Current Streak', lb_1st:'1st', lb_2nd:'2nd',
    lb_3rd:'3rd', lb_best_today:'Best Today', lb_top3_note:'All-time Top 3',
    tl_title:'Timeline', heatmap_title:'Heatmap',
    wl_title:'Window Log', wl_usage:'Usage Log', wl_rules:'Rules',
    wl_proc_rules:'App Rules', wl_title_rules:'Page Rules',
    wl_focus_apps:'✅ Focus Apps', wl_dist_apps:'🚫 Distract Apps',
    wl_focus_pages:'✅ Focus Pages', wl_dist_pages:'🚫 Distract Pages',
    wl_ph_proc_white:'App name (e.g. winword)', wl_ph_proc_black:'App name (e.g. game)',
    wl_ph_title_white:'Keyword (e.g. GitHub)', wl_ph_title_black:'Keyword (e.g. bilibili)',
    wl_browser_note:'💡 Page rules match browser titles only (Chrome / Edge / Firefox etc.)',
    add:'Add', dismiss:'Dismiss', ev_title:'Event Log', ev_waiting:'Waiting for camera...',
    brk_banner:'Time for a break!', phone_banner:'⚠️ Warning: Phone detected! Put it down and focus.',
    phone_put_down:'I put it down',
    dp_label:'Distracted', dp_sub:'Time away from focus',
    popup_phone_sub:'Phone detected — put it down now!', popup_phone_btn:'I put it down',
    popup_posture_title:'Sit Up Straight!', popup_posture_sub:'Slouching detected. Protect your spine.',
    popup_posture_btn:'I straightened up',
    popup_eye_title:'Long Screen Time', popup_eye_sub:'Time to rest your eyes', popup_gotit:'Got it',
    popup_rest_now:'Rest Now', popup_disable_session:'Disable for this session',
    popup_dist_title:'You seem distracted', popup_dist_sub:'Distraction detected. Please refocus.',
    popup_dist_btn:"I'm back on track",
    popup_away_title:'Return to Your Desk', popup_away_sub:"You've been away. Please return.",
    popup_away_btn:"I'm back",
    popup_top3_title:'Glory Moment', popup_top3_sub:'Congrats! This session entered the all-time Top 3!',
    popup_top3_btn:'Awesome!',
    popup_water_title:'Stay Hydrated!', popup_water_sub:"You haven't had water in over 60 minutes.",
    popup_water_btn:'I drank water',
    kh_sens:'Sensitivity', kh_calib:'Calibrate', kh_mute:'Mute', kh_theme:'Theme', kh_refresh:'Refresh',
    pause_label:'On Break', pause_quote:'Relax and come back soon.',
    pause_elapsed:'Break time', pause_remain:'Remaining', pause_resume:'Resume',
    err_title:'System Error', err_copy:'Copy Error', err_dismiss:'Dismiss',
    about_version:'Version', about_build:'Build Time', about_close:'Close',
    about_qq:'Join QQ Group', about_tg:'Join Telegram Group',
    calib_inherited:'Calibrated (Inherited)', calib_done_status:'Calibrated',
    calib_start_toast:'4-corner calibration started — gaze at each highlighted corner', calib_cam_first:'Please enable camera first',
    calib_load_fail:'Calibration module failed to load', calib_done_toast:'Calibration complete! Focus zone set.',
    calib_gaze_at:'Gaze at', calib_move_gaze:'Move gaze to corner…',
    calib_stay_still:'Hold still, acquiring…', calib_processing:'Processing…',
    calib_next_corner:'Turn to next corner', calib_acquired:'Acquired!',
    calib_intro_title:'Calibration Guide',
    calib_intro_msg:'Look at each highlighted screen corner in order. Hold your gaze steady for 1-2 seconds and the system will auto-capture.',
    calib_corner_0:'Top Left', calib_corner_1:'Bottom Left', calib_corner_2:'Bottom Right', calib_corner_3:'Top Right',
    calib_timeout_note:'Auto-capture timed out, forcing capture…',
    eye_relax:'👀 Look far away to relax your eyes',
    break_stop:'Stop', break_on:'On break...', break_set_time:'Set break time',
    break_started_ev:'Break started, monitoring paused', break_end_toast:'Break ended, resuming...',
    break_start_label:'Start', break_end_ev:'Break ended, restarting...',
    posture_bad:'Slouching',
    slot_morning:'AM', slot_afternoon:'PM', slot_evening:'Eve',
    hm_focus:'Focus',
    tl_notstarted:'Not started', tl_focused:'Focused', tl_distracted_lbl:'Distracted',
    tl_away_lbl:'Away', tl_phone_lbl:'Phone',
    tl_time:'Time:', tl_stats:'Stats:', tl_period:'Period:', tl_duration:'Duration:',
    tl_norecord:'No records',
    lb_gap_to_3rd:'Gap to 3rd:', lb_no_record:'Go create your first record!',
    mins_ago:'min ago', mins_unit:'min',
    goal_focus:'Focus', goal_today_sessions:"Today's sessions:",
    rotate_label:'Rotate:', rotate_degrees:'°', rotate_toast:'Rotated to',
    err_copied:'Error copied', fatigue_disabled:'Fatigue alerts off for this session',
    ev_calib_start:'Starting gaze calibration...', ev_calib_done:'Gaze calibration complete',
    sens_0:'Strict', sens_1:'Normal', sens_2:'Dual Screen', sens_3:'Multi Screen',
    ev_focus_in:'Focus started', ev_dist:'Distracted', ev_phone:'Phone detected',
    ev_away:'User left', ev_back:'User returned', ev_break_end:'Break ended, resuming',
  }
};

function T(key) { return (LANGS[_lang] || LANGS.zh)[key] || LANGS.zh[key] || key; }

function applyLang() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = T(key);
    if (val) el.textContent = val;
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const key = el.getAttribute('data-i18n-ph');
    const val = T(key);
    if (val) el.placeholder = val;
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    const val = T(key);
    if (val) el.title = val;
  });
  const langBtnTxt = document.getElementById('langBtnTxt');
  if (langBtnTxt) langBtnTxt.textContent = _lang === 'zh' ? 'EN' : '中';
  // Re-render dynamic buttons
  const sensBtn = document.getElementById('sensBtn');
  if (sensBtn) sensBtn.textContent = (T('sens_btn').split(':')[0]) + ': ' + T('sens_' + sensIdx);
  const rotateBtn = document.getElementById('rotateBtn');
  if (rotateBtn) rotateBtn.textContent = T('rotate_label') + ' ' + (S.cameraRotation || 0) + T('rotate_degrees');
  const soundBtn = document.getElementById('soundBtn');
  if (soundBtn) soundBtn.textContent = T(S.soundOn ? 'sound_on' : 'sound_off');
  applyTheme();
}

function toggleLang() {
  _lang = _lang === 'zh' ? 'en' : 'zh';
  localStorage.setItem('dnp_lang', _lang);
  applyLang();
}

// ── About Modal ────────────────────────────────────────────────
function _openAbout() {
  const overlay = document.getElementById('aboutOverlay');
  if (!overlay) return;
  overlay.style.display = 'flex';
  fetch('/api/version').then(r => r.json()).then(d => {
    const v = document.getElementById('aboutVersion');
    const b = document.getElementById('aboutBuild');
    if (v) v.textContent = 'V' + (d.version || '3.0');
    if (b) b.textContent = d.build_time || '—';
  }).catch(() => {});
}
function _closeAbout() {
  const overlay = document.getElementById('aboutOverlay');
  if (overlay) overlay.style.display = 'none';
}

// ──────────────────────────────────────────────────────────────
const S = {
  // 启动控制
  booted: false,
  camOn: false,
  paused: false,
  status: null,
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

  eyeTimeSec: 0,
  eyeMinutes: 0,
  eyeNeedLongRest: false,

  // 实时传感器数据
  eyeFatigue: 0,
  fatigueState: 'normal',       // 来自后端的疲劳状态: normal | mild | moderate | severe
  severeFatigueStart: 0,        // 进入 severe 状态的时间戳 (ms)，0 表示未在 severe 状态
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
  calFocusHalfYaw: 0,   // 四角校准结果：水平半范围
  calFocusHalfPitch: 0, // 四角校准结果：垂直半范围

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
    baseline_neck_length: S.calNeckLength || 0,
    focus_half_yaw: S.calFocusHalfYaw || 0,
    focus_half_pitch: S.calFocusHalfPitch || 0
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
        S.soundOn = cfg.sound_on !== undefined ? cfg.sound_on : true;
        sensIdx = cfg.sens_idx !== undefined ? cfg.sens_idx : 1;
        localStorage.setItem('donotplay_sensIdx', sensIdx); // 持久化到本地
        S.cameraIndex = cfg.camera_index || 0;
        S.cameraRotation = cfg.camera_rotation || 0;
        S.cameraFlip = cfg.camera_flip !== undefined ? cfg.camera_flip : true;

        // 更新校准数据
        if (cfg.baseline_yaw !== undefined) {
          S.calYaw = cfg.baseline_yaw;
          S.calPitch = cfg.baseline_pitch;
          S.calEAR = cfg.baseline_ear || 0.28;
          S.calNeckLength = cfg.baseline_neck_length || 0;
          S.calFocusHalfYaw = cfg.focus_half_yaw || 0;
          S.calFocusHalfPitch = cfg.focus_half_pitch || 0;
          S.calibrated = true;
          const calBtn = document.getElementById('calibBtn');
          if (calBtn) {
            // calibBtn is NOT highlighted when already calibrated; only amber during active calibration
            const calStatus = document.getElementById('calibStatus');
            if (calStatus) calStatus.textContent = T('calib_inherited');
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
  S.soundOn = S.soundOn !== undefined ? S.soundOn : true;
  sensIdx = sensIdx || 1;
  const soundBtn = document.getElementById('soundBtn');
  if (soundBtn) soundBtn.querySelector('[data-i18n]')
    ? soundBtn.querySelector('[data-i18n]').textContent = T(S.soundOn ? 'sound_on' : 'sound_off')
    : soundBtn.textContent = T(S.soundOn ? 'sound_on' : 'sound_off');

  const sensBtn = document.getElementById('sensBtn');
  if (sensBtn) {
    const span = sensBtn.querySelector('[data-i18n]');
    const lbl = T('sens_btn').replace('正常', T('sens_' + sensIdx)).replace('Normal', T('sens_' + sensIdx));
    if (span) span.textContent = (T('sens_btn').split(':')[0]) + ': ' + T('sens_' + sensIdx);
    else sensBtn.textContent = (T('sens_btn').split(':')[0]) + ': ' + T('sens_' + sensIdx);
  }

  const rotateBtn = document.getElementById('rotateBtn');
  if (rotateBtn) rotateBtn.textContent = T('rotate_label') + ' ' + S.cameraRotation + T('rotate_degrees');

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

// ─── Apple 风格音效引擎：三音阶钟声 + 200ms 淡入淡出 ADSR ───
// 所有提示音节流：2 秒冷却防止噪音
let _lastSoundAt = 0;
function _throttleSound(minGapMs = 1500) {
  const now = performance.now();
  if (now - _lastSoundAt < minGapMs) return false;
  _lastSoundAt = now;
  return true;
}

function _ensureAudio() {
  if (!audioCtx) audioCtx = new AudioCtx();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

// 单个音：正弦波 + 余弦包络 (pluck)，产生干净的 ding 感
function _playDing(freq, startOffset, duration = 0.35, peakVol = 0.10) {
  if (!audioCtx) return;
  const t0 = audioCtx.currentTime + startOffset;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'sine';
  osc.frequency.setValueAtTime(freq, t0);
  // ADSR: 5ms attack, exponential release
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(peakVol, t0 + 0.008);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  osc.connect(gain); gain.connect(audioCtx.destination);
  osc.start(t0);
  osc.stop(t0 + duration + 0.02);
}

function playTone(freq, duration, type = 'sine', vol = 0.08) {
  // 兼容旧调用：单音回退
  if (!S.soundOn || !audioUnlocked) return;
  _ensureAudio();
  _playDing(freq, 0, duration, vol);
}

// 三音阶 "Ding" 序列（苹果提示风）：80ms 间隔
function _tripleDing(freqs, peakVol = 0.09) {
  if (!S.soundOn || !audioUnlocked) return;
  if (!_throttleSound()) return;
  _ensureAudio();
  _playDing(freqs[0], 0.00, 0.28, peakVol);
  _playDing(freqs[1], 0.08, 0.30, peakVol);
  _playDing(freqs[2], 0.16, 0.42, peakVol);
}

// 警告（下行，温和）：G5 → E5 → C5
function playWarning()  { _tripleDing([784, 659, 523], 0.09); }
// 恢复/专注开始（上行）：C5 → E5 → G5
function playRecovery() { _tripleDing([523, 659, 784], 0.08); }
// 轻柔双音：用于次要提醒（饮水、离开）
function playSoftAlert() {
  if (!S.soundOn || !audioUnlocked) return;
  if (!_throttleSound(2500)) return;
  _ensureAudio();
  _playDing(659, 0.00, 0.35, 0.07);
  _playDing(880, 0.10, 0.45, 0.06);
}

function playFocusIn()        { playRecovery(); }
function playDistractedTick() { playWarning(); }
function playAlert()          { playWarning(); }
function playPostureWarn()    { playSoftAlert(); }   // 驼背用柔和提示，不用警告
function playPostureGood()    { playRecovery(); }
function playPostureAlert()   { playSoftAlert(); }
function playDrowsyAlert()    { playSoftAlert(); }

function _showShortRestToast() {
  // 显示20秒倒计时提示：看远方放松
  const existingToast = document.getElementById('shortRestToast');
  if (existingToast) existingToast.remove();
  
  const toast = document.createElement('div');
  toast.id = 'shortRestToast';
  toast.style.cssText = `
    position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%);
    background: rgba(103,58,183,0.95); color: #fff; border-radius: 16px;
    padding: 14px 28px; font-size: 15px; font-weight: 600; z-index: 9999;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4); text-align: center; min-width: 220px;
  `;
  
  let countdown = 20;
  toast.textContent = `${T('eye_relax')} ${countdown}s`;
  document.body.appendChild(toast);
  
  const t = setInterval(() => {
    countdown--;
    if (countdown > 0) {
      toast.textContent = `${T('eye_relax')} ${countdown}s`;
    } else {
      clearInterval(t);
      toast.remove();
    }
  }, 1000);
  
  playSoftAlert();
}

function toggleSound() {
  S.soundOn = !S.soundOn;
  const sb = document.getElementById('soundBtn');
  const sp = sb?.querySelector('[data-i18n]');
  const lbl = T(S.soundOn ? 'sound_on' : 'sound_off');
  if (sp) sp.textContent = lbl; else if (sb) sb.textContent = lbl;
  sb?.classList.toggle('active', S.soundOn);
  if (S.soundOn) { if (!audioCtx) audioCtx = new AudioCtx(); playTone(700, 0.1, 'sine', 0.04); }
  showToast(T(S.soundOn ? 'sound_on' : 'sound_off'), 'info');
  // 同步配置到后端
  saveConfigToBackend();
}

/* 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
   CALIBRATION
   鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲 */
let calibTimer = null;
let _calibWaitingStep = -1;  // 当前正在等待后端就绪通知的步骤索引
let isCalibrating = false;

// 四角校准顺序：左上→左下→右下→右上（顺时针一圈）
const CALIB_CORNERS = [
  { step: 0, labelKey: 'calib_corner_0', corner: 0 },
  { step: 1, labelKey: 'calib_corner_1', corner: 1 },
  { step: 2, labelKey: 'calib_corner_2', corner: 2 },
  { step: 3, labelKey: 'calib_corner_3', corner: 3 },
];
let _calibCornerResults = [];  // [{step, yaw, pitch}, ...]
let _calibForceStep = -1;      // step currently being force-captured
let _calibForceFallback = null; // timeout handle for force-capture

function startCalibration() {
  if (!S.camOn) { showToast(T('calib_cam_first'), 'info'); return; }
  const overlay = document.getElementById('calibOverlay');
  if (!overlay) { showToast(T('calib_load_fail'), 'error'); return; }

  _calibCornerResults = [];
  _calibWaitingStep = -1;
  _calibForceStep = -1;
  if (_calibForceFallback) { clearTimeout(_calibForceFallback); _calibForceFallback = null; }
  addEv(T('ev_calib_start'), 'i');
  document.getElementById('calibBtn')?.classList.add('active');

  // Reset all corner states
  for (let i = 0; i < 4; i++) {
    const t = document.getElementById(`calibTarget${i}`);
    const d = document.getElementById(`calibDot${i}`);
    if (t) { t.classList.remove('active', 'done'); }
    if (d) { d.classList.remove('active', 'done'); d.textContent = '○'; }
  }

  // Show intro panel, hide main step UI
  const introEl = document.getElementById('calibIntro');
  const mainEl  = document.getElementById('calibMain');
  if (introEl) {
    const tEl = document.getElementById('calibIntroTitle');
    const mEl = document.getElementById('calibIntroMsg');
    if (tEl) tEl.textContent = T('calib_intro_title');
    if (mEl) mEl.textContent = T('calib_intro_msg');
    introEl.style.display = 'flex';
  }
  if (mainEl) mainEl.style.display = 'none';
  overlay.classList.add('on');

  // 3-second countdown then start actual steps
  let cnt = 3;
  const cntEl = document.getElementById('calibIntroCountdown');
  if (cntEl) cntEl.textContent = cnt;
  const tick = setInterval(() => {
    cnt--;
    if (cnt > 0) {
      if (cntEl) cntEl.textContent = cnt;
    } else {
      clearInterval(tick);
      if (introEl) introEl.style.display = 'none';
      if (mainEl) mainEl.style.display = 'flex';
      showToast(T('calib_start_toast'), 'info');
      _runCalibCornerStep(0);
    }
  }, 1000);
}

function _runCalibCornerStep(stepIdx) {
  if (stepIdx >= CALIB_CORNERS.length) {
    _finalizeCornerCalib();
    return;
  }
  const c = CALIB_CORNERS[stepIdx];
  const textEl   = document.getElementById('calibText');
  const countEl  = document.getElementById('calibCountdown');
  const targetEl = document.getElementById(`calibTarget${c.corner}`);
  const dotEl    = document.getElementById(`calibDot${c.corner}`);

  if (textEl) textEl.textContent = `${T('calib_gaze_at')} ${T(c.labelKey)}`;
  if (countEl) countEl.textContent = T('calib_move_gaze');
  if (dotEl)   { dotEl.classList.add('active'); dotEl.textContent = '●'; }
  if (targetEl) targetEl.classList.add('active');

  clearInterval(calibTimer);
  if (_calibForceFallback) { clearTimeout(_calibForceFallback); _calibForceFallback = null; }

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({ cmd: 'start_corner_step', step: c.step }));
  }

  calibTimer = setTimeout(() => {
    calibTimer = null;
    if (_calibWaitingStep === stepIdx) {
      if (countEl) countEl.textContent = T('calib_stay_still');
    }
  }, 750);

  // 10-second force-capture fallback: if backend never sends corner_calib_ready
  _calibForceFallback = setTimeout(() => {
    _calibForceFallback = null;
    if (_calibWaitingStep !== stepIdx) return;
    const cEl = document.getElementById('calibCountdown');
    if (cEl) cEl.textContent = T('calib_timeout_note');
    _calibForceStep = stepIdx;
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ cmd: 'end_corner_step' }));
    }
    // Secondary fallback: if end_corner_step produces no result in 3s, advance anyway
    setTimeout(() => {
      if (_calibForceStep !== stepIdx) return; // already handled by corner_calib_result
      _calibForceStep = -1;
      if (!_calibCornerResults.find(r => r.step === c.step)) {
        _calibCornerResults.push({ step: c.step, yaw: 0, pitch: 0 });
      }
      _advanceCalibCorner(stepIdx);
    }, 3000);
  }, 10000);

  _calibWaitingStep = stepIdx;
}

function _advanceCalibCorner(stepIdx) {
  const c = CALIB_CORNERS[stepIdx];
  const textEl   = document.getElementById('calibText');
  const countEl  = document.getElementById('calibCountdown');
  const targetEl = document.getElementById(`calibTarget${c.corner}`);
  const dotEl    = document.getElementById(`calibDot${c.corner}`);

  if (targetEl) { targetEl.classList.remove('active'); targetEl.classList.add('done'); }
  if (dotEl)    { dotEl.classList.remove('active'); dotEl.classList.add('done'); dotEl.textContent = '✓'; }
  if (textEl)   textEl.textContent = `✓ ${T(c.labelKey)} ${T('calib_acquired')}`;
  if (countEl)  countEl.textContent = T('calib_next_corner');

  _calibWaitingStep = -1;
  if (_calibForceFallback) { clearTimeout(_calibForceFallback); _calibForceFallback = null; }
  playRecovery();
  setTimeout(() => _runCalibCornerStep(stepIdx + 1), 1200);
}

function _finalizeCornerCalib() {
  const overlay = document.getElementById('calibOverlay');
  const textEl  = document.getElementById('calibText');
  const countEl = document.getElementById('calibCountdown');
  if (textEl) textEl.textContent = T('calib_processing');
  if (countEl) countEl.textContent = '';

  // Wait 1.5s for any pending corner_calib_result from forced captures to arrive
  setTimeout(() => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({
        cmd: 'finalize_corner_calib',
        corners: _calibCornerResults,
        ear_baseline: S.calEAR || 0.28,
        neck_baseline: S.calNeckLength || 0,
      }));
    }

    setTimeout(() => {
      if (overlay) overlay.classList.remove('on');
      S.calibrated = true;
      fetch('/api/load-config')
        .then(r => r.ok ? r.json() : null)
        .then(cfg => {
          if (cfg) {
          S.calYaw = cfg.baseline_yaw || 0;
          S.calPitch = cfg.baseline_pitch || 0;
          S.calEAR = cfg.baseline_ear || 0.28;
          S.calNeckLength = cfg.baseline_neck_length || 0;
          S.calFocusHalfYaw = cfg.focus_half_yaw || 0;
          S.calFocusHalfPitch = cfg.focus_half_pitch || 0;
          console.log(`[CALIB] 四角校准完成: center=(${S.calYaw.toFixed(1)}, ${S.calPitch.toFixed(1)}), half=(${S.calFocusHalfYaw.toFixed(1)}, ${S.calFocusHalfPitch.toFixed(1)})`);
        }
      })
      .catch(e => console.warn('[CALIB] 拉取后端基准失败:', e));

    const calStatus = document.getElementById('calibStatus');
    if (calStatus) calStatus.textContent = T('calib_done_status');
    document.getElementById('calibBtn')?.classList.remove('active');
    playRecovery();
    addEv(T('ev_calib_done'), 'i');
    showToast(T('calib_done_toast'), 'success');
    _calibCornerResults = [];
    }, 500);
  }, 1500);
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

    const btn2 = document.getElementById('breakStartBtn');
    if (btn2) {
      const txt2 = btn2.querySelector('.md3-btn-text') || btn2.querySelector('span[data-i18n="start"]') || btn2;
      if (txt2 && txt2 !== btn2) txt2.textContent = T('break_stop');
      else btn2.textContent = T('break_stop');
      const ic2 = btn2.querySelector('.md3-btn-icon'); if (ic2) ic2.textContent = '🔔';
      btn2.classList.add('danger');
    }
    document.getElementById('breakStatus').textContent = T('break_on');
    addEv(T('break_started_ev'), 'i');
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
      showToast(T('break_end_toast'), 'info');
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
  const btn = document.getElementById('breakStartBtn');
  if (btn) {
    const txt = btn.querySelector('.md3-btn-text') || btn.querySelector('span[data-i18n="start"]') || btn;
    if (txt && txt !== btn) txt.textContent = T('break_start_label');
    else btn.textContent = T('break_start_label');
    const ic = btn.querySelector('.md3-btn-icon'); if (ic) ic.textContent = '☕';
    btn.classList.remove('danger');
  }
  document.getElementById('breakStatus').textContent = T('break_set_time');
  document.getElementById('breakTimer').textContent = '00:00';
  document.getElementById('breakArc').style.strokeDashoffset = 163.36;
  document.getElementById('pauseOverlay')?.classList.remove('on');
  S.lastChange = Date.now();
  addEv(T('break_end_ev'), 'i');
  // 系统通知：休息结束
  try { notifyOS('DoNotPlay 休息结束', '休息已完成，恢复专注吧！', 'w'); } catch(_){}
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
  // Windows 系统通知（仅 warning / error 弹出，避免正向事件打断专注）
  if (type === 'w' || type === 'e') {
    notifyOS('DoNotPlay', msg, type);
  }
}

// ── Windows 系统通知 (pywebview 环境，统一走后端 PowerShell + WinRT) ──
let _lastNotifyAt = 0;
let _lastNotifyKey = '';
function notifyOS(title, body, type = 'i') {
  const now = Date.now();
  const key = (title || '') + '|' + (body || '');
  if (now - _lastNotifyAt < 1500) return;
  if (key === _lastNotifyKey && now - _lastNotifyAt < 8000) return;
  _lastNotifyAt = now;
  _lastNotifyKey = key;
  try {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ cmd: 'notify_os', title: title || 'DoNotPlay', body: body || '', type }));
    }
  } catch (_) { /* ignore */ }
}



function updatePosture(noseY, faceH) {
  if (!S.calibrated) return;
  const drift = noseY - S.calNoseY;
  const tag = document.getElementById('postureTag');
  if (drift > 0.08) {
    if (tag) { tag.textContent = T('posture_bad'); tag.className = 'posture-indicator bad'; tag.style.display = 'block'; }
  } else {
    if (tag) { tag.textContent = T('posture_good'); tag.className = 'posture-indicator'; tag.style.display = 'block'; }
  }
}

async function updateHeatmap() {
  const container = document.getElementById('heatmap');
  if (!container) return;

  let hmData = {};
  try {
    const r = await fetch('/api/window/heatmap?days=30');
    if (r.ok) hmData = await r.json();
  } catch (_) { /* use empty data */ }

  const DAYS = 30;
  const slotNames = ['morning', 'afternoon', 'evening'];
  const slotLabels = [T('slot_morning'), T('slot_afternoon'), T('slot_evening')];
  let html = '';

  for (let i = DAYS - 1; i >= 0; i--) {
    const dObj = new Date();
    dObj.setMinutes(dObj.getMinutes() - dObj.getTimezoneOffset());
    const dateStr = new Date(dObj.getTime() - i * 86400000).toISOString().slice(0, 10);
    const dayData = hmData[dateStr] || {};

    for (let s = 0; s < 3; s++) {
      const slot = slotNames[s];
      const sec = dayData[slot] || 0;
      const hrs = sec / 3600;
      let bg = 'var(--bg3)';
      if (hrs >= 2) bg = 'var(--green)';
      else if (hrs >= 1) bg = 'var(--green-d)';
      else if (hrs >= 0.25) bg = 'rgba(129, 201, 149, 0.5)';
      const min = Math.round(sec / 60);
      const ck = hrs >= 2 ? '<span class="hm-check">✓</span>' : '';
      html += `<div class="hm-cell" style="background:${bg}" title="${dateStr} ${slotLabels[s]}: ${min}m ${T('hm_focus')}">${ck}</div>`;
    }
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

    S.tlData.push({
      validStatuses,
      time: timeStr,
      counts: { ...counts }
    });
    // 保留最近 30 分钟的数据，超出后才开始滚动
    if (S.tlData.length > 30) S.tlData.shift();

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
    init: { bg: 'var(--bg3)', lbl: T('tl_notstarted'), h: 0 },
    active: { bg: '#41af6a', lbl: T('tl_focused'), h: 100 },
    distracted: { bg: '#f4b451', lbl: T('tl_distracted_lbl'), h: 55 },
    away: { bg: '#90a4ae', lbl: T('tl_away_lbl'), h: 30 },
    phone: { bg: '#e53935', lbl: T('tl_phone_lbl'), h: 80 }
  };

  // 新数据从右侧出现：先在左侧填充占位条，再在右侧追加真实数据
  const TOTAL_BARS = 30;
  const need = TOTAL_BARS - S.tlData.length;
  for (let i = 0; i < Math.max(0, need); i++) {
    const ph = document.createElement('div');
    ph.className = 'tl-bar tl-bar-placeholder';
    ph.style.background = 'var(--bg3)';
    ph.style.opacity = '0.35';
    ph.style.height = '15%';
    ph.title = T('tl_notstarted');
    container.appendChild(ph);
  }

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
      if (det.active > 0) parts.push(`${T('tl_focused')} ${det.active}s`);
      if (det.distracted > 0) parts.push(`${T('tl_distracted_lbl')} ${det.distracted}s`);
      if (det.phone > 0) parts.push(`${T('tl_phone_lbl')} ${det.phone}s`);
      if (det.away > 0) parts.push(`${T('tl_away_lbl')} ${det.away}s`);

      el.title = `${T('tl_time')} ${item.time}\n${T('tl_stats')} ${parts.join(', ')}`;
    } else {
      el.style.background = 'var(--bg3)';
      el.style.height = '15%';
      el.style.opacity = '0.5';
      el.title = T('tl_notstarted');
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
      const startStr = data.start ? new Date(data.start).toLocaleTimeString() : (_lang==='zh'?'历史':'History');
      const endStr = data.end ? new Date(data.end).toLocaleTimeString() : (_lang==='zh'?'存量':'Legacy');
      barEl.title = `${T('tl_period')} ${startStr} - ${endStr}\n${T('tl_duration')} ${fmtMsLong(data.ms)}`;
    } else {
      valEl.textContent = '-';
      barEl.title = T('tl_norecord');
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
      el.textContent = _lang==='zh'?'🌟 已登榜':'🌟 On leaderboard';
      el.style.color = 'var(--green)';
    } else if (bronzeMs > 0) {
      const diff = bronzeMs - curMs;
      el.textContent = `${T('lb_gap_to_3rd')} ${fmtMsShort(diff)}`;
      el.style.color = 'var(--hint)';
    } else {
      el.textContent = T('lb_no_record');
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
    init:       { bg: 'var(--bg3)',      tc: 'var(--muted)',  lbl: T('st_init') },
    active:     { bg: 'var(--green-bg)', tc: 'var(--green)',  lbl: T('st_running') },
    distracted: { bg: 'var(--amber-bg)', tc: 'var(--amber)',  lbl: T('st_distracted') },
    away:       { bg: 'var(--red-bg)',   tc: 'var(--red-d)',  lbl: T('st_away') },
    phone:      { bg: 'var(--red-bg)',   tc: 'var(--red-d)',  lbl: T('st_phone') }
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

  // 状态发生改变时：重置计时器并立即清除所有告警样式
  if (nextState !== m.cur) {
    m.cur = nextState;
    m.start = now;
    card.classList.remove('t-warn', 't-danger', 'warn', 'danger', 'strong-warn', 't-unavailable', 't-stale');
  }

  // 基础状态处理
  if (nextState === 'unavailable') {
    card.classList.add('t-unavailable');
    return;
  }
  if (nextState === 'stale') {
    card.classList.add('t-stale');
    return;
  }

  // 正常/无效/错误状态：防御性清除所有告警样式，立即恢复原色
  if (!nextState || nextState === 'normal' || nextState === 'error') {
    card.classList.remove('t-warn', 't-danger', 'warn', 'danger');
    return;
  }

  // 告警状态（warn / danger）：仅在持续时间达到阈值后才变色，减少对用户的干扰
  // 持续 30 秒 → 变黄；持续 60 秒 → 变红；未达阈值保持原样
  const elapsed = now - m.start;
  if (elapsed >= 60000) {
    card.classList.add('danger');
    card.classList.remove('warn', 't-warn', 't-danger');
  } else if (elapsed >= 30000) {
    card.classList.add('warn');
    card.classList.remove('danger', 't-warn', 't-danger');
  } else {
    card.classList.remove('t-warn', 't-danger', 'warn', 'danger');
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
      
      if (!available) return { text: _lang==='zh'?'未检测到':'Not detected', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: _lang==='zh'?'传感器离线':'Sensor offline', detail: 'Stale', state: 'stale', available: true, stale: true };

      const fatigueState = S.fatigueState || 'normal';
      const val = S.eyeFatigue || 0;
      let text = _lang==='zh'?'良好':'Good';
      let state = 'normal';

      if (fatigueState === 'severe') {
        const severeDurationMs = S.severeFatigueStart > 0 ? (Date.now() - S.severeFatigueStart) : 0;
        if (severeDurationMs >= 10 * 60 * 1000) {
          text = _lang==='zh'?'极度疲劳':'Extreme fatigue'; state = 'danger';
        } else {
          text = _lang==='zh'?'严重疲劳':'Severe fatigue'; state = 'warn';
        }
      } else if (fatigueState === 'moderate') {
        text = _lang==='zh'?'中度疲劳':'Moderate fatigue'; state = 'warn';
      } else if (fatigueState === 'mild') {
        text = _lang==='zh'?'轻微疲劳':'Mild fatigue'; state = 'normal';
      }
      
      return { text: `${text} (${Math.round(val)}%)`, detail: Math.round(val), state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Fatigue Error:', e);
      return { text: _lang==='zh'?'指标故障':'Error', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretGaze: () => {
    try {
      const available = !!S.lastGazeUpdate && S.lastGazeUpdate > 0;
      const stale = available && (Date.now() - S.lastGazeUpdate > 8000);
      
      if (!available) return { text: _lang==='zh'?'未锁定':'Unlocked', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: '跟踪丢失', detail: 'Stale', state: 'stale', available: true, stale: true };

      const angle = S.gazeAngle || { yaw: 0, pitch: 0 };
      // 优先使用后端计算的有效阈值（已考虑四角校准+灵敏度），降级到前端预设
      const effYaw   = (angle.yaw_threshold   && angle.yaw_threshold   > 0) ? angle.yaw_threshold   : (SENS_PRESETS[sensIdx] || SENS_PRESETS[1]).yaw;
      const effPitch = (angle.pitch_threshold && angle.pitch_threshold > 0) ? angle.pitch_threshold : (SENS_PRESETS[sensIdx] || SENS_PRESETS[1]).pitch;
      const offX = Math.abs(angle.yaw) / effYaw;
      const offY = Math.abs(angle.pitch) / effPitch;
      const ratio = Math.max(offX, offY);

      S.gazeHistory.push(ratio);
      if (S.gazeHistory.length > 5) S.gazeHistory.shift();
      const smoothedRatio = MetricInterpreter.avg(S.gazeHistory);

      // 若后端已判定 off_screen，直接显示游离
      const backendOffScreen = angle.state === 'off_screen';
      let text = _lang==='zh'?'实时专注':'On track';
      let state = 'normal';
      if (backendOffScreen || smoothedRatio >= 1.0) { text = _lang==='zh'?'视线游离':'Off screen'; state = 'danger'; }
      else if (smoothedRatio >= 0.7) { text = _lang==='zh'?'注意力偏转':'Drifting'; state = 'warn'; }

      return { text, detail: smoothedRatio.toFixed(1), state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Gaze Error:', e);
      return { text: _lang==='zh'?'指标故障':'Error', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretNeck: () => {
    try {
      const available = !!S.lastNeckUpdate && S.lastNeckUpdate > 0;
      const stale = available && (Date.now() - S.lastNeckUpdate > 8000);
      
      if (!available) return { text: _lang==='zh'?'等待数据':'Waiting', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: _lang==='zh'?'姿态丢失':'Pose lost', detail: 'Stale', state: 'stale', available: true, stale: true };

      const pct = S.neckStrain?.pct || (S.neckStrain?.ratio ? S.neckStrain.ratio * 100 : 100);
      S.neckHistory.push(pct);
      if (S.neckHistory.length > 5) S.neckHistory.shift();
      const smoothed = MetricInterpreter.avg(S.neckHistory);

      let text = _lang==='zh'?'颈部挺直':'Upright';
      let state = 'normal';
      if (smoothed < 70) { text = _lang==='zh'?'严重前倾':'Severe lean'; state = 'danger'; }
      else if (smoothed < 85) { text = _lang==='zh'?'颈部压迫':'Neck strain'; state = 'warn'; }

      return { text, detail: smoothed, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Neck Error:', e);
      return { text: _lang==='zh'?'指标故障':'Error', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretShoulder: () => {
    try {
      const available = !!S.lastShoulderUpdate && S.lastShoulderUpdate > 0;
      const stale = available && (Date.now() - S.lastShoulderUpdate > 8000);
      
      if (!available) return { text: _lang==='zh'?'扫描中':'Scanning', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: _lang==='zh'?'骨架丢失':'Skeleton lost', detail: 'Stale', state: 'stale', available: true, stale: true };

      const tilt = S.shoulderSymmetry?.tilt !== undefined ? S.shoulderSymmetry.tilt : 0;
      S.shoulderHistory.push(tilt);
      if (S.shoulderHistory.length > 5) S.shoulderHistory.shift();
      const smoothed = MetricInterpreter.avg(S.shoulderHistory);
      const absTilt = Math.abs(smoothed);

      let text, state = 'normal';
      if (absTilt > 10.0) { text = _lang==='zh'?'严重侧倾':'Severe tilt'; state = 'danger'; }
      else if (absTilt > 5.0) { text = _lang==='zh'?'轻微侧倾':'Slight tilt'; state = 'warn'; }
      else { text = _lang==='zh'?'端正':'Level'; }

      if (absTilt > 2.0) {
        const dir = smoothed > 0 ? 'R' : 'L';
        text += ` ${dir}${absTilt.toFixed(0)}°`;
      }
      return { text, detail: smoothed, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Shoulder Error:', e);
      return { text: _lang==='zh'?'故障':'Error', detail: '-', state: 'error', available: false, stale: false };
    }
  },

  interpretHydration: () => {
    try {
      const stale = MetricInterpreter.isStale(S.lastHydrationUpdate);
      const available = !!S.lastHydrationUpdate;
      if (!available) return { text: _lang==='zh'?'暂无数据':'No data', detail: '-', state: 'unavailable', available: false, stale: false };
      if (stale) return { text: _lang==='zh'?'数据过期':'Stale data', detail: '-', state: 'stale', available: true, stale: true };

      const mins = Math.round(S.hydrationMinutes);
      let text, state = '';
      if (mins < 0) {
        text = _lang==='zh'?'还没喝水':'Not yet';
      } else if (mins === 0) {
        text = _lang==='zh'?'刚才':'Just now';
      } else {
        text = `${mins} ${T('mins_ago')}`;
      }
      if (mins > 60) state = 'warn';
      if (mins > 120) state = 'danger';

      return { text, detail: mins, state, available: true, stale: false };
    } catch (e) {
      console.error('[INTERPRET] Hydration Error:', e);
      return { text: _lang==='zh'?'指标异常':'Error', detail: '-', state: 'error', available: false, stale: false };
    }
  }
};


function renderThrottledMetrics() {
  const now = Date.now();
  if (now - (S.lastMetricUIUpdate || 0) < 500) return;
  S.lastMetricUIUpdate = now;

  const metrics = [
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

  // 连续用眼卡片
  const eyeUsageEl = document.getElementById('cEyeUsage');
  if (eyeUsageEl) {
    const m = S.eyeMinutes || 0;
    eyeUsageEl.textContent = m >= 60 ? `${Math.floor(m/60)}h${m%60}m` : `${m}${T('mins_unit')}`;
    eyeUsageEl.style.color = m >= 60 ? 'var(--red)' : m >= 40 ? 'var(--amber)' : 'var(--text)';
    const card = document.getElementById('cardEyeUsage');
    if (card) {
      card.classList.toggle('warn', m >= 40 && m < 60);
      card.classList.toggle('danger', m >= 60);
    }
  }
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
    addEv(_lang==='zh'?'已连接到后端':'Connected to backend', 'g');
    S.camOn = true;
    S.warmupUntil = Date.now() + 5000; // 启动预热：5秒内不触发弹窗
    setStatus('active');
    sendSensUpdate();
    // 关键修复：WS 重连后强制清除后端可能残留的 paused 状态（解决"休息模式：核心已关闭"卡死）
    if (!breakTimerObj.active) {
      try { _ws.send(JSON.stringify({ cmd: 'pause_monitoring', paused: false })); } catch(_){}
    }
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
        if (sBtn) sBtn.textContent = (T('sens_btn').split(':')[0]) + ': ' + T('sens_' + sensIdx);
        console.log('[WS-DATA] 收到后端下发的配置同步指令');
      } else if (data.type === 'soft_refresh_done') {
        console.log('[WS-DATA] 后端通知刷新已完成，正在重启页面...');
        window.location.reload();
      } else {
        // 处理四角校准角落结果（手动 end_corner_step 触发）
        if (data.corner_calib_result) {
          const r = data.corner_calib_result;
          _calibCornerResults = _calibCornerResults.filter(c => c.step !== r.step);
          _calibCornerResults.push(r);
          console.log(`[CALIB-CORNER] 收到角落 ${r.step} 结果: yaw=${r.yaw}, pitch=${r.pitch}`);
          // Advance UI if this was a force-captured step
          if (_calibForceStep === r.step) {
            _calibForceStep = -1;
            const fi = CALIB_CORNERS.findIndex(c => c.step === r.step);
            if (fi >= 0) _advanceCalibCorner(fi);
          }
        }
        // 自动稳定就绪通知（backend 主动触发）
        if (data.corner_calib_ready) {
          const r = data.corner_calib_ready;
          _calibCornerResults = _calibCornerResults.filter(c => c.step !== r.step);
          _calibCornerResults.push(r);
          if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify({ cmd: 'end_corner_step' }));
          }
          if (_calibWaitingStep >= 0 && _calibWaitingStep < CALIB_CORNERS.length) {
            const stepIdx = _calibWaitingStep;
            _advanceCalibCorner(stepIdx);
            console.log(`[CALIB-CORNER] 角落 ${r.step} 自动就绪: yaw=${r.yaw}, pitch=${r.pitch}`);
          }
        }
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
  const goalMin = gInput ? (parseInt(gInput.value) || 300) : 300;
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
  if (gProg) gProg.textContent = `${Math.floor(curMin)}m / ${goalMin}m ${T('goal_focus')}`;

  const sgInput = document.getElementById('sessionGoalInput');
  const sessGoalMin = sgInput ? (parseInt(sgInput.value) || 60) : 60;
  const sessCurMin = S.phaseFocusMs / 60000;
  const sessPct = Math.min(Math.round((sessCurMin / sessGoalMin) * 100), 100);

  if (document.getElementById('sessionGoalArc')) document.getElementById('sessionGoalArc').style.strokeDashoffset = 163.36 * (1 - sessPct / 100);
  if (document.getElementById('sessionGoalPct')) document.getElementById('sessionGoalPct').textContent = sessPct + '%';
  if (document.getElementById('sessionGoalProgress')) document.getElementById('sessionGoalProgress').textContent = `${Math.floor(sessCurMin)}m / ${sessGoalMin}m ${T('goal_focus')}`;

  if (sessCurMin >= sessGoalMin && !S.breakShown) {
    document.getElementById('brkBanner')?.classList.add('on');
    S.breakShown = true;
    playAlert();
  }

  updateBreakUI();

  const totalActive = S.focusMs + S.distMs + S.awayMs + S.phoneMs;
  const fPct = totalActive > 0 ? Math.round((S.focusMs / totalActive) * 100) : 0;
  const fScoreEl = document.getElementById('fScore');
  const fFillEl = document.getElementById('fFill');
  if (fScoreEl) fScoreEl.textContent = fPct + '%';
  if (fFillEl) fFillEl.style.width = fPct + '%';

  const fiveMinsAgo = now - 5 * 60 * 1000;
  const recentHist = S.focusHist.filter(x => x.t > fiveMinsAgo);
  const rPct = recentHist.length > 0 ? Math.round((recentHist.filter(x => x.f).length / recentHist.length) * 100) : 0;
  const rScoreEl = document.getElementById('rScore');
  const rFillEl = document.getElementById('rFill');
  if (rScoreEl) rScoreEl.textContent = rPct + '%';
  if (rFillEl) rFillEl.style.width = rPct + '%';

  const todaySessions = ensureTodayStats().sessions || 1;
  const goalTodayEl = document.getElementById('goalToday');
  if (goalTodayEl) goalTodayEl.textContent = `${T('goal_today_sessions')} ${todaySessions}`;

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
  const eye_fatigue = null;  // 已废弃，改用 eye_usage
  const gaze_angle = null;   // 已废弃，视线追踪已移除
  const eye_usage = data.eye_usage || null;
  const neck_strain = data.neck_strain || null;
  const shoulder_symmetry = data.shoulder_symmetry || null;

  // 同步后端数据到全局状态对象 S 并记录更新时间
  // 连续用眼数据
  if (eye_usage) {
    S.eyeTimeSec = eye_usage.eye_time_sec || 0;
    S.eyeMinutes = eye_usage.eye_minutes || 0;
    S.eyeNeedLongRest = !!eye_usage.need_long_rest;
    if (eye_usage.short_rest_trigger) {
      _showShortRestToast();
    }
  }
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
      addEv(_lang==='zh'?'检测到饮水，计时器已重置':'Hydration detected, timer reset', 'g');
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
    if (S.status !== 'active') { playFocusIn(); addEv(T('ev_focus_in'), 'g'); }
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
    if (S.status !== 'distracted') { addEv(T('ev_dist'), 'w'); S._distNotifyAcc = 0; }
    S.distMs += dt; today.distMs += dt; S.phaseDistMs += dt;
    // 走神累计每满 1 分钟推一次系统通知
    S._distNotifyAcc = (S._distNotifyAcc || 0) + dt;
    if (S._distNotifyAcc >= 60000) {
      S._distNotifyAcc -= 60000;
      const mins = Math.floor(((S.phaseDistMs || 0) / 60000));
      try { notifyOS('DoNotPlay 走神警告', `已连续走神累计 ${mins} 分钟，快回来专注！`, 'w'); } catch(_){}
    }
  }
  else if (status === 'phone') {
    const today = ensureTodayStats();
    if (S.status !== 'phone') { addEv(T('ev_phone'), 'w'); }
    S.phoneMs += dt; today.phoneMs += dt; S.phasePhoneMs += dt;
  }
  else if (status === 'away') {
    const today = ensureTodayStats();
    if (S.status !== 'away') { addEv(T('ev_away'), 'w'); }
    S.awayMs += dt; today.awayMs += dt; S.phaseAwayMs += dt;
  }

  // 3. 连续专注中断逻辑
  if (S.inStreak && status !== 'active') {
    if (S.interruptionStartMs === 0) S.interruptionStartMs = now;
    if (now - S.interruptionStartMs > 3000) {
      const reason = (status === 'phone') ? T('ev_phone') : (status === 'away' ? T('ev_away') : T('ev_dist'));
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

  // 5. 统一弹窗激活逻辑 (Popup State Sync) — 粘性版：抗闪烁
  // 规则：弹窗一旦弹出至少保持 minVisibleMs；隐藏前需要"应该隐藏"信号稳定 hideDebounceMs
  // 全局粘性辅助：返回 'shown' (本帧首次出现) | 'visible' | 'hidden' | 'kept' (条件false但仍粘住)
  const _stickyPopup = (el, shouldShow, opts = {}) => {
    if (!el) return 'hidden';
    const { minVisibleMs = 2500, hideDebounceMs = 800 } = opts;
    const t = Date.now();
    el._stick = el._stick || { showAt: 0, falseSince: 0 };
    if (shouldShow) {
      el._stick.falseSince = 0;
      if (!el.classList.contains('show')) {
        el._stick.showAt = t;
        el.classList.add('show');
        return 'shown';
      }
      return 'visible';
    } else {
      if (!el._stick.falseSince) el._stick.falseSince = t;
      if (el.classList.contains('show')) {
        const visDur = t - (el._stick.showAt || t);
        const falseDur = t - el._stick.falseSince;
        if (visDur >= minVisibleMs && falseDur >= hideDebounceMs) {
          el.classList.remove('show');
          el._stick.showAt = 0;
          el._stick.falseSince = 0;
          return 'hidden';
        }
        return 'kept';
      }
      return 'hidden';
    }
  };

  // A. 手机预警（支持手动关闭后 5 秒冷却期）
  const phonePop = document.getElementById('doNotPlayPopup');
  if (phonePop) {
    const want = phoneAlertActive && now > (S._phoneDismissUntil || 0) && now > (S.warmupUntil || 0);
    const r = _stickyPopup(phonePop, want, { minVisibleMs: 2500, hideDebounceMs: 800 });
    if (r === 'shown') { playAlert(); console.info('[UI-POPUP] 手机检测激活'); }
  }

  // B. 分心预警（独立计时器 distractedSince，必须连续 5s）
  const distPop = document.getElementById('distractedWarningPopup');
  if (distPop) {
    let want = false;
    if (status === 'distracted' && now > (S.warmupUntil || 0)) {
      if (S.status !== 'distracted') S.distractedSince = now;
      if (S.distractedSince > 0 && (now - S.distractedSince > 5000) && now > (S._distractedDismissUntil || 0)) {
        want = true;
      }
    } else {
      S.distractedSince = 0;
    }
    const r = _stickyPopup(distPop, want, { minVisibleMs: 3000, hideDebounceMs: 1000 });
    if (r === 'shown') { playDistractedTick(); console.info('[UI-POPUP] 分心预警激活'); }
  }

  // C. 离开预警 (后端 status 字段驱动)
  const awayPop = document.getElementById('awayWarningPopup');
  if (awayPop) {
    const want = (status === 'away' && now > (S.warmupUntil || 0));
    const r = _stickyPopup(awayPop, want, { minVisibleMs: 3000, hideDebounceMs: 1500 });
    if (r === 'shown') { S.awayAlertActive = true; playSoftAlert(); console.info('[UI-POPUP] 离开预警激活'); }
    else if (r === 'hidden' && S.awayAlertActive) { S.awayAlertActive = false; S.lastFace = now; console.info('[UI-POPUP] 离开预警清除'); }
  }

  // D. 姿态预警 (持续 10s 驼背)
  const postPop = document.getElementById('postureWarningPopup');
  if (postPop) {
    let want = false;
    if (isSlouching && status !== 'away' && now > (S.warmupUntil || 0)) {
      if (!S._slouchSince) S._slouchSince = now;
      if ((now - S._slouchSince) > 10000 && now > (S._postureDismissUntil || 0)) want = true;
    } else {
      S._slouchSince = 0;
    }
    const r = _stickyPopup(postPop, want, { minVisibleMs: 4000, hideDebounceMs: 1500 });
    if (r === 'shown') { playPostureWarn(); console.info('[UI-POPUP] 姿势预警激活'); }
  }

  // E. 连续用眼 60 分钟警告
  const drowsyPop = document.getElementById('drowsyWarningPopup');
  if (drowsyPop) {
    const want = (S.eyeNeedLongRest && status !== 'away' && now > (S.warmupUntil || 0)
                  && now > (S._drowsyDismissUntil || 0) && !S._fatigueDisabledForSession);
    const r = _stickyPopup(drowsyPop, want, { minVisibleMs: 4000, hideDebounceMs: 1500 });
    if (r === 'shown') { playDrowsyAlert(); console.info('[UI-POPUP] 连续用眼60分钟预警激活'); }
  }

  // F. 饮水提醒 — 仅在用户在场 (非 away) 时提醒
  const waterPop = document.getElementById('waterWarningPopup');
  if (waterPop) {
    const want = (S.hydrationMinutes > 60 && status !== 'away' && now > (S.warmupUntil || 0) && !S.waterAlertShown);
    const r = _stickyPopup(waterPop, want, { minVisibleMs: 4000, hideDebounceMs: 1500 });
    if (r === 'shown') { S.waterAlertShown = true; playSoftAlert(); console.info('[UI-POPUP] 饮水预警激活'); }
    else if (r === 'hidden') { S.waterAlertShown = false; }
  }

  // 6. 状态同步到仪表盘
  setStatus(status);

  // 7. 窗口活动面板
  if (data.window_activity) {
    renderWinActivity(data.window_activity);
  }

  // 8. 诊断信息处理
  const postTag = document.getElementById('postureTag');
  if (postTag) {
    if (data.is_stuck) { postTag.textContent = _lang==='zh'?'检测器响应缓慢，请点击刷新':'Detector slow, click Refresh'; postTag.style.color = '#f44336'; }
    else if (!faceDetected && (data.personDetected || data.poseValid)) { postTag.textContent = _lang==='zh'?'检测到身体，请确保头部在画面中央':'Body detected, center your face'; postTag.style.color = '#ff9800'; }
    else if (faceDetected) {
      if (postTag.textContent.includes('检测') || postTag.textContent.includes('响应') || postTag.textContent.includes('slow') || postTag.textContent.includes('detected')) { postTag.textContent = T('posture_good'); postTag.style.color = ''; }
    }
  }
}

function initCam() {
  if (vidStream) {
    vidStream.src = '/video_feed?t=' + Date.now();
    vidStream.onload = () => { if (document.getElementById('noCam')) document.getElementById('noCam').style.display = 'none'; };
  }
  // WebSocket 连接已在 start() 中建立，不在此重复调用
}

// ── 窗口活动监控 UI ──
const _waCache = { lastTodayFetch: 0, lastUnknownFetch: 0 };
let _lastWaState = null;
let _distSoundTimer = null;

function _playBeep(freq, dur, vol) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = 'sine'; osc.frequency.value = freq;
    gain.gain.setValueAtTime(0, ctx.currentTime);
    gain.gain.linearRampToValueAtTime(vol, ctx.currentTime + 0.02);
    gain.gain.linearRampToValueAtTime(0, ctx.currentTime + dur);
    osc.start(ctx.currentTime); osc.stop(ctx.currentTime + dur + 0.05);
    setTimeout(() => { try { ctx.close(); } catch(_){} }, (dur + 0.15) * 1000);
  } catch (_) {}
}

function _startDistSound() {
  // 优化：只在进入走神状态时响一次，避免每 2s 重复刷屏
  playWarning();
}

function _stopDistSound() {
  if (_distSoundTimer) { clearInterval(_distSoundTimer); _distSoundTimer = null; }
}

function _secToMin(sec) {
  const m = Math.floor(sec / 60);
  return m + 'm';
}

function _formatStay(sec) {
  if (!sec || sec < 0) return '0s';
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}m${s}s` : `${m}m`;
}

function renderWinActivity(wa) {
  const chip    = document.getElementById('winStateChip');
  const procEl  = document.getElementById('waProc');
  const titleEl = document.getElementById('waTitle');
  const stayEl  = document.getElementById('waStay');
  if (!chip) return;

  const st = wa.state || 'unknown';
  if (_lastWaState !== null && _lastWaState !== st) {
    if (st === 'distracted') { _stopDistSound(); _startDistSound(); }
    else { _stopDistSound(); if (_lastWaState === 'distracted') playRecovery(); }
  }
  _lastWaState = st;
  chip.className = 'wa-chip wa-' + st;
  const stTxt = st === 'focus' ? '✅ 专注'
              : st === 'distracted' ? '🚫 走神'
              : st === 'neutral' ? '⚪ 中性'
              : '❓ 待定';
  chip.textContent = stTxt;

  if (procEl)  procEl.textContent  = wa.proc  || '—';
  if (titleEl) titleEl.textContent = wa.title || T('win_waiting');
  if (stayEl)  stayEl.textContent  = _formatStay(wa.stay_sec);

  // Recent windows (multi-monitor)
  const recentEl = document.getElementById('waRecentList');
  if (recentEl && wa.recent_windows && wa.recent_windows.length > 1) {
    const now = Date.now() / 1000;
    const recents = wa.recent_windows.filter(w => (now - w.ts) < 300 && w.title !== wa.title).slice(0, 3);
    if (recents.length) {
      recentEl.innerHTML = recents.map(w => {
        const name = (w.proc || '').replace(/\.exe$/i, '');
        const title = (w.title || '').slice(0, 28);
        const sc = w.state === 'focus' ? 'wl-focus' : w.state === 'distracted' ? 'wl-dist' : 'wl-unk';
        return `<div class="wa-recent-item"><span class="wa-recent-dot ${sc}"></span><span class="wa-recent-name">${name}</span><span class="wa-recent-title">${title}</span></div>`;
      }).join('');
      recentEl.style.display = '';
    } else {
      recentEl.style.display = 'none';
    }
  } else if (recentEl) {
    recentEl.style.display = 'none';
  }

  const now = Date.now();
  if (now - _waCache.lastTodayFetch > 15000) {
    _waCache.lastTodayFetch = now;
    _fetchWinToday();
  }
}

async function _fetchWinToday() {
  try {
    const r = await fetch('/api/window/today');
    if (!r.ok) return;
    const rows = await r.json();
    let focusSec = 0, distSec = 0;
    for (const row of rows) {
      if (row.state === 'focus') focusSec += row.total_sec;
      else if (row.state === 'distracted') distSec += row.total_sec;
    }
    const el = (id) => document.getElementById(id);
    if (el('waTodayFocus')) el('waTodayFocus').textContent = _secToMin(focusSec);
    if (el('waTodayDist'))  el('waTodayDist').textContent  = _secToMin(distSec);
  } catch (_) { /* silent */ }
}

async function _fetchWinUnknown() {
  try {
    const r = await fetch('/api/window/unknown');
    if (!r.ok) return;
    const rows = await r.json();
    const section = document.getElementById('waUnknownSection');
    const listEl  = document.getElementById('waUnknownList');
    const cntEl   = document.getElementById('waUnknownCnt');
    if (!listEl) return;
    if (!rows.length) { if (section) section.style.display = 'none'; return; }
    if (section) section.style.display = '';
    if (cntEl) cntEl.textContent = rows.length;
    listEl.innerHTML = rows.slice(0, 6).map(row => {
      const label = `${row.proc_name}: ${(row.title || '').slice(0, 22)}`;
      return `<div class="wa-unknown-item">
        <span class="wa-item-label" title="${row.proc_name}: ${row.title}">${label}</span>
        <button class="wa-btn wa-btn-focus" onclick="_classifyWin(${row.id},'focus')">${T('st_focus')}</button>
        <button class="wa-btn wa-btn-dist"  onclick="_classifyWin(${row.id},'distracted')">${T('st_distracted_short')}</button>
      </div>`;
    }).join('');
  } catch (_) { /* silent */ }
}

async function _classifyWin(id, state) {
  try {
    await fetch('/api/window/classify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, state })
    });
    _waCache.lastTodayFetch = 0;
    _waCache.lastUnknownFetch = 0;
    _fetchWinToday();
    _fetchWinUnknown();
    _renderWinV3();
  } catch (_) { /* silent */ }
}

// ════════════════════════════════════════════════════════════════
//  v3 窗口面板：三色统计 / Top5 双栏 / 待分类拨杆 / 分心提醒
// ════════════════════════════════════════════════════════════════

function _fmtMin(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return h + 'h' + (m ? m + 'm' : '');
}

function _stateZh(s) {
  return s === 'focus' ? '专注' : s === 'distracted' ? '走神' :
         s === 'neutral' ? '中性' : '待定';
}

async function _fetchWinV3Stats() {
  try {
    const r = await fetch('/api/window/stats_today');
    if (!r.ok) return;
    const d = await r.json();
    const total = Math.max(1, d.total || 0);
    const segs = [
      ['waSegFocus', 'waStatFocus', d.focus],
      ['waSegDist',  'waStatDist',  d.distracted],
      ['waSegNeu',   'waStatNeu',   d.neutral],
      ['waSegUnk',   'waStatUnk',   d.unknown],
    ];
    for (const [segId, lblId, sec] of segs) {
      const seg = document.getElementById(segId);
      const lbl = document.getElementById(lblId);
      if (seg) seg.style.width = ((sec / total) * 100).toFixed(1) + '%';
      if (lbl) lbl.textContent = _fmtMin(sec);
    }
  } catch (_) {}
}

async function _fetchWinV3Top() {
  try {
    const r = await fetch('/api/window/top?n=5');
    if (!r.ok) return;
    const d = await r.json();
    const render = (items, mountId) => {
      const el = document.getElementById(mountId);
      if (!el) return;
      if (!items.length) {
        el.innerHTML = '<div class="wa-top-empty">— 暂无 —</div>';
        return;
      }
      const max = Math.max(1, items[0].total);
      el.innerHTML = items.map(it => {
        const w = ((it.total / max) * 100).toFixed(0);
        const titleStr = (it.sample_titles || []).slice(0, 3)
          .map(s => `${s.title.replace(/</g,'&lt;')} (${_fmtMin(s.sec)})`).join('\n');
        const safeName = (it.display || '').replace(/</g,'&lt;');
        const safeProc = (it.proc_raw || '').replace(/'/g,"\\'");
        return `<div class="wa-top-row" title="${titleStr}">
          <span class="wa-top-name">${safeName}</span>
          <div class="wa-top-bar"><div class="wa-top-fill" style="width:${w}%"></div></div>
          <span class="wa-top-dur">${_fmtMin(it.total)}</span>
          <span class="wa-top-acts">
            <button class="wa-mini wa-mini-f" title="标记专注" onclick="_classifyDisplay('${safeName.replace(/'/g,"\\'")}','${safeProc}','focus')">F</button>
            <button class="wa-mini wa-mini-d" title="标记走神" onclick="_classifyDisplay('${safeName.replace(/'/g,"\\'")}','${safeProc}','distracted')">D</button>
            <button class="wa-mini wa-mini-n" title="标记中性/重置" onclick="_classifyDisplay('${safeName.replace(/'/g,"\\'")}','${safeProc}','neutral')">N</button>
          </span>
        </div>`;
      }).join('');
    };
    render(d.focus || [], 'waTopFocus');
    render(d.distracted || [], 'waTopDist');
  } catch (_) {}
}

async function _classifyDisplay(display, proc, state, kind) {
  try {
    await fetch('/api/window/classify_display', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ display, proc, state, kind })
    });
    _renderWinV3();
    _renderAllRules();
  } catch (_) {}
}

function _toggleSec(panelId) {
  const p = document.getElementById(panelId);
  const arr = document.getElementById(panelId + 'Arr');
  if (!p) return;
  const isOpen = p.style.display !== 'none' && !p.hasAttribute('data-user-collapsed');
  if (isOpen) {
    p.style.display = 'none';
    p.setAttribute('data-user-collapsed', '');
  } else {
    p.style.display = '';
    p.removeAttribute('data-user-collapsed');
  }
  if (arr) arr.textContent = isOpen ? '▸' : '▾';
  if (!isOpen) {
    if (panelId === 'wlRules') { _renderAllRules(); _initRuleTabs(); }
  }
}

// ── Bento 单一右侧抽屉（合并：摄像头 + 窗口监测 + 日志） ──
function _initDrawer() {
  const bodyR = document.getElementById('drawerBody');
  if (bodyR) {
    // 摄像头面板优先排在最前面
    document.querySelectorAll('[data-drawer-item-left]').forEach(el => {
      if (el.parentElement !== bodyR) bodyR.appendChild(el);
    });
    document.querySelectorAll('[data-drawer-item]').forEach(el => {
      if (el.parentElement !== bodyR) bodyR.appendChild(el);
    });
    // 移入抽屉后强制重连摄像头流（DOM 重 parent 会断开 MJPEG 流）
    setTimeout(() => {
      const vs = document.getElementById('vid_stream');
      if (vs) vs.src = '/video_feed?t=' + Date.now();
      // 规则总表默认展开时主动渲染一次
      try { _renderAllRules(); _initRuleTabs(); } catch(_) {}
    }, 100);
  }
  const btnR = document.getElementById('drawerBtn');
  const btnL = document.getElementById('drawerLeftBtn');
  const btnM = document.getElementById('menuBtn');
  if (btnR) btnR.onclick = () => _toggleDrawer();
  if (btnL) btnL.onclick = () => _toggleDrawer();
  if (btnM) btnM.onclick = () => _toggleDrawer();
  // 点校准时自动呼出抽屉（避免看不到摄像头校准透视）
  const calibBtn = document.getElementById('calibBtn');
  if (calibBtn) calibBtn.addEventListener('click', () => _toggleDrawer(true), true);
}
function _toggleDrawer(force) {
  const d = document.getElementById('drawer');
  const m = document.getElementById('drawerMask');
  if (!d) return;
  const willOpen = (typeof force === 'boolean') ? force : !d.classList.contains('open');
  d.classList.toggle('open', willOpen);
  if (m) m.classList.toggle('show', willOpen);
  if (willOpen) {
    try { _renderAllRules(); } catch(_) {}
    const vs = document.getElementById('vid_stream');
    if (vs && !vs.src) vs.src = '/video_feed?t=' + Date.now();
  }
}
function _toggleDrawerLeft(force) { _toggleDrawer(force); }
function _anyDrawerOpen() {
  const a = document.getElementById('drawer');
  return !!(a && a.classList.contains('open'));
}
function _closeAllDrawers() { _toggleDrawer(false); }
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initDrawer);
} else {
  _initDrawer();
}

// ── 完整规则总表（程序 / 网站，每条可拨档；固定仅今日）──
async function _renderAllRules() {
  try {
    const r = await fetch('/api/window/all_rules?today_only=1');
    if (!r.ok) return;
    const d = await r.json();
    _drawRuleList('wlRuleProc', d.procs || [], 'proc');
    _drawRuleList('wlRuleSite', d.sites || [], 'site');
  } catch (_) {}
}

function _drawRuleList(mountId, items, kind) {
  const el = document.getElementById(mountId);
  if (!el) return;
  if (!items.length) {
    el.innerHTML = '<div class="wa-rules-empty">— 暂无规则 —</div>';
    return;
  }
  // 按 state 分组
  const groups = { focus: [], distracted: [], neutral: [], unknown: [] };
  for (const it of items) (groups[it.state] || (groups[it.state] = [])).push(it);
  const gHead = (s) => s === 'focus' ? '✅ 专注' : s === 'distracted' ? '🚫 走神' : s === 'neutral' ? '⚪ 中性' : '❓ 待分类（今日访问过）';
  const gColor = (s) => s === 'focus' ? '#22c55e' : s === 'distracted' ? '#f97316' : s === 'neutral' ? '#94a3b8' : '#a78bfa';

  let html = '';
  for (const st of ['unknown', 'focus', 'distracted', 'neutral']) {
    const arr = groups[st] || [];
    if (!arr.length) continue;
    html += `<div class="wa-rules-group">
      <div class="wa-rules-ghead" style="color:${gColor(st)}">${gHead(st)}（${arr.length}）</div>`;
    for (const it of arr) {
      const keyEsc = (it.key || '').replace(/'/g, "\\'");
      const display = (it.display || it.key || '').replace(/</g, '&lt;');
      const exe = (it.exe || it.key || '').replace(/'/g, "\\'");
      const argProc = kind === 'proc' ? exe : '';
      // 三色按钮：当前 state 高亮
      const btn = (s, lbl, cls) => `<button class="wa-rule-btn ${cls} ${it.state === s ? 'on' : ''}"
        onclick="_classifyDisplay('${keyEsc}','${argProc}','${s}','${kind}')">${lbl}</button>`;
      html += `<div class="wa-rule-row">
        <span class="wa-rule-name">${display}<span class="wa-rule-key">${(it.key||'').replace(/</g,'&lt;')}</span></span>
        <span class="wa-rule-acts">${btn('focus','F','wa-rule-f')}${btn('distracted','D','wa-rule-d')}${btn('neutral','N','wa-rule-n')}</span>
      </div>`;
    }
    html += '</div>';
  }
  el.innerHTML = html;
}

function _initRuleTabs() {
  const rtabs = document.querySelectorAll('.wl-rtab');
  if (!rtabs.length) return;
  if (!rtabs[0].dataset.bound) {
    rtabs.forEach(btn => {
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        rtabs.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const isProc = btn.dataset.rt === 'proc';
        document.getElementById('wlRuleProc')?.classList.toggle('hidden', !isProc);
        document.getElementById('wlRuleSite')?.classList.toggle('hidden', isProc);
      });
    });
  }
}

function _renderWinV3() {
  _fetchWinV3Stats();
  _fetchWinV3Top();
  // 若规则面板展开，也刷新
  const r = document.getElementById('wlRules');
  if (r && r.style.display !== 'none') _renderAllRules();
}

// ── 分心应用 >60s 弹窗（v3）──
let _winDistAlertState = { dismissedUntil: 0, lastDisplay: '' };

async function _checkDistractedAlert() {
  try {
    const r = await fetch('/api/window/distracted_alert');
    if (!r.ok) return;
    const d = await r.json();
    const pop = document.getElementById('winDistractedPopup');
    if (!pop) return;
    const now = Date.now();
    // Reset dismissal if window changed
    if (d.display && d.display !== _winDistAlertState.lastDisplay) {
      _winDistAlertState.lastDisplay = d.display;
      _winDistAlertState.dismissedUntil = 0;
    }
    if (d.should_alert && now > _winDistAlertState.dismissedUntil) {
      const nameEl = document.getElementById('winDistAppName');
      const durEl  = document.getElementById('winDistDur');
      if (nameEl) nameEl.textContent = d.display || d.title || '该应用';
      if (durEl)  durEl.textContent  = d.stay_sec;
      if (!pop.classList.contains('show')) {
        pop.classList.add('show');
        try { playWarning && playWarning(); } catch (_) {}
      }
    } else if (!d.should_alert) {
      pop.classList.remove('show');
    }
  } catch (_) {}
}

// ── 窗口日志 Tab ──────────────────────────────────────────────
let _winLogVisible = false;

async function _renderWinLog() {
  const logList = document.getElementById('winLogList');
  if (!logList) return;

  // Fetch top10 and log in parallel
  let rows = [], top10 = [];
  try {
    const [r1, r2] = await Promise.all([
      fetch('/api/window/log'),
      fetch('/api/window/top10'),
    ]);
    if (r1.ok) rows = await r1.json();
    if (r2.ok) top10 = await r2.json();
  } catch (_) { /* silent */ }

  // Top 10 summary
  const summaryEl = document.getElementById('winLogTop10');
  if (summaryEl) {
    if (!top10.length) {
      summaryEl.innerHTML = '<div class="wl-empty">' + T('ev_waiting').replace('camera...','') + '...</div>';
    } else {
      const max = top10[0]?.total || 1;
      summaryEl.innerHTML = top10.map(item => {
        const fMin = Math.round((item.focus || 0) / 60);
        const dMin = Math.round((item.distracted || 0) / 60);
        const tMin = Math.round(item.total / 60);
        const bar  = Math.round((item.total / max) * 100);
        const name = item.name.slice(0, 20);
        return `<div class="wl-top-row">
          <span class="wl-top-name" title="${item.name}">${name}</span>
          <div class="wl-top-bar-wrap"><div class="wl-top-bar" style="width:${bar}%"></div></div>
          <span class="wl-top-dur">${tMin}m</span>
        </div>`;
      }).join('');
    }
  }

  // Timeline log
  if (!rows.length) {
    logList.innerHTML = '<div class="wl-empty">' + (_lang === 'zh' ? '今日暂无窗口记录' : 'No records today') + '</div>';
    return;
  }
  logList.innerHTML = rows.map(row => {
    const t    = new Date(row.start_time * 1000).toLocaleTimeString(_lang === 'zh' ? 'zh-CN' : 'en-GB', { hour12: false, hour: '2-digit', minute: '2-digit' });
    const dur  = row.duration >= 60
      ? `${Math.floor(row.duration / 60)}m`
      : `${Math.round(row.duration)}s`;
    const name  = row.display_name || (row.proc_name || '').replace(/\.exe$/i, '');
    const title = (row.title || '').slice(0, 30);
    const stClass = row.state === 'focus' ? 'wl-focus' : row.state === 'distracted' ? 'wl-dist' : 'wl-unk';
    const stLabel = row.state === 'focus' ? T('st_focus') : row.state === 'distracted' ? T('st_distracted_short') : (_lang === 'zh' ? '未知' : 'Unknown');
    const tipTxt  = _lang === 'zh' ? '点击更改' : 'Click to change';
    const focusLbl = T('st_focus');
    const distLbl  = T('st_distracted_short');
    // All states are editable: show clickable tag that expands to buttons
    const btns = `<span class="wl-unk-ctrl" id="wlCtrl_${row.id}">
         <span class="wl-tag ${stClass} wl-tag-sm wl-classify-trigger" onclick="_toggleClassify(${row.id})" title="${tipTxt}">${stLabel} ▾</span>
         <span class="wl-classify-btns" style="display:none;gap:3px">
           <button class="wl-btn wl-btn-f" onclick="event.stopPropagation();_classifyWin(${row.id},'focus')">${focusLbl}</button>
           <button class="wl-btn wl-btn-d" onclick="event.stopPropagation();_classifyWin(${row.id},'distracted')">${distLbl}</button>
         </span>
       </span>`;
    const rowClass = row.state === 'focus' ? 'wl-row-focus' : row.state === 'distracted' ? 'wl-row-dist' : 'wl-row-unk';
    return `<div class="wl-row ${rowClass}">
      <span class="wl-time">${t}</span>
      <span class="wl-name">${name}</span>
      <span class="wl-title" title="${row.title}">${title}</span>
      <span class="wl-dur">${dur}</span>
      ${btns}
    </div>`;
  }).join('');
}

async function _renderRules() {
  try {
    const r = await fetch('/api/window/keywords');
    if (!r.ok) return;
    const rows = await r.json();

    const renderList = (items) => items.length === 0
      ? '<div class="wl-kw-empty">' + (_lang === 'zh' ? '暂无' : 'None') + '</div>'
      : items.map(kw =>
          `<div class="wl-kw-item">
            <span>${kw.keyword}</span>
            <button class="wl-kw-del" onclick="_removeKeyword(${kw.id})" title="删除">×</button>
          </div>`
        ).join('');

    const set = (id, items) => { const el = document.getElementById(id); if (el) el.innerHTML = renderList(items); };
    set('wlProcWhite',  rows.filter(x => x.list_type === 'white' && x.rule_type === 'proc'));
    set('wlProcBlack',  rows.filter(x => x.list_type === 'black' && x.rule_type === 'proc'));
    set('wlTitleWhite', rows.filter(x => x.list_type === 'white' && x.rule_type === 'title'));
    set('wlTitleBlack', rows.filter(x => x.list_type === 'black' && x.rule_type === 'title'));
  } catch (_) { /* silent */ }
}

async function _addKeyword(listType, ruleType, inputId) {
  const input = document.getElementById(inputId);
  const kw = (input?.value || '').trim();
  if (!kw) return;
  try {
    await fetch('/api/window/keywords', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword: kw, list_type: listType, rule_type: ruleType })
    });
    if (input) input.value = '';
    _renderRules();
  } catch (_) { /* silent */ }
}

async function _removeKeyword(id) {
  try {
    await fetch(`/api/window/keywords/${id}`, { method: 'DELETE' });
    _renderRules();
  } catch (_) { /* silent */ }
}

function _toggleClassify(id) {
  const ctrl = document.getElementById('wlCtrl_' + id);
  if (!ctrl) return;
  const trigger = ctrl.querySelector('.wl-classify-trigger');
  const btns    = ctrl.querySelector('.wl-classify-btns');
  if (!btns) return;
  const isOpen = btns.style.display !== 'none';
  btns.style.display   = isOpen ? 'none' : 'flex';
  if (trigger) trigger.style.opacity = isOpen ? '1' : '0.5';
}

function _initWinLog() {
  // v4: 启动时直接渲染新面板
  _renderWinV3();

  // 自动刷新：v3 面板 5s（含统计/Top/Pending/规则总表）
  setInterval(_renderWinV3, 5000);

  // 规则总表面板若展开，单独 3s 自动刷新（捕捉新出现的域名）
  setInterval(() => {
    const r = document.getElementById('wlRules');
    if (r && r.style.display !== 'none') _renderAllRules();
  }, 3000);

  // 分心提醒轮询（每 5s 一次）
  setInterval(_checkDistractedAlert, 5000);

  // 分心弹窗按钮
  const okBtn   = document.getElementById('winDistOkBtn');
  const keepBtn = document.getElementById('winDistKeepBtn');
  const closeAll = (mins) => {
    document.getElementById('winDistractedPopup')?.classList.remove('show');
    _winDistAlertState.dismissedUntil = Date.now() + mins * 60 * 1000;
  };
  if (okBtn)   okBtn.addEventListener('click', () => closeAll(2));
  if (keepBtn) keepBtn.addEventListener('click', () => closeAll(5));
}

function _initRuleTabs_LEGACY_REMOVED() {
  // 旧版已被 v4 版本替代（参见上方），保留空函数避免外部引用报错
}

const on = (id, evt, fn) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener(evt, fn);
  else console.warn(`[DOM-MISSING] ${id}`);
};

document.addEventListener('DOMContentLoaded', () => {
  on('calibBtn', 'click', startCalibration);
  on('camBtn', 'click', () => { connectWebSocket(); initCam(); });
  on('soundBtn', 'click', toggleSound);
  on('themeBtn', 'click', toggleTheme);
  on('langBtn', 'click', toggleLang);
  on('aboutBtn', 'click', _openAbout);
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
    showToast(T('fatigue_disabled'), 'info');
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
    if (msg) navigator.clipboard.writeText(msg).then(() => showToast(T('err_copied'), 'success'));
  });

  applyTheme();
  applyLang();
  _initWinLog();
  _initGoalTabs();
});

// 目标 tab 切换（每日/本次/休息）
function _initGoalTabs() {
  const tabs = document.querySelectorAll('.goal-tab');
  if (!tabs.length) return;
  tabs.forEach(t => {
    t.addEventListener('click', () => {
      const k = t.dataset.gt;
      tabs.forEach(x => x.classList.toggle('active', x === t));
      ['daily', 'session', 'break'].forEach(name => {
        const p = document.getElementById('goalPane_' + name);
        if (p) p.style.display = (name === k) ? '' : 'none';
      });
    });
  });
}

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
  if (btn) btn.textContent = T('rotate_label') + ' ' + S.cameraRotation + T('rotate_degrees');

  if (_ws && _ws.readyState === WebSocket.OPEN) {
    _ws.send(JSON.stringify({
      cmd: 'set_camera_rotation',
      rotation: S.cameraRotation,
      flip: S.cameraFlip
    }));
  }
  saveConfigToBackend();
  showToast(T('rotate_toast') + ' ' + S.cameraRotation + T('rotate_degrees'), 'info');
}

// ── Keyboard Shortcuts (已按用户要求全部移除) ───────────────

// 健壮的启动逻辑：兼容 DOM 已加载或未加载的情况
if (document.readyState === 'complete' || document.readyState === 'interactive') {
  console.log('[BOOT] 检测到 DOM 已就绪，立即启动...');
  start();
} else {
  console.log('[BOOT] 等待 window.load 事件启动...');
  window.addEventListener('load', start);
}
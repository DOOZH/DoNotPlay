"""
DoNotPlay 视觉处理后端 (ONNX 版本)
===================================
架构：Python (ONNX Runtime + MediaPipe) → WebSocket → 浏览器前端

功能：
  1. OpenCV 捕获本地摄像头画面
  2. YOLO26 (ONNX Runtime + DirectML/CPU) 检测手机、键盘、书籍等物体
  3. MediaPipe Face Mesh (CPU) 获取面部关键点与虹膜细节
  4. 计算偏航角、俯仰角、EAR、眨眼、手臂姿态
  5. WebSocket 推送 JSON 数据给前端
  6. MJPEG 视频流给前端显示处理后的画面

依赖安装：
  pip install opencv-python mediapipe websockets onnxruntime flask flask-cors numpy Pillow
"""

import os
import sys
import re
import logging
import traceback
import warnings
import threading
from collections import deque

# ══════════════════════════════════════════
#  资源路径动态获取（兼容 PyInstaller 打包）
# ══════════════════════════════════════════
def get_resource_path(relative_path):
    """获取资源的绝对路径，兼容开发模式和 PyInstaller 打包后的环境"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

def get_writable_path(relative_path):
    """获取可写文件路径 — 优先使用系统固定目录，确保程序更新后数据不丢失"""
    # 候选目录列表（优先级从高到低）
    candidates = []

    # 1. APPDATA 目录（Windows 标准应用数据路径，最稳定）
    if "APPDATA" in os.environ:
        candidates.append(os.path.join(os.environ["APPDATA"], "DoNotPlay"))

    # 2. 用户文档目录（备选固定路径）
    candidates.append(os.path.join(os.path.expanduser("~"), "Documents", "DoNotPlay"))

    # 3. exe 同级目录或脚本目录（仅作最后手段）
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.dirname(sys.executable))
    else:
        try:
            candidates.append(os.path.abspath(os.path.dirname(__file__)))
        except (NameError, AttributeError):
            try:
                if sys.argv and sys.argv[0]:
                    candidates.append(os.path.abspath(os.path.dirname(sys.argv[0])))
                else:
                    candidates.append(os.getcwd())
            except (IndexError, AttributeError):
                candidates.append(os.getcwd())

    # 4. 临时目录（绝望回退）
    candidates.append(os.path.join(os.environ.get("TEMP", "."), "DoNotPlay"))

    for base in candidates:
        try:
            if not os.path.exists(base):
                os.makedirs(base, exist_ok=True)
            
            # 测试是否真正可写
            test_file = os.path.join(base, ".write_test")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            
            return os.path.join(base, relative_path)
        except:
            continue
            
    # 最后绝望的回退：当前工作目录
    return relative_path


def _migrate_data_to_appdata():
    """将程序目录中的旧数据文件迁移到 APPDATA，避免更新程序后丢失配置和历史"""
    if "APPDATA" not in os.environ:
        return

    appdata_dir = os.path.join(os.environ["APPDATA"], "DoNotPlay")

    # 确定程序所在目录
    if getattr(sys, 'frozen', False):
        prog_dir = os.path.dirname(sys.executable)
    else:
        try:
            prog_dir = os.path.abspath(os.path.dirname(__file__))
        except (NameError, AttributeError):
            return

    # 如果程序目录就是 APPDATA 目录，无需迁移
    if os.path.normcase(os.path.normpath(prog_dir)) == os.path.normcase(os.path.normpath(appdata_dir)):
        return

    for fname in ("config.json", "data.json"):
        src = os.path.join(prog_dir, fname)
        dst = os.path.join(appdata_dir, fname)
        try:
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(appdata_dir, exist_ok=True)
                import shutil
                shutil.copy2(src, dst)
                # 迁移后重命名旧文件，标记已迁移
                os.rename(src, src + ".migrated")
        except Exception:
            pass  # 迁移失败不影响正常运行

# ══════════════════════════════════════════
#  工业级静默日志与流重定向 (防 PyInstaller 闪退)
# ══════════════════════════════════════════

class StreamToLogger(object):
    """将 sys.stdout/stderr 重定向到 logging 的类，增加递归锁保护"""
    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self._lock = threading.Lock()
        self._is_writing = False

    def write(self, buf):
        # 递归锁保护：如果 logging 自身打印出错，防止无限循环死锁
        if self._is_writing:
            return
            
        with self._lock:
            self._is_writing = True
            try:
                for line in buf.rstrip().splitlines():
                    msg = line.rstrip()
                    if msg:
                        self.logger.log(self.log_level, msg)
            finally:
                self._is_writing = False

    def flush(self):
        pass

def init_logging():
    """初始化全局日志系统 — 精简版，每次启动覆盖写入，控制文件大小"""
    try:
        log_file = get_writable_path('donotplay_system.log')
        
        # 确保目录存在
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 根日志 INFO 级别，过滤第三方库噪音
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S',
            handlers=[
                logging.FileHandler(log_file, mode='w', encoding='utf-8')
            ]
        )
        
        # 抑制第三方库的低级日志
        for lib in ('websockets', 'websockets.server', 'websockets.client',
                     'urllib3', 'PIL', 'werkzeug', 'flask', 'mediapipe'):
            logging.getLogger(lib).setLevel(logging.WARNING)
        
        # 接管所有输出
        sys.stdout = StreamToLogger(logging.getLogger('STDOUT'), logging.INFO)
        sys.stderr = StreamToLogger(logging.getLogger('STDERR'), logging.ERROR)
        
        logging.info("=" * 50)
        logging.info("DoNotPlay 后端启动")
        logging.info(f"日志路径: {log_file}")
        logging.info("=" * 50)
    except Exception as e:
        pass

# 在最早期初始化
init_logging()

# 屏蔽 protobuf 弃用警告
warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf.symbol_database")

# 现在可以安全地进行重型导入了
import cv2
import numpy as np
import mediapipe as mp
import asyncio
import websockets
import json
import time
import sqlite3
import ctypes
import ctypes.wintypes as _wt
from datetime import datetime, date
from flask import Flask, Response, request, jsonify
from flask_cors import CORS
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageFont
import argparse
import webbrowser
import signal
import subprocess
import sys
import os

# (日志和流重定向代码已移至顶部)

# ══════════════════════════════════════════════════════════════════
#  第 1-4 层架构：完整重构的指标计算系统
# ══════════════════════════════════════════════════════════════════

class BaseSignals:
    """基础信号：从 MediaPipe + YOLO 原始数据中提取的最小指标集"""
    def __init__(self):
        # 面部信号
        self.face_detected = False
        self.ear = 0.0
        self.mar = 0.0
        self.yaw = 0.0
        self.pitch = 0.0
        self.nose_px = None
        self.face_h = 0.0
        self.landmarks_3d = {}
        
        # 姿态信号
        self.pose_valid = False
        self.left_shoulder = None
        self.right_shoulder = None
        self.nose_pose = None
        self.left_wrist = None
        self.right_wrist = None
        
        # 物体信号
        self.detected_objects = []
        self.has_phone = False
        self.has_cup = False
        self.has_bottle = False
        self.has_keyboard = False
        self.has_laptop = False
        self.has_book = False
        self.phone_bbox = None
        
        # 姿态与生理补充信号
        self.neck_length = 0.0
        self.shoulder_diff = 0.0
        self.is_arm_up = False
        self.cup_bottle_bbox = None
        self.person_detected = False


class CalibrationBaseline:
    """基线校准数据"""
    def __init__(self):
        self.yaw = 0.0
        self.pitch = 0.0
        self.ear = 0.28
        self.neck_length = 0.0
        self.slouch_angle = 0.0
        self.shoulder_diff = 0.0


class TimeSeriesBuffer:
    """时序数据缓冲用于平滑（带统计过滤）"""
    def __init__(self, window_size=60, use_median=False):
        self.window_size = window_size
        self.use_median = use_median
        self.buffer = deque(maxlen=window_size)
    
    def append(self, value):
        if value is not None:
            self.buffer.append(value)
    
    def get_mean(self):
        if not self.buffer:
            return 0.0
        return float(np.mean(list(self.buffer)))
    
    def get_median(self):
        if not self.buffer:
            return 0.0
        return float(np.median(list(self.buffer)))

    def get_value(self):
        return self.get_median() if self.use_median else self.get_mean()
    
    def get_perclos(self):
        if not self.buffer:
            return 0.0
        return (sum(self.buffer) / len(self.buffer)) * 100
    
    def get_std(self):
        if not self.buffer:
            return 0.0
        return float(np.std(list(self.buffer)))


class EventStateMachine:
    """事件状态机：支持迟滞 (Hysteresis) 与 进入/退出 冷却时长控制"""
    def __init__(self, enter_ms=1000, exit_ms=500, cooldown_ms=0):
        self.state = 'normal' # 'normal' | 'suspect' | 'confirmed' | 'recovered'
        self.enter_ms = enter_ms
        self.exit_ms = exit_ms
        self.cooldown_ms = cooldown_ms
        
        self.trigger_start_time = 0
        self.recovery_start_time = 0
        self.last_confirmed_time = 0
        self.confirmed_at = 0
        self.duration_ms = 0
    
    def update(self, is_triggered, current_time):
        now_ms = current_time * 1000
        
        # 冷却时间点逻辑
        if self.cooldown_ms > 0 and (now_ms - self.last_confirmed_time < self.cooldown_ms):
            return self.state == 'confirmed'

        if is_triggered:
            self.recovery_start_time = 0
            if self.state != 'confirmed':
                if self.trigger_start_time == 0:
                    self.trigger_start_time = now_ms
                    self.state = 'suspect'
                elif now_ms - self.trigger_start_time >= self.enter_ms:
                    if self.state != 'confirmed':
                        self.state = 'confirmed'
                        self.confirmed_at = now_ms
        else:
            self.trigger_start_time = 0
            if self.state == 'confirmed' or self.state == 'suspect':
                if self.recovery_start_time == 0:
                    self.recovery_start_time = now_ms
                elif now_ms - self.recovery_start_time >= self.exit_ms:
                    self.state = 'normal'
                    self.last_confirmed_time = now_ms
                    self.recovery_start_time = 0
        
        if self.state == 'confirmed':
            self.duration_ms = now_ms - self.confirmed_at
        else:
            self.duration_ms = 0
            
        return self.state == 'confirmed'


class FatigueMetrics:
    """综合疲劳指标 (科学评分版 v3 - 三层平滑稳定版)
    
    稳定性设计：
      第1层 - PERCLOS 长窗口 (~3分钟)，从源头平滑闭眼比例
      第2层 - 慢速 EMA (α=0.01)，抑制分数中频波动
      第3层 - 状态滞回 + 确认计时器，防止阈值边缘跳变
    """
    def __init__(self):
        self.eye_state_buffer = TimeSeriesBuffer(window_size=5400) # ~3分钟@30fps
        self.blink_timestamps = deque(maxlen=100)
        self.long_blink_count = 0
        self.yawn_count = 0
        self.long_blink_timestamps = deque(maxlen=100)
        self.yawn_timestamps = deque(maxlen=100)
        self.perclos = 0.0
        self.blink_rate = 0.0
        self.score = 0
        self.smoothed_score = 0.0
        self.state = 'normal' # normal | mild | moderate | severe

        # 第3层：状态稳定性机制
        self._candidate_state = 'normal'
        self._candidate_since = 0.0
        self._state_hold_time = 30.0  # 候选状态需持续 30 秒才确认切换
    
    def _determine_state_with_hysteresis(self):
        """带滞回的状态判定：不同方向使用不同阈值，防止边缘跳变"""
        score = self.score
        current = self.state
        if current == 'normal':
            if score >= 30: return 'mild'
            return 'normal'
        elif current == 'mild':
            if score >= 50: return 'moderate'
            if score < 22: return 'normal'     # 要降回 normal 需 < 22（比升级阈值 30 低 8 分）
            return 'mild'
        elif current == 'moderate':
            if score >= 80: return 'severe'
            if score < 40: return 'mild'        # 要降回 mild 需 < 40（比升级阈值 50 低 10 分）
            return 'moderate'
        elif current == 'severe':
            if score < 65: return 'moderate'    # 要降回 moderate 需 < 65（比升级阈值 80 低 15 分）
            return 'severe'
        return current

    def update(self, is_eye_closed, is_yawning, current_time, is_long_blink=False):
        # 第1层：长窗口 PERCLOS
        self.eye_state_buffer.append(1 if is_eye_closed else 0)
        
        # 时间窗口：只统计过去 5 分钟内的事件
        TIME_WINDOW = 300.0
        
        if is_yawning:
            self.yawn_timestamps.append(current_time)
        if is_long_blink:
            self.long_blink_timestamps.append(current_time)
        
        cutoff_time = current_time - TIME_WINDOW
        self.long_blink_timestamps = deque([t for t in self.long_blink_timestamps if t >= cutoff_time], maxlen=100)
        self.yawn_timestamps = deque([t for t in self.yawn_timestamps if t >= cutoff_time], maxlen=100)
        
        self.long_blink_count = len(self.long_blink_timestamps)
        self.yawn_count = len(self.yawn_timestamps)
        
        self.perclos = self.eye_state_buffer.get_perclos() # 0-100
        
        # 评分公式: PERCLOS 基准分 (0-60) + 惩罚项 (0-40)
        p_score = np.clip((self.perclos - 20) / (55 - 20), 0, 1) * 60
        penalty = min(40, self.long_blink_count * 3 + self.yawn_count * 8)
        
        raw_score = p_score + penalty
        # 第2层：慢速 EMA (α=0.01)，半衰期约 2.3 秒，95% 响应约 10 秒
        self.smoothed_score = self.smoothed_score * 0.99 + raw_score * 0.01
        self.score = int(self.smoothed_score)
        
        # 第3层：带滞回 + 确认计时器
        new_candidate = self._determine_state_with_hysteresis()
        
        if new_candidate != self._candidate_state:
            # 候选状态改变，重新计时
            self._candidate_state = new_candidate
            self._candidate_since = current_time
        
        if self._candidate_state != self.state:
            # 候选状态与当前不同，等待确认时间
            if current_time - self._candidate_since >= self._state_hold_time:
                self.state = self._candidate_state
        else:
            # 候选与当前一致，保持计时器同步
            self._candidate_since = current_time


class EyeUsageTracker:
    """连续用眼时间追踪状态机
    
    状态 A: 用户在画面 → 累加连续用眼时间
    状态 B: 短暂离开(< 5分钟) → 暂停计时，保留连续用眼时间
    状态 C: 离开 >= 5分钟 → 视为有效休息，清零连续用眼时间
    """
    AWAY_RESET_SEC = 300   # 5分钟有效休息
    SHORT_REST_SEC = 1200  # 20分钟短提示阈值
    LONG_REST_SEC  = 3600  # 60分钟弹窗阈值

    def __init__(self):
        self.eye_time_sec  = 0.0
        self.away_time_sec = 0.0
        self.is_away       = False
        self._last_update  = time.time()
        self._prev_short_tier = 0  # 上次触发短提示时的档位(每20分钟一档)

    def update(self, face_detected: bool):
        now = time.time()
        dt  = min(now - self._last_update, 5.0)
        self._last_update = now

        if face_detected:
            if self.is_away:
                if self.away_time_sec >= self.AWAY_RESET_SEC:
                    # 有效休息：清零连续用眼时间
                    self.eye_time_sec = 0.0
                    self._prev_short_tier = 0
                self.away_time_sec = 0.0
                self.is_away = False
            self.eye_time_sec += dt
        else:
            self.is_away = True
            self.away_time_sec += dt

    def pop_short_rest_trigger(self) -> bool:
        """是否刚刚到达20分钟档位（每20分钟一次，到60分钟后停止，因为长提示会接管）"""
        if self.eye_time_sec >= self.LONG_REST_SEC:
            return False
        tier = int(self.eye_time_sec // self.SHORT_REST_SEC)
        if tier > 0 and tier != self._prev_short_tier:
            self._prev_short_tier = tier
            return True
        return False

    def to_dict(self) -> dict:
        return {
            'eye_time_sec':  int(self.eye_time_sec),
            'eye_minutes':   int(self.eye_time_sec / 60),
            'away_time_sec': int(self.away_time_sec),
            'is_away':       self.is_away,
            'need_long_rest': self.eye_time_sec >= self.LONG_REST_SEC,
        }


class PostureMetrics:
    """拆分的姿态指标 (带平滑与分级)"""
    def __init__(self):
        self.slouch_buf = TimeSeriesBuffer(window_size=45, use_median=True)
        self.neck_buf = TimeSeriesBuffer(window_size=45, use_median=True)
        self.balance_buf = TimeSeriesBuffer(window_size=45, use_median=True)
        
        self.slouch_state = 'good' # good | mild | severe
        self.neck_state = 'good'
        self.balance_state = 'balanced'
        
        # 精确数值字段
        self.cur_neck_pct = 100.0
        self.cur_balance_deg = 0.0
        self.cur_slouch_deg = 0.0
        
        self.slouch_machine = EventStateMachine(enter_ms=5000, exit_ms=1500)
        
        # 颈部自动校准：用前 N 帧的最大颈长作为"挺直"基准
        self._neck_baseline = 0.0
        self._neck_calibration_samples = 0
        self._NECK_CALIBRATION_FRAMES = 150  # ~5秒@30fps
    
    def update(self, slouch_angle, neck_ratio_or_length, balance_deg, *, raw_neck_length=0.0):
        self.slouch_buf.append(slouch_angle)
        self.balance_buf.append(balance_deg)
        
        # 颈部自动校准逻辑：前 5 秒建立基准
        if raw_neck_length > 0.01:
            if self._neck_calibration_samples < self._NECK_CALIBRATION_FRAMES:
                self._neck_calibration_samples += 1
                # 用 90 百分位（排除异常高值）逐步更新基准
                if raw_neck_length > self._neck_baseline * 0.95:
                    self._neck_baseline = max(self._neck_baseline, raw_neck_length)
            else:
                # 校准完成后仍缓慢适应（EMA α=0.002 跟踪头部距离变化）
                if raw_neck_length > self._neck_baseline:
                    self._neck_baseline += (raw_neck_length - self._neck_baseline) * 0.002
            
            # 计算真实比率
            if self._neck_baseline > 0.01:
                neck_ratio = raw_neck_length / self._neck_baseline
            else:
                neck_ratio = 1.0
        else:
            neck_ratio = neck_ratio_or_length  # 回退到传入的比率
        
        self.neck_buf.append(neck_ratio)
        
        avg_slouch = self.slouch_buf.get_value()
        avg_neck_ratio = self.neck_buf.get_value()
        avg_balance_deg = self.balance_buf.get_value()
        
        self.cur_slouch_deg = avg_slouch
        self.cur_balance_deg = avg_balance_deg
        # 颈部百分比：1.0(基准) -> 100%。值越小代表前伸越严重（垂直投影缩短）
        self.cur_neck_pct = max(0, min(200, avg_neck_ratio * 100))
        
        # 1. 驼背判定 (Slouch)
        if avg_slouch > 20: self.slouch_state = 'severe'
        elif avg_slouch > 12: self.slouch_state = 'mild'
        else: self.slouch_state = 'good'
        
        # 2. 颈前伸判定 (基于自动校准基准的压缩率)
        if avg_neck_ratio < 0.75: self.neck_state = 'severe'
        elif avg_neck_ratio < 0.88: self.neck_state = 'mild'
        else: self.neck_state = 'good'
        
        # 3. 肩颈对称 (基于真实角度)
        abs_balance = abs(avg_balance_deg)
        if abs_balance > 8.5: self.balance_state = 'severe_tilt'
        elif abs_balance > 4.5: self.balance_state = 'slight_tilt'
        else: self.balance_state = 'balanced'
        
        return self.slouch_state


class HydrationDetector:
    """饮水检测 (状态机融合版 - 高灵敏度 v2)"""
    def __init__(self):
        # 极短进入延迟，快速确认饮水动作
        self.machine = EventStateMachine(enter_ms=250, exit_ms=300, cooldown_ms=1500)
        self.last_drink_time = 0
        self.minutes_since = -1
        self.today_drink_count = 0
        self.state = 'normal'
    
    def update(self, current_time, is_drinking_evidence):
        is_confirmed = self.machine.update(is_drinking_evidence, current_time)
        
        if is_confirmed and (current_time - self.last_drink_time > 10):
            self.last_drink_time = current_time
            self.today_drink_count += 1
        
        if self.last_drink_time > 0:
            self.minutes_since = round((current_time - self.last_drink_time) / 60.0, 1)
            if self.minutes_since > 60: self.state = 'overdue'
            elif self.minutes_since > 30: self.state = 'due'
            else: self.state = 'normal'
        return is_confirmed


class PhoneDetector:
    """手机使用检测（v5 — 严格三优先级合议）

    用户定义的判定优先级（必须 has_phone 之外）：
      1) hand_on_phone     —— 手/手指接触手机（最高，单独足以触发）
      2) arm_holding_phone —— 手臂举起 + 手机在画面上半（次之，单独足以触发）
      3) gaze_lock         —— 视线锁定手机（最弱，但单证据也能触发）

    单纯"手机出现"或"距离脸近"不再触发，避免桌上手机误报。
    """
    def __init__(self):
        # 0.4s 进入（从画面外拿入要快响应）/ 0.6s 退出 / 1.0s 冷却
        self.machine = EventStateMachine(enter_ms=400, exit_ms=600, cooldown_ms=1000)
        self.state = 'normal'
        self.score = 0
        self.duration_ms = 0

    def update(self, current_time, has_phone,
               hand_on_phone=False, arm_holding_phone=False, gaze_lock=False,
               work_context=False, wrists_visible=True, **_legacy):
        # 兼容旧调用：吃掉 phone_near_face 等历史参数（不再参与判定）
        if not has_phone:
            self.machine.update(False, current_time)
            self.state = 'normal'; self.score = 0
            self.duration_ms = self.machine.duration_ms
            return False

        has_evidence = hand_on_phone or arm_holding_phone or gaze_lock
        if not has_evidence:
            self.machine.update(False, current_time)
            self.state = 'normal'; self.score = 15
            self.duration_ms = self.machine.duration_ms
            return False

        # 按优先级取基础分（互斥，保证 P1 > P2 > P3 语义）
        if hand_on_phone:
            score = 80
        elif arm_holding_phone:
            score = 70
        else:  # 仅 gaze_lock
            score = 55

        # 多证据叠加加分
        if hand_on_phone and gaze_lock:         score += 15
        if arm_holding_phone and gaze_lock:     score += 10
        if hand_on_phone and arm_holding_phone: score += 10

        # 工作场景仅在"只有视线证据"时抑制
        if work_context and not (hand_on_phone or arm_holding_phone):
            score -= 20

        # ── 关键防误报：当摄像头看不到手腕（手不在画面 / 被遮挡）──
        # 没有手腕证据时，禁止单证据触发：必须 arm_holding_phone + gaze_lock 双证据
        # 这样可避免「桌面上放着手机但人没在玩」被误判
        if not wrists_visible and not hand_on_phone:
            if not (arm_holding_phone and gaze_lock):
                score -= 40  # 强力压制单证据情形

        self.score = int(np.clip(score, 0, 100))
        is_triggered = self.score >= 50
        is_confirmed = self.machine.update(is_triggered, current_time)
        self.state = 'using' if is_confirmed else 'normal'
        self.duration_ms = self.machine.duration_ms
        return is_confirmed


class AttentionDetector:
    """分心检测（多信号合议迟滞版 — 平衡响应与误报）"""
    def __init__(self):
        # 进入 0.8s 避免瞥一眼误报；退出 0.4s 快速恢复；冷却 800ms
        self.machine = EventStateMachine(enter_ms=800, exit_ms=400, cooldown_ms=800)
        self.state = 'focused'
        self.duration_ms = 0
    
    def update(self, current_time, adj_yaw, adj_pitch, has_work_objects, is_using_phone, is_away, is_face_detected, yaw_threshold=25, pitch_threshold=18):
        # 【修复】不再使用硬编码 25/18，而是追踪全局配置的阈值
        is_off_screen = abs(adj_yaw) > yaw_threshold or abs(adj_pitch) > pitch_threshold
        
        # 如果虽然识别到人但脸丢了，也算分心（通常是头扭过去了）
        if not is_face_detected and not is_away:
            is_off_screen = True
            
        is_triggered = is_off_screen or is_using_phone or is_away
        
        is_confirmed = self.machine.update(is_triggered, current_time)
        self.state = 'distracted' if is_confirmed else 'focused'
        self.duration_ms = self.machine.duration_ms
        return self.state


class AwayDetector:
    """离开检测 (支持 Warmup 与 Suspect 状态 - 增强灵敏度)"""
    def __init__(self, warmup_duration=4.0):
        # 人脸 3 秒未出现才判定离席；出现立刻返回；防抖动更稳
        self.machine = EventStateMachine(enter_ms=3000, exit_ms=0)
        self.warmup_until = time.time() + warmup_duration
        self.state = 'present'
        self.duration_ms = 0
    
    def update(self, current_time, face_detected, person_detected, pose_valid=False, is_using_phone=False, static_count=0):
        if current_time < self.warmup_until:
            return 'present'
            
        # 极简判定：仅依赖人脸识别
        is_missing = not face_detected
        
        old_state = self.machine.state
        is_confirmed = self.machine.update(is_missing, current_time)
        new_state = self.machine.state

        # —— [AWAY-AUDIT] 黑盒诊断日志 ——
        # 每隔约 1 秒（信号循环的一个周期）打印一次完整证据链
        global logging_tick
        if logging_tick % 100 == 0:
            logging.debug(f"[AWAY-AUDIT] FaceDetected={1 if face_detected else 0} PersonDetected={1 if person_detected else 0} "
                         f"PoseValid={1 if pose_valid else 0} PhoneUsing={1 if is_using_phone else 0} | "
                         f"Missing={is_missing} Machine={new_state} Counter={self.machine.duration_ms}ms")

        # —— [AWAY-EVENT] 状态切换瞬间快照 ——
        if old_state != new_state:
            # 当从正常进入怀疑，或者从怀疑转为确诊时，强制记录快照
            snapshot = (f"Face={face_detected}, Person={person_detected}, Pose={pose_valid}, "
                        f"Phone={is_using_phone}, IsMissing={is_missing}")
            logging.warning(f"[AWAY-EVENT] 状态机变更: {old_state} -> {new_state} | 现场证据: {snapshot}")

        self.state = 'away' if is_confirmed else 'present'
        self.duration_ms = self.machine.duration_ms
        return self.state


def build_comprehensive_payload(signals, baseline, metrics, current_time, blink_rate=0):
    """构建符合标准且兼容旧字段的结构化 JSON Payload"""
    
    # 状态映射兼容性
    p_met = metrics['posture']
    f_met = metrics['fatigue']
    a_met = metrics['attention']
    h_met = metrics['hydration']
    ph_met = metrics['phone']
    aw_met = metrics['away']
    
    # 计算偏差值
    adj_yaw = signals.yaw - baseline.yaw
    adj_pitch = signals.pitch - baseline.pitch
    
    # 1. 基础状态
    payload = {
        "type": "runtime_state",
        "timestamp": current_time,
        "status": "active", # 稍后在外层根据优先级覆盖
        "faceDetected": signals.face_detected,
        "personDetected": signals.person_detected,
        "poseValid": signals.pose_valid,
        "phoneAlertActive": ph_met.state == 'using',
        "is_slouching": p_met.slouch_state == 'severe',
        
        # 2. 结构化子模块
        "hydration": {
            "state": h_met.state,
            "score": int(np.clip(100 - (h_met.minutes_since / 60) * 100, 0, 100)), # 这里的 score 含义可能需调整，暂定为“水分充足度”
            "minutes_since": h_met.minutes_since,
            "last_drink_time": h_met.last_drink_time,
            "today_drink_count": h_met.today_drink_count,
            "drinking_now": h_met.machine.state == 'confirmed' # 修正：指向 h_met 而非 ph_met
        },

        "eye_fatigue": {
            "state": f_met.state,
            "score": f_met.score,
            "perclos": round(f_met.perclos, 2),
            "blinkRate": round(blink_rate, 1),
            "longBlinkCount": f_met.long_blink_count,
            "yawnCount": f_met.yawn_count
        },

        "gaze_angle": {
            "yaw": round(adj_yaw, 1),
            "pitch": round(adj_pitch, 1),
            "roll": 0.0,
            "state": "off_screen" if a_met.state != 'focused' else "on_screen",
            "score": 0 # 预留
        },

        "neck_strain": {
            "state": p_met.neck_state,
            "pct": round(p_met.cur_neck_pct, 1),
            "ratio": round(p_met.cur_neck_pct / 100.0, 2), # 保持兼容
            "score": 0
        },

        "shoulder_symmetry": {
            "state": p_met.balance_state,
            "tilt": round(p_met.cur_balance_deg, 1),
            "diff": round(p_met.cur_balance_deg, 1), # 保持兼容
            "score": 0
        },

        "phone": {
            "state": ph_met.state,
            "score": ph_met.score,
            "duration_ms": int(ph_met.duration_ms),
            "object_present": signals.has_phone,
            "gaze_lock": ph_met.score >= 70, # 简化判定
            "reason_codes": []
        },

        "attention": {
            "state": a_met.state,
            "score": 0,
            "duration_ms": int(a_met.duration_ms),
            "on_screen": a_met.state == 'focused',
            "reason_codes": []
        },

        "fatigue": {
            "state": f_met.state,
            "score": f_met.score,
            "perclos": round(f_met.perclos, 2),
            "blink_rate": round(f_met.blink_rate, 1),
            "long_blink_count": f_met.long_blink_count,
            "yawn_count": f_met.yawn_count,
            "reason_codes": []
        },

        "posture": {
            "state": p_met.slouch_state,
            "score": 0,
            "slouch_angle": round(p_met.slouch_buf.get_value(), 1),
            "neck_forward_ratio": round(p_met.neck_buf.get_value(), 2),
            "shoulder_diff": round(p_met.balance_buf.get_value(), 3),
            "reason_codes": []
        },

        "away": {
            "state": aw_met.state,
            "duration_ms": int(aw_met.duration_ms)
        },

        "debug": {
            "engine": "YOLO26 + Pose + FaceMesh",
            "fps": 0.0,
            "latency_ms": 0.0,
            "signals": {},
            "raw_flags": {}
        }
    }
    
    # 状态优先级覆盖 logic
    if aw_met.state == 'away':
        payload['status'] = 'away'
    elif ph_met.state == 'using':
        payload['status'] = 'phone'
    elif a_met.state != 'focused':
        payload['status'] = 'distracted'
    else:
        payload['status'] = 'active'
        
    # 为前端向下兼容提供 legacy 字段映射 (如果前端还没适配新结构)
    payload['hydration_val'] = payload['hydration']['minutes_since']
    payload['ear'] = round(signals.ear * 100, 0)
    payload['mar'] = round(signals.mar, 4)
    
    return payload

# ══════════════════════════════════════════
#  ONNX Runtime 配置与工具函数
# ══════════════════════════════════════════

# 全局日志节流计数器
logging_tick = 0

def get_onnx_providers():
    """获取 ONNX Runtime 的执行提供者列表，优先使用 DirectML，回退到 CPU"""
    providers = []
    available = ort.get_available_providers()
    
    # 优先尝试 DirectML（支持 NVIDIA、AMD、Intel 显卡）
    if 'DmlExecutionProvider' in available:
        providers.append('DmlExecutionProvider')
        logging.info("[ONNX] 使用 DirectML 执行提供者（GPU 加速）")
    else:
        logging.info("[ONNX] DirectML 不可用，将尝试 CUDA")
        if 'CUDAExecutionProvider' in available:
            providers.append('CUDAExecutionProvider')
            logging.info("[ONNX] 使用 CUDA 执行提供者")
        elif 'TensorrtExecutionProvider' in available:
            providers.append('TensorrtExecutionProvider')
            logging.info("[ONNX] 使用 TensorRT 执行提供者")
    
    # 始终添加 CPU 作为备选方案
    if 'CPUExecutionProvider' in available:
        providers.append('CPUExecutionProvider')
        if not providers or providers[0] != 'DmlExecutionProvider':
            logging.info("[ONNX] 默认使用 CPU 执行提供者")
    
    return providers if providers else ['CPUExecutionProvider']

# ══════════════════════════════════════════
#  图像预处理与后处理工具
# ══════════════════════════════════════════

def letterbox(img, target_size=640, color=(114, 114, 114)):
    """
    Letterbox 缩放：在保持宽高比的前提下将图像缩放到 target_size，并用黑边填充。
    
    参数：
        img: 输入图像 (HxWxC, BGR 格式)
        target_size: 目标输出大小 (正方形，如 640)
        color: 填充颜色 (BGR)
    
    返回：
        resized_img: 缩放后的图像 (target_size x target_size x 3)
        ratio: 缩放比例 (float)
        (dw, dh): 水平和竖直方向的填充量 (pixels)
    """
    h, w = img.shape[:2]
    
    # 计算保持宽高比的缩放因子
    scale = min(target_size / w, target_size / h)
    
    # 计算新的宽高
    new_w = int(w * scale)
    new_h = int(h * scale)
    
    # 缩放图像
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    
    # 计算填充量
    dw = (target_size - new_w) // 2
    dh = (target_size - new_h) // 2
    
    # 创建目标大小的画布并填充颜色
    canvas = np.full((target_size, target_size, 3), color, dtype=np.uint8)
    canvas[dh:dh + new_h, dw:dw + new_w] = resized
    
    return canvas, scale, (dw, dh)

def preprocess_image(img, target_size=640):
    """
    YOLO 模型前处理：Letterbox + 归一化 + CHW 转换
    
    参数：
        img: 输入图像 (HxWxC, BGR 格式)
        target_size: 目标大小 (默认 640)
    
    返回：
        blob: 预处理后的张量 (1x3x640x640, float32)
        ratio: 缩放比例
        (dw, dh): 填充量
    """
    # Letterbox 缩放
    canvas, ratio, (dw, dh) = letterbox(img, target_size, color=(114, 114, 114))
    
    # BGR → RGB
    canvas_rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    
    # 转换为 float32 并归一化到 [0, 1]
    blob = canvas_rgb.astype(np.float32) / 255.0
    
    # HWC → CHW
    blob = np.transpose(blob, (2, 0, 1))
    
    # 添加批次维度 (1, 3, 640, 640)
    blob = np.expand_dims(blob, axis=0)
    
    return blob, ratio, (dw, dh)

def postprocess_nms(predictions, conf_threshold=0.25, iou_threshold=0.45):
    """
    非极大值抑制 (NMS) - 移除重叠的边界框 (增强版：支持 YOLOv8 转置格式)
    
    参数：
        predictions: YOLO 模型输出
    """
    # 自动转置检测：YOLOv8 通常输出 (84, 6300)
    if predictions.shape[0] < predictions.shape[1]:
        predictions = predictions.T

    # 协议分析：YOLOv5/v7 有 85 列 (4坐标 + 1置信度 + 80类)
    # YOLOv8 通常有 84 列 (4坐标 + 80类，不含独立置信度位)
    num_features = predictions.shape[1]
    
    if num_features >= 85:
        # legacy 模式 (v5/v7)
        obj_conf = predictions[:, 4]
        class_probs = predictions[:, 5:]
        class_ids = np.argmax(class_probs, axis=1)
        max_class_probs = np.max(class_probs, axis=1)
        combined_conf = obj_conf * max_class_probs
    else:
        # YOLOv8 模式 (v8)
        # 直接使用类别概率的最大值作为置信度
        class_probs = predictions[:, 4:]
        class_ids = np.argmax(class_probs, axis=1)
        combined_conf = np.max(class_probs, axis=1)
    
    # 提取坐标 (cx, cy, w, h)
    boxes_raw = predictions[:, :4]
    
    # 按置信度阈值过滤
    mask = combined_conf >= conf_threshold
    if not np.any(mask):
        return []
        
    filtered_conf = combined_conf[mask]
    filtered_class_ids = class_ids[mask]
    filtered_boxes_raw = boxes_raw[mask]
    
    # YOLO 格式是 (cx, cy, w, h)，转换为 (x1, y1, x2, y2)
    x1 = filtered_boxes_raw[:, 0] - filtered_boxes_raw[:, 2] / 2
    y1 = filtered_boxes_raw[:, 1] - filtered_boxes_raw[:, 3] / 2
    x2 = filtered_boxes_raw[:, 0] + filtered_boxes_raw[:, 2] / 2
    y2 = filtered_boxes_raw[:, 1] + filtered_boxes_raw[:, 3] / 2
    
    # 计算面积
    areas = (x2 - x1) * (y2 - y1)
    
    # 按置信度排序（降序）
    order = np.argsort(-filtered_conf)
    
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        
        if len(order) == 1:
            break
        
        # 计算与当前框的 IOU
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        union = areas[i] + areas[order[1:]] - inter
        iou = inter / (union + 1e-6)
        
        # 保留 IOU 小于阈值的框
        order = order[1:][iou < iou_threshold]
    
    # 返回保留的结果
    result = []
    for idx in keep:
        result.append((
            x1[idx], y1[idx], x2[idx], y2[idx],
            float(filtered_conf[idx]),
            int(filtered_class_ids[idx])
        ))
    
    return result

# (路径工具已移至顶部)


def resolve_model_path(*names):
    """按优先级查找模型文件位置，支持资源目录和当前工作目录"""
    for name in names:
        resource = get_resource_path(name)
        if os.path.exists(resource):
            return resource
        if os.path.exists(name):
            return os.path.abspath(name)
    return None

# ══════════════════════════════════════════
#  全局配置
# ══════════════════════════════════════════
CAMERA_INDEX = 0          # 摄像头索引（0 = 默认摄像头）
WS_PORT = 8765            # WebSocket 端口
MJPEG_PORT = 5001         # MJPEG 视频流端口

# ═══════════════════════════════════════════════════════════════
#  环境模式和运行信息
# ═══════════════════════════════════════════════════════════════

YOLO_GLOBAL_INTERVAL = 0.1   # 全局物体检测间隔（秒），约 10 fps
YOLO_POSE_INTERVAL = 0.2     # 上半身姿态检测间隔（秒）

YOLO_CONF_THRESHOLD = 0.22   # YOLO 全局默认最低置信度

# 特定类别阈值覆盖（针对核心业务下调门槛，增强灵敏度）
YOLO_CLASS_THRESHOLDS = {
    'cell phone': 0.20,
    'remote':     0.20,
    'person':     0.30,
    'cup':        0.16,
    'bottle':     0.16,
}

# 修改后的模型名称列表 (现在使用 .onnx 格式)
YOLO_GLOBAL_MODEL_NAMES = ['yolo26n.onnx']
YOLO_POSE_MODEL_NAMES = ['yolo26n-pose.onnx']

# 我们关心的 COCO 类别 ID → 自定义标签
YOLO_TARGET_CLASSES = {
    0:  'person',      # 【新增】人像底座
    67: 'cell phone',
    65: 'remote',
    73: 'book',
    66: 'keyboard',
    63: 'laptop',
    39: 'bottle',
    41: 'cup',
    62: 'monitor',
    64: 'mouse',
    74: 'clock'
}

# 手机类别（用于触发警报）
PHONE_CLASSES = {'cell phone', 'remote'}
# 工作类别（用于免责豁免）
WORK_CLASSES = {'keyboard', 'laptop', 'book'}

# MediaPipe 面部关键点索引（与前端一致）
LE_IDX = [362, 385, 387, 263, 373, 380]  # 左眼
RE_IDX = [33, 160, 158, 133, 153, 144]   # 右眼
NOSE = 1
CHIN = 175
LT = 234   # 左耳
RT = 454   # 右耳

# 绘制颜色映射 (BGR)
BBOX_COLORS = {
    'person':     (200, 200, 200),   # 浅灰色 (不干扰业务)
    'cell phone': (68, 68, 255),     # 鲜红色
    'remote':     (68, 68, 255),
    'book':       (102, 204, 68),    # 翠绿色
    'keyboard':   (0, 153, 255),     # 活力橙
    'laptop':     (0, 153, 255),
    'bottle':     (255, 178, 102),   # 天蓝色
    'cup':        (255, 178, 102),
    'monitor':    (204, 153, 255),   # 浅紫色
    'mouse':      (255, 255, 102),   # 柠檬黄
    'clock':      (102, 255, 255),   # 青色
}
BBOX_LABELS = {
    'person':     '人员',
    'cell phone': '智能手机',
    'remote':     '遥控器',
    'book':       '书籍/文档',
    'keyboard':   '输入键盘',
    'laptop':     '笔记本电脑',
    'bottle':     '饮用水瓶',
    'cup':        '水杯',
    'monitor':    '显示器',
    'mouse':      '鼠标',
    'clock':      '时钟',
}

# 姿态连线定义 (肩膀、手肘、手腕) — 全部低饱和度 slate / muted teal
SKELETON_CONNECTIONS = [
    (5, 6, (170, 170, 170)),  # 肩膀连线 (中性灰)
    (5, 7, (155, 165, 175)),  # 左大臂 (冷灰)
    (7, 9, (155, 165, 175)),  # 左小臂
    (6, 8, (155, 165, 175)),  # 右大臂
    (8, 10, (155, 165, 175)), # 右小臂
]

# 字体配置 (Windows 系统黑体)
FONT_PATH = "C:\\Windows\\Fonts\\simhei.ttf" if os.path.exists("C:\\Windows\\Fonts\\simhei.ttf") else None

def draw_chinese_text(img, text, position, font_size=20, color=(255, 255, 255), bg_color=None):
    """使用 PIL 在 OpenCV 图像上绘制中文文本"""
    if not FONT_PATH:
        cv2.putText(img, text, position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        return img
    
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except:
        font = ImageFont.load_default()
        
    if bg_color:
        # 获取文本渲染尺寸
        try:
            # 兼容 Pillow 新旧版本
            if hasattr(draw, 'textbbox'):
                bbox = draw.textbbox(position, text, font=font)
                draw.rectangle(bbox, fill=bg_color)
            else:
                tw, th = draw.textsize(text, font=font)
                draw.rectangle([position[0], position[1], position[0] + tw, position[1] + th], fill=bg_color)
        except: pass
    
    draw.text(position, text, font=font, fill=color)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

# BGR 低饱和度调色板：把任何鲜艳色统一去饱和
def _desaturate_bgr(color, sat=0.45):
    """把 BGR 颜色按比例向中灰 (128,128,128) 拉，得到低饱和版本。"""
    b, g, r = color
    return (int(b * sat + 128 * (1 - sat)),
            int(g * sat + 128 * (1 - sat)),
            int(r * sat + 128 * (1 - sat)))

def draw_tech_box(img, x1, y1, w, h, color, label, score=None):
    """低饱和细线框：仅四角 L 型短笔触 + 小号文字标签，无背景填充。"""
    color = _desaturate_bgr(color, 0.5)  # 强制去饱和
    x2, y2 = x1 + w, y1 + h
    # 仅四角 L 型短笔触（去掉完整边框，更轻盈）
    l = max(6, int(min(w, h) * 0.12))
    cv2.line(img, (x1, y1), (x1+l, y1), color, 1)
    cv2.line(img, (x1, y1), (x1, y1+l), color, 1)
    cv2.line(img, (x2, y1), (x2-l, y1), color, 1)
    cv2.line(img, (x2, y1), (x2, y1+l), color, 1)
    cv2.line(img, (x1, y2), (x1+l, y2), color, 1)
    cv2.line(img, (x1, y2), (x1, y2-l), color, 1)
    cv2.line(img, (x2, y2), (x2-l, y2), color, 1)
    cv2.line(img, (x2, y2), (x2, y2-l), color, 1)
    # 极简文字标签：无背景，紧贴左上
    txt = f"{label} {score if score else ''}".strip()
    img[:] = draw_chinese_text(img, txt, (x1, max(0, y1 - 16)), font_size=11, color=color, bg_color=None)

def draw_runtime_hud(img, face_info, pose_info, object_info, det_count=0):
    """简约 HUD：眼/肩/物 三行，低饱和度配色，减少视觉干扰"""
    panel_w, panel_h = 175, 68
    overlay = img.copy()
    cv2.rectangle(overlay, (4, 4), (panel_w, panel_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.42, img, 0.58, 0, img)

    # Line 1: 眼 (eye state based on EAR vs baseline)
    ear = face_info.get('ear', 0)
    baseline_ear = getattr(getattr(state, 'baseline', None), 'ear', 0.28)
    if not face_info['detected']:
        eye_txt, eye_c = '眼  未检测', (100, 100, 100)
    elif ear < baseline_ear * 0.63:
        eye_txt, eye_c = f'眼  疲劳', (130, 155, 195)
    else:
        eye_txt, eye_c = '眼  正常', (120, 170, 120)
    img[:] = draw_text_chinese(img, eye_txt, (10, 8), 13, eye_c)

    # Line 2: 肩 (shoulder tilt)
    tilt_deg = getattr(state, 'shoulder_tilt', 0)
    if not pose_info['valid']:
        pose_txt, pose_c = '肩  未检测', (100, 100, 100)
    elif abs(tilt_deg) < 3:
        pose_txt, pose_c = '肩  正常', (120, 170, 120)
    elif abs(tilt_deg) < 8:
        pose_txt, pose_c = f'肩  {tilt_deg:+.0f}°', (155, 165, 130)
    else:
        pose_txt, pose_c = f'肩  {tilt_deg:+.0f}°  ⚠', (130, 130, 175)
    img[:] = draw_text_chinese(img, pose_txt, (10, 30), 13, pose_c)

    # Line 3: 物 (objects, only if present)
    if object_info:
        obj_txt, obj_c = f'物  {object_info}', (170, 145, 100)
    else:
        obj_txt, obj_c = '物  无', (90, 90, 90)
    img[:] = draw_text_chinese(img, obj_txt, (10, 52), 13, obj_c)

def draw_hud_decoration(img, w, h, state):
    """【简约化】移除全屏装饰，仅保留极细的姿态进度参考"""
    # 移除扫描线和大型角架
    
    # 仅保留右侧极细的姿态条
    base_neck = max(state.baseline_neck_length, 0.001)
    strain_pct = np.clip(state.neck_length / base_neck, 0.5, 1.5)
    bar_h = 100
    bar_x = w - 20
    bar_y = h // 2 - 50
    # 极细背景线
    cv2.line(img, (bar_x, bar_y), (bar_x, bar_y + bar_h), (50, 50, 50), 1)
    fill_len = int(np.clip((strain_pct - 0.5) * bar_h, 0, bar_h))
    color_bar = (100, 200, 100) if 0.8 < strain_pct < 1.2 else (100, 100, 200)
    cv2.line(img, (bar_x, bar_y + bar_h), (bar_x, bar_y + bar_h - fill_len), color_bar, 2)

def draw_face_hud(img, state, face_center, face_h):
    """已禁用：用户要求不在人脸上加任何方框/十字/文字标记"""
    return

def draw_gaze_hud(img, state, face_center):
    """【简约化】视线提示"""
    if not face_center: return
    cx, cy = int(face_center[0]), int(face_center[1])
    target_x = cx + int(state.yaw * 1.2)
    target_y = cy - int(state.pitch * 1.2)
    # 仅一个小圆点
    cv2.circle(img, (target_x, target_y), 2, (0, 255, 255), -1)

def draw_skeleton_hud(img, pose_map, w, h):
    """【增强版】绘制完整的骨架连线和关键点"""
    if not pose_map: return
    # 1. 绘制连线
    for start_id, end_id, color in SKELETON_CONNECTIONS:
        if start_id in pose_map and end_id in pose_map:
            p1 = pose_map[start_id]
            p2 = pose_map[end_id]
            if p1.get('z', 0) > 0.1 and p2.get('z', 0) > 0.1:
                cv2.line(img, (int(p1['x']*w), int(p1['y']*h)), (int(p2['x']*w), int(p2['y']*h)), color, 2)

    # 1.1 【新增】姿态平衡辅助线 (Shoulder Leveler)
    if 5 in pose_map and 6 in pose_map:
        ls, rs = pose_map[5], pose_map[6]
        if ls.get('z', 0) > 0.2 and rs.get('z', 0) > 0.2:
            lx, ly = int(ls['x']*w), int(ls['y']*h)
            rx, ry = int(rs['x']*w), int(rs['y']*h)
            mid_y = (ly + ry) // 2
            # 水平参考线 — 仅在肩膀明显倾斜时才绘制（减少视觉干扰）
            if abs(ly - ry) > (h * 0.025):
                # 当前肩膀连线（低饱和：好绿/警告暖灰/差冷灰）
                tilt_color = (155, 175, 160) if abs(ly - ry) < (h * 0.05) else (165, 160, 180)
                cv2.line(img, (lx, ly), (rx, ry), tilt_color, 1)

    # 1.2 重心垂直引导线 — 已按用户要求移除（鼻子向下的直线视觉干扰过强）

    # 2. 绘制核心关键点（低饱和度小点，避免抢戏）
    for idx, pt in pose_map.items():
        if pt.get('z', 0) > 0.05:
            cx, cy = int(pt['x'] * w), int(pt['y'] * h)
            # 全部使用低饱和灰阶，仅以小色相区分部位
            color = (180, 180, 180)
            if idx in [5, 6]: color = (165, 175, 185)   # 肩膀
            elif idx in [9, 10]: color = (185, 170, 180) # 手腕
            cv2.circle(img, (cx, cy), 3, color, -1)

# ═══════════════════════════════════════════════════════════════
#  环境模式和运行信息
# ═══════════════════════════════════════════════════════════════
# 运行元信息：模型引擎标签
APP_VERSION = "3.0"
# Build time: use exe mtime when frozen (packaged), else current datetime
if getattr(sys, 'frozen', False):
    try:
        import os as _os_bt
        _BUILD_TIME = datetime.fromtimestamp(_os_bt.path.getmtime(sys.executable)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        _BUILD_TIME = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
else:
    _BUILD_TIME = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
ENGINE_LABEL = "YOLO26 + Pose + FaceMesh"

CONFIG_FILE = get_writable_path('config.json')
DATA_FILE = get_writable_path('data.json')

# 启动时自动迁移旧数据到 APPDATA
_migrate_data_to_appdata()

# ═══════════════════════════════════════════════════════════════
#  配置数据结构定义
# ═══════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    # ──── 灵敏度和硬件设置 ────
    'sens_idx': 1,
    'camera_index': 0,
    
    # ──── 校准数据（头部姿态） ────
    'baseline_yaw': 0.0,
    'baseline_pitch': 0.0,
    'baseline_ear': 0.28,
    'baseline_neck_length': 0.0,
    'baseline_shoulder_tilt': 0.0,
    
    # ──── 四角校准专注区间 ────
    'focus_half_yaw': 0.0,    # 水平半范围 (0=未做四角校准)
    'focus_half_pitch': 0.0,  # 垂直半范围
    
    # ──── 用户界面设置 ────
    'theme': 'auto',  # 'auto', 'dark', 'light'
    'sound_on': False,
    'camera_rotation': 270,  # 0, 90, 180, 270
    'camera_flip': True,   # 是否镜像翻转
    
    # ──── 上次更新时间戳 ────
    'last_updated': 0,
}

DEFAULT_DATA = {
    # ──── 每日数据 (按日期键) ────
    'days': {},  # { '2026-04-12': { focusMs, distMs, awayMs, phoneMs, sessions, bestStreak, tlData, ... } }
    
    # ──── 历史统计 ────
    'all_time_best_streak': 0,  # 历史最佳连续专注时间
    
    # ──── 排行榜 (TOP 3) ────
    'top3': [
        {'ms': 0, 'start': 0, 'end': 0, 'label': '历史存量记录'},
        {'ms': 0, 'start': 0, 'end': 0, 'label': '空位'},
        {'ms': 0, 'start': 0, 'end': 0, 'label': '空位'}
    ],
    
    # ──── 小时级别热力图数据 ────
    'hour_focus_ms': {},  # { '2026-04-12T14:00Z': focusMs, ... }
    
    # ──── 事件日志 ────
    'events': [],  # [ { timestamp, type, data }, ... ]
    
    # ──── 上次更新时间戳 ────
    'last_updated': 0,
}

def load_config():
    """加载用户配置"""
    cfg_path = CONFIG_FILE
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception as e:
            logging.error(f"[CONFIG] 读取配置文件失败: {e}")
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(cfg):
    """保存用户配置"""
    try:
        cfg = {**DEFAULT_CONFIG, **cfg}  # 合并默认值
        cfg['last_updated'] = time.time()
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logging.info(f"[CONFIG] 配置已保存")
    except Exception as e:
        logging.error(f"[CONFIG] 保存配置失败: {e}")

def load_data():
    """加载使用数据"""
    data_path = DATA_FILE
    if os.path.exists(data_path):
        try:
            with open(data_path, 'r', encoding='utf-8') as f:
                return {**DEFAULT_DATA, **json.load(f)}
        except Exception as e:
            logging.error(f"[DATA] 读取数据文件失败: {e}")
            return DEFAULT_DATA
    return DEFAULT_DATA

def save_data(data):
    """保存使用数据"""
    try:
        data = {**DEFAULT_DATA, **data}  # 合并默认值
        data['last_updated'] = time.time()
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logging.info(f"[DATA] 数据已保存")
    except Exception as e:
        logging.error(f"[DATA] 保存数据失败: {e}")

# 初始化加载配置
current_config = load_config()

# 灵敏度预设 (与前端 SENS_PRESETS 对应) - 已收紧
SENS_TABLE = [
    {'yaw': 25, 'pitch': 18, 'delay': 2500},  # 0. 严格：极窄视线容差，视线稍偏即触发
    {'yaw': 40, 'pitch': 25, 'delay': 5000},  # 1. 正常：标准专注判定
    {'yaw': 65, 'pitch': 35, 'delay': 8000},  # 2. 双屏：允许查看侧方副屏
    {'yaw': 85, 'pitch': 50, 'delay': 11000}, # 3. 多屏：多屏宽松模式
]

# 四角校准后的阈值修正系数 [严格, 正常, 双屏, 多屏]
# 乘以校准半范围，得到最终有效阈值
# 正常/双屏/多屏 加 30% 缓冲，防止用户在校准边缘时误判走神
SENS_MULTIPLIERS = [1.1, 1.3, 1.3, 1.3]
# 双屏/多屏额外增加的横向角度容忍量(度)
SENS_BONUS_YAW   = [0,   0,  30,  60]

# 动态参数
SENS_IDX = current_config['sens_idx']
YAW_T = SENS_TABLE[SENS_IDX]['yaw']
PITCH_T = SENS_TABLE[SENS_IDX]['pitch']
STATE_COOLDOWN = SENS_TABLE[SENS_IDX]['delay'] / 1000.0

# ══════════════════════════════════════════
#  全局状态（线程安全）
# ══════════════════════════════════════════
class SharedState:
    """在多个线程之间共享的检测结果"""
    def __init__(self):
        self.lock = threading.Lock()
        # —— 输出给前端的数据 ——
        self.baseline_yaw = current_config.get('baseline_yaw', 0.0)
        self.baseline_pitch = current_config.get('baseline_pitch', 0.0)
        self.baseline_ear = current_config.get('baseline_ear', 0.28)
        self.is_calibrating = False
        self.calibration_samples = []
        self.is_monitoring_paused = False
        
        # 四角校准状态
        self.calib_corner_step = -1       # -1=空闲, 0-3=正在采集的角落编号
        self.calib_corner_samples = []    # 当前角落的采样缓冲
        self.calib_corner_warmup = 0      # 切换角落后的预热帧数（跳过，等待用户转移视线）
        self.pending_corner_result = None # {step, yaw, pitch} 等待发送给前端的结果
        self.calib_corner_auto_ready = None  # {step, yaw, pitch} 稳定后自动触发，发送后清空
        self.focus_half_yaw = current_config.get('focus_half_yaw', 0.0)
        self.focus_half_pitch = current_config.get('focus_half_pitch', 0.0)
        self.eff_yaw_threshold = float(SENS_TABLE[current_config.get('sens_idx', 1)]['yaw'])
        self.eff_pitch_threshold = float(SENS_TABLE[current_config.get('sens_idx', 1)]['pitch'])
        
        self.status = 'init'          # 'active' | 'distracted' | 'phone' | 'away'
        self.yaw = 0.0
        self.pitch = 0.0
        self.ear = 0.0
        self.mar = 0.0
        self.blinks = 0
        self.blink_rate = 0.0         # 每分钟眨眼次数
        self.nose_y = 0.0
        self.face_h = 0.0
        self.face_detected = False
        self.hands_visible = False
        # 物体检测
        self.detected_objects = []     # [{class, score, bbox}]
        self.has_phone = False
        self.has_keyboard = False
        self.has_book = False
        # 手臂姿态
        self.is_arm_up = False
        self.left_wrist_y = 1.0
        self.right_wrist_y = 1.0
        self.left_shoulder_y = 0.0
        self.right_shoulder_y = 0.0
        self.neck_length = 0.0
        self.baseline_neck_length = current_config.get('baseline_neck_length', 0.0)
        self.baseline_shoulder_tilt = current_config.get('baseline_shoulder_tilt', 0.0)
        self.shoulder_diff = 0.0
        self.shoulder_tilt = 0.0  # 【新增】精确角度
        self.posture_ratio = 0.0
        self.neck_pct = 100.0     # 【新增】颈部健康百分比
        self.slouch_angle = 0.0
        self.is_slouching = False
        self.face_keypoints_3d = {}
        # 饮水监测
        self.last_drink_time = 0      # 初始为 0
        self.hydration_minutes = -1   # -1 表示尚未检测到喝水
        # 疲劳计算 (PERCLOS)
        self.eye_state_history = []  # 存储最近 1800 帧 (约 60s) 的开合状态 (1=闭眼, 0=睁眼)
        self.perclos = 0.0
        # 判定逻辑
        self.is_looking_down = False
        self.phone_gaze_start = 0
        self.phone_alert_active = False
        self.trigger_reason = ''
        # 眨眼追踪
        self.eye_open = True
        self.last_blink_time = 0
        self.blink_timestamps = []
        self.needs_reinit_cam = False
        self.is_restarting = False

        # —— 时序平滑数据 ——
        self.face_history = deque(maxlen=30)   # 最近 30 帧 (约 1s) 的人脸检测状态
        self.person_history = deque(maxlen=30) # 最近 30 帧 (约 1s) 的 YOLO 人体探测状态
        self.pose_history = deque(maxlen=30)   # 【新增】最近 30 帧 (约 1s) 的骨骼姿态状态

        # ——检测缓存层 (用于解决闪烁问题) ——
        self.last_valid_pose_map = {}
        self.last_valid_pose_time = 0
        self.current_theme = 'dark' # 默认为深色

        # ════════════════════════════════════════════
        # 【重构】第 1-4 层指标初始化
        # ════════════════════════════════════════════
        
        # 基础信号
        self.signals = BaseSignals()
        
        # 基线校准
        self.baseline = CalibrationBaseline()
        self.baseline.yaw = self.baseline_yaw
        self.baseline.pitch = self.baseline_pitch
        self.baseline.ear = current_config.get('baseline_ear', 0.28)
        self.baseline.neck_length = current_config.get('baseline_neck_length', 0.0)
        
        # 复合指标
        self.metrics = {
            'fatigue': FatigueMetrics(),
            'hydration': HydrationDetector(),
            'phone': PhoneDetector(),
            'attention': AttentionDetector(),
            'posture': PostureMetrics(),
            'away': AwayDetector(),
        }

        # MJPEG 帧
        self.jpeg_frame = None
        
        # 连续用眼追踪
        self.eye_usage_tracker = EyeUsageTracker()
        self.eye_usage_short_rest = False  # 一次性标志，发送后清除

    def full_reset(self):
        """全面重置所有实时监测状态，用于刷新功能"""
        with self.lock:
            self.status = 'init'
            self.is_monitoring_paused = False
            self.is_calibrating = False
            self.calibration_samples = []
            self.face_detected = False
            self.yaw = 0.0
            self.pitch = 0.0
            self.ear = 0.0
            self.mar = 0.0
            self.blinks = 0
            self.blink_rate = 0.0
            self.nose_y = 0.0
            self.face_h = 0.0
            self.hands_visible = False
            self.detected_objects = []
            self.has_phone = False
            self.has_keyboard = False
            self.has_book = False
            self.is_arm_up = False
            self.neck_length = 0.0
            self.shoulder_diff = 0.0
            self.posture_ratio = 0.0
            self.slouch_angle = 0.0
            self.is_slouching = False
            self.face_keypoints_3d = {}
            self.eye_state_history = []
            self.perclos = 0.0
            self.is_looking_down = False
            self.phone_gaze_start = 0
            self.phone_alert_active = False
            self.trigger_reason = ''
            self.eye_open = True
            self.last_blink_time = 0
            self.blink_timestamps = []
            self.arm_up_frames = 0 # 消抖计数器
            self.phone_frames = 0  # 玩手机消抖
            self.drink_frames = 0  # 喝水消抖
            self.gaze_phone_frames = 0 # 视线落入手机框消抖
            # 备注：不强制重置饮水时间，保持提醒的连续性

    def reset_fatigue(self):
        """清空疲劳历史，用于在用户手动关闭警报后重置统计数据"""
        with self.lock:
            self.eye_state_history = []
            self.perclos = 0.0
            self.blinks = 0
            self.blink_timestamps = []

state = SharedState()

# ══════════════════════════════════════════
#  数学工具函数
# ══════════════════════════════════════════
def _dist(a, b):
    """两个关键点之间的欧氏距离"""
    return np.hypot(a.x - b.x, a.y - b.y)

def eye_aspect_ratio(landmarks, indices):
    """计算眼睛纵横比 (EAR)"""
    pts = [landmarks[i] for i in indices]
    v1 = _dist(pts[1], pts[5])
    v2 = _dist(pts[2], pts[4])
    h = _dist(pts[0], pts[3])
    return (v1 + v2) / (2.0 * h + 1e-6)

def mouth_aspect_ratio(landmarks):
    """计算嘴部纵横比 (MAR)"""
    inner_top = landmarks[13]
    inner_bottom = landmarks[14]
    inner_left = landmarks[78]
    inner_right = landmarks[308]
    v = _dist(inner_top, inner_bottom)
    h = _dist(inner_left, inner_right)
    return v / (h + 1e-6)

def head_pose(landmarks):
    """根据面部关键点计算偏航角和俯仰角"""
    nose = landmarks[NOSE]
    lt = landmarks[LT]
    rt = landmarks[RT]
    chin = landmarks[CHIN]
    le = landmarks[RE_IDX[0]]
    re = landmarks[LE_IDX[0]]

    eye_mid_x = (le.x + re.x) / 2.0
    eye_mid_y = (le.y + re.y) / 2.0
    t_mid = (lt.x + rt.x) / 2.0
    t_span = abs(rt.x - lt.x)

    yaw = (nose.x - t_mid) / (t_span / 2.0 + 1e-6) * 45.0
    fh = abs(chin.y - eye_mid_y)
    pitch = ((nose.y - eye_mid_y) / (fh + 1e-6) - 0.38) * 100.0

    return yaw, pitch, nose.y, fh


def _vec_norm(vec):
    return np.sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2])


def compute_slouch_angle(face_landmarks, pose_landmarks):
    """计算耳朵与肩膀连线与垂直线的角度，用于判定是否驼背"""
    if not face_landmarks or not pose_landmarks:
        return 0.0

    shoulders = [(11, 234), (12, 454)]  # 左肩-左耳, 右肩-右耳
    angles = []
    for shoulder_idx, ear_idx in shoulders:
        if shoulder_idx >= len(pose_landmarks) or ear_idx >= len(face_landmarks):
            continue
        shoulder = pose_landmarks[shoulder_idx]
        ear = face_landmarks[ear_idx]
        if getattr(shoulder, 'visibility', 1.0) < 0.5:
            continue
        if getattr(ear, 'visibility', 1.0) < 0.5:
            continue

        dx = shoulder.x - ear.x
        dy = shoulder.y - ear.y
        dz = 0.0
        length = _vec_norm((dx, dy, dz))
        if length < 1e-6:
            continue
        vertical_proj = abs(dy)
        cos_val = min(1.0, max(0.0, vertical_proj / length))
        angle = np.degrees(np.arccos(cos_val))
        angles.append(angle)

    return float(np.mean(angles)) if angles else 0.0


def compute_slouch_angle_from_pose(pose_keypoints, w, h):
    """从 YOLO Pose 关键点计算驼背角度 (修正像素比例)"""
    if not pose_keypoints:
        return 0.0

    shoulders = [(5, 3), (6, 4)]  # 左肩-左耳, 右肩-右耳
    angles = []
    for shoulder_idx, ear_idx in shoulders:
        shoulder = pose_keypoints.get(shoulder_idx)
        ear = pose_keypoints.get(ear_idx)
        if not shoulder or not ear:
            continue
        # 将归一化坐标还原为像素比例，消除长宽比畸变 (通常为 640x480)
        dx = (shoulder['x'] - ear['x']) * w
        dy = (shoulder['y'] - ear['y']) * h
        dz = 0.0
        length = _vec_norm((dx, dy, dz))
        if length < 1e-6:
            continue
        vertical_proj = abs(dy)
        cos_val = min(1.0, max(0.0, vertical_proj / length))
        angle = np.degrees(np.arccos(cos_val))
        angles.append(angle)

    return float(np.mean(angles)) if angles else 0.0


def get_pose_face_crop_bounds(pose_keypoints, w, h, margin=0.25):
    """根据 YOLO Pose 关键点计算人脸裁剪区域"""
    if not pose_keypoints:
        return None

    indices = [0, 1, 2, 3, 4]
    pts = [pose_keypoints.get(i) for i in indices if pose_keypoints.get(i)]
    if not pts:
        return None

    xs = [p['x'] for p in pts]
    ys = [p['y'] for p in pts]
    x_min = max(0, min(xs) - margin)
    y_min = max(0, min(ys) - margin)
    x_max = min(1, max(xs) + margin)
    y_max = min(1, max(ys) + margin)
    x1 = int(x_min * w)
    y1 = int(y_min * h)
    x2 = int(x_max * w)
    y2 = int(y_max * h)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return None
    return x1, y1, x2, y2


def get_face_crop_bounds(landmarks, w, h, margin=0.25):
    """根据面部关键点计算一个安全的人脸裁剪区域"""
    if not landmarks:
        return None
    xs = [pt.x for pt in landmarks]
    ys = [pt.y for pt in landmarks]
    if not xs or not ys:
        return None
    x_min = max(0, min(xs) - margin)
    y_min = max(0, min(ys) - margin)
    x_max = min(1, max(xs) + margin)
    y_max = min(1, max(ys) + margin)
    x1 = int(x_min * w)
    y1 = int(y_min * h)
    x2 = int(x_max * w)
    y2 = int(y_max * h)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return None
    return x1, y1, x2, y2


# —— 中文文字渲染工具 ——
def draw_text_chinese(img, text, pos, font_size=20, color=(255, 255, 255)):
    """使用 PIL 在 OpenCV 图像上绘制中文"""
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    try:
        # 强制使用 Windows 微软雅黑绝对路径
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", font_size)
    except:
        try:
            # 备选：黑体
            font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", font_size)
        except:
            # 实在没有就用默认
            font = ImageFont.load_default()
    
    draw.text(pos, text, font=font, fill=color)
    return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

# ══════════════════════════════════════════
#  YOLO 物体检测线程
# ══════════════════════════════════════════
class YOLODetector:
    """ONNX Runtime 物体检测器 - 全局物体检测"""

    def __init__(self, model_path='yolo26n.onnx'):
        logging.info(f"[YOLO] 正在加载 ONNX 模型: {model_path} ...")
        self.model = None
        self.model_path = model_path
        self.input_size = 640
        
        try:
            # 获取执行提供者
            providers = get_onnx_providers()
            
            # 创建推理会话
            self.session = ort.InferenceSession(model_path, providers=providers)
            logging.info(f"[YOLO] ONNX Runtime 初始化成功，使用执行提供者: {providers}")
            
            # 获取输入输出信息
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
            logging.info(f"[YOLO] 输入名称: {self.input_name}, 输出名称: {self.output_names}")
            
            self.model = self.session
        except Exception as e:
            logging.error(f"[YOLO] 加载 ONNX 模型失败: {e}")
            self.model = None
            self.session = None

    def detect(self, frame):
        """对一帧执行检测，返回过滤后的结果列表"""
        if self.model is None:
            return [], 0
        
        try:
            # 预处理
            blob, ratio, (dw, dh) = preprocess_image(frame, self.input_size)
            
            # 推理
            outputs = self.session.run(self.output_names, {self.input_name: blob})
            raw_out = outputs[0]
            
            global logging_tick
            detections = []
            valid_candidates = []
            
            # —— 协议解析与审计 ——
            # A. One-to-One (End-to-End)
            if len(raw_out.shape) == 3 and raw_out.shape[2] == 6:
                pred = raw_out[0]
                raw_count = len(pred)
                
                for i in range(raw_count):
                    row = pred[i]
                    score = float(row[4])
                    cls_id = int(row[5])
                    class_name = YOLO_TARGET_CLASSES.get(cls_id, f"obj_{cls_id}")
                    
                    # 获取该类别的特定阈值
                    thresh = YOLO_CLASS_THRESHOLDS.get(class_name, YOLO_CONF_THRESHOLD)
                    
                    if score < thresh:
                        if i < 5 and logging_tick % 100 == 0:
                            logging.debug(f"[YOLO-OBJ-REJECT] idx={i} cls={class_name} sc={score:.2f} < thresh={thresh} (Reason: low_score)")
                        continue
                    
                    if cls_id not in YOLO_TARGET_CLASSES:
                        if i < 5 and logging_tick % 100 == 0:
                            logging.debug(f"[YOLO-OBJ-REJECT] idx={i} cls={cls_id} sc={score:.2f} (Reason: unsupported_class)")
                        continue
                        
                    detections.append({
                        'id': cls_id,
                        'class': class_name,
                        'score': score,
                        'bbox_raw': row[0:4]
                    })

            # B. One-to-Many (Candidate + NMS)
            else:
                pred = raw_out[0]
                if pred.shape[0] < pred.shape[1]: pred = pred.T
                
                # NMS 之前进行预筛选与审计
                num_classes = pred.shape[1] - 4
                scores = np.max(pred[:, 4:4+num_classes], axis=1)
                class_ids = np.argmax(pred[:, 4:4+num_classes], axis=1)
                
                # 记录原始 Top 5 (仅在最终结果可能为空时有用)
                raw_count = len(pred)
                
                # 使用 NMS 处理关键候选框
                keep_boxes = postprocess_nms(pred, conf_threshold=YOLO_CONF_THRESHOLD, iou_threshold=0.45)
                
                for x1, y1, x2, y2, conf, cid in keep_boxes:
                    cid = int(cid)
                    c_name = YOLO_TARGET_CLASSES.get(cid, f"obj_{cid}")
                    
                    # 二次应用类特定阈值
                    c_thresh = YOLO_CLASS_THRESHOLDS.get(c_name, YOLO_CONF_THRESHOLD)
                    if conf < c_thresh:
                        continue
                        
                    detections.append({
                        'id': cid,
                        'class': c_name,
                        'score': float(conf),
                        'bbox_raw': [x1, y1, x2, y2]
                    })

            # —— 坐标映射与空检快照 ——
            h, w = frame.shape[:2]
            final_detections = []
            for det in detections:
                x1_r, y1_r, x2_r, y2_r = det.pop('bbox_raw')
                # 反向映射 Letterbox
                x1 = (x1_r - dw) / ratio
                y1 = (y1_r - dh) / ratio
                x2 = (x2_r - dw) / ratio
                y2 = (y2_r - dh) / ratio
                
                bbox_w = max(1, x2 - x1)
                bbox_h = max(1, y2 - y1)
                
                det['is_target'] = True # 已经在筛选阶段保证是 target
                det['bbox'] = [float(x1), float(y1), float(bbox_w), float(bbox_h)]
                final_detections.append(det)

            # [YOLO-DEBUG-SNAPSHOT] 如果有原始数据但最终结果为 0，打印前 5 个原始由于什么被拒
            if len(final_detections) == 0 and raw_count > 0 and logging_tick % 50 == 0:
                logging.debug(f"[YOLO-OBJ-WARN] raw={raw_count} detections exist, but all failed score/class filters.")
                # 这里简单提取 pred 中分数最高的几个进行展示
                # 注意：协议 A 和 B 的 pred 结构不同，此处省略具体排序实现，但在日志中已体现审计
            
            return final_detections, raw_count
            
        except Exception as e:
            logging.error(f"[YOLO] 检测失败: {e}")
            logging.error(traceback.format_exc())
            return [], 0


class YOLOPoseDetector:
    """ONNX Runtime 姿态检测器 - 上半身关键点检测"""

    def __init__(self, model_path=None):
        self.model = None
        self.session = None
        self.input_size = 640
        
        if not model_path:
            logging.info("[POSE] 未提供模型路径，姿态检测将被禁用")
            return
        
        logging.info(f"[POSE] 正在加载 ONNX 姿态模型: {model_path} ...")
        
        try:
            # 获取执行提供者
            providers = get_onnx_providers()
            
            # 创建推理会话
            self.session = ort.InferenceSession(model_path, providers=providers)
            logging.info(f"[POSE] ONNX Runtime 初始化成功，使用执行提供者: {providers}")
            
            # 获取输入输出信息
            self.input_name = self.session.get_inputs()[0].name
            self.output_names = [output.name for output in self.session.get_outputs()]
            logging.info(f"[POSE] 输入名称: {self.input_name}, 输出名称: {self.output_names}")
            
            self.model = self.session
        except Exception as e:
            logging.error(f"[POSE] 加载 ONNX 姿态模型失败: {e}")
            self.model = None
            self.session = None

    def detect(self, frame):
        """执行姿态检测，返回关键点列表 (Multi-candidate Retry Loop)"""
        if self.model is None or self.session is None:
            return []
        
        try:
            # 预处理
            blob, ratio, (dw, dh) = preprocess_image(frame, self.input_size)
            
            # 推理
            outputs = self.session.run(self.output_names, {self.input_name: blob})
            raw_out = outputs[0]
            pred = raw_out[0]
            
            # 转置检查
            if pred.shape[0] < pred.shape[1]:
                pred = pred.T
            
            # 获取所有过线候选框
            confs = pred[:, 4]
            pose_conf_thresh = 0.45
            candidate_indices = np.where(confs > pose_conf_thresh)[0]
            # 按分数降序排列
            candidate_indices = candidate_indices[np.argsort(confs[candidate_indices])[::-1]]
            
            h, w = frame.shape[:2]
            pose_results = []
            global logging_tick

            for rank, idx in enumerate(candidate_indices):
                cand_row = pred[idx]
                score = confs[idx]
                row_len = cand_row.shape[0]
                
                # 动态起始位映射
                kpt_start = 6 if row_len == 57 else 5
                
                keypoints = []
                valid_kpt_count = 0
                for kpt_idx in range(17):
                    offset = kpt_start + kpt_idx * 3
                    if offset + 2 < row_len:
                        raw_x, raw_y, raw_z = cand_row[offset:offset+3]
                        px = (raw_x - dw) / ratio
                        py = (raw_y - dh) / ratio
                        # z 代表点位置的可信度
                        if raw_z > 0.1:
                            valid_kpt_count += 1
                        
                        keypoints.append({
                            'x': float(px / w),
                            'y': float(py / h),
                            'z': float(raw_z)
                        })
                    else:
                        keypoints.append({'x': 0.0, 'y': 0.0, 'z': 0.0})

                # —— 判定核心点合理性 (Robust Contract) ——
                kp0, kp5, kp6 = keypoints[0], keypoints[5], keypoints[6]
                has_node = kp0['z'] > 0.1 and (abs(kp0['x']) > 0.0001 or abs(kp0['y']) > 0.0001)
                has_ls = kp5['z'] > 0.1 and (abs(kp5['x']) > 0.0001 or abs(kp5['y']) > 0.0001)
                has_rs = kp6['z'] > 0.1 and (abs(kp6['x']) > 0.0001 or abs(kp6['y']) > 0.0001)
                
                valid_pose = has_node or has_ls or has_rs
                
                if logging_tick % 100 == 0:
                    logging.debug(f"[YOLO-POSE-PARSE] idx={idx} rank={rank} score={score:.4f} v_kpts={valid_kpt_count} core={valid_pose}")

                if valid_pose:
                    pose_results.append(keypoints)
                    break
                else:
                    if logging_tick % 100 == 0:
                        reason = "missing_core_joints" if valid_kpt_count > 0 else "all_kpts_low_conf"
                        logging.warning(f"[YOLO-POSE-WARN] candidate {idx} rejected: {reason} (v_kpts={valid_kpt_count})")
            
            return pose_results
            
        except Exception as e:
            logging.error(f"[POSE] 姿态解析内核中断: {e}")
            return []

# ══════════════════════════════════════════
#  主视觉处理循环
# ══════════════════════════════════════════
def vision_loop():
    """主循环：读取摄像头 → MediaPipe + YOLO → 更新 state"""
    global state

    # 从配置中获取最新的摄像头索引
    cam_idx = current_config.get('camera_index', 0)
    logging.info(f"[CAM] 正在尝试打开摄像头 {cam_idx}...")
    
    cap = None
    max_attempts = 3
    for attempt in range(max_attempts):
        cap = cv2.VideoCapture(cam_idx)
        if cap and cap.isOpened():
            logging.info(f"[BOOT] 摄像头 {cam_idx} 打开成功")
            break
        cap = cv2.VideoCapture(0)
        if cap and cap.isOpened():
            logging.info(f"[CAM] 摄像头 {cam_idx} 打开失败，回退到摄像头 0")
            break
    
    # 如果仍然打不开，记录日志但继续运行（使用模拟画面）
    if not cap or not cap.isOpened():
        logging.warning("[CAM] 警告：无法打开任何摄像头设备，将使用黑色占位画面")
        cap = None
    
    # 设置分辨率
    if cap and cap.isOpened():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        # 【新增】将缓冲区大小强行限制为 1，确保每次读取的都是绝对实时的最新画面
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logging.info(f"[BOOT] 摄像头配置成功：{w}x{h}")
    else:
        # 如果没有摄像头，使用默认分辨率
        w, h = 640, 480
        logging.warning("[CAM] 摄像头不可用，使用默认分辨率 640x480")

    # —— 预热：发送一个“正在初始化”的画面给前端 ——
    ret, initial_frame = cap.read()
    if ret:
        init_rot = current_config.get('camera_rotation', 270)
        if init_rot == 90:
            initial_frame = cv2.rotate(initial_frame, cv2.ROTATE_90_CLOCKWISE)
        elif init_rot == 180:
            initial_frame = cv2.rotate(initial_frame, cv2.ROTATE_180)
        elif init_rot == 270:
            initial_frame = cv2.rotate(initial_frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if current_config.get('camera_flip', True):
            initial_frame = cv2.flip(initial_frame, 1)
        initial_frame = draw_text_chinese(initial_frame, "系统模型正在加载中...", (10, 5), 24, (255, 165, 0))
        _, jpeg = cv2.imencode('.jpg', initial_frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
        with state.lock:
            state.jpeg_frame = jpeg.tobytes()

    logging.info("[MP] 正在从 MediaPipe 初始化 Face Mesh 模型...")
    face_mesh = None
    try:
        mp_face_mesh = mp.solutions.face_mesh
        face_mesh = mp_face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        logging.info("[BOOT] MediaPipe Face Mesh 初始化成功 ✅")
    except Exception as e:
        logging.error(f"[MP] 无法初始化 MediaPipe Face Mesh：{e}")
        face_mesh = None

    # —— 初始化 YOLO26 全局检测模型 ——
    global_detector = None
    try:
        global_model_path = resolve_model_path(*YOLO_GLOBAL_MODEL_NAMES)
        if global_model_path:
            global_detector = YOLODetector(global_model_path)
            if global_detector.model is not None:
                logging.info("[BOOT] YOLO26 全局检测器加载成功 ✅")
            else:
                logging.error("[YOLO] 错误：yolo26n.onnx 文件损坏或无法加载")
                global_detector = None
        else:
            logging.error("[YOLO] ❌ 失败：未在根目录找到 yolo26n.onnx。程序将无法进行物品检测。")
            global_detector = None
    except Exception as e:
        logging.error(f"[YOLO] 加载 YOLO26 失败: {e}")
        global_detector = None

    # —— 初始化 YOLO26 姿态检测模型 ——
    pose_detector = None
    try:
        pose_model_path = resolve_model_path(*YOLO_POSE_MODEL_NAMES)
        if pose_model_path:
            pose_detector = YOLOPoseDetector(pose_model_path)
            if pose_detector.model is not None:
                logging.info("[BOOT] YOLO26 姿态检测器加载成功 ✅")
            else:
                logging.error("[POSE] 错误：yolo26n-pose.onnx 文件损坏或无法加载")
                pose_detector = None
        else:
            logging.error("[POSE] ❌ 失败：未在根目录找到 yolo26n-pose.onnx。程序将无法进行姿态检测。")
            pose_detector = None
    except Exception as e:
        logging.error(f"[POSE] 加载 YOLO26-Pose 失败: {e}")
        pose_detector = None

    global logging_tick
    last_yolo_time = 0
    last_yolo_detections = []
    raw_det_count = 0
    last_pose_time = 0
    last_pose_keypoints = None

    BLINK_EAR = 0.22
    BLINK_COOLDOWN = 0.35  # 秒

    fps_counter = 0
    fps_timer = time.time()
    
    # —— 日志去重状态缓存 ——
    last_summary_log = ""
    last_obj_raw_count = -1
    last_pose_raw_count = -1

    logging.info(
        f"[BOOT] 子系统总结: 摄像头={'✓' if cap and cap.isOpened() else '✗'} | "
        f"MediaPipe={'✓' if face_mesh else '✗'} | "
        f"YOLO物体={'✓' if global_detector else '✗'} | "
        f"YOLO姿态={'✓' if pose_detector else '✗'}"
    )
    logging.info(f"[BOOT] DoNotPlay 视觉后端已启动 | WS=ws://localhost:{WS_PORT} | HTTP=http://localhost:{MJPEG_PORT}")

    try:
        while not _shutdown_event.is_set():
            # —— 0. 重启感知 (优雅退出) ——
            with state.lock:
                if state.is_restarting:
                    logging.info("[VISION] 收到重启信号，正在主动释放摄像头资源...")
                    break

            # —— 1. 硬件生命周期管理 (支持休息期间释放设备) ——
            with state.lock:
                is_paused = state.is_monitoring_paused
                needs_reinit = state.needs_reinit_cam
                
            # A. 处理人为请求的引擎重启
            if needs_reinit:
                logging.info("[CAM] 正在应要求重启摄像头引擎...")
                if cap: cap.release()
                time.sleep(0.5)
                cam_idx = current_config.get('camera_index', 0)
                cap = cv2.VideoCapture(cam_idx)
                if cap and cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                with state.lock: state.needs_reinit_cam = False
                logging.info("[CAM] 摄像头引擎重启成功 ✅")

            # B. 核心：休息期间释放硬件
            if is_paused:
                if cap is not None:
                    logging.info("[CAM] 休息模式启动：正在彻底释放摄像头硬件以保护隐私... 💡")
                    cap.release()
                    cap = None
                    time.sleep(0.3)
                
                # —— 动态主题适配逻辑 ——
                with state.lock:
                    theme = state.current_theme
                
                if theme == 'light':
                    bg_color = (245, 245, 245) # 亮色背景 (BGR)
                    title_color = (50, 50, 50)  # 深色文字
                    sub_color = (120, 120, 120) 
                else:
                    bg_color = (20, 20, 20)    # 深色背景
                    title_color = (0, 255, 255) # 青色文字
                    sub_color = (150, 150, 150)

                # 生成休息占位画面
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                frame[:] = bg_color 
                frame = draw_text_chinese(frame, "休息模式：核心已关闭", (180, 220), 28, title_color)
                frame = draw_text_chinese(frame, "设备已释放，监控彻底暂停中", (180, 265), 16, sub_color)
                ret = True
                w, h = 640, 480
                
                # 更新 JPEG 帧，并稍微睡眠以降低负载
                _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                with state.lock:
                    state.jpeg_frame = jpeg.tobytes()
                    state.face_detected = False
                time.sleep(1.0) # 休息期间一秒一帧占位即可
                continue
            else:
                # 结束休息后自动恢复硬件
                if cap is None:
                    logging.info("[CAM] 休息结束：正在激活摄像头硬件并恢复监控... 🎥")
                    cam_idx = current_config.get('camera_index', 0)
                    cap = cv2.VideoCapture(cam_idx)
                    if cap and cap.isOpened():
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    else:
                        logging.error("[CAM] 激活失败，请检查摄像头是否连接")
                        time.sleep(2.0)
                        continue

            # 如果摄像头不可用，生成黑色占位画面
            if cap is None or not cap.isOpened():
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                frame = draw_text_chinese(frame, "摄像头不可用", (240, 220), 24, (100, 100, 100))
                ret = True
                w, h = 640, 480
            else:
                ret, frame = cap.read()
            
            if not ret and cap is not None:
                time.sleep(0.01)
                continue

            now = time.time()
            
            # —— 核心变量初始化（确保在所有代码路径中都已定义，防止 UnboundLocalError） ——
            face_detected = False
            yaw_val = 0.0
            pitch_val = 0.0
            ear_val = 0.0
            mar_val = 0.0
            nose_y_val = 0.0
            face_h_val = 0.0
            adj_yaw = 0.0
            adj_pitch = 0.0
            blink_rate = 0
            hands_visible = False
            is_arm_up = False
            has_phone = False
            has_keyboard = False
            has_book = False
            lwrist_y = 1.0
            rwrist_y = 1.0
            ls_y = 0.0
            rs_y = 0.0
            neck_length_val = 0.0
            shoulder_diff_val = 0.0
            computed_status = 'active'
            is_phone_violation = False
            is_looking_down = False
            slouch_angle = 0.0
            is_slouching = False
            face_keypoints_3d = {}
            left_iris = {}
            right_iris = {}
            posture_ratio = 0.0
            
            # —— 扩展：加入僵死检测变量 ——
            if not hasattr(state, 'last_pose_raw_data'): state.last_pose_raw_data = None
            if not hasattr(state, 'static_frame_count'): state.static_frame_count = 0
            mp_results = None

            with state.lock:
                p = state.is_monitoring_paused
                
            if p:
                # 休息期间：方向修正并直接转换为 JPEG 输出，跳过所有 AI 检测
                pause_rot = current_config.get('camera_rotation', 270)
                if pause_rot == 90:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                elif pause_rot == 180:
                    frame = cv2.rotate(frame, cv2.ROTATE_180)
                elif pause_rot == 270:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                if current_config.get('camera_flip', True):
                    frame = cv2.flip(frame, 1)
                display_frame = frame.copy()
                display_frame = draw_text_chinese(display_frame, "监控已暂停 (休息中)", (10, 5), 28, (0, 255, 255))
                
                _, jpeg = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                with state.lock:
                    state.jpeg_frame = jpeg.tobytes()
                # 暂停期间显著降低循环频率，节省更多性能
                time.sleep(0.1)
                continue

            # —— 执行视觉检测 ——
            try:
                # —— 方向修正：从配置动态获取旋转和翻转参数 ——
                rot_val = current_config.get('camera_rotation', 0)
                flip_val = current_config.get('camera_flip', True)
                
                if rot_val == 90:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                elif rot_val == 180:
                    frame = cv2.rotate(frame, cv2.ROTATE_180)
                elif rot_val == 270:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                
                if flip_val:
                    frame = cv2.flip(frame, 1)

                # —— 关键修复：在旋转和翻转后，重新获取真实的宽高比例 ——
                h, w = frame.shape[:2]
                
                display_frame = frame.copy()
                # 重新获取旋转后的宽高
                h, w = frame.shape[:2]

                # ════════════════════════════════════
                #  1. MediaPipe Face Mesh 处理
                # ════════════════════════════════════
                mp_results = None

                # 2. YOLO Pose 上半身关键点检测
                pose_results = [] # 核心修复 1: 确保变量每帧重置，防止非检测帧污染
                pose_data = None
                pose_attempted = False
                
                if pose_detector and (now - last_pose_time > YOLO_POSE_INTERVAL):
                    last_pose_time = now
                    pose_attempted = True
                    try:
                        pose_results = pose_detector.detect(frame)
                        pr_count = len(pose_results) if pose_results else 0
                        
                        # [YOLO-POSE-RAW] 仅在数量变化时或每 100 帧记录一次
                        if pr_count != last_pose_raw_count or logging_tick % 100 == 0:
                            if pr_count > 0: # 仅记录有人的情况，减少“0 结果”刷屏
                                logging.debug(f"[YOLO-POSE-RAW] pose_results_count={pr_count}")
                            last_pose_raw_count = pr_count
                            
                        if pose_results:
                            pose_data = pose_results[0]
                    except Exception as e:
                        logging.error(f"[POSE] 上半身姿态检测保护：{e}")
                        logging.error(traceback.format_exc())
                        pose_data = None

                pose_map = {i: pt for i, pt in enumerate(pose_data)} if pose_data else {}
                
                # [YOLO-POSE-MAP]
                if pose_results:
                    if not pose_map:
                        logging.debug("[YOLO-POSE-WARN] pose_results exists but pose_map is empty after parsing")
                    else:
                        has_nose = 0 in pose_map
                        has_ls = 5 in pose_map
                        has_rs = 6 in pose_map
                        # 仅在每 100 帧或关键点缺失时提示
                        if logging_tick % 100 == 0 or not has_nose or not has_ls or not has_rs:
                            logging.debug(f"[YOLO-POSE-MAP] keypoint_count={len(pose_map)} has_nose={has_nose} has_l_shoulder={has_ls} has_r_shoulder={has_rs}")
                        
                        # 每 100 帧打印一次核心坐标详情，防止刷屏
                        if logging_tick % 100 == 0:
                            if has_nose: logging.debug(f"[YOLO-POSE-MAP] kp0 (nose)={pose_map[0]}")
                            if has_ls:   logging.debug(f"[YOLO-POSE-MAP] kp5 (l_shoulder)={pose_map[5]}")
                            if has_rs:   logging.debug(f"[YOLO-POSE-MAP] kp6 (r_shoulder)={pose_map[6]}")
                
                # —— 姿态平滑逻辑 (Pose Smoothing) ——
                with state.lock:
                    if not hasattr(state, 'pose_fail_streak'): state.pose_fail_streak = 0
                    
                    if pose_map:
                        # 发现新姿态，刷新缓存并重置失败计数
                        state.last_valid_pose_map = pose_map
                        state.last_valid_pose_time = now
                        state.pose_fail_streak = 0
                    else:
                        # 失败处理：仅当真正尝试了检测且结果为空时，才增加连续失败计数 (核心修复 2)
                        if pose_attempted:
                            state.pose_fail_streak += 1
                        
                        # 尝试短时间(0.5s)复用旧缓存
                        # 如果距离上次有效时间超过 0.5s，或者连续失败帧数过多
                        if (now - state.last_valid_pose_time < 0.5) and state.pose_fail_streak < 15:
                            pose_map = state.last_valid_pose_map
                            if logging_tick % 100 == 0:
                                logging.debug(f"[POSE-SMOOTH] 使用缓存姿态 (存活中: {now - state.last_valid_pose_time:.2f}s)")
                        else:
                            # 彻底失效：强制清空，确保 HUD 显示 no pose
                            pose_map = {}
                            if state.pose_fail_streak == 15 or (now - state.last_valid_pose_time >= 0.5 and now - state.last_valid_pose_time < 0.6):
                                logging.debug(f"[POSE-SMOOTH] 姿态缓存已过期或连续失败次数过多，置为 no pose。")
                
                # —— 检测僵死 (Stuck Detection) ——
                if pose_map:
                    # 将当前姿态坐标序列化用于对比
                    curr_raw = str([(p['x'], p['y']) for p in pose_map.values()])
                    if curr_raw == state.last_pose_raw_data:
                        state.static_frame_count += 1
                    else:
                        state.static_frame_count = 0
                    state.last_pose_raw_data = curr_raw
                    
                    if state.static_frame_count > 50: # 连续 50 帧完全一样，极度可疑
                        if logging_tick % 100 == 0:
                            logging.debug(f"[POSE] 发现姿态数据僵死！已持续 {state.static_frame_count} 帧。")

                crop_box = None
                if pose_map and face_mesh:
                    crop_box = get_pose_face_crop_bounds(pose_map, w, h, margin=0.30)

                fm_landmarks = None
                if face_mesh:
                    face_crop = frame
                    x1 = y1 = x2 = y2 = 0
                    if crop_box:
                        x1, y1, x2, y2 = crop_box
                        face_crop = frame[y1:y2, x1:x2]
                    try:
                        face_crop_rgb = cv2.cvtColor(face_crop, cv2.COLOR_BGR2RGB)
                        face_mesh_results = face_mesh.process(face_crop_rgb)
                        if face_mesh_results and face_mesh_results.multi_face_landmarks:
                            fm_landmarks = face_mesh_results.multi_face_landmarks[0].landmark
                            face_detected = True
                            
                            # 提取核心指标
                            yaw_val, pitch_val, nose_y_val, face_h_val = head_pose(fm_landmarks)
                            ear_val = (eye_aspect_ratio(fm_landmarks, LE_IDX) + eye_aspect_ratio(fm_landmarks, RE_IDX)) / 2.0
                            mar_val = mouth_aspect_ratio(fm_landmarks)
                            
                            # 计算校准偏差
                            with state.lock:
                                adj_yaw = yaw_val - state.baseline_yaw
                                adj_pitch = pitch_val - state.baseline_pitch
                        else:
                            # —— 失败补偿机制：局部检测没搜到脸，尝试全画幅扫描 ——
                            if crop_box is not None:
                                face_mesh_results_full = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                                if face_mesh_results_full and face_mesh_results_full.multi_face_landmarks:
                                    fm_landmarks = face_mesh_results_full.multi_face_landmarks[0].landmark
                                    face_detected = True
                                    x1, y1, x2, y2 = 0, 0, w, h
                                    yaw_val, pitch_val, nose_y_val, face_h_val = head_pose(fm_landmarks)
                                    ear_val = (eye_aspect_ratio(fm_landmarks, LE_IDX) + eye_aspect_ratio(fm_landmarks, RE_IDX)) / 2.0
                                    mar_val = mouth_aspect_ratio(fm_landmarks)
                                    with state.lock:
                                        adj_yaw = yaw_val - state.baseline_yaw
                                        adj_pitch = pitch_val - state.baseline_pitch
                    except Exception as e:
                        logging.error(f"[MP] Face Mesh 处理失败：{e}")
                        logging.error(traceback.format_exc())
                        fm_landmarks = None

                    if fm_landmarks:
                        face_keypoints_3d = {
                            'nose': {'x': round(fm_landmarks[NOSE].x, 4), 'y': round(fm_landmarks[NOSE].y, 4), 'z': round(getattr(fm_landmarks[NOSE], 'z', 0.0), 4)},
                            'left_ear': {'x': round(fm_landmarks[LT].x, 4), 'y': round(fm_landmarks[LT].y, 4), 'z': round(getattr(fm_landmarks[LT], 'z', 0.0), 4)},
                            'right_ear': {'x': round(fm_landmarks[RT].x, 4), 'y': round(fm_landmarks[RT].y, 4), 'z': round(getattr(fm_landmarks[RT], 'z', 0.0), 4)},
                            'left_eye': {'x': round(fm_landmarks[RE_IDX[0]].x, 4), 'y': round(fm_landmarks[RE_IDX[0]].y, 4), 'z': round(getattr(fm_landmarks[RE_IDX[0]], 'z', 0.0), 4)},
                            'right_eye': {'x': round(fm_landmarks[LE_IDX[0]].x, 4), 'y': round(fm_landmarks[LE_IDX[0]].y, 4), 'z': round(getattr(fm_landmarks[LE_IDX[0]], 'z', 0.0), 4)}
                        }

                    if fm_landmarks:
                        # 计算当前裁剪框内的人脸中心点（用于绘制十字准星）
                        ncx, ncy = int(fm_landmarks[NOSE].x * (x2 - x1)) + x1, int(fm_landmarks[NOSE].y * (y2 - y1)) + y1
                        
                        # 绘制关键点（简化版：眼部+鼻尖）
                        for idx in LE_IDX + RE_IDX + [NOSE]:
                            if idx < len(fm_landmarks):
                                pt = fm_landmarks[idx]
                                cx, cy = int(pt.x * (x2 - x1)) + x1, int(pt.y * (y2 - y1)) + y1
                                cv2.circle(display_frame, (cx, cy), 3, (100, 255, 100), -1)
                        # 绘制人脸中心十字准星
                        cv2.line(display_frame, (ncx - 10, ncy), (ncx + 10, ncy), (100, 255, 100), 1)
                        cv2.line(display_frame, (ncx, ncy - 10), (ncx, ncy + 10), (100, 255, 100), 1)
                    
                    # —— 调试可视化：绘制裁剪框或搜索状态 ——
                    if crop_box and not face_detected:
                        cx1, cy1, cx2, cy2 = crop_box
                        # 橙色框表示系统“尝试”在此区域寻找人脸但失败了
                        cv2.rectangle(display_frame, (cx1, cy1), (cx2, cy2), (0, 165, 255), 1)
                    elif crop_box and face_detected and (x2 - x1 < w):
                        # 绿色细框表示系统当前锁定的面部检测区域
                        cv2.rectangle(display_frame, (x1, y1), (x2, y2), (100, 255, 100), 1)

                slouch_angle = 0.0
                is_slouching = False
                if pose_map:
                    # 手腕上抬判定——放宽为 y < 0.80（原为 0.70，举手机到胸前的姿势也计入）
                    detect_up = False
                    if pose_map.get(9) and pose_map.get(10):
                        if pose_map[9]['y'] < 0.80 or pose_map[10]['y'] < 0.80:
                            detect_up = True
                    
                    # 消抖处理：3 帧即可（原为 5 帧，从画面外拿入时响应更快）
                    with state.lock:
                        # 安全检查：确保变量存在（防御旧进程残留）
                        if not hasattr(state, 'arm_up_frames'): state.arm_up_frames = 0
                        
                        if detect_up:
                            state.arm_up_frames = min(state.arm_up_frames + 1, 10)
                        else:
                            state.arm_up_frames = max(state.arm_up_frames - 1, 0)
                        is_arm_up = state.arm_up_frames >= 3
                        
                        # 【新增】：手腕与桌面物品(键盘/书籍)的交互判定，防止低头看书误判离开
                        hands_visible = False
                        left_wrist = pose_map.get(9)
                        right_wrist = pose_map.get(10)
                        # 把手腕信号导出到 BaseSignals 供下游模块（如 PhoneDetector）使用
                        state.signals.left_wrist = left_wrist
                        state.signals.right_wrist = right_wrist
                        if left_wrist or right_wrist:
                            for det in last_yolo_detections:
                                if det['class'] in ['keyboard', 'laptop', 'book']:
                                    bx, by, bw, bh = det['bbox']
                                    padding = 40  # 容差范围
                                    x_min, x_max = bx - padding, bx + bw + padding
                                    y_min, y_max = by - padding, by + bh + padding
                                    
                                    if left_wrist and (x_min <= left_wrist['x'] * w <= x_max) and (y_min <= left_wrist['y'] * h <= y_max):
                                        hands_visible = True
                                        break
                                    if right_wrist and (x_min <= right_wrist['x'] * w <= x_max) and (y_min <= right_wrist['y'] * h <= y_max):
                                        hands_visible = True
                                        break

                    left_shoulder = pose_map.get(5)
                    right_shoulder = pose_map.get(6)
                    nose_point = pose_map.get(0)
                    if left_shoulder and right_shoulder:
                        ls_x, ls_y = left_shoulder['x'], left_shoulder['y']
                        rs_x, rs_y = right_shoulder['x'], right_shoulder['y']
                        
                        # 1. 精确计算肩膀倾斜角 (使用像素比例还原真实的物理角度)
                        dx = (rs_x - ls_x) * w
                        dy = (rs_y - ls_y) * h
                        shoulder_tilt_deg = np.degrees(np.arctan2(dy, max(abs(dx), 1)))
                        # 减去校准时的基线倾斜角，消除摄像头安装角度的影响
                        shoulder_diff_val = shoulder_tilt_deg - getattr(state, 'baseline_shoulder_tilt', 0.0)
                        
                        # 2. 精确计算颈部长度 (鼻子到双肩中点的距离)
                        if nose_point:
                            neck_length_val = (ls_y + rs_y) / 2.0 - nose_point['y']
                    
                    slouch_angle = compute_slouch_angle_from_pose(pose_map, w, h)
                    is_slouching = slouch_angle > 17.0

                # 自动校准采样
                with state.lock:
                    if state.is_calibrating:
                        if len(state.calibration_samples) < 30:
                            state.calibration_samples.append({
                                'neck': neck_length_val,
                                'yaw': yaw_val,
                                'pitch': pitch_val,
                                'ear': ear_val,
                                'shoulder_tilt': shoulder_diff_val
                            })
                        else:
                            state.baseline_neck_length = sum(s['neck'] for s in state.calibration_samples) / 30
                            state.baseline_yaw = sum(s['yaw'] for s in state.calibration_samples) / 30
                            state.baseline_pitch = sum(s['pitch'] for s in state.calibration_samples) / 30
                            state.baseline_ear = sum(s['ear'] for s in state.calibration_samples) / 30
                            state.baseline_shoulder_tilt = sum(s['shoulder_tilt'] for s in state.calibration_samples) / 30
                            state.is_calibrating = False
                            
                            # 同步到 CalibrationBaseline 对象（被 build_comprehensive_payload 使用）
                            state.baseline.yaw = state.baseline_yaw
                            state.baseline.pitch = state.baseline_pitch
                            state.baseline.ear = state.baseline_ear
                            state.baseline.neck_length = state.baseline_neck_length
                            
                            # 持久化存储校准数据
                            save_config({
                                'sens_idx': SENS_IDX, 
                                'baseline_yaw': round(state.baseline_yaw, 4),
                                'baseline_pitch': round(state.baseline_pitch, 4),
                                'baseline_ear': round(state.baseline_ear, 4),
                                'baseline_neck_length': round(state.baseline_neck_length, 4),
                                'baseline_shoulder_tilt': round(state.baseline_shoulder_tilt, 4),
                            })
                            logging.info(f"[CALIB] 校准完成: Neck={state.baseline_neck_length:.4f}, Yaw={state.baseline_yaw:.1f}, EAR={state.baseline_ear:.3f}, ShoulderTilt={state.baseline_shoulder_tilt:.1f}")

                # 四角专注区间校准：采集当前角落帧
                if face_detected:
                    with state.lock:
                        if state.calib_corner_step >= 0:
                            # 预热阶段：跳过前 N 帧，等待用户将视线移至新角落
                            if state.calib_corner_warmup > 0:
                                state.calib_corner_warmup -= 1
                            else:
                                state.calib_corner_samples.append({'yaw': yaw_val, 'pitch': pitch_val, 'ear': ear_val})
                                # 自动稳定检测：≥10帧 且标准差 < 4° 时发出就绪通知
                                samp = state.calib_corner_samples
                                if len(samp) >= 10 and state.calib_corner_auto_ready is None:
                                    yaws   = [s['yaw']   for s in samp]
                                    pitches = [s['pitch'] for s in samp]
                                    mean_y = sum(yaws) / len(yaws)
                                    mean_p = sum(pitches) / len(pitches)
                                    std_y = (sum((v - mean_y)**2 for v in yaws) / len(yaws)) ** 0.5
                                    std_p = (sum((v - mean_p)**2 for v in pitches) / len(pitches)) ** 0.5
                                    # Trigger if stable (std < 8°) OR if we have enough samples regardless of stability
                                    if std_y < 8.0 and std_p < 8.0:
                                        state.calib_corner_auto_ready = {
                                            'step': state.calib_corner_step,
                                            'yaw':  round(mean_y, 4),
                                            'pitch': round(mean_p, 4),
                                        }
                                # Force-trigger after 50 samples even if noisy (use best 20-sample window)
                                elif len(samp) >= 50 and state.calib_corner_auto_ready is None:
                                    best_start, best_std = 0, float('inf')
                                    for i in range(len(samp) - 20):
                                        window = samp[i:i+20]
                                        wy = [s['yaw'] for s in window]
                                        mean_w = sum(wy) / 20
                                        std_w = (sum((v - mean_w)**2 for v in wy) / 20) ** 0.5
                                        if std_w < best_std:
                                            best_std = std_w
                                            best_start = i
                                    window = samp[best_start:best_start+20]
                                    mean_y = sum(s['yaw'] for s in window) / 20
                                    mean_p = sum(s['pitch'] for s in window) / 20
                                    state.calib_corner_auto_ready = {
                                        'step': state.calib_corner_step,
                                        'yaw':  round(mean_y, 4),
                                        'pitch': round(mean_p, 4),
                                    }

                # 疲劳度 PERCLOS 计算与眨眼检测
                if face_detected:
                    with state.lock:
                        # 动态判定阈值：基于校准基线的相对比例，正确处理天生眼睛较小的人
                        # 取 baseline 的 68% 作为闭眼判定阈值（相对于本人校准值），不做绝对值裁剪
                        # 仅保留极低安全底线(0.08)，防止未校准时(baseline=0)出错
                        dynamic_threshold = max(state.baseline_ear * 0.68, 0.08)
                        is_closed = ear_val < dynamic_threshold
                        if is_closed and state.eye_open:
                            state.eye_open = False
                        elif not is_closed and not state.eye_open:
                            state.eye_open = True
                            if now - state.last_blink_time > BLINK_COOLDOWN:
                                state.blinks += 1
                                state.last_blink_time = now
                                state.blink_timestamps.append(now)
                        
                        # 记录开合状态 (1=闭眼, 0=睁眼)
                        state.eye_state_history.append(1 if is_closed else 0)
                        if len(state.eye_state_history) > 1200: 
                            state.eye_state_history.pop(0)
                        if len(state.eye_state_history) > 100:
                            state.perclos = (sum(state.eye_state_history) / len(state.eye_state_history)) * 100
                        
                        state.blink_timestamps = [t for t in state.blink_timestamps if now - t < 60]
                        blink_rate = len(state.blink_timestamps)
                else:
                    # 没脸时重置部分瞬时状态，但不清空历史
                    state.eye_open = True 

                # ════════════════════════════════════
                #  3. YOLO 全局物体检测（节流）
                # ════════════════════════════════════
                if global_detector and (now - last_yolo_time > YOLO_GLOBAL_INTERVAL):
                    last_yolo_time = now
                    try:
                        last_yolo_detections, raw_det_count = global_detector.detect(frame)
                        
                        # [YOLO-OBJ-RAW] 仅在有结果且数量变化或每 100 帧记录
                        if raw_det_count != last_obj_raw_count or logging_tick % 100 == 0:
                            if raw_det_count > 0:
                                logging.debug(f"[YOLO-OBJ-RAW] raw_count={raw_det_count}")
                                # 【调试增强】打印前 10 个原始 Detection 详情
                                for idx, det in enumerate(last_yolo_detections[:10]):
                                    logging.debug(f"[YOLO-OBJ-RAW] idx={idx} id={det.get('id', -1)} class={det['class']} score={det['score']:.2f} bbox={[int(b) for b in det['bbox']]}")
                            last_obj_raw_count = raw_det_count
                    except Exception as e:
                        logging.error(f"[YOLO] 全局检测保护：{e}")
                        logging.error(traceback.format_exc())
                        last_yolo_detections, raw_det_count = [], 0

                # 使用 Pose 数据中的鼻子作为面部关键参考点
                nose_px = None
                if pose_map and 0 in pose_map:
                    nose_px = (pose_map[0]['x'] * w, pose_map[0]['y'] * h)

                # —— 绘制高级 HUD 元素 ——
                
                # 1. 绘制全局装饰 (边角、扫描线、姿态条)
                draw_hud_decoration(display_frame, w, h, state)

                # —— 计算视线焦点 (校准后的广角投影) ——
                gaze_x, gaze_y = -1, -1
                if face_detected and nose_px:
                    # 将转换系数从 1.5 提升至 11.0，使焦点能够大幅跟随头部转动
                    gaze_x = nose_px[0] + (adj_yaw * 11.0)
                    gaze_y = nose_px[1] - (adj_pitch * 11.0)

                # —— 2. 绘制检测到的物体并收集 HUD 信息 ——
                ph_phys_now = False # 物理举起
                ph_gaze_now = False # 视线落入
                ph_down_now = False # 低头看手机 (叠加信号)
                ph_arm_gaze_now = False # 手臂举起 + 视线朝向手 (遮挡手机场景)
                dr_detected_now = False
                obj_hud_list = []
                yolo_person_detected = False 
                has_phone = False
                has_keyboard = False
                has_book = False
                
                # —— 目标优先级排序逻辑 (Priority Logic) ——
                # 业务高优先级目标列表
                PRIORITY_CLASSES = ['cell phone', 'remote', 'keyboard', 'laptop', 'book', 'bottle', 'cup']
                
                # 分类整理结果
                priority_dets = [d for d in last_yolo_detections if d['class'] in PRIORITY_CLASSES]
                person_dets = [d for d in last_yolo_detections if d['class'] == 'person']
                other_dets = [d for d in last_yolo_detections if d['class'] not in PRIORITY_CLASSES and d['class'] != 'person']
                
                # 按顺序组合 (业务 > 其他 > 人员作为背景)
                sorted_dets = sorted(priority_dets, key=lambda x: x['score'], reverse=True) + \
                              sorted(other_dets, key=lambda x: x['score'], reverse=True) + \
                              sorted(person_dets, key=lambda x: x['score'], reverse=True)
                
                # [YOLO-OBJ-FINAL] 记录最终决策队列
                if logging_tick % 100 == 0 and len(sorted_dets) > 0:
                    logging.debug(f"[YOLO-OBJ-FINAL] Queue_size={len(sorted_dets)} Top_priorities={[d['class'] for d in sorted_dets[:3]]}")

                for det in sorted_dets:
                    cls_name = det['class']
                    score = det['score']
                    bx, by, bw, bh = det['bbox']
                    cx, cy = bx + bw/2, by + bh/2
                    is_target = det.get('is_target', False)
                    
                    # —— 席位在场判定 ——
                    # 提高人在席位的置信度门槛，只要看到身子或头就算人在 (0.2 -> 0.45)
                    # 此处调高门槛以过滤如“椅子、衣物”等导致的离席判定失效
                    if cls_name == 'person' and score > 0.45:
                        yolo_person_detected = True
                    
                    # 汇总至左上角 HUD 列表 (限制前 4 个，优先保证业务目标)
                    display_label = BBOX_LABELS.get(cls_name, cls_name)
                    if not is_target:
                        display_label = f"[RAW]{display_label}"
                    
                    if len(obj_hud_list) < 4:
                        # 如果是 person 类，且已经有其他目标，则不占前排位置
                        if cls_name == 'person' and len(obj_hud_list) >= 2 and len(priority_dets) > 0:
                            pass
                        else:
                            obj_hud_list.append(f"{display_label} {score:.2f}")

                    # 获取物体中心与鼻子的距离
                    dist_to_nose = 1000.0
                    if nose_px:
                        dist_to_nose = np.hypot(cx - nose_px[0], cy - nose_px[1])

                    # —— 执行绘制与业务判定 ——
                    if is_target:
                        # A. 业务白名单物体：执行特定动作检测（喝水、手机等）
                        if cls_name in ['bottle', 'cup']:
                            color = (255, 180, 0) # 亮蓝
                            box_label = "CUP/BOTTLE"
                            # 杯子垂直移动检测：记录当前 Y 位置，和上次比较
                            cup_y_normalized = cy / max(h, 1)
                            if not hasattr(state, '_last_cup_y'): state._last_cup_y = cup_y_normalized
                            if not hasattr(state, '_cup_move_frames'): state._cup_move_frames = 0
                            cup_y_delta = abs(cup_y_normalized - state._last_cup_y)
                            state._last_cup_y = state._last_cup_y * 0.7 + cup_y_normalized * 0.3  # 平滑
                            cup_is_moving = cup_y_delta > 0.008  # 约 5px 移动即视为运动

                            # 原有：杯子靠近嘴巴
                            if face_detected and dist_to_nose < (face_h_val * h * 1.2):
                                box_label = "DRINKING..."
                                cv2.line(display_frame, (int(cx), int(cy)), (int(nose_px[0]), int(nose_px[1])), color, 2)
                                dr_detected_now = True
                            # 杯子存在 + 手臂举起 + 头部后仰 (pitch < -5°)
                            elif face_detected and is_arm_up and adj_pitch < -5:
                                box_label = "DRINKING..."
                                dr_detected_now = True
                            # 杯子存在 + 手臂举起 + 杯子在垂直方向移动 → 举杯喝水
                            elif is_arm_up and cup_is_moving:
                                box_label = "DRINKING..."
                                dr_detected_now = True
                        elif cls_name in PHONE_CLASSES:
                            # 尺寸过滤：手表比手机小得多，要求检测到的物体面积至少占帧面积的 1%
                            # 手机在视野中通常 > 1%，手表通常 < 0.3%
                            phone_area_ratio = (bw * bh) / max(w * h, 1)
                            if phone_area_ratio < 0.01:
                                # 目标太小，疑似手表或遥控器小物件，不触发手机警报
                                color = (120, 120, 200)
                                box_label = "WATCH?"
                                # has_phone 保持 False，不更新 ph_phys_now/ph_gaze_now
                            else:
                                color = (0, 0, 255) # 鲜红
                                # 物理距离判定：扩大到 50% 帧宽，捕捉平视或远距离手持手机
                                if nose_px and dist_to_nose < (w * 0.50):
                                    ph_phys_now = True
                                # 视线落入手机区域 (扩大容差至 ±40px)
                                if (bx - 40) <= gaze_x <= (bx + bw + 40) and (by - 40) <= gaze_y <= (by + bh + 40):
                                    ph_gaze_now = True
                                    draw_text_chinese(display_frame, "GAZE LOCKING...", (int(bx), int(by)-20), 14, color)
                                # 叠加信号：手机存在 + 低头 (pitch > 8°) → 大概率在看手机
                                if face_detected and adj_pitch > 8:
                                    ph_down_now = True
                                box_label = "PHONE [!]" if state.phone_alert_active else "PHONE"
                                if ph_phys_now:
                                    cv2.line(display_frame, (int(cx), int(cy)), (int(nose_px[0]), int(nose_px[1])), color, 2)
                                has_phone = True
                        else:
                            color = (0, 255, 255) if cls_name in ['keyboard', 'laptop'] else (0, 255, 0)
                            if cls_name in ['keyboard', 'laptop']: has_keyboard = True
                            elif cls_name == 'book': has_book = True
                            box_label = display_label
                        
                        # 跳过人体框：人本身不需要边框（骨架连线已提供位置信息）
                        if cls_name != 'person':
                            draw_tech_box(display_frame, int(bx), int(by), int(bw), int(bh), color, box_label, score)
                    else:
                        # B. 非业务目标（RAW 模式）— 不再绘制任何框，避免打扰画面
                        pass

                # —— 调试日志：输出最终识别摘要 ——
                if raw_det_count > 0:
                    if not obj_hud_list:
                        logging.debug(f"[YOLO-OBJ-WARN] raw_count={raw_det_count} detections exist, but all failed score thresholds?")
                    elif logging_tick % 100 == 0:
                        logging.debug(f"[YOLO-OBJ-HUD] obj_hud_list={obj_hud_list}")

                # 3. 汇总判定结果与消抖 (State Smoothing)
                with state.lock:
                    # 安全检查：防御旧进程成员缺失
                    if not hasattr(state, 'drink_frames'): state.drink_frames = 0
                    if not hasattr(state, 'phone_frames'): state.phone_frames = 0
                    if not hasattr(state, 'gaze_phone_frames'): state.gaze_phone_frames = 0
                    if not hasattr(state, 'phone_down_frames'): state.phone_down_frames = 0
                    if not hasattr(state, 'phone_arm_gaze_frames'): state.phone_arm_gaze_frames = 0
                    if not hasattr(state, 'last_phone_seen'): state.last_phone_seen = 0.0
                    if not hasattr(state, 'phone_hold_frames'): state.phone_hold_frames = 0

                    # 记录手机最后被识别的时间 (用于遮挡持续推断)
                    if has_phone:
                        state.last_phone_seen = now

                    # 手被遮挡时的推断信号：手臂举起 + 视线朝向手腕区域 + 手机最近 3 秒内出现过
                    phone_recently_seen = (now - state.last_phone_seen) < 3.0
                    if is_arm_up and face_detected and phone_recently_seen and not has_phone:
                        # 手机刚消失但手臂仍举着 → 很可能被手遮挡，维持检测
                        ph_arm_gaze_now = True
                    elif is_arm_up and face_detected and not has_phone:
                        # 手臂举起 + 视线低于正常(pitch > 5°) → 可疑但需更长时间确认
                        if adj_pitch > 5:
                            ph_arm_gaze_now = True

                    # 喝水消抖：需持续约 0.4 秒 (12帧)
                    if dr_detected_now: state.drink_frames = min(state.drink_frames + 1, 40)
                    else: state.drink_frames = max(0, state.drink_frames - 1)
                    if state.drink_frames >= 12: state.last_drink_time = now

                    # 手机物理消抖 v3：3 帧 (~0.1s) 极速响应，放下后快速衰减
                    if ph_phys_now: state.phone_frames = min(state.phone_frames + 1, 30)
                    else: state.phone_frames = max(0, state.phone_frames - 3)
                    
                    # 手机视线消抖 v3：5 帧 (~0.17s)，快速衰减
                    if ph_gaze_now: state.gaze_phone_frames = min(state.gaze_phone_frames + 1, 60)
                    else: state.gaze_phone_frames = max(0, state.gaze_phone_frames - 3)

                    # 低头看手机消抖 v3：20 帧 (~0.67s)，快速衰减
                    if ph_down_now: state.phone_down_frames = min(state.phone_down_frames + 1, 120)
                    else: state.phone_down_frames = max(0, state.phone_down_frames - 4)

                    # 手臂举起遮挡推断消抖 v3：30 帧 (~1s)，快速衰减
                    if ph_arm_gaze_now: state.phone_arm_gaze_frames = min(state.phone_arm_gaze_frames + 1, 120)
                    else: state.phone_arm_gaze_frames = max(0, state.phone_arm_gaze_frames - 4)

                    # 手持手机消抖 v3：6 帧 (~0.2s) 极速触发，放下后快速衰减
                    if has_phone: state.phone_hold_frames = min(state.phone_hold_frames + 1, 40)
                    else: state.phone_hold_frames = max(0, state.phone_hold_frames - 3)

                    # 最终警报 v3：大幅降低所有触发阈值
                    state.phone_alert_active = (state.phone_hold_frames >= 6) or (state.phone_frames >= 3) or (state.gaze_phone_frames >= 5) or (state.phone_down_frames >= 20) or (state.phone_arm_gaze_frames >= 30)

                # 3. 绘制人脸/视线追踪
                if face_detected and nose_px:
                    # 绘制人脸锁定 HUD
                    draw_face_hud(display_frame, state, nose_px, face_h_val)
                    # 绘制视线准星
                    draw_gaze_hud(display_frame, state, nose_px)

                # 4. 绘制人体骨架
                if pose_map:
                    draw_skeleton_hud(display_frame, pose_map, w, h)

                # —— 时序平滑处理 (Presence Smoothing) ——
                with state.lock:
                    state.face_history.append(face_detected)
                    state.person_history.append(yolo_person_detected)
                    state.pose_history.append(len(pose_map) > 0)
                    
                    # 规则：30 帧中只要有 5 帧检测到（约占 1/6），即认为存在
                    face_detected_smoothed = sum(state.face_history) >= 5
                    person_detected_smoothed = sum(state.person_history) >= 5
                    pose_detected_smoothed = sum(state.pose_history) >= 5

                # [YOLO-POSE-HUD] 构造姿态汇总状态
                pose_hud_status = {
                    'valid': len(pose_map) > 0,
                    'shoulders': (5 in pose_map and 6 in pose_map),
                    'slouch': slouch_angle
                }
                
                # [YOLO-HUD-SUMMARY]
                current_summary_log = f"F={face_detected_smoothed} P={person_detected_smoothed} Pose={pose_detected_smoothed} O={len(obj_hud_list)}"
                if logging_tick % 100 == 0:
                    logging.debug(f"[YOLO-HUD-SUMMARY] FACE={'yes' if face_detected_smoothed else 'no'} | PERSON={'yes' if person_detected_smoothed else 'no'} | POSE={'valid' if pose_detected_smoothed else 'no pose'} | OBJ={len(obj_hud_list)} raw={raw_det_count}")

                # 5. 绘制顶层汇总 HUD
                draw_runtime_hud(display_frame, 
                    {
                        'detected': face_detected_smoothed,
                        'person': person_detected_smoothed, 
                        'ear': ear_val,
                        'yaw': adj_yaw,
                        'pitch': adj_pitch
                    },
                    pose_hud_status,
                    " | ".join(obj_hud_list),
                    raw_det_count
                )

                # 收紧判定核心：直接对比预设阈值，不再提供额外的 1.5 倍增益
                if abs(adj_yaw) < YAW_T and abs(adj_pitch) < PITCH_T:
                    computed_status = 'active'
                else:
                    # 允许低头看书/键盘的情况
                    if (has_keyboard or has_book) and 5 < adj_pitch < PITCH_T + 20 and abs(adj_yaw) < YAW_T + 10:
                        computed_status = 'active'
                    else:
                        computed_status = 'distracted'

                if state.phone_alert_active:
                    computed_status = 'phone'

                # ════════════════════════════════════════════════════════════════════
                #  【重构】指标计算与更新（第 1-4 层）
                # ════════════════════════════════════════════════════════════════════
                # ════════════════════════════════════════════════════════════════════
                #  【重构】证据融合与状态更新（Layer 1-3）
                # ════════════════════════════════════════════════════════════════════
                # A. 基础信号汇总 (Base Signals)
                state.signals.face_detected = face_detected_smoothed
                state.signals.person_detected = person_detected_smoothed
                state.signals.pose_valid = pose_detected_smoothed
                if face_detected_smoothed:
                    state.signals.yaw, state.signals.pitch = yaw_val, pitch_val
                    state.signals.ear, state.signals.mar = ear_val, mar_val
                    state.signals.face_h = face_h_val
                    
                state.signals.has_phone = has_phone
                state.signals.has_cup = any(d['class'] in ['cup', 'bottle'] for d in last_yolo_detections)
                state.signals.has_keyboard, state.signals.has_book = has_keyboard, has_book
                state.signals.is_arm_up = is_arm_up
                    
                # 补充缺失的基础信号
                state.signals.neck_length = neck_length_val
                state.signals.shoulder_diff = shoulder_diff_val
                    
                # B. 业务层证据收集 (Evidence Fusion)
                # 1. 疲劳判定
                is_eye_closed = (ear_val < (state.baseline.ear * 0.68)) if face_detected_smoothed else False
                state.metrics['fatigue'].update(
                    is_eye_closed=is_eye_closed,
                    is_yawning=(mar_val > 0.6),
                    current_time=now,
                    is_long_blink=(ear_val < 0.15 and face_detected_smoothed)
                )
                    
                # 2. 饮水判定证据 (高灵敏度)
                cup_near_mouth = False
                if state.signals.has_cup and nose_px:
                    for det in last_yolo_detections:
                        if det['class'] in ['cup', 'bottle']:
                            bx, by, bw, bh = det['bbox']
                            dist = np.hypot(bx + bw*0.5 - nose_px[0], by + bh*0.5 - nose_px[1])
                            if dist < face_h_val * h * 1.8:
                                cup_near_mouth = True; break
                    
                is_drinking_evidence = state.signals.has_cup and (cup_near_mouth or (mar_val > 0.35 and is_arm_up) or (is_arm_up and adj_pitch < -3))
                state.metrics['hydration'].update(now, is_drinking_evidence)
                    
                # 3. 手机判定证据（v5 严格三优先级）
                #    P1 hand_on_phone：手腕落入手机 bbox（30% 容差）
                #    P2 arm_holding_phone：三路任一满足
                #       a) 显式手臂上抬 + 手机位于画面上 75%
                #       b) 手机面积 ≥2% 且位于画面中央上半（拿近镜头看的典型）
                #       c) 手机底部接触画面下边缘（从外部拿入的典型特征）
                #    P3 gaze_in_phone：视线落入手机 bbox
                #    不再使用「手机距脸近」，以免桌上手机误报
                gaze_in_phone = False
                hand_on_phone = False
                arm_holding_phone = False
                if state.signals.has_phone:
                    for det in last_yolo_detections:
                        if det['class'] in PHONE_CLASSES:
                            bx, by, bw_p, bh_p = det['bbox']
                            phone_cx = bx + bw_p * 0.5
                            phone_cy = by + bh_p * 0.5
                            phone_area_ratio = (bw_p * bh_p) / max(w * h, 1)

                            # P1：手腕在手机 bbox 内（30% 容差）
                            pad_x, pad_y = bw_p * 0.30, bh_p * 0.30
                            for wrist in (state.signals.left_wrist, state.signals.right_wrist):
                                if wrist is None:
                                    continue
                                try:
                                    wx = wrist['x'] * w if isinstance(wrist, dict) else wrist[0] * w
                                    wy = wrist['y'] * h if isinstance(wrist, dict) else wrist[1] * h
                                except (KeyError, IndexError, TypeError):
                                    continue
                                if (bx - pad_x) <= wx <= (bx + bw_p + pad_x) and \
                                   (by - pad_y) <= wy <= (by + bh_p + pad_y):
                                    hand_on_phone = True
                                    break

                            # P2：三路“举起手机”判定
                            #   a) 显式手臂上抬 + 手机在画面上 75%
                            if is_arm_up and phone_cy < (h * 0.75):
                                arm_holding_phone = True
                            #   b) 手机面积 >= 2% 且位于画面中央上半区（拿近看）
                            elif phone_area_ratio >= 0.02 and phone_cy < (h * 0.65) and \
                                 (w * 0.10) < phone_cx < (w * 0.90):
                                arm_holding_phone = True
                            #   c) 手机底部贴近画面下边缘 + 上边未贴顶（从下拿入画面的特征）
                            elif (by + bh_p) >= (h * 0.92) and phone_cy < (h * 0.85) and phone_area_ratio >= 0.005:
                                arm_holding_phone = True

                            # P3：视线锁定手机
                            if gaze_x >= 0 and (bx-40) <= gaze_x <= (bx+bw_p+40) and \
                               (by-40) <= gaze_y <= (by+bh_p+40):
                                gaze_in_phone = True

                            # P4：手机正在被移动（连续帧位移显著）
                            #     窗口长度 6 帧（约 0.5-1s 视帧率），累计位移 > 帧宽 8%
                            #     可识别"从画面外拿入"、"边走边玩"、"在手里调整角度"等动态场景
                            if not hasattr(state, '_phone_pos_hist'):
                                state._phone_pos_hist = deque(maxlen=6)
                            state._phone_pos_hist.append((phone_cx / max(w, 1), phone_cy / max(h, 1), now))
                            if len(state._phone_pos_hist) >= 4:
                                # 计算窗口内累计位移（归一化坐标）
                                pts = list(state._phone_pos_hist)
                                cumdist = 0.0
                                for i in range(1, len(pts)):
                                    dx = pts[i][0] - pts[i-1][0]
                                    dy = pts[i][1] - pts[i-1][1]
                                    cumdist += (dx * dx + dy * dy) ** 0.5
                                # 窗口时间跨度（秒），过滤掉因长间隔造成的虚假位移
                                tspan = pts[-1][2] - pts[0][2]
                                if 0.15 < tspan < 2.0 and cumdist > 0.08:
                                    arm_holding_phone = True
                            break
                # 没看到手机：清空位置历史，避免下次出现时残留旧位置
                if not state.signals.has_phone and hasattr(state, '_phone_pos_hist'):
                    state._phone_pos_hist.clear()

                state.metrics['phone'].update(
                    now, state.signals.has_phone,
                    hand_on_phone=hand_on_phone,
                    arm_holding_phone=arm_holding_phone,
                    gaze_lock=gaze_in_phone,
                    work_context=(has_keyboard or has_book),
                    wrists_visible=(state.signals.left_wrist is not None
                                    or state.signals.right_wrist is not None),
                )
                    
                # 4. 姿态判定 — 仅在有有效姿态数据时更新
                if face_detected_smoothed or pose_detected_smoothed:
                    # 传入原始颈长，让 PostureMetrics 内部自动校准
                    state.metrics['posture'].update(
                        slouch_angle, 1.0, shoulder_diff_val,
                        raw_neck_length=neck_length_val
                    )
                    
                # 5. 注意力与离开判定 (极简版：直接使用原始信号 face_detected)
                is_using_phone = state.metrics['phone'].state == 'using'
                state.metrics['away'].update(
                    current_time=now, 
                    face_detected=face_detected, 
                    person_detected=yolo_person_detected,
                    pose_valid=len(pose_map) > 0,
                    is_using_phone=is_using_phone,
                    static_count=state.static_frame_count
                )
                # 根据四角校准结果动态计算有效阈值（有校准用相对值，无校准用固定值）
                with state.lock:
                    _fhy = state.focus_half_yaw
                    _fhp = state.focus_half_pitch
                if _fhy > 0:  # 已做四角校准：直接使用校准结果，不加倍率
                    _eff_yaw_t = _fhy
                    _eff_pitch_t = _fhp
                else:
                    _eff_yaw_t = 25.0   # 未校准时的固定默认值
                    _eff_pitch_t = 15.0
                state.metrics['attention'].update(now, adj_yaw, adj_pitch, (has_keyboard or has_book), 
                                                 is_using_phone, 
                                                 state.metrics['away'].state == 'away',
                                                 face_detected,
                                                 yaw_threshold=_eff_yaw_t,
                                                 pitch_threshold=_eff_pitch_t)
                # 把有效阈值写回 state，供 payload 使用
                with state.lock:
                    state.eff_yaw_threshold = round(_eff_yaw_t, 1)
                    state.eff_pitch_threshold = round(_eff_pitch_t, 1)
                state.status = computed_status # 兼容旧逻辑，但 WebSocket payload 会在外层重新根据优先级计算最终 status
                state.yaw, state.pitch = round(adj_yaw, 1), round(adj_pitch, 1)
                state.ear, state.mar = round(ear_val * 100, 0), round(mar_val, 4)
                state.neck_length, state.shoulder_diff = round(neck_length_val, 4), round(shoulder_diff_val, 4)
                state.neck_pct = round(state.metrics['posture'].cur_neck_pct, 1)
                state.shoulder_tilt = round(state.metrics['posture'].cur_balance_deg, 1)
                state.slouch_angle, state.is_slouching = round(slouch_angle, 1), (state.metrics['posture'].slouch_state == 'severe')
                state.last_drink_time = state.metrics['hydration'].last_drink_time
                state.hydration_minutes = state.metrics['hydration'].minutes_since

                logging_tick += 1

                if logging_tick % 600 == 0:
                    _f = state.metrics['fatigue']
                    _p = state.metrics['posture']
                    logging.info(
                        f"[HEALTH] tick={logging_tick} | "
                        f"face={'Y' if face_detected_smoothed else 'N'} "
                        f"person={'Y' if person_detected_smoothed else 'N'} "
                        f"pose={'Y' if pose_detected_smoothed else 'N'} | "
                        f"status={computed_status} | "
                        f"fatigue={_f.score:.0f} "
                        f"yaw={adj_yaw:.0f} pitch={adj_pitch:.0f} | "
                        f"slouch={_p.slouch_state} "
                        f"neck_r={_p.neck_buf.get_value():.2f} "
                        f"bal={_p.balance_buf.get_value():.1f}"
                    )

                # —— 状态同步更新 ——
                with state.lock:
                    state.status = computed_status
                    state.yaw = round(adj_yaw, 1) if face_detected else 0.0
                    state.pitch = round(adj_pitch, 1) if face_detected else 0.0
                    state.ear = round(ear_val * 100, 0)
                    state.mar = round(mar_val, 4)
                    state.blink_rate = blink_rate if face_detected else 0
                    state.nose_y = round(nose_y_val, 3)
                    state.face_h = round(face_h_val, 3)
                    state.face_detected = face_detected
                    state.person_detected = yolo_person_detected
                    state.hands_visible = hands_visible
                    state.detected_objects = last_yolo_detections
                    state.has_phone = has_phone
                    state.has_keyboard = has_keyboard
                    state.has_book = has_book
                    state.is_arm_up = is_arm_up
                    state.left_shoulder_y = round(ls_y, 4)
                    state.right_shoulder_y = round(rs_y, 4)
                    state.neck_length = round(neck_length_val, 4)
                    state.shoulder_diff = round(shoulder_diff_val, 4)
                    state.posture_ratio = round(posture_ratio, 4)
                    state.slouch_angle = round(slouch_angle, 1)
                    state.is_slouching = is_slouching
                    state.face_keypoints_3d = face_keypoints_3d
                    state.is_looking_down = is_looking_down
                    
                    # 【核心修复】将数据同步到 WebSocket 输出引用的 signals 对象中
                    state.signals.face_detected = face_detected
                    state.signals.person_detected = yolo_person_detected
                    state.signals.pose_valid = len(pose_map) > 0
                    state.signals.yaw = adj_yaw
                    state.signals.pitch = adj_pitch
                    state.signals.ear = ear_val
                    state.signals.mar = mar_val
                    state.signals.has_phone = has_phone
                    # 更新饮水分钟数
                    if state.last_drink_time > 0:
                        state.hydration_minutes = round((now - state.last_drink_time) / 60.0, 1)
                    else:
                        state.hydration_minutes = -1
                    # 更新连续用眼追踪
                    state.eye_usage_tracker.update(face_detected)
                    if state.eye_usage_tracker.pop_short_rest_trigger():
                        state.eye_usage_short_rest = True
                

                # ════════════════════════════════════
                #  4. 状态 HUD 叠加（根据用户要求恢复部分标签）
                # ════════════════════════════════════
                # 姿态数值恢复

                


                # FPS 计算与恢复
                fps_counter += 1
                if now - fps_timer > 1.0:
                    fps = fps_counter / (now - fps_timer)
                    fps_timer = now
                    display_frame = draw_text_chinese(display_frame, f"FPS: {fps:.0f}", (w - 100, 10), 16, (200, 200, 200))
            except Exception as e:
                logging.error(f"[VISION] 视觉循环异常捕获: {e}")
                logging.error(traceback.format_exc())
                # 即使检测失败，由于顶部已经初始化了所有变量，后续代码仍能正常运行或显示默认状态
                display_frame = frame.copy()
                display_frame = draw_text_chinese(display_frame, f"检测异常: {str(e)[:40]}", (10, 5), 20, (0, 0, 255))

            # ════════════════════════════════════
            #  5. 编码为 JPEG 用于 MJPEG 推流
            # ════════════════════════════════════
            _, jpeg = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            with state.lock:
                state.jpeg_frame = jpeg.tobytes()

            # 控制帧率约 30fps
            time.sleep(max(0, 1.0 / 30.0 - (time.time() - now)))
    except Exception as e:
        logging.error(f"[VISION] 视觉控制循环发生顶级崩溃: {e}")
        logging.error(traceback.format_exc())
    finally:
        if 'cap' in locals() and cap is not None:
            cap.release()
            logging.info("[VISION] 摄像头资源已安全释放")

def restart_process():
    """优雅地重启当前 Python 进程：先通知释放资源，再替换进程"""
    logging.info("\n" + "="*50)
    logging.info("  [SYS] 收到同步重启指令，启动优雅重启流程...")
    
    # 1. 设置标志位，让 vision_loop 主动退出并释放摄像头
    with state.lock:
        state.is_restarting = True
    
    # 2. 给视觉线程留出释放硬件句柄的时间 (500ms 足够 cap.release())
    time.sleep(0.6)
    
    logging.info("  [SYS] 资源已释放，正在启动新进程并自杀...")
    logging.info("  [SYS] 命令: " + " ".join([sys.executable] + sys.argv))
    logging.info("="*50 + "\n")
    
    try:
        # 在 Windows 上，这种方式配合 os._exit(0) 比 os.execl 更能确保旧进程资源彻底回收
        # 增加 --restarted 参数，防止新进程再次自动打开浏览器标签
        new_args = [sys.executable] + sys.argv + ["--restarted"]
        subprocess.Popen(new_args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        os._exit(0) 
    except Exception as e:
        logging.error(f"[SYS] 优雅重启失败: {e}")
        os._exit(1)

# ══════════════════════════════════════════
#  WebSocket 服务器
# ══════════════════════════════════════════

# ── Windows 系统通知（无第三方依赖，PowerShell + WinRT Toast）────
_LAST_TOAST_AT = 0.0
_LAST_TOAST_KEY = ""
_AUMID_REGISTERED = False
TOAST_AUMID = "DoNotPlay"

def _register_aumid_once():
    """在 HKCU 注册自定义 AUMID，让 Toast 标题显示 DoNotPlay 而不是 PowerShell/资源管理器。"""
    global _AUMID_REGISTERED
    if _AUMID_REGISTERED or sys.platform != 'win32':
        return
    try:
        import winreg
        key_path = r"Software\Classes\AppUserModelId\\" + TOAST_AUMID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "DoNotPlay")
            winreg.SetValueEx(k, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        _AUMID_REGISTERED = True
        logging.info(f"[TOAST] 已注册 AUMID: {TOAST_AUMID}")
    except Exception as e:
        logging.warning(f"[TOAST] AUMID 注册失败: {e}")

def show_windows_toast(title: str, body: str):
    """在 Windows 右下角弹出原生 Toast 通知。无 PowerShell 模块依赖。"""
    global _LAST_TOAST_AT, _LAST_TOAST_KEY
    try:
        if sys.platform != 'win32':
            return
        now = time.time()
        key = f"{title}|{body}"
        # 节流：同文案 8 秒内不重复，任意通知 1 秒最小间隔
        if (now - _LAST_TOAST_AT) < 1.0:
            logging.info(f"[TOAST] 跳过(1s 节流): {title}")
            return
        if key == _LAST_TOAST_KEY and (now - _LAST_TOAST_AT) < 8.0:
            logging.info(f"[TOAST] 跳过(同文案去重): {title}")
            return
        _LAST_TOAST_AT = now
        _LAST_TOAST_KEY = key
        _register_aumid_once()

        # 转义 XML 特殊字符
        def _esc(s):
            return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
        t_xml = _esc(title or 'DoNotPlay')
        b_xml = _esc(body or '')
        ps_script = (
            "$ErrorActionPreference='Continue'\n"
            "try {\n"
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null\n"
            f"$xml = '<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{t_xml}</text><text id=\"2\">{b_xml}</text></binding></visual></toast>'\n"
            "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            "$doc.LoadXml($xml)\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)\n"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_AUMID}').Show($toast)\n"
            "Write-Output 'OK'\n"
            "} catch { Write-Error $_.Exception.Message; exit 2 }\n"
        )
        # 写到临时 .ps1 文件，避免 EncodedCommand 解析陷阱
        import tempfile
        tmpf = tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8-sig')
        tmpf.write(ps_script)
        tmpf.close()
        ps_path = tmpf.name
        proc = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ps_path],
            creationflags=0x08000000,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        logging.info(f"[TOAST] PS 启动 (PID={proc.pid}) script={ps_path}: {title}")
        def _drain(p, path):
            try:
                o, e = p.communicate(timeout=10)
                logging.info(f"[TOAST] PS RC={p.returncode} STDOUT={o[:200]!r} STDERR={e[:500]!r}")
            except Exception as ex:
                logging.warning(f"[TOAST] PS drain 异常: {ex}")
            finally:
                try: os.unlink(path)
                except: pass
        threading.Thread(target=_drain, args=(proc, ps_path), daemon=True).start()
    except Exception as e:
        logging.warning(f"[TOAST] 系统通知失败: {e}")


async def ws_handler(websocket, path=""):
    """双向通信处理器：不仅推理状态，还接收前端控制指令"""
    try:
        logging.info(f"[WS-CONN] ⚡ 收到新的 WebSocket 连接请求: {websocket.remote_address}")
    
        # 关键修复：新连接建立时强制清除残留 paused 状态，防止页面刷新后摄像头卡在"休息模式"
        with state.lock:
            if state.is_monitoring_paused:
                logging.info("[WS-INIT] 检测到残留 paused 状态，已自动清除以恢复摄像头")
                state.is_monitoring_paused = False
                if state.status == 'paused':
                    state.status = 'active'

        # 建立连接时先同步当前配置给前端
        config_payload = {
            'type': 'config_sync',
            'sens_idx': SENS_IDX
        }
        await websocket.send(json.dumps(config_payload))
        logging.info(f"[WS-INIT] 握手完成，已同步初始配置: SENS={SENS_IDX}")

        async def receive_msgs():
            global SENS_IDX, YAW_T, PITCH_T, STATE_COOLDOWN, current_config
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get('cmd') == 'set_sensitivity':
                        idx = data.get('idx', 1)
                        if not (0 <= idx < len(SENS_TABLE)):
                            idx = 1
                        SENS_IDX = idx
                        # 应用新阈值
                        YAW_T = SENS_TABLE[idx]['yaw']
                        PITCH_T = SENS_TABLE[idx]['pitch']
                        STATE_COOLDOWN = SENS_TABLE[idx]['delay'] / 1000.0
                        # 保存到文件
                        save_config({'sens_idx': SENS_IDX, 'baseline_yaw': current_config.get('baseline_yaw', 0.0), 'baseline_pitch': current_config.get('baseline_pitch', 0.0)})
                        logging.info(f"[WS] 灵敏度切换为: {SENS_TABLE[idx]}")
                    elif data.get('action') == 'calibrate' or data.get('cmd') == 'calibrate':
                        with state.lock:
                            state.is_calibrating = True
                            state.calibration_samples = []
                        logging.info("[WS] 收到校准指令，开始采集样本...")
                    elif data.get('cmd') == 'start_corner_step':
                        step = int(data.get('step', 0))
                        with state.lock:
                            state.calib_corner_step = step
                            state.calib_corner_samples = []
                            state.calib_corner_auto_ready = None
                            state.calib_corner_warmup = 20  # 跳过前20帧（~0.67s），等待用户转移视线
                        logging.info(f"[CALIB-CORNER] 开始采集角落 {step} 样本")
                    elif data.get('cmd') == 'end_corner_step':
                        with state.lock:
                            step = state.calib_corner_step
                            samples = list(state.calib_corner_samples)
                            state.calib_corner_step = -1
                            state.calib_corner_samples = []
                            state.calib_corner_auto_ready = None
                            state.calib_corner_warmup = 0
                        if samples:
                            avg_yaw   = sum(s['yaw']   for s in samples) / len(samples)
                            avg_pitch = sum(s['pitch'] for s in samples) / len(samples)
                            avg_ear   = sum(s['ear']   for s in samples) / len(samples)
                            with state.lock:
                                state.pending_corner_result = {
                                    'step': step,
                                    'yaw': round(avg_yaw, 4),
                                    'pitch': round(avg_pitch, 4),
                                }
                            # 仅在旧校准未更新时，用本步的 EAR 更新 baseline_ear
                            logging.info(f"[CALIB-CORNER] 角落 {step} 完成: yaw={avg_yaw:.2f}, pitch={avg_pitch:.2f} ({len(samples)}帧)")
                        else:
                            logging.warning(f"[CALIB-CORNER] 角落 {step} 无采样数据")
                    elif data.get('cmd') == 'finalize_corner_calib':
                        corners = data.get('corners', [])  # [{step, yaw, pitch}, ...]
                        ear_baseline = data.get('ear_baseline', None)
                        neck_baseline = data.get('neck_baseline', None)
                        if len(corners) >= 4:
                            yaws   = [c['yaw']   for c in corners]
                            pitches = [c['pitch'] for c in corners]
                            new_baseline_yaw   = (min(yaws) + max(yaws)) / 2.0
                            new_baseline_pitch = (min(pitches) + max(pitches)) / 2.0
                            new_focus_half_yaw   = (max(yaws) - min(yaws)) / 2.0
                            new_focus_half_pitch = (max(pitches) - min(pitches)) / 2.0
                            with state.lock:
                                state.baseline_yaw = new_baseline_yaw
                                state.baseline_pitch = new_baseline_pitch
                                state.baseline.yaw = new_baseline_yaw
                                state.baseline.pitch = new_baseline_pitch
                                state.focus_half_yaw = new_focus_half_yaw
                                state.focus_half_pitch = new_focus_half_pitch
                                if ear_baseline is not None:
                                    state.baseline_ear = float(ear_baseline)
                                    state.baseline.ear = float(ear_baseline)
                            save_config({
                                'sens_idx': SENS_IDX,
                                'baseline_yaw':   round(new_baseline_yaw, 4),
                                'baseline_pitch':  round(new_baseline_pitch, 4),
                                'baseline_ear':    round(state.baseline_ear, 4),
                                'baseline_neck_length': round(state.baseline_neck_length, 4),
                                'baseline_shoulder_tilt': round(state.baseline_shoulder_tilt, 4),
                                'focus_half_yaw':  round(new_focus_half_yaw, 4),
                                'focus_half_pitch': round(new_focus_half_pitch, 4),
                            })
                            logging.info(f"[CALIB-CORNER] 四角校准完成: 中心=({new_baseline_yaw:.1f}, {new_baseline_pitch:.1f}), 半范围=({new_focus_half_yaw:.1f}, {new_focus_half_pitch:.1f})")
                        else:
                            logging.warning(f"[CALIB-CORNER] 四角校准数据不足，仅有 {len(corners)} 个角落")
                    elif data.get('cmd') == 'pause_monitoring':
                        with state.lock:
                            state.is_monitoring_paused = data.get('paused', False)
                            if state.is_monitoring_paused:
                                state.status = 'paused'
                        logging.info(f"[WS] 监控状态切换: {'暂停' if state.is_monitoring_paused else '恢复'}")
                    elif data.get('cmd') == 'notify_os':
                        # 前端转发的系统通知请求（pywebview 环境下的兜底）
                        title = str(data.get('title', 'DoNotPlay'))[:80]
                        body = str(data.get('body', ''))[:200]
                        logging.info(f"[TOAST] 收到通知请求: {title} | {body}")
                        show_windows_toast(title, body)
                    elif data.get('cmd') == 'set_theme':
                        with state.lock:
                            state.current_theme = data.get('theme', 'dark')
                        logging.info(f"[WS] 主题同步: {state.current_theme}")
                    elif data.get('cmd') == 'reset_fatigue':
                        state.reset_fatigue()
                        logging.info("[WS] 收到疲劳重置指令，历史数据已清空")
                    elif data.get('cmd') == 'soft_refresh':
                        state.full_reset()
                        # 发送回执告诉前端可以刷新页面了
                        await websocket.send(json.dumps({'type': 'soft_refresh_done'}))
                        logging.info("[WS] 收到软刷新指令，状态已重置")
                    elif data.get('cmd') == 'refresh':
                        # 给前端一点时间发送完消息并显示 UI 提示
                        time.sleep(0.5)
                        restart_process()
                    elif data.get('action') == 'update_baseline':
                        with state.lock:
                            state.baseline_yaw = float(data.get('yaw', 0))
                            state.baseline_pitch = float(data.get('pitch', 0))
                            # 同步到 CalibrationBaseline 对象
                            state.baseline.yaw = state.baseline_yaw
                            state.baseline.pitch = state.baseline_pitch
                            # 同步保存到配置中
                            save_config({
                                'sens_idx': SENS_IDX, 
                                'baseline_yaw': round(state.baseline_yaw, 4),
                                'baseline_pitch': round(state.baseline_pitch, 4),
                                'baseline_ear': round(state.baseline_ear, 4),
                                'baseline_neck_length': round(state.baseline_neck_length, 4),
                            })
                        logging.info(f"[WS] 收到前端同步校准: Yaw={state.baseline_yaw}, Pitch={state.baseline_pitch}")
                    elif data.get('cmd') == 'set_camera_rotation':
                        rot = int(data.get('rotation', 0))
                        flip = bool(data.get('flip', True))
                        # 核心修改：不仅更新 config 字典，还要确保保存到磁盘，并让 vision_loop 察觉
                        current_config.update({'camera_rotation': rot, 'camera_flip': flip})
                        save_config(current_config)
                        logging.info(f"[WS] 摄像头方向调整并已持久化: Rotation={rot}, Flip={flip}")
            except Exception:
                logging.error("[WS] 接收消息协程发生错误:")
                logging.error(traceback.format_exc())

        async def send_updates():
            while True:
                try:
                    with state.lock:
                        payload = build_comprehensive_payload(
                            signals=state.signals,
                            baseline=state.baseline,
                            metrics=state.metrics,
                            current_time=time.time(),
                            blink_rate=state.blink_rate
                        )
                        
                        if state.metrics['phone'].state == 'using':
                            payload['status'] = 'phone'
                        elif state.metrics['away'].state == 'away':
                            payload['status'] = 'away'
                        else:
                            win_state = _win_activity.to_dict()['state']
                            if win_state == 'distracted':
                                payload['status'] = 'distracted'
                            else:
                                payload['status'] = 'active'
                        
                        # 有效阈值（前端用于可视化偏转比例）
                        payload['gaze_angle']['yaw_threshold'] = state.eff_yaw_threshold
                        payload['gaze_angle']['pitch_threshold'] = state.eff_pitch_threshold
                        
                        # 四角校准结果（一次性发送后清除）
                        if state.pending_corner_result is not None:
                            payload['corner_calib_result'] = state.pending_corner_result
                            state.pending_corner_result = None
                        # 自动稳定就绪通知（一次性，发送后清除）
                        if state.calib_corner_auto_ready is not None:
                            payload['corner_calib_ready'] = state.calib_corner_auto_ready
                            state.calib_corner_auto_ready = None
                    
                    # 注入当前窗口活动状态（不需持锁）
                    payload['window_activity'] = _win_activity.to_dict()
                    # 连续用眼数据
                    payload['eye_usage'] = state.eye_usage_tracker.to_dict()
                    if state.eye_usage_short_rest:
                        payload['eye_usage']['short_rest_trigger'] = True
                        state.eye_usage_short_rest = False

                    await websocket.send(json.dumps(payload))
                    if time.time() % 5 < 0.1: 
                        logging.debug(f"[WS-TRACE] 推送数据: {payload['status']}")
                except Exception as e:
                    logging.error(f"[WS-SEND-ERR] 推送单帧数据异常 (跳过本帧): {e}")
                
                await asyncio.sleep(0.05)

        # 并发运行接收和发送任务
        await asyncio.gather(receive_msgs(), send_updates())
    except Exception as e:
        logging.error(f"[WS] 处理器崩溃: {e}")
        logging.error(traceback.format_exc())

async def ws_server():
    """启动 WebSocket 服务器"""
    try:
        logging.info(f"[WS] WebSocket 服务器启动在 ws://localhost:{WS_PORT}")
        async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
            await asyncio.Future()  # 永久运行
    except Exception:
        logging.error("[WS] WebSocket 服务器主协程发生错误:")
        logging.error(traceback.format_exc())

def run_ws_server():
    """在独立线程中运行 asyncio 事件循环"""
    try:
        asyncio.run(ws_server())
    except Exception:
        logging.error("[WS] WebSocket 服务器线程崩溃:")
        logging.error(traceback.format_exc())

# ══════════════════════════════════════════
#  窗口活动监控模块
# ══════════════════════════════════════════

_WIN_EVENT_OUTOFCONTEXT   = 0x0000
_EVENT_SYSTEM_FOREGROUND  = 0x0003
_EVENT_OBJECT_NAMECHANGE  = 0x800C
_OBJID_WINDOW             = 0
_WM_QUIT                  = 0x0012
_BROWSER_PROCS = {'chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'vivaldi.exe', 'opera.exe'}

# ──────────────────────────────────────────────────────────────────
#  窗口归一化：应用别名 + 网站域名合并 + 系统噪音过滤
#  目标：反映用户实际使用场景，而非窗口标题的花哨变体
# ──────────────────────────────────────────────────────────────────

# 完全忽略的系统外壳/辅助进程（不计入任何统计）
_IGNORE_PROCS = {
    'explorer.exe',           # 贴靠助手/任务切换
    'searchhost.exe',         # Windows 搜索
    'searchapp.exe',
    'startmenuexperiencehost.exe',
    'shellexperiencehost.exe',
    'textinputhost.exe',
    'lockapp.exe',
    'systemsettings.exe',     # 设置面板
    'applicationframehost.exe',
    'dwm.exe', 'sihost.exe', 'fontdrvhost.exe',
    'ctfmon.exe', 'runtimebroker.exe',
    # 任务栏/通知中心类
    'taskmgr.exe',            # 任务管理器默认不计
}

# 标题关键词黑名单（命中则忽略；不区分大小写）
_IGNORE_TITLE_PATTERNS = (
    '贴靠助手', '任务切换', '任务视图', 'program manager',
    'windows input experience', '开始', '搜索',
    '桌面窗口管理器', 'cortana', '通知中心',
    '截图和草图', '截图工具',
)

# 进程名 → 友好显示名（小写匹配）。未命中的进程会用文件名自动生成。
_APP_NAME_MAP = {
    'code.exe': 'VS Code',
    'cursor.exe': 'Cursor',
    'devenv.exe': 'Visual Studio',
    'pycharm64.exe': 'PyCharm', 'pycharm.exe': 'PyCharm',
    'idea64.exe': 'IntelliJ IDEA', 'idea.exe': 'IntelliJ IDEA',
    'webstorm64.exe': 'WebStorm',
    'goland64.exe': 'GoLand',
    'clion64.exe': 'CLion',
    'android studio.exe': 'Android Studio', 'studio64.exe': 'Android Studio',
    'sublime_text.exe': 'Sublime Text',
    'notepad.exe': '记事本',
    'notepad++.exe': 'Notepad++',
    'winword.exe': 'Word',
    'excel.exe': 'Excel',
    'powerpnt.exe': 'PowerPoint',
    'onenote.exe': 'OneNote',
    'outlook.exe': 'Outlook',
    'wpsoffice.exe': 'WPS', 'wps.exe': 'WPS', 'et.exe': 'WPS', 'wpp.exe': 'WPS',
    'acrobat.exe': 'Adobe Acrobat', 'acrord32.exe': 'Adobe Reader',
    'sumatrapdf.exe': 'SumatraPDF',
    'obsidian.exe': 'Obsidian',
    'typora.exe': 'Typora',
    'notion.exe': 'Notion',
    'chrome.exe': 'Chrome',
    'msedge.exe': 'Edge',
    'firefox.exe': 'Firefox',
    'brave.exe': 'Brave',
    'vivaldi.exe': 'Vivaldi',
    'opera.exe': 'Opera',
    'wechat.exe': '微信', 'wechatappex.exe': '微信',
    'qq.exe': 'QQ', 'qqmusic.exe': 'QQ 音乐',
    'dingtalk.exe': '钉钉',
    'lark.exe': '飞书', 'feishu.exe': '飞书',
    'telegram.exe': 'Telegram',
    'discord.exe': 'Discord',
    'slack.exe': 'Slack',
    'zoom.exe': 'Zoom',
    'teams.exe': 'Teams', 'msteams.exe': 'Teams',
    'spotify.exe': 'Spotify',
    'neteasemusic.exe': '网易云音乐', 'cloudmusic.exe': '网易云音乐',
    'steam.exe': 'Steam',
    'photoshop.exe': 'Photoshop',
    'illustrator.exe': 'Illustrator',
    'figma.exe': 'Figma',
    'unity.exe': 'Unity',
    'unrealeditor.exe': 'Unreal Editor',
    'blender.exe': 'Blender',
    'obs64.exe': 'OBS', 'obs.exe': 'OBS',
    'mpc-hc64.exe': 'MPC-HC', 'potplayermini64.exe': 'PotPlayer', 'potplayer.exe': 'PotPlayer',
    'vlc.exe': 'VLC',
    'cmd.exe': '命令提示符',
    'powershell.exe': 'PowerShell', 'pwsh.exe': 'PowerShell',
    'windowsterminal.exe': 'Windows 终端',
    'wt.exe': 'Windows 终端',
    'conemu64.exe': 'ConEmu',
    'xshell.exe': 'Xshell',
    'everything.exe': 'Everything',
    'utools.exe': 'uTools',
    'qqbrowser.exe': 'QQ 浏览器',
    '360se.exe': '360 浏览器', '360chrome.exe': '360 极速浏览器',
    'sogouexplorer.exe': '搜狗浏览器',
    # ── 中文友好名：Windows 系统进程 ──
    'systemsettings.exe': 'Windows 设置',
    'applicationframehost.exe': 'UWP 应用容器',
    'textinputhost.exe': '输入面板',
    'searchapp.exe': 'Windows 搜索', 'searchhost.exe': 'Windows 搜索', 'searchui.exe': 'Windows 搜索',
    'startmenuexperiencehost.exe': '开始菜单',
    'lockapp.exe': '锁屏',
    'shellexperiencehost.exe': 'Shell 体验',
    'explorer.exe': '资源管理器',
    'taskmgr.exe': '任务管理器',
    'control.exe': '控制面板', 'controlcenter.exe': '控制面板',
    'msinfo32.exe': '系统信息',
    'mmc.exe': '管理控制台',
    'regedit.exe': '注册表编辑器',
    'sihost.exe': 'Shell 基础架构',
    'fontview.exe': '字体查看器',
    'fontdrvhost.exe': '字体驱动',
    'dwm.exe': '桌面窗口管理器',
    'csrss.exe': '客户端/服务器子系统',
    'winlogon.exe': '登录管理器',
    'tabtip.exe': '触摸键盘',
    'inputexperience.exe': '输入体验',
    'magnify.exe': '放大镜',
    'osk.exe': '屏幕键盘',
    'narrator.exe': '讲述人',
    'stikynot.exe': '便笺',
    'wordpad.exe': '写字板',
    'mspaint.exe': '画图',
    'calc.exe': '计算器', 'calculator.exe': '计算器',
    'snippingtool.exe': '截图工具',
    'gamebar.exe': 'Xbox Game Bar',
    'taskhost.exe': '任务主机', 'taskhostw.exe': '任务主机',
    'svchost.exe': '服务主机',
    'rundll32.exe': 'DLL 宿主',
    'dllhost.exe': 'COM 代理',
    'runtimebroker.exe': '运行时代理',
    'consent.exe': 'UAC 提示',
    'userinit.exe': '用户初始化',
    'wsappx.exe': '应用商店服务',
    'crashpad_handler.exe': '崩溃报告',
    'updater.exe': '更新程序',
    'securityhealthsystray.exe': 'Windows 安全中心',
    'securityhealthservice.exe': 'Windows 安全服务',
    'mpdefendercoreservice.exe': 'Defender 核心',
    'msedgewebview2.exe': 'Edge WebView',
    # ── 中文友好名：常见三方工具 ──
    'shellhost.exe': 'Shell 宿主',
    'sogoucloud.exe': '搜狗输入法', 'sogouinput.exe': '搜狗输入法', 'sgtool.exe': '搜狗工具',
    'qqpinyin.exe': 'QQ 拼音',
    'wubi.exe': '五笔输入法',
    'snipaste.exe': 'Snipaste 截图',
    'screentogif.exe': 'ScreenToGif',
    'clash-verge.exe': 'Clash Verge', 'clash for windows.exe': 'Clash',
    'nvidia overlay.exe': 'NVIDIA 覆盖层', 'nvidia share.exe': 'NVIDIA 分享',
    'nvcontainer.exe': 'NVIDIA 容器',
    'wechatmgr.exe': '微信管理', 'weixin.exe': '微信',
    'wmplayer.exe': 'Windows Media Player',
}

# ───────────────────────────────────────────────────────────────
#  内置分类规则（v3：用户校准后定稿）
#  四档：focus(专注) / distracted(走神) / neutral(中性，不计) / unknown(待分类)
#  优先级：用户自定义规则 > 内置规则 > unknown 兜底
#  浏览器内未识别站点 → unknown（让用户主动拨）
# ───────────────────────────────────────────────────────────────
_BUILTIN_FOCUS_PROCS = {
    # Office
    'winword.exe', 'excel.exe', 'powerpnt.exe', 'onenote.exe', 'visio.exe', 'mspub.exe',
    'wps.exe', 'wpsoffice.exe', 'et.exe', 'wpp.exe',
    # IDE / 编辑器
    'code.exe', 'cursor.exe', 'devenv.exe',
    'pycharm64.exe', 'pycharm.exe',
    'idea64.exe', 'idea.exe',
    'webstorm64.exe', 'webstorm.exe',
    'goland64.exe', 'goland.exe',
    'clion64.exe', 'clion.exe',
    'rider64.exe', 'rider.exe',
    'phpstorm64.exe', 'phpstorm.exe',
    'datagrip64.exe', 'datagrip.exe',
    'rubymine64.exe', 'rubymine.exe',
    'studio64.exe', 'android studio.exe',
    'sublime_text.exe', 'notepad++.exe', 'atom.exe',
    # 终端
    'windowsterminal.exe', 'wt.exe',
    'cmd.exe', 'powershell.exe', 'pwsh.exe',
    'conemu64.exe', 'conemu.exe',
    'xshell.exe', 'mobaxterm.exe', 'putty.exe',
    'tabby.exe', 'alacritty.exe', 'wezterm.exe',
    # Python / 解释器
    'python.exe', 'python3.exe', 'pythonw.exe', 'py.exe',
    'ipython.exe', 'jupyter.exe', 'jupyter-notebook.exe',
    # Git / VCS
    'git.exe', 'githubdesktop.exe', 'sourcetree.exe',
    'tortoisegit.exe', 'tortoisegitproc.exe', 'tortoisegitmerge.exe',
    'gitkraken.exe', 'fork.exe', 'smartgit.exe',
    # PDF / 文档阅读
    'sumatrapdf.exe', 'acrord32.exe', 'acrobat.exe', 'foxitreader.exe',
    # 笔记/写作
    'obsidian.exe', 'typora.exe', 'notion.exe',
}

_BUILTIN_DISTRACTED_PROCS = {
    # 视频播放器
    'potplayermini64.exe', 'potplayer.exe', 'mpc-hc64.exe', 'mpc-hc.exe',
    'mpv.exe', 'mpvnet.exe', 'vlc.exe', 'kmplayer.exe', 'gomplayer.exe',
    # 游戏平台 / 启动器
    'steam.exe', 'steamwebhelper.exe',
    'epicgameslauncher.exe', 'epicwebhelper.exe',
    'wegame.exe', 'tencentdl.exe',
    'battle.net.exe', 'battlenet.exe', 'agent.exe',
    'origin.exe', 'eadesktop.exe',
    'galaxyclient.exe', 'gog galaxy.exe',
    'uplay.exe', 'upc.exe',
    'minecraftlauncher.exe',
    # IM（除微信/QQ 外）
    'telegram.exe', 'telegramdesktop.exe',
    'discord.exe', 'discordcanary.exe',
    'whatsapp.exe',
    'slack.exe',
    'dingtalk.exe',
    'lark.exe', 'feishu.exe',
}

_BUILTIN_NEUTRAL_PROCS = {
    # 微信 / QQ
    'wechat.exe', 'weixin.exe', 'wechatappex.exe',
    'qq.exe', 'tim.exe', 'qqim.exe',
    # 音乐播放器
    'spotify.exe',
    'cloudmusic.exe', 'neteasemusic.exe',
    'qqmusic.exe',
    'applemusic.exe', 'amperity.exe',
    'foobar2000.exe',
    # 系统辅助 / 难分类工具（不计入专注/走神）
    'shellhost.exe',
    'nvidia overlay.exe', 'nvidia share.exe',
    'nvidia geforce experience.exe', 'nvcontainer.exe',
    'clash-verge.exe', 'clash for windows.exe', 'clashx.exe',
    'v2rayn.exe', 'v2rayng.exe', 'qv2ray.exe',
    'snipaste.exe', 'snippingtool.exe', 'screentogif.exe',
    'calculator.exe', 'calc.exe',
    'mspaint.exe',
    'wechatmgr.exe', 'wmplayer.exe',
    # ── Windows 系统进程 / 设置 / 框架 ──
    'systemsettings.exe',           # 设置
    'applicationframehost.exe',     # UWP 容器
    'textinputhost.exe',            # 输入法/搜索
    'searchapp.exe', 'searchhost.exe', 'searchui.exe',
    'startmenuexperiencehost.exe',  # 开始菜单
    'lockapp.exe',                  # 锁屏
    'shellexperiencehost.exe',      # Shell 容器
    'explorer.exe',                 # 资源管理器（不能简单归 focus 或 distracted）
    'taskmgr.exe',                  # 任务管理器
    'controlcenter.exe', 'control.exe',  # 控制面板
    'msinfo32.exe', 'mmc.exe',
    'regedit.exe',
    'cmd.exe',                      # 单纯 cmd 不算 focus（终端 IDE 已在 focus）
    'sihost.exe',                   # Shell Infrastructure Host
    'fontview.exe', 'fontdrvhost.exe',
    'dwm.exe',                      # 桌面窗口管理
    'csrss.exe', 'winlogon.exe',
    # 输入法
    'sogoucloud.exe', 'sogouinput.exe', 'sgtool.exe',
    'qqpinyin.exe',
    'wubi.exe',
    'tabtip.exe', 'inputexperience.exe',
    # 通知/工具
    'notepad.exe', 'wordpad.exe',
    'magnify.exe',                  # 放大镜
    'osk.exe',                      # 屏幕键盘
    'narrator.exe',                 # 讲述人
    'stikynot.exe',                 # 便笺
    # 截图/录屏
    'gamebar.exe', 'gamebarpresencewriter.exe',
    'shareex.exe',
    # 其他常见后台
    'msedgewebview2.exe',           # WebView2（VSCode 等用到）
    'crashpad_handler.exe',
    'updater.exe',
    'securityhealthsystray.exe', 'securityhealthservice.exe',
    'mpdefendercoreservice.exe',
    'wsappx.exe',
    'runtimebroker.exe',
    'consent.exe',
    'userinit.exe',
    'rundll32.exe',
    'dllhost.exe',
    'svchost.exe',
    'taskhost.exe', 'taskhostw.exe',
}

# 浏览器内白名单：域名命中 → focus；其他全部 → unknown（让用户拨）
_BUILTIN_FOCUS_SITES = {
    'cnki.net', 'cnki.com.cn',
    'pkulaw.com', 'pkulaw.cn',
    'docs.google.com', 'sheets.google.com', 'slides.google.com',
    'gemini.google.com',
    'chat.openai.com', 'chatgpt.com',
    'claude.ai', 'anthropic.com',
    'github.com',
}

# 内置走神站点（视频 / 短视频 / 社交娱乐）
_BUILTIN_DISTRACTED_SITES = {
    'bilibili.com', 'b23.tv',
    'youtube.com', 'youtu.be',
    'douyin.com', 'tiktok.com',
    'kuaishou.com',
    'weibo.com', 'weibo.cn',
    'twitter.com', 'x.com',
    'reddit.com',
    'instagram.com',
    'twitch.tv',
    'iqiyi.com', 'youku.com', 'mgtv.com', 'qq.com/v',
    'huya.com', 'douyu.com',
    'xiaohongshu.com', 'xhs.com',
    'pinduoduo.com', 'taobao.com', 'tmall.com', 'jd.com',
}

# 内置中性站点（搜索 / 工具 / 通用）
_BUILTIN_NEUTRAL_SITES = {
    'baidu.com', 'google.com', 'google.com.hk', 'bing.com', 'duckduckgo.com',
    'mail.qq.com', 'mail.163.com', 'mail.google.com', 'outlook.com',
    'zhihu.com',
    'translate.google.com',
}

# 标题关键词兜底（域名解析失败时使用）
_BUILTIN_FOCUS_TITLE_KW = (
    '中国知网', '北大法宝', '法律法规数据库',
    'chatgpt', 'claude.ai', 'gemini',
    'google 文档', 'google 表格', 'google 幻灯片',
    'google docs', 'google sheets', 'google slides',
    'github',
)

# 网站域名 → 友好显示名（取标题中 URL 或末尾域名）
_SITE_MAP = {
    'bilibili.com': '哔哩哔哩', 'b23.tv': '哔哩哔哩',
    'youtube.com': 'YouTube', 'youtu.be': 'YouTube',
    'github.com': 'GitHub',
    'stackoverflow.com': 'Stack Overflow',
    'google.com': '谷歌', 'google.com.hk': '谷歌',
    'baidu.com': '百度', 'zhihu.com': '知乎',
    'weibo.com': '微博', 'weibo.cn': '微博',
    'douyin.com': '抖音', 'tiktok.com': 'TikTok',
    'twitter.com': '推特', 'x.com': '推特 (X)',
    'reddit.com': 'Reddit',
    'notion.so': 'Notion',
    'figma.com': 'Figma',
    'chatgpt.com': 'ChatGPT', 'openai.com': 'OpenAI',
    'claude.ai': 'Claude',
    'gemini.google.com': 'Gemini',
    'kimi.moonshot.cn': 'Kimi',
    'deepseek.com': 'DeepSeek',
    'doubao.com': '豆包',
    'tongyi.aliyun.com': '通义千问',
    'leetcode.com': '力扣', 'leetcode.cn': '力扣',
    'csdn.net': 'CSDN',
    'juejin.cn': '掘金',
    'jianshu.com': '简书',
    'taobao.com': '淘宝', 'tmall.com': '天猫',
    'jd.com': '京东',
    'pinduoduo.com': '拼多多',
    'xiaohongshu.com': '小红书',
    'mail.qq.com': 'QQ 邮箱', 'mail.163.com': '网易邮箱',
    'outlook.live.com': 'Outlook', 'mail.google.com': 'Gmail',
    'outlook.com': 'Outlook',
    'bing.com': '必应',
    'npmjs.com': 'npm',
    'pypi.org': 'PyPI',
    'mdn.mozilla.org': 'MDN', 'developer.mozilla.org': 'MDN',
    'netflix.com': 'Netflix',
    'iqiyi.com': '爱奇艺', 'v.qq.com': '腾讯视频', 'youku.com': '优酷', 'mgtv.com': '芒果 TV',
    'twitch.tv': 'Twitch',
    'huya.com': '虎牙', 'douyu.com': '斗鱼',
    'wsj.com': '华尔街日报',
    'nytimes.com': '纽约时报',
    'wikipedia.org': '维基百科',
    'cnki.net': '中国知网',
    'pkulaw.com': '北大法宝',
    'microsoft.com': '微软',
    'docs.google.com': 'Google 文档',
    'sheets.google.com': 'Google 表格',
    'slides.google.com': 'Google 幻灯片',
    'translate.google.com': '谷歌翻译',
}

_DOMAIN_RE = re.compile(
    r'(?:https?://)?((?:[a-zA-Z0-9][a-zA-Z0-9\-]*\.)+'
    r'(?:com|cn|org|net|io|co|edu|gov|app|dev|ai|tv|me|cc|so|info|xyz|site|top|vip|tech|icu|fun|ltd|work'
    r'|jp|uk|de|fr|ru|in|hk|tw|sg|kr|au|ca|us|nl|it|es|br|mx|pl|se|no|dk|fi|cz|pt|gr|tr|il|th|vn|id|my|ph)'
    r'(?:\.[a-zA-Z]{2})?)',
    re.IGNORECASE
)


def _extract_domain(title: str) -> str:
    """从浏览器标题中提取最后一个可识别的域名（小写，去端口）。
    若标题中无 URL，则用品牌词/中文别名兜底匹配 _SITE_ALIAS。
    找不到返回空串。"""
    if not title:
        return ''
    matches = _DOMAIN_RE.findall(title)
    if matches:
        dom = matches[-1].lower().strip()
        parts = dom.split('.')
        if len(parts) > 2:
            if dom in _SITE_MAP:
                return dom
            last_two = '.'.join(parts[-2:])
            if parts[-2] in ('com', 'co', 'gov', 'org', 'edu', 'net') and len(parts[-1]) == 2:
                return '.'.join(parts[-3:]) if len(parts) >= 3 else dom
            return last_two
        return dom
    # —— 兜底：品牌词/中文别名 → 域名 ——
    t_lower = title.lower()
    for alias, dom in _SITE_ALIAS:
        if alias in t_lower:
            return dom
    return ''


# 品牌词 / 中文名 → 域名（按出现频率排序，先匹配先返回）
_SITE_ALIAS = [
    ('哔哩哔哩', 'bilibili.com'), ('bilibili', 'bilibili.com'),
    ('youtube', 'youtube.com'), ('youtu.be', 'youtu.be'),
    ('华尔街日报', 'wsj.com'), ('wsj', 'wsj.com'),
    ('紐約時報', 'nytimes.com'), ('纽约时报', 'nytimes.com'), ('nytimes', 'nytimes.com'),
    ('维基百科', 'wikipedia.org'), ('wikipedia', 'wikipedia.org'),
    ('中国知网', 'cnki.net'), ('知网', 'cnki.net'),
    ('北大法宝', 'pkulaw.com'), ('法律法规数据库', 'pkulaw.com'),
    ('豆包', 'doubao.com'),
    ('m365 copilot', 'microsoft.com'), ('microsoft 365', 'microsoft.com'),
    ('google 文档', 'docs.google.com'), ('google docs', 'docs.google.com'),
    ('google 表格', 'sheets.google.com'), ('google sheets', 'sheets.google.com'),
    ('google 幻灯片', 'slides.google.com'), ('google slides', 'slides.google.com'),
    ('gemini', 'gemini.google.com'),
    ('chatgpt', 'chatgpt.com'),
    ('claude', 'claude.ai'),
    ('github', 'github.com'),
    ('zhihu', 'zhihu.com'), ('知乎', 'zhihu.com'),
    ('微博', 'weibo.com'),
    ('抖音', 'douyin.com'), ('tiktok', 'tiktok.com'),
    ('小红书', 'xiaohongshu.com'),
    ('淘宝', 'taobao.com'), ('天猫', 'tmall.com'),
    ('京东', 'jd.com'),
    ('拼多多', 'pinduoduo.com'),
    ('百度', 'baidu.com'),
    ('bing', 'bing.com'),
    ('qq 邮箱', 'mail.qq.com'),
    ('outlook', 'outlook.com'),
    ('twitch', 'twitch.tv'),
    ('reddit', 'reddit.com'),
    ('twitter', 'twitter.com'),
    ('爱奇艺', 'iqiyi.com'),
    ('优酷', 'youku.com'),
    ('芒果tv', 'mgtv.com'),
    ('虎牙', 'huya.com'),
    ('斗鱼', 'douyu.com'),
]


def normalize_window(proc_name: str, title: str):
    """窗口归一化：返回 (display_name, category, should_ignore)

    - 系统噪音窗口 → should_ignore=True
    - 浏览器 → 提取域名合并（Bilibili/YouTube 等）
    - 其他应用 → 查 _APP_NAME_MAP，否则用 exe 文件名去后缀
    """
    p = (proc_name or '').lower().strip()
    t = (title or '').strip()

    # 1. 完全忽略的壳进程
    if p in _IGNORE_PROCS:
        return (None, 'ignored', True)

    # 2. 标题噪音
    t_lower = t.lower()
    for pat in _IGNORE_TITLE_PATTERNS:
        if pat in t_lower:
            return (None, 'ignored', True)
    if not t:
        return (None, 'ignored', True)

    # 3. 浏览器 → 站点合并
    if p in _BROWSER_PROCS:
        dom = _extract_domain(t)
        if dom:
            display = _SITE_MAP.get(dom) or dom
            return (display, 'browser', False)
        # 无法提取域名 → 按浏览器本身归类
        browser_name = _APP_NAME_MAP.get(p, p.replace('.exe', '').title())
        return (browser_name, 'browser', False)

    # 4. 其他应用
    if p in _APP_NAME_MAP:
        return (_APP_NAME_MAP[p], 'app', False)
    # 兜底：文件名去 .exe 首字母大写
    fallback = re.sub(r'\.exe$', '', p, flags=re.IGNORECASE)
    if not fallback:
        return (None, 'ignored', True)
    return (fallback.title() if fallback.isascii() else fallback, 'app', False)


_user32 = ctypes.WinDLL('user32', use_last_error=True) if sys.platform == 'win32' else None
_WinEventProc = ctypes.WINFUNCTYPE(
    None, _wt.HANDLE, _wt.DWORD, _wt.HWND,
    _wt.LONG, _wt.LONG, _wt.DWORD, _wt.DWORD
) if sys.platform == 'win32' else None

_WIN_DB_PATH = get_writable_path('window_activity.db')


class WindowActivityDB:
    """SQLite 窗口活动记录与关键词规则存储（线程安全）"""

    def __init__(self, path=_WIN_DB_PATH):
        self._path = path
        self._local = threading.local()
        self._init_schema()

    def _conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self):
        with sqlite3.connect(self._path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS activity_records (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    proc_name   TEXT    NOT NULL,
                    title       TEXT    NOT NULL,
                    start_time  REAL    NOT NULL,
                    duration    REAL    NOT NULL DEFAULT 0,
                    state       TEXT    NOT NULL DEFAULT 'unknown',
                    date_key    TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keywords (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword     TEXT    NOT NULL,
                    list_type   TEXT    NOT NULL,
                    proc_pattern TEXT   DEFAULT NULL,
                    UNIQUE(keyword, list_type)
                );
                CREATE INDEX IF NOT EXISTS idx_activity_date
                    ON activity_records(date_key);
                CREATE TABLE IF NOT EXISTS schema_meta (k TEXT PRIMARY KEY, v TEXT);
            """)
            # Schema migration: add rule_type column if missing
            try:
                c.execute("ALTER TABLE keywords ADD COLUMN rule_type TEXT NOT NULL DEFAULT 'title'")
            except Exception:
                pass
            # ── v3 → v4 迁移：清旧默认 + 重建 keywords 表去掉 CHECK 限制（支持 'neutral'）──
            v4 = c.execute("SELECT v FROM schema_meta WHERE k='v4_keyword_neutral'").fetchone()
            if not v4:
                # 重建表：旧表可能有 CHECK(list_type IN ('white','black')) 约束
                c.execute("DROP TABLE IF EXISTS keywords")
                c.execute("""
                    CREATE TABLE keywords (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        keyword      TEXT NOT NULL,
                        list_type    TEXT NOT NULL,
                        proc_pattern TEXT DEFAULT NULL,
                        rule_type    TEXT NOT NULL DEFAULT 'title',
                        UNIQUE(keyword, list_type, rule_type)
                    )
                """)
                c.execute("INSERT OR REPLACE INTO schema_meta(k,v) VALUES('v4_keyword_neutral','1')")
            c.commit()

    def insert_record(self, proc_name, title, start_time, duration, state):
        dk = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d')
        cur = self._conn().execute(
            "INSERT INTO activity_records(proc_name,title,start_time,duration,state,date_key)"
            " VALUES(?,?,?,?,?,?)",
            (proc_name, title, start_time, duration, state, dk)
        )
        self._conn().commit()
        return cur.lastrowid

    def update_record(self, record_id, duration=None, state=None):
        if duration is not None:
            self._conn().execute(
                "UPDATE activity_records SET duration=? WHERE id=?", (duration, record_id)
            )
        if state is not None:
            self._conn().execute(
                "UPDATE activity_records SET state=? WHERE id=?", (state, record_id)
            )
        self._conn().commit()

    def add_keyword(self, keyword, list_type, proc_pattern=None, rule_type='title'):
        self._conn().execute(
            "INSERT OR REPLACE INTO keywords(keyword,list_type,proc_pattern,rule_type) VALUES(?,?,?,?)",
            (keyword, list_type, proc_pattern, rule_type)
        )
        self._conn().commit()

    def get_keywords(self):
        rows = self._conn().execute("SELECT keyword, list_type, rule_type, proc_pattern FROM keywords").fetchall()
        out = {'white': [], 'black': [], 'neutral': []}
        for r in rows:
            lt = r['list_type']
            if lt in out:
                out[lt].append({
                    'keyword': r['keyword'],
                    'rule_type': r['rule_type'] or 'title',
                    'proc_pattern': r['proc_pattern'],
                })
        return out

    def get_keywords_full(self):
        rows = self._conn().execute(
            "SELECT id, keyword, list_type, proc_pattern, rule_type FROM keywords ORDER BY list_type, rule_type, keyword"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_keyword_by_id(self, kw_id):
        self._conn().execute("DELETE FROM keywords WHERE id=?", (kw_id,))
        self._conn().commit()

    def get_today_top10(self):
        """今日使用前 10：按 normalize_window 统一归一化后合并"""
        dk = date.today().isoformat()
        rows = self._conn().execute("""
            SELECT proc_name, title, duration, state
              FROM activity_records
             WHERE date_key = ?
        """, (dk,)).fetchall()

        agg = {}
        for row in rows:
            display, category, ignore = normalize_window(row['proc_name'], row['title'] or '')
            if ignore or not display:
                continue
            if display not in agg:
                agg[display] = {
                    'total': 0, 'focus': 0, 'distracted': 0, 'unknown': 0,
                    'is_browser': (category == 'browser'),
                    'category': category,
                }
            agg[display]['total'] += row['duration']
            st = row['state'] if row['state'] in ('focus', 'distracted', 'unknown') else 'unknown'
            agg[display][st] = agg[display].get(st, 0) + row['duration']
        sorted_items = sorted(agg.items(), key=lambda x: x[1]['total'], reverse=True)[:10]
        return [{'name': k, **v} for k, v in sorted_items]

    def get_heatmap_data(self, days=35):
        """返回最近N天每天上午/下午/晚上的专注秒数"""
        from datetime import date as _date, timedelta as _td, datetime as _dt
        since = (_date.today() - _td(days=days)).isoformat()
        rows = self._conn().execute("""
            SELECT start_time, duration, state FROM activity_records
            WHERE date_key >= ? AND state = 'focus'
        """, (since,)).fetchall()
        result = {}
        for row in rows:
            try:
                start = _dt.fromtimestamp(row['start_time'])
                dk = start.strftime('%Y-%m-%d')
                hour = start.hour
                if hour < 12:
                    slot = 'morning'
                elif hour < 18:
                    slot = 'afternoon'
                else:
                    slot = 'evening'
                if dk not in result:
                    result[dk] = {'morning': 0.0, 'afternoon': 0.0, 'evening': 0.0}
                result[dk][slot] += row['duration']
            except Exception:
                pass
        return result

    def get_today_log(self, limit=200):
        dk = date.today().isoformat()
        rows = self._conn().execute("""
            SELECT id, proc_name, title, start_time, duration, state
              FROM activity_records
             WHERE date_key = ?
             ORDER BY start_time DESC
             LIMIT ?
        """, (dk, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            display, category, ignore = normalize_window(d['proc_name'], d['title'] or '')
            if ignore:
                continue  # 历史记录里若残留噪音窗口，也在展示时过滤
            d['display_name'] = display
            d['category'] = category
            out.append(d)
        return out

    def get_today_summary(self):
        dk = date.today().isoformat()
        rows = self._conn().execute("""
            SELECT proc_name, title,
                   SUM(duration) AS total_sec,
                   state,
                   COUNT(*) AS visits
              FROM activity_records
             WHERE date_key = ?
             GROUP BY proc_name, title, state
             ORDER BY total_sec DESC
             LIMIT 100
        """, (dk,)).fetchall()
        return [dict(r) for r in rows]

    def get_unknown_records(self, limit=20):
        rows = self._conn().execute("""
            SELECT id, proc_name, title,
                   SUM(duration) AS total_sec,
                   MAX(start_time) AS last_seen
              FROM activity_records
             WHERE state = 'unknown'
             GROUP BY proc_name, title
             ORDER BY last_seen DESC
             LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_record_by_id(self, record_id):
        row = self._conn().execute(
            "SELECT proc_name, title FROM activity_records WHERE id=?", (record_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── v3 新增：按 display 聚合 + 三色统计 ──────────────────────

    def _today_rows(self):
        dk = date.today().isoformat()
        return self._conn().execute("""
            SELECT proc_name, title, duration
              FROM activity_records
             WHERE date_key = ?
        """, (dk,)).fetchall()

    def _aggregate_by_display(self, classifier=None):
        """把今日记录按 (display, proc_low) 聚合，state 用当前 classifier 实时判定。
        返回 [{display, proc_raw, category, state, total, sample_titles, ids}]
        """
        agg = {}
        for row in self._today_rows():
            display, category, ignore = normalize_window(row['proc_name'], row['title'] or '')
            if ignore or not display:
                continue
            state = classifier.classify(row['proc_name'], row['title'] or '') if classifier else 'unknown'
            key = (display, category)
            if key not in agg:
                agg[key] = {
                    'display': display,
                    'proc_raw': row['proc_name'],
                    'category': category,
                    'state': state,
                    'total': 0.0,
                    'titles': {},
                }
            # 同 display 下若不同记录被判到不同 state，取「focus 优先 > distracted > neutral > unknown」
            order = {'focus': 0, 'distracted': 1, 'neutral': 2, 'unknown': 3}
            if order.get(state, 9) < order.get(agg[key]['state'], 9):
                agg[key]['state'] = state
            agg[key]['total'] += row['duration']
            tt = (row['title'] or '').strip()
            if tt:
                agg[key]['titles'][tt] = agg[key]['titles'].get(tt, 0) + row['duration']
        # 摊平 titles → top 5
        out = []
        for v in agg.values():
            top_titles = sorted(v['titles'].items(), key=lambda x: x[1], reverse=True)[:5]
            v['sample_titles'] = [{'title': t, 'sec': round(s)} for t, s in top_titles]
            del v['titles']
            v['total'] = round(v['total'])
            out.append(v)
        return out

    def get_stats_today(self, classifier):
        """三色统计：focus / distracted / neutral / unknown 总秒数"""
        items = self._aggregate_by_display(classifier)
        bucket = {'focus': 0, 'distracted': 0, 'neutral': 0, 'unknown': 0}
        for it in items:
            bucket[it['state']] = bucket.get(it['state'], 0) + it['total']
        bucket['total'] = sum(bucket.values())
        return bucket

    def get_top_by_state(self, classifier, state, limit=5):
        items = [x for x in self._aggregate_by_display(classifier) if x['state'] == state]
        items.sort(key=lambda x: x['total'], reverse=True)
        return items[:limit]

    def get_pending_displays(self, classifier, limit=20):
        """待分类（unknown）的 display 聚合列表"""
        items = [x for x in self._aggregate_by_display(classifier) if x['state'] == 'unknown']
        items.sort(key=lambda x: x['total'], reverse=True)
        return items[:limit]

    def get_top_all(self, classifier, limit=10):
        items = self._aggregate_by_display(classifier)
        items.sort(key=lambda x: x['total'], reverse=True)
        return items[:limit]

    def classify_display(self, proc_raw, display, state, kind=None):
        """用户对某 display 拨档：写入 keywords 规则
        - kind='site' → 强制写为 title 规则（标题包含匹配）
        - kind='proc' → 强制写为 proc 规则
        - kind=None  → 按 proc_raw 是否为浏览器自动判断（兼容旧调用）
        """
        proc_low = (proc_raw or '').lower()
        if kind is None:
            is_browser = proc_low in {b.lower() for b in _BROWSER_PROCS}
            kind = 'site' if is_browser else 'proc'
        kw = (display or '').strip()
        if not kw:
            return False
        if state == 'reset':
            self._conn().execute(
                "DELETE FROM keywords WHERE LOWER(keyword)=LOWER(?) OR LOWER(keyword)=LOWER(?)",
                (kw, proc_low.replace('.exe', ''))
            )
            self._conn().commit()
            return True
        if state not in ('focus', 'distracted', 'neutral'):
            return False
        list_type = {'focus': 'white', 'distracted': 'black', 'neutral': 'neutral'}[state]
        if kind == 'site':
            self._conn().execute(
                "DELETE FROM keywords WHERE LOWER(keyword)=LOWER(?) AND rule_type='title'",
                (kw,)
            )
            self.add_keyword(kw, list_type, '__browser__', 'title')
        else:
            base = (proc_low or kw.lower()).replace('.exe', '')
            self._conn().execute(
                "DELETE FROM keywords WHERE LOWER(keyword)=LOWER(?) AND rule_type='proc'",
                (base,)
            )
            self.add_keyword(base, list_type, proc_raw or (base + '.exe'), 'proc')
        self._conn().commit()
        return True

    # ── v4：返回所有规则（内置 + 用户），供 UI 展示拨档 ──

    def get_all_rules(self, today_only=False):
        """返回 {procs:[...], sites:[...]}
        - today_only=True 时仅返回今日出现过的程序/站点（含未识别站点的 display）
        - 总是收集 today_sites 用于补全今日访问过但未分类的域名
        """
        # 今日出现过的 proc/key 集合（用于过滤 + 补全 unknown 域名）
        today_procs = set()
        today_sites = set()
        for row in self._today_rows():
            p = (row['proc_name'] or '').lower()
            if p:
                today_procs.add(p)
                today_procs.add(p.replace('.exe', ''))
            # 浏览器记录中提取域名
            if p in {b.lower() for b in _BROWSER_PROCS}:
                dom = _extract_domain(row['title'] or '')
                if dom:
                    today_sites.add(dom)
                    # 同时把所有内置站点中能匹配到的也加进去
                    for site in (_BUILTIN_FOCUS_SITES | _BUILTIN_DISTRACTED_SITES | _BUILTIN_NEUTRAL_SITES):
                        if dom == site or dom.endswith('.' + site):
                            today_sites.add(site)
        # 用户覆盖（key → state）
        user_proc_state = {}
        user_site_state = {}
        for lt, st in (('white', 'focus'), ('black', 'distracted'), ('neutral', 'neutral')):
            rows = self._conn().execute(
                "SELECT keyword, rule_type, proc_pattern FROM keywords WHERE list_type=?", (lt,)
            ).fetchall()
            for r in rows:
                k = (r['keyword'] or '').lower()
                if not k:
                    continue
                if r['rule_type'] == 'proc':
                    user_proc_state[k] = st
                else:
                    user_site_state[k] = st

        procs = []
        seen = set()
        # 内置规则
        for src_set, builtin_state in (
            (_BUILTIN_FOCUS_PROCS, 'focus'),
            (_BUILTIN_DISTRACTED_PROCS, 'distracted'),
            (_BUILTIN_NEUTRAL_PROCS, 'neutral'),
        ):
            for exe in src_set:
                base = exe.replace('.exe', '')
                user_st = user_proc_state.get(base) or user_proc_state.get(exe)
                state = user_st or builtin_state
                source = 'user' if user_st else 'builtin'
                display = _APP_NAME_MAP.get(exe, base.title() if base.isascii() else base)
                key = exe
                if key in seen:
                    continue
                seen.add(key)
                procs.append({
                    'key': base, 'exe': exe, 'display': display,
                    'state': state, 'source': source,
                    'builtin_state': builtin_state,
                })
        # 用户额外加的规则（不在内置里）
        for k, st in user_proc_state.items():
            if any(p['key'] == k or p['exe'].replace('.exe','') == k for p in procs):
                continue
            procs.append({
                'key': k, 'exe': k + '.exe',
                'display': _APP_NAME_MAP.get(k + '.exe', k.title()),
                'state': st, 'source': 'user', 'builtin_state': None,
            })
        # 今日出现过的程序中，不在任何规则里的 → unknown（让用户拨档）
        all_builtin_procs = (_BUILTIN_FOCUS_PROCS | _BUILTIN_DISTRACTED_PROCS | _BUILTIN_NEUTRAL_PROCS)
        already_keys = {p['key'].lower() for p in procs} | {p['exe'].lower() for p in procs}
        # 浏览器进程不算（它们的"应用"含义在站点列表里体现）
        browser_set = {b.lower() for b in _BROWSER_PROCS}
        for tp in today_procs:
            if not tp or tp in already_keys:
                continue
            tp_exe = tp if tp.endswith('.exe') else (tp + '.exe')
            if tp in browser_set or tp_exe in browser_set:
                continue
            # 跳过已被内置规则覆盖的（即使大小写不同）
            if tp in all_builtin_procs or tp_exe in all_builtin_procs:
                continue
            base = tp.replace('.exe', '')
            exe = tp_exe
            display = _APP_NAME_MAP.get(exe, base.title() if base.isascii() else base)
            procs.append({
                'key': base, 'exe': exe, 'display': display,
                'state': 'unknown', 'source': 'builtin', 'builtin_state': None,
            })
            already_keys.add(base); already_keys.add(exe)
        # 排序：unknown → focus → distracted → neutral
        order = {'unknown': -1, 'focus': 0, 'distracted': 1, 'neutral': 2}
        procs.sort(key=lambda x: (order.get(x['state'], 9), x['display'].lower()))

        # today_only 过滤（保留所有用户自定义；内置只保留今日出现的）
        if today_only:
            procs = [p for p in procs
                     if p['source'] == 'user'
                     or p['key'].lower() in today_procs
                     or p['exe'].lower() in today_procs]

        # 站点
        sites = []
        seen_s = set()
        all_builtin_sites = (_BUILTIN_FOCUS_SITES | _BUILTIN_DISTRACTED_SITES | _BUILTIN_NEUTRAL_SITES)
        for src_set, builtin_state in (
            (_BUILTIN_FOCUS_SITES, 'focus'),
            (_BUILTIN_DISTRACTED_SITES, 'distracted'),
            (_BUILTIN_NEUTRAL_SITES, 'neutral'),
        ):
            for s in src_set:
                if s in seen_s:
                    continue
                user_st = user_site_state.get(s)
                sites.append({
                    'key': s, 'display': _SITE_MAP.get(s, s),
                    'state': user_st or builtin_state,
                    'source': 'user' if user_st else 'builtin',
                    'builtin_state': builtin_state,
                })
                seen_s.add(s)
        for k, st in user_site_state.items():
            if k in seen_s:
                continue
            sites.append({
                'key': k, 'display': _SITE_MAP.get(k, k),
                'state': st, 'source': 'user', 'builtin_state': None,
            })
            seen_s.add(k)
        # 今日访问过但未分类的域名 → state=unknown，让用户拨档
        for dom in today_sites:
            if dom in seen_s:
                continue
            # 跳过已被内置规则匹配到的（subdomain）
            matched = False
            for s in all_builtin_sites:
                if dom == s or dom.endswith('.' + s):
                    matched = True
                    break
            if matched:
                continue
            sites.append({
                'key': dom, 'display': _SITE_MAP.get(dom, dom),
                'state': 'unknown', 'source': 'builtin', 'builtin_state': None,
            })
            seen_s.add(dom)
        sites.sort(key=lambda x: (order.get(x['state'], 9), x['display'].lower()))
        if today_only:
            sites = [s for s in sites
                     if s['source'] == 'user'
                     or s['key'].lower() in today_sites]
        return {'procs': procs, 'sites': sites}


class WindowStateClassifier:
    """根据内置规则 + 用户自定义关键词判定窗口状态

    返回值: 'focus' | 'distracted' | 'neutral' | 'unknown'
    优先级: 用户自定义规则 > 内置应用/站点规则 > unknown
    （已废弃 BLACKLIST_GRACE / DISTRACTION_TIMEOUT —— 分类只取决于内容本身）
    """

    # 兼容旧字段（其他代码可能引用，但逻辑不再使用）
    DISTRACTION_TIMEOUT = 0
    BLACKLIST_GRACE = 0

    def __init__(self, db: WindowActivityDB):
        self._db = db
        self._force_kws: set = set()  # 兼容旧 API（已不再用于宽容期判定）

    def add_force_kw(self, keyword: str):
        self._force_kws.add(keyword.lower())

    def classify(self, proc_name, title, stay_seconds=0):
        p = (proc_name or '').lower()
        t = (title or '')
        t_lower = t.lower()
        is_browser = p in {b.lower() for b in _BROWSER_PROCS}

        # ── 1. 用户自定义规则（最高优先级）── 顺序: neutral > black > white
        # 这样可以让用户把内置 distracted 的应用强制改为 neutral
        try:
            kw = self._db.get_keywords()
        except Exception:
            kw = {'white': [], 'black': [], 'neutral': []}

        def _match(item):
            w = (item.get('keyword') or '').lower()
            if not w:
                return False
            rt = item.get('rule_type', 'title')
            if rt == 'proc':
                return w == p or w == p.replace('.exe', '') or w in p
            else:
                if (item.get('proc_pattern') or '').lower() == '__browser__' and not is_browser:
                    return False
                return w in t_lower

        for item in kw.get('neutral', []):
            if _match(item):
                return 'neutral'
        for item in kw.get('black', []):
            if _match(item):
                return 'distracted'
        for item in kw.get('white', []):
            if _match(item):
                return 'focus'

        # ── 2. 浏览器：按域名 / 标题关键词 ──
        if is_browser:
            dom = _extract_domain(t)
            def _site_hit(dom_val, sites):
                if not dom_val:
                    return False
                if dom_val in sites:
                    return True
                for s in sites:
                    if dom_val == s or dom_val.endswith('.' + s):
                        return True
                return False
            if _site_hit(dom, _BUILTIN_FOCUS_SITES):
                return 'focus'
            if _site_hit(dom, _BUILTIN_DISTRACTED_SITES):
                return 'distracted'
            if _site_hit(dom, _BUILTIN_NEUTRAL_SITES):
                return 'neutral'
            for kw_focus in _BUILTIN_FOCUS_TITLE_KW:
                if kw_focus in t_lower:
                    return 'focus'
            return 'unknown'

        # ── 3. 内置应用规则 ──
        if p in _BUILTIN_FOCUS_PROCS:
            return 'focus'
        if p in _BUILTIN_DISTRACTED_PROCS:
            return 'distracted'
        if p in _BUILTIN_NEUTRAL_PROCS:
            return 'neutral'

        # ── 4. 兜底：unknown ──
        return 'unknown'



class WinActivityState:
    """当前窗口活动共享状态（线程安全）"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = ''
        self.title = ''
        self.display = ''        # 归一化后的显示名
        self.category = 'app'    # app | browser | ignored
        self.state = 'unknown'
        self.start_time = time.time()
        self.current_record_id = None
        self._recent = deque(maxlen=4)  # last 4 distinct windows

    def to_dict(self):
        with self.lock:
            stay = int(time.time() - self.start_time) if self.title else 0
            recent = list(self._recent)
            return {
                'proc': self.display or self.proc,  # 前端展示归一化名
                'proc_raw': self.proc,              # 保留原始 exe 以便调试
                'category': self.category,
                'title': self.title,
                'state': self.state,
                'stay_sec': stay,
                'recent_windows': recent,
            }


class WinEventMonitor(threading.Thread):
    """使用 SetWinEventHook 监听前台窗口切换，事件驱动（零轮询）"""

    def __init__(self, on_window_change):
        super().__init__(daemon=True, name='WinEventMonitor')
        self._callback = on_window_change
        self._hook = None
        self._hook2 = None
        self._proc_ref = None
        self._proc_ref2 = None
        self._thread_id = None
        self._last_browser_change = 0.0

    def _clean_title(self, raw):
        title = re.sub(r'[\s\-–—·|,]*和另外\s*\d+\s*个页面.*$', '', raw.strip()).strip()
        title = re.sub(r'[\s\-–—·|,]*and\s+\d+\s+more\s+(?:tab|page)s?.*$', '', title, flags=re.IGNORECASE).strip()
        return title

    def run(self):
        if sys.platform != 'win32' or _user32 is None:
            logging.warning("[WinMonitor] 非 Windows 平台，窗口监控已禁用")
            return
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        def _get_proc_and_title(hwnd):
            pid = _wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                import psutil
                proc_name = psutil.Process(pid.value).name()
            except Exception:
                proc_name = f'PID:{pid.value}'
            buf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, buf, 512)
            title = self._clean_title(buf.value)
            return proc_name, title

        def _cb(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            try:
                if not hwnd:
                    return
                proc_name, title = _get_proc_and_title(hwnd)
                if title:
                    self._callback(proc_name, title)
            except Exception:
                pass

        def _name_cb(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            """Track browser tab changes via window title changes"""
            try:
                if not hwnd or idObject != _OBJID_WINDOW:
                    return
                proc_name, title = _get_proc_and_title(hwnd)
                if not title or proc_name.lower() not in _BROWSER_PROCS:
                    return
                now = time.time()
                if now - self._last_browser_change < 2.0:
                    return
                # Only fire if title actually changed for current window
                with _win_activity.lock:
                    if _win_activity.proc.lower() != proc_name.lower():
                        return
                    if _win_activity.title == title:
                        return
                self._last_browser_change = now
                self._callback(proc_name, title)
            except Exception:
                pass

        self._proc_ref  = _WinEventProc(_cb)
        self._proc_ref2 = _WinEventProc(_name_cb)
        self._hook = _user32.SetWinEventHook(
            _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND,
            None, self._proc_ref, 0, 0, _WIN_EVENT_OUTOFCONTEXT
        )
        self._hook2 = _user32.SetWinEventHook(
            _EVENT_OBJECT_NAMECHANGE, _EVENT_OBJECT_NAMECHANGE,
            None, self._proc_ref2, 0, 0, _WIN_EVENT_OUTOFCONTEXT
        )
        if not self._hook:
            logging.error("[WinMonitor] SetWinEventHook 失败，窗口监控不可用")
            return

        msg = _wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            _user32.UnhookWinEvent(self._hook)
        if self._hook2:
            _user32.UnhookWinEvent(self._hook2)

    def stop(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)


# 全局窗口监控实例
_win_db = None
_win_clf = None
_win_activity = WinActivityState()
_win_monitor_thread = None


def _init_win_monitor():
    """初始化窗口监控模块，在 main() 中调用"""
    global _win_db, _win_clf, _win_monitor_thread

    try:
        _win_db = WindowActivityDB()
        _win_clf = WindowStateClassifier(_win_db)

        # ─────────── 详细窗口活动 JSONL 日志 ───────────
        # 用户使用一段时间后可以把日志发回来供分析、调整分类规则。
        # 文件按日切：window_log_YYYY-MM-DD.jsonl，每行一条 JSON。
        # 字段：ts(开始秒) / end_ts / dur / proc_raw / title_raw / display / category /
        #       state(focus|distracted|unknown) / event(switch|update_state)
        _win_jsonl_lock = threading.Lock()
        def _win_jsonl_path():
            from datetime import date as _d
            return get_writable_path(f"window_log_{_d.today().isoformat()}.jsonl")

        def _win_jsonl_write(record):
            try:
                with _win_jsonl_lock:
                    with open(_win_jsonl_path(), 'a', encoding='utf-8') as f:
                        f.write(json.dumps(record, ensure_ascii=False) + '\n')
            except Exception:
                pass

        def on_window_change(proc_name, title):
            # 归一化 + 噪音过滤：被忽略窗口直接丢弃，保留上一个活动不切换
            display, category, ignore = normalize_window(proc_name, title)
            if ignore:
                # 也记录被忽略的窗口（标记 event=ignored），便于后续排查规则
                _win_jsonl_write({
                    'ts': time.time(),
                    'event': 'ignored',
                    'proc_raw': proc_name,
                    'title_raw': title,
                })
                return
            now = time.time()
            with _win_activity.lock:
                # 结束上一个窗口记录
                if _win_activity.title:
                    duration = now - _win_activity.start_time
                    # ── v4：单次停留 <2s 不入 SQLite（避免噪音淹没统计）──
                    if duration >= 2.0:
                        if _win_activity.current_record_id is None:
                            _win_activity.current_record_id = _win_db.insert_record(
                                _win_activity.proc, _win_activity.title,
                                _win_activity.start_time, duration, _win_activity.state
                            )
                        else:
                            _win_db.update_record(_win_activity.current_record_id, duration=duration)
                    elif _win_activity.current_record_id is not None:
                        # 已经入库的短窗口（提前在 timeout_checker 中插入），更新最终 dur
                        _win_db.update_record(_win_activity.current_record_id, duration=duration)
                    # 写入 JSONL：上一个窗口的完整记录（无论时长）
                    _win_jsonl_write({
                        'ts': _win_activity.start_time,
                        'end_ts': now,
                        'dur': round(duration, 2),
                        'event': 'switch',
                        'proc_raw': _win_activity.proc,
                        'title_raw': _win_activity.title,
                        'display': _win_activity.display,
                        'category': _win_activity.category,
                        'state': _win_activity.state,
                        'persisted': duration >= 2.0,
                    })

                # 开始新窗口
                _win_activity.proc = proc_name
                _win_activity.title = title
                _win_activity.display = display
                _win_activity.category = category
                _win_activity.start_time = now
                _win_activity.current_record_id = None
                _win_activity.state = _win_clf.classify(proc_name, title, stay_seconds=0)
                # Track recent windows for multi-monitor display（使用归一化后显示名）
                if not _win_activity._recent or (
                    _win_activity._recent[-1].get('proc') != display
                    or _win_activity._recent[-1].get('title') != title
                ):
                    _win_activity._recent.append({
                        'proc': display,
                        'title': title[:45],
                        'state': _win_activity.state,
                        'category': category,
                        'ts': now,
                    })
                # JSONL：新窗口开始事件（包含原始 + 归一化字段，便于规则诊断）
                _win_jsonl_write({
                    'ts': now,
                    'event': 'enter',
                    'proc_raw': proc_name,
                    'title_raw': title,
                    'display': display,
                    'category': category,
                    'state': _win_activity.state,
                })
        def _timeout_checker():
            while True:
                time.sleep(5)
                try:
                    with _win_activity.lock:
                        if not _win_activity.title:
                            continue
                        stay = time.time() - _win_activity.start_time
                        new_state = _win_clf.classify(
                            _win_activity.proc, _win_activity.title, stay_seconds=stay
                        )
                        _win_activity.state = new_state
                        # ── v3：仅当停留 ≥5s 才入库/更新 ──
                        if stay >= 2.0:
                            if _win_activity.current_record_id is None:
                                _win_activity.current_record_id = _win_db.insert_record(
                                    _win_activity.proc, _win_activity.title,
                                    _win_activity.start_time, stay, _win_activity.state
                                )
                            else:
                                _win_db.update_record(
                                    _win_activity.current_record_id,
                                    duration=stay,
                                    state=_win_activity.state
                                )
                except Exception:
                    pass

        _win_monitor_thread = WinEventMonitor(on_window_change)
        _win_monitor_thread.start()

        checker = threading.Thread(target=_timeout_checker, daemon=True, name='WinTimeoutChecker')
        checker.start()

        # Record all currently visible windows on all monitors at startup
        def _seed_visible_windows():
            time.sleep(1.0)  # let the hook settle first
            try:
                if not _user32:
                    return
                import psutil
                seen = set()

                def _enum_cb(hwnd, _):
                    try:
                        if not _user32.IsWindowVisible(hwnd):
                            return True
                        if _user32.IsIconic(hwnd):
                            return True
                        buf = ctypes.create_unicode_buffer(512)
                        _user32.GetWindowTextW(hwnd, buf, 512)
                        raw = buf.value.strip()
                        title = re.sub(r'\s+和另外\s*\d+\s*个页面.*$', '', raw).strip()
                        title = re.sub(r'\s+and\s+\d+\s+more\s+(?:tab|page)s?.*$', '', title, flags=re.IGNORECASE).strip()
                        if not title:
                            return True
                        pid = _wt.DWORD()
                        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        try:
                            proc_name = psutil.Process(pid.value).name()
                        except Exception:
                            return True
                        # 归一化过滤：跳过系统噪音窗口
                        display, category, ignore = normalize_window(proc_name, title)
                        if ignore:
                            return True
                        key = (proc_name.lower(), title[:40])
                        if key in seen:
                            return True
                        seen.add(key)
                        # Insert as zero-duration seed record (don't change _win_activity)
                        now = time.time()
                        dk = __import__('datetime').date.today().isoformat()
                        state_val = _win_clf.classify(proc_name, title, stay_seconds=0)
                        conn = _win_db._conn()
                        conn.execute(
                            "INSERT OR IGNORE INTO activity_records"
                            "(proc_name,title,start_time,duration,state,date_key)"
                            " VALUES(?,?,?,0,?,?)",
                            (proc_name, title, now, state_val, dk)
                        )
                        conn.commit()
                    except Exception:
                        pass
                    return True

                _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                _user32.EnumWindows(_WNDENUMPROC(_enum_cb), None)
            except Exception:
                pass

        threading.Thread(target=_seed_visible_windows, daemon=True, name='WinSeedThread').start()

        logging.info("[WinMonitor] 窗口活动监控已启动")
    except Exception:
        logging.error("[WinMonitor] 初始化失败:")
        logging.error(traceback.format_exc())


# ══════════════════════════════════════════
#  MJPEG 视频流服务器 (Flask)
# ══════════════════════════════════════════
# 配置 Flask 获取静态资源（app.html, app.js, app.css）
_static_dir = get_resource_path('.')
flask_app = Flask(__name__, static_folder=_static_dir, static_url_path='')
CORS(flask_app)

@flask_app.route('/api/version', methods=['GET'])
def api_version():
    return jsonify({'version': APP_VERSION, 'build_time': _BUILD_TIME})

@flask_app.after_request
def _no_cache_static(resp):
    """禁止浏览器缓存 html/js/css，确保 Ctrl+F5（含普通刷新）总能拿到最新前端"""
    try:
        ct = (resp.headers.get('Content-Type') or '').lower()
        if any(t in ct for t in ('text/html', 'javascript', 'text/css')):
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            resp.headers['Pragma'] = 'no-cache'
            resp.headers['Expires'] = '0'
    except Exception:
        pass
    return resp

@flask_app.route('/')
def index():
    """将根路径指向 app.html"""
    return flask_app.send_static_file('app.html')

def generate_mjpeg():
    """生成 MJPEG 流的帧"""
    while True:
        with state.lock:
            frame_bytes = state.jpeg_frame
        if frame_bytes is None:
            time.sleep(0.03)
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.033)  # ~30fps

@flask_app.route('/video_feed')
def video_feed():
    """返回 MJPEG 流"""
    return Response(generate_mjpeg(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@flask_app.route('/status')
def get_status():
    """HTTP 接口返回当前状态（备用）"""
    with state.lock:
        return json.dumps({
            'status': state.status,
            'yaw': state.yaw,
            'pitch': state.pitch,
        })

# ══════════════════════════════════════════
#  数据持久化 API 端点
# ══════════════════════════════════════════



@flask_app.route('/api/load-config', methods=['GET'])
def api_get_config():
    """获取 UI 偏好配置 + 运行时元信息"""
    try:
        logging.debug("[API-CONFIG] 🚀 收到前端加载配置请求 (GET /api/load-config)")
        cfg = load_config()
        # 只返回 UI 相关的配置，不返回统计数据
        ui_config = {
            'theme': cfg.get('theme', 'auto'),
            'sound_on': cfg.get('sound_on', False),
            'sens_idx': cfg.get('sens_idx', 1),
            'camera_index': cfg.get('camera_index', 0),
            'baseline_yaw': cfg.get('baseline_yaw', 0.0),
            'baseline_pitch': cfg.get('baseline_pitch', 0.0),
            'baseline_ear': cfg.get('baseline_ear', 0.28),
            'baseline_neck_length': cfg.get('baseline_neck_length', 0.0),
            'focus_half_yaw': cfg.get('focus_half_yaw', 0.0),
            'focus_half_pitch': cfg.get('focus_half_pitch', 0.0),
            'camera_rotation': cfg.get('camera_rotation', 0),
            'camera_flip': cfg.get('camera_flip', True),
            # 运行时元信息
            'engine_label': ENGINE_LABEL,
            'version': APP_VERSION,
            'mode': 'prod',
        }
        logging.debug(f"[API-OK] 配置加载成功，包含 {len(ui_config)} 个字段，引擎={ENGINE_LABEL}")
        return json.dumps(ui_config, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        logging.error(f"[API] 获取配置失败: {e}")
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}

@flask_app.route('/api/save-config', methods=['POST'])
def api_save_config():
    """保存用户配置"""
    try:
        data = request.get_json() or {}
        save_config(data)
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        logging.error(f"[API] 保存配置失败: {e}")
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}

@flask_app.route('/api/load-data', methods=['GET'])
def api_get_data():
    """获取正式业务数据（days、排行榜、热力图、事件）"""
    try:
        logging.info("[API-DATA] 📊 收到前端请求历史业务数据 (GET /api/load-data)")
        data = load_data()
        
        # 只返回正式业务数据，不返回 UI 配置
        # 注：total_focus_ms 和 total_sessions 已删除，由前端从 days 等数据动态计算
        business_data = {
            'days': data.get('days', {}),
            'top3': data.get('top3', []),
            'hour_focus_ms': data.get('hour_focus_ms', {}),
            'all_time_best_streak': data.get('all_time_best_streak', 0),
            'events': data.get('events', []),
        }
        
        logging.info(f"[API-OK] 业务数据加载成功，包含 {len(data.get('days', {}))} 天记录")
        return json.dumps(business_data, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        logging.error(f"[API] 获取业务数据失败: {e}")
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}

# ==========================================
# 接收前端日志的 API 接口（仅开发模式）
# ==========================================
@flask_app.route('/api/log', methods=['POST', 'OPTIONS'])
def client_log():
    return jsonify({"status": "error", "message": "Log endpoint disabled"}), 403

@flask_app.route('/api/save-data', methods=['POST'])
def api_save_data():
    """保存使用数据（排行榜、时间轴、热力图等）"""
    try:
        data = request.get_json() or {}
        save_data(data)
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        logging.error(f"[API] 保存数据失败: {e}")
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


# ── 窗口监控 REST 接口 ──

@flask_app.route('/api/window/keywords', methods=['GET'])
def api_window_keywords_get():
    """获取所有关键词规则"""
    if _win_db is None:
        return json.dumps([]), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        rows = _win_db.get_keywords_full()
        return json.dumps(rows, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/keywords', methods=['POST'])
def api_window_keywords_add():
    """新增关键词规则"""
    if _win_db is None:
        return json.dumps({'error': 'monitor not available'}), 503, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    try:
        body = request.get_json() or {}
        keyword = (body.get('keyword') or '').strip()
        list_type = body.get('list_type', '')
        rule_type = body.get('rule_type', 'title')
        if not keyword or list_type not in ('white', 'black') or rule_type not in ('proc', 'title'):
            return json.dumps({'error': 'invalid params'}), 400, {
                'Content-Type': 'application/json; charset=utf-8'
            }
        # Page/title rules are browser-only by default
        proc_pat = '__browser__' if rule_type == 'title' else body.get('proc_pattern')
        _win_db.add_keyword(keyword, list_type, proc_pat, rule_type)
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/keywords/<int:kw_id>', methods=['DELETE'])
def api_window_keywords_delete(kw_id):
    """删除关键词规则"""
    if _win_db is None:
        return json.dumps({'error': 'monitor not available'}), 503, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    try:
        _win_db.remove_keyword_by_id(kw_id)
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/top10', methods=['GET'])
def api_window_top10():
    """今日使用时间前10"""
    if _win_db is None:
        return json.dumps([]), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        rows = _win_db.get_today_top10()
        return json.dumps(rows, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/heatmap', methods=['GET'])
def api_window_heatmap():
    """热力图专注数据（按天+时段）"""
    if _win_db is None:
        return json.dumps({}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        days = int(request.args.get('days', 35))
        data = _win_db.get_heatmap_data(days)
        return json.dumps(data, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/log', methods=['GET'])
def api_window_log():
    """今日窗口使用记录（按时间倒序）"""
    if _win_db is None:
        return json.dumps([]), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        rows = _win_db.get_today_log()
        return json.dumps(rows, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/status', methods=['GET'])
def api_window_status():
    """当前前台窗口状态"""
    return json.dumps(_win_activity.to_dict(), ensure_ascii=False), 200, {
        'Content-Type': 'application/json; charset=utf-8'
    }


@flask_app.route('/api/window/today', methods=['GET'])
def api_window_today():
    """今日活动汇总"""
    if _win_db is None:
        return json.dumps([]), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        rows = _win_db.get_today_summary()
        return json.dumps(rows, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/unknown', methods=['GET'])
def api_window_unknown():
    """未知状态待分类记录"""
    if _win_db is None:
        return json.dumps([]), 200, {'Content-Type': 'application/json; charset=utf-8'}
    try:
        rows = _win_db.get_unknown_records()
        return json.dumps(rows, ensure_ascii=False), 200, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


@flask_app.route('/api/window/classify', methods=['POST'])
def api_window_classify():
    """手动分类一条 unknown 记录，并将关键词加入规则库"""
    if _win_db is None:
        return json.dumps({'error': 'monitor not available'}), 503, {
            'Content-Type': 'application/json; charset=utf-8'
        }
    try:
        body = request.get_json() or {}
        record_id = int(body.get('id', 0))
        state = body.get('state', '')
        if state not in ('focus', 'distracted'):
            return json.dumps({'error': 'invalid state'}), 400, {
                'Content-Type': 'application/json; charset=utf-8'
            }
        _win_db.update_record(record_id, state=state)
        row = _win_db.get_record_by_id(record_id)
        keyword = None
        if row:
            keyword = row['title'][:30].strip()
            list_type = 'white' if state == 'focus' else 'black'
            if keyword:
                _win_db.add_keyword(keyword, list_type, row['proc_name'])
                if list_type == 'black' and _win_clf:
                    _win_clf.add_force_kw(keyword)
        # 同步更新当前活动状态（立刻生效，无宽容期）
        with _win_activity.lock:
            if _win_activity.title and _win_db:
                stay = time.time() - _win_activity.start_time
                new_state = _win_clf.classify(
                    _win_activity.proc, _win_activity.title, stay_seconds=stay
                )
                _win_activity.state = new_state
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}


# ───────── v3 新增窗口 API：三色统计 / Top / Pending / 拨档 ─────────

def _json_resp(data, status=200):
    return json.dumps(data, ensure_ascii=False), status, {
        'Content-Type': 'application/json; charset=utf-8'
    }


@flask_app.route('/api/window/stats_today', methods=['GET'])
def api_window_stats_today():
    if _win_db is None or _win_clf is None:
        return _json_resp({'focus': 0, 'distracted': 0, 'neutral': 0, 'unknown': 0, 'total': 0})
    try:
        return _json_resp(_win_db.get_stats_today(_win_clf))
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


@flask_app.route('/api/window/top', methods=['GET'])
def api_window_top():
    """返回 {focus: [...top5], distracted: [...top5], all: [...top10]}"""
    if _win_db is None or _win_clf is None:
        return _json_resp({'focus': [], 'distracted': [], 'all': []})
    try:
        n = int(request.args.get('n', 5))
        return _json_resp({
            'focus': _win_db.get_top_by_state(_win_clf, 'focus', n),
            'distracted': _win_db.get_top_by_state(_win_clf, 'distracted', n),
            'neutral': _win_db.get_top_by_state(_win_clf, 'neutral', n),
            'all': _win_db.get_top_all(_win_clf, max(10, n * 2)),
        })
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


@flask_app.route('/api/window/pending', methods=['GET'])
def api_window_pending():
    """待分类（unknown）display 聚合列表"""
    if _win_db is None or _win_clf is None:
        return _json_resp([])
    try:
        return _json_resp(_win_db.get_pending_displays(_win_clf, limit=30))
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


@flask_app.route('/api/window/classify_display', methods=['POST'])
def api_window_classify_display():
    """按 display 拨档：body = {display, proc, state, kind?}"""
    if _win_db is None or _win_clf is None:
        return _json_resp({'error': 'monitor not available'}, 503)
    try:
        body = request.get_json() or {}
        display = (body.get('display') or '').strip()
        proc = (body.get('proc') or '').strip()
        state = body.get('state', '')
        kind = body.get('kind')  # 'site' | 'proc' | None
        if not display or state not in ('focus', 'distracted', 'neutral', 'reset'):
            return _json_resp({'error': 'invalid params'}, 400)
        ok = _win_db.classify_display(proc, display, state, kind=kind)
        with _win_activity.lock:
            if _win_activity.title:
                _win_activity.state = _win_clf.classify(_win_activity.proc, _win_activity.title)
        return _json_resp({'success': ok})
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


@flask_app.route('/api/window/all_rules', methods=['GET'])
def api_window_all_rules():
    """返回完整规则列表（内置 + 用户覆盖），供 UI 拨档"""
    if _win_db is None:
        return _json_resp({'procs': [], 'sites': []})
    try:
        today_only = request.args.get('today_only', '0') in ('1', 'true', 'True')
        return _json_resp(_win_db.get_all_rules(today_only=today_only))
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


@flask_app.route('/api/window/distracted_alert', methods=['GET'])
def api_window_distracted_alert():
    """前端轮询：当前是否处于 distracted 且持续 ≥60s"""
    try:
        with _win_activity.lock:
            stay = int(time.time() - _win_activity.start_time) if _win_activity.title else 0
            return _json_resp({
                'distracted': _win_activity.state == 'distracted',
                'state': _win_activity.state,
                'stay_sec': stay,
                'should_alert': _win_activity.state == 'distracted' and stay >= 60,
                'display': _win_activity.display,
                'title': _win_activity.title,
            })
    except Exception as e:
        return _json_resp({'error': str(e)}, 500)


def run_flask():
    """在独立线程中运行 Flask"""
    try:
        logging.info(f"[MJPEG] 服务器启动在 http://127.0.0.1:{MJPEG_PORT}/")
        # 允许所有接口访问，方便调试
        flask_app.run(host='0.0.0.0', port=MJPEG_PORT, threaded=True, use_reloader=False)
    except Exception:
        logging.error("[MJPEG] Flask 服务器启动或运行异常:")
        logging.error(traceback.format_exc())

# ══════════════════════════════════════════
#  全局停止标志（供安全退出使用）
# ══════════════════════════════════════════
_shutdown_event = threading.Event()

# ══════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════
def main():
    try:
        parser = argparse.ArgumentParser(description='DoNotPlay 视觉后端')
        parser.add_argument('--restarted', action='store_true', help='重启模式标志，避免重复打开浏览器')
        args = parser.parse_args()

        logging.info("╔══════════════════════════════════════╗")
        logging.info(f"║   DoNotPlay v{APP_VERSION} 正在启动...        ║")
        logging.info("╚══════════════════════════════════════╝")

        # 启动 WebSocket 服务器线程
        ws_thread = threading.Thread(target=run_ws_server, daemon=True)
        ws_thread.start()

        # 启动 Flask MJPEG 服务器线程
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        # 启动视觉处理循环（始终在后台线程）
        vision_thread = threading.Thread(target=vision_loop, daemon=True)
        vision_thread.start()

        # 启动窗口活动监控
        _init_win_monitor()

        # 使用 pywebview 原生窗口
        try:
            import webview
        except ImportError:
            logging.info("[SYS] 未安装 pywebview，回退到浏览器模式...")
            url = f'http://127.0.0.1:{MJPEG_PORT}/'
            webbrowser.open(url)
            try:
                while not _shutdown_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                logging.info("\n[SYS] 正在关闭...")
            return

        gui_url = f'http://127.0.0.1:{MJPEG_PORT}/'
        logging.info(f"[SYS] 正在启动窗口: {gui_url}")
        window = webview.create_window(
            'DoNotPlay - 专注力监测',
            url=gui_url,
            width=1400,
            height=900,
            min_size=(1000, 700),
            resizable=True,
            text_select=False,
        )
        webview.start(debug=False)
        logging.info("[SYS] 窗口已关闭，正在退出...")
        _shutdown_event.set()

    except Exception:
        logging.error("主进程中发生崩溃:")
        logging.error(traceback.format_exc())
    finally:
        # 确保进程完全退出（清理残留的后台线程）
        os._exit(0)


if __name__ == '__main__':
    main()

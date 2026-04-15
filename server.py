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
        
        self.slouch_machine = EventStateMachine(enter_ms=3000, exit_ms=1000)
        
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
    """手机使用检测 (证据融合版 v3 - 极速响应)"""
    def __init__(self):
        # v3: 极速进入(300ms) + 极速退出(150ms) + 极短冷却(300ms)
        self.machine = EventStateMachine(enter_ms=300, exit_ms=150, cooldown_ms=300)
        self.state = 'normal'
        self.score = 0
        self.duration_ms = 0
    
    def update(self, current_time, has_phone, phone_near_face, gaze_lock, work_context):
        score = 0
        if has_phone: score += 45     # 提高: YOLO 看到手机即高分
        if phone_near_face: score += 35
        if gaze_lock: score += 40
        if work_context: score -= 15  # 降低抑制，避免漏检
        
        self.score = int(np.clip(score, 0, 100))
        # v3: 降低门槛到 40，仅 has_phone(45) 几乎就能触发
        is_triggered = self.score >= 40
        
        is_confirmed = self.machine.update(is_triggered, current_time)
        self.state = 'using' if is_confirmed else 'normal'
        self.duration_ms = self.machine.duration_ms
        return is_confirmed


class AttentionDetector:
    """分心检测 (广角迟滞版 - 增强响应速度)"""
    def __init__(self):
        # 【增强】进入延迟缩短到 0.6s，大幅提升响应灵敏度
        self.machine = EventStateMachine(enter_ms=600, exit_ms=400, cooldown_ms=1000)
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
        # 极简修改：人脸 2 秒钟没有出现就离席；人脸只要出现，瞬间返回
        self.machine = EventStateMachine(enter_ms=2000, exit_ms=0)
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

# 姿态连线定义 (肩膀、手肘、手腕)
SKELETON_CONNECTIONS = [
    (5, 6, (0, 255, 255)),   # 肩膀连线 (青色)
    (5, 7, (0, 165, 255)),   # 左大臂 (橙色)
    (7, 9, (0, 165, 255)),   # 左小臂
    (6, 8, (255, 165, 0)),   # 右大臂 (蓝色)
    (8, 10, (255, 165, 0)),  # 右小臂
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

def draw_tech_box(img, x1, y1, w, h, color, label, score=None):
    """【增强版】绘制显眼的物体框，并带有中文标签和置信度"""
    x2, y2 = x1 + w, y1 + h
    # 绘制半透明边框
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)
    # 绘制四角加重 (L型)
    l = int(min(w, h) * 0.15)
    cv2.line(img, (x1, y1), (x1+l, y1), color, 2)
    cv2.line(img, (x1, y1), (x1, y1+l), color, 2)
    cv2.line(img, (x2, y1), (x2-l, y1), color, 2)
    cv2.line(img, (x2, y1), (x2, y1+l), color, 2)
    cv2.line(img, (x1, y2), (x1+l, y2), color, 2)
    cv2.line(img, (x1, y2), (x1, y2-l), color, 2)
    cv2.line(img, (x2, y2), (x2-l, y2), color, 2)
    cv2.line(img, (x2, y2), (x2, y2-l), color, 2)

    # 绘制标签背景
    txt = f"{label} {score if score else ''}"
    img[:] = draw_chinese_text(img, txt, (x1, y1 - 22 if y1 > 25 else y1 + 5), font_size=14, color=(255, 255, 255), bg_color=color)

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
    """【简约化】移除人脸大框，仅在眉心或眼睛附近显示微小标记"""
    if not face_center: return
    cx, cy = int(face_center[0]), int(face_center[1])
    color = (150, 150, 150)
    # 仅在中心画一个小十字准星（极细）
    cv2.line(img, (cx-3, cy), (cx+3, cy), color, 1)
    cv2.line(img, (cx, cy-3), (cx, cy+3), color, 1)
    
    # 侧边仅显示极小的数值提示
    draw_chinese_text(img, f"POS:{int(state.yaw)},{int(state.pitch)}", (cx + 10, cy - 10), 10, (120, 120, 120))

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
            # 绘制完美水平参考虚线
            cv2.line(img, (lx - 20, mid_y), (rx + 20, mid_y), (150, 150, 150), 1)
            # 绘制当前肩膀连线
            tilt_color = (80, 180, 80) if abs(ly - ry) < (h * 0.03) else (80, 120, 175)
            if abs(ly - ry) > (h * 0.06): tilt_color = (80, 80, 160)
            cv2.line(img, (lx, ly), (rx, ry), tilt_color, 2)
            # 绘制落差指示
            cv2.line(img, (lx, ly), (lx, mid_y), tilt_color, 1)
            cv2.line(img, (rx, ry), (rx, mid_y), tilt_color, 1)

    # 1.2 【新增】重心垂直引导线 (Spine Axis)
    if 0 in pose_map and (5 in pose_map or 6 in pose_map):
        nose = pose_map[0]
        if nose.get('z', 0) > 0.3:
            nx, ny = int(nose['x']*w), int(nose['y']*h)
            # 从鼻子垂直向下画一条细线，辅助观察身体偏转
            cv2.line(img, (nx, ny), (nx, h), (200, 200, 200), 1, cv2.LINE_AA)

    # 2. 绘制核心关键点
    for idx, pt in pose_map.items():
        # 放宽显示过滤 (Task 2): 只要坐标在大致合理范围内就渲染，减少 z 的强限制
        # z > 0.05 是为了过滤掉完全没有置信度的异常点
        if pt.get('z', 0) > 0.05:
            cx, cy = int(pt['x'] * w), int(pt['y'] * h)
            # 不同部位不同颜色
            color = (255, 255, 255)
            if idx == 0: color = (0, 255, 255) # 鼻子-黄
            elif idx in [5,6]: color = (0, 165, 255) # 肩膀-橙
            elif idx in [9,10]: color = (255, 0, 255) # 手腕-紫
            cv2.circle(img, (cx, cy), 4, color, -1)
            cv2.circle(img, (cx, cy), 5, (0,0,0), 1)

# ═══════════════════════════════════════════════════════════════
#  环境模式和运行信息
# ═══════════════════════════════════════════════════════════════
# 运行元信息：模型引擎标签
APP_VERSION = "2.0"
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
SENS_MULTIPLIERS = [1.0, 1.3, 1.3, 1.3]
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
                    # 瞬时状态判断
                    detect_up = False
                    if pose_map.get(9) and pose_map.get(10):
                        if pose_map[9]['y'] < 0.7 or pose_map[10]['y'] < 0.7:
                            detect_up = True
                    
                    # 消抖处理 (基于 5 帧积算)
                    with state.lock:
                        # 安全检查：确保变量存在（防御旧进程残留）
                        if not hasattr(state, 'arm_up_frames'): state.arm_up_frames = 0
                        
                        if detect_up:
                            state.arm_up_frames = min(state.arm_up_frames + 1, 10)
                        else:
                            state.arm_up_frames = max(state.arm_up_frames - 1, 0)
                        # 达到 5 帧才正式判定为抬起
                        is_arm_up = state.arm_up_frames >= 5
                        
                        # 【新增】：手腕与桌面物品(键盘/书籍)的交互判定，防止低头看书误判离开
                        hands_visible = False
                        left_wrist = pose_map.get(9)
                        right_wrist = pose_map.get(10)
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
                                    if std_y < 4.0 and std_p < 4.0:
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
                        
                        draw_tech_box(display_frame, int(bx), int(by), int(bw), int(bh), color, box_label, score)
                    else:
                        # B. 非业务目标物体 (RAW 模式可视化)
                        # 使用灰色 Box，仅用于协助用户排查模型“看错”了什么
                        draw_tech_box(display_frame, int(bx), int(by), int(bw), int(bh), (150, 150, 150), display_label, score)

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
                    
                # 3. 手机判定证据
                phone_near_face = False
                gaze_in_phone = False
                if state.signals.has_phone and nose_px:
                    for det in last_yolo_detections:
                        if det['class'] in PHONE_CLASSES:
                            bx, by, bw, bh = det['bbox']
                            dist = np.hypot(bx + bw*0.5 - nose_px[0], by + bh*0.5 - nose_px[1])
                            # 同步修正判定距离至 50% 帧宽
                            if dist < (w * 0.50): phone_near_face = True
                            # 修正：增加 gaze_x >= 0 检查，防止人脸丢失时的边界误报
                            if gaze_x >= 0 and (bx-40) <= gaze_x <= (bx+bw+40) and (by-40) <= gaze_y <= (by+bh+40): 
                                gaze_in_phone = True
                            break
                    
                state.metrics['phone'].update(now, state.signals.has_phone, phone_near_face, gaze_in_phone, (has_keyboard or has_book))
                    
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
                if _fhy > 0:  # 已做四角校准
                    _eff_yaw_t = _fhy * SENS_MULTIPLIERS[SENS_IDX] + SENS_BONUS_YAW[SENS_IDX]
                    _eff_pitch_t = _fhp * SENS_MULTIPLIERS[SENS_IDX]
                else:
                    _eff_yaw_t = YAW_T
                    _eff_pitch_t = PITCH_T
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
async def ws_handler(websocket, path=""):
    """双向通信处理器：不仅推理状态，还接收前端控制指令"""
    try:
        logging.info(f"[WS-CONN] ⚡ 收到新的 WebSocket 连接请求: {websocket.remote_address}")
    
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
                    list_type   TEXT    NOT NULL CHECK(list_type IN ('white','black')),
                    proc_pattern TEXT   DEFAULT NULL,
                    UNIQUE(keyword, list_type)
                );
                CREATE INDEX IF NOT EXISTS idx_activity_date
                    ON activity_records(date_key);
            """)
            # Schema migration: add rule_type column if missing
            try:
                c.execute("ALTER TABLE keywords ADD COLUMN rule_type TEXT NOT NULL DEFAULT 'title'")
            except Exception:
                pass  # column already exists
            defaults = [
                ('代码', 'white', None, 'title'), ('code', 'white', None, 'title'),
                ('文档', 'white', None, 'title'), ('设计', 'white', None, 'title'),
                ('会议', 'white', None, 'title'), ('学习', 'white', None, 'title'),
                ('winword', 'white', None, 'proc'), ('excel', 'white', None, 'proc'),
                ('powerpnt', 'white', None, 'proc'), ('code', 'white', 'Code.exe', 'proc'),
                ('微信', 'black', 'WeChat.exe', 'proc'), ('游戏', 'black', None, 'title'),
                ('抖音', 'black', None, 'title'), ('bilibili', 'black', None, 'title'),
                ('YouTube', 'black', None, 'title'), ('娱乐', 'black', None, 'title'),
            ]
            c.executemany(
                "INSERT OR IGNORE INTO keywords(keyword,list_type,proc_pattern,rule_type) VALUES(?,?,?,?)",
                defaults
            )
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
        rows = self._conn().execute("SELECT keyword, list_type, rule_type FROM keywords").fetchall()
        return {
            'white': [{'keyword': r['keyword'], 'rule_type': r['rule_type'] or 'title'} for r in rows if r['list_type'] == 'white'],
            'black': [{'keyword': r['keyword'], 'rule_type': r['rule_type'] or 'title'} for r in rows if r['list_type'] == 'black'],
        }

    def get_keywords_full(self):
        rows = self._conn().execute(
            "SELECT id, keyword, list_type, proc_pattern, rule_type FROM keywords ORDER BY list_type, rule_type, keyword"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_keyword_by_id(self, kw_id):
        self._conn().execute("DELETE FROM keywords WHERE id=?", (kw_id,))
        self._conn().commit()

    def get_today_top10(self):
        """今日使用前10，非浏览器按进程合并，浏览器按标题合并"""
        from datetime import datetime as _dt
        dk = date.today().isoformat()
        rows = self._conn().execute("""
            SELECT proc_name, title, duration, state
              FROM activity_records
             WHERE date_key = ?
        """, (dk,)).fetchall()
        agg = {}
        for row in rows:
            proc = row['proc_name']
            title = row['title'] or ''
            is_browser = proc.lower() in {b.lower() for b in _BROWSER_PROCS}
            if is_browser:
                key = title.strip()
            else:
                key = re.sub(r'\.exe$', '', proc, flags=re.IGNORECASE).strip()
            if not key:
                continue
            if key not in agg:
                agg[key] = {'total': 0, 'focus': 0, 'distracted': 0, 'unknown': 0, 'is_browser': is_browser}
            agg[key]['total'] += row['duration']
            st = row['state'] if row['state'] in ('focus', 'distracted', 'unknown') else 'unknown'
            agg[key][st] = agg[key].get(st, 0) + row['duration']
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
        return [dict(r) for r in rows]

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


class WindowStateClassifier:
    """根据白/黑名单和超时阈值判定窗口状态"""

    DISTRACTION_TIMEOUT = 120  # 停留超过 2 分钟且未匹配规则视为走神

    def __init__(self, db: WindowActivityDB):
        self._db = db

    def classify(self, proc_name, title, stay_seconds=0):
        """返回 'focus' | 'distracted' | 'unknown'"""
        kw = self._db.get_keywords()
        proc_lower = proc_name.lower()
        title_lower = title.lower()
        combined = f"{proc_lower} {title_lower}"
        is_browser = proc_lower in {b.lower() for b in _BROWSER_PROCS}
        for item in kw['white']:
            w = item['keyword'].lower()
            if item['rule_type'] == 'proc':
                if w in proc_lower:
                    return 'focus'
            else:  # title rule
                pp = (item.get('proc_pattern') or '').lower()
                if pp == '__browser__' and not is_browser:
                    continue
                if w in combined:
                    return 'focus'
        for item in kw['black']:
            b = item['keyword'].lower()
            if item['rule_type'] == 'proc':
                if b in proc_lower:
                    return 'distracted'
            else:
                pp = (item.get('proc_pattern') or '').lower()
                if pp == '__browser__' and not is_browser:
                    continue
                if b in combined:
                    return 'distracted'
        if stay_seconds >= self.DISTRACTION_TIMEOUT:
            return 'distracted'
        return 'unknown'


class WinActivityState:
    """当前窗口活动共享状态（线程安全）"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = ''
        self.title = ''
        self.state = 'unknown'
        self.start_time = time.time()
        self.current_record_id = None
        self._recent = deque(maxlen=4)  # last 4 distinct windows

    def to_dict(self):
        with self.lock:
            stay = int(time.time() - self.start_time) if self.title else 0
            recent = list(self._recent)
            return {
                'proc': self.proc,
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

        def on_window_change(proc_name, title):
            now = time.time()
            with _win_activity.lock:
                # 结束上一个窗口记录
                if _win_activity.title:
                    duration = now - _win_activity.start_time
                    if _win_activity.current_record_id is None:
                        _win_activity.current_record_id = _win_db.insert_record(
                            _win_activity.proc, _win_activity.title,
                            _win_activity.start_time, duration, _win_activity.state
                        )
                    else:
                        _win_db.update_record(_win_activity.current_record_id, duration=duration)

                # 开始新窗口
                _win_activity.proc = proc_name
                _win_activity.title = title
                _win_activity.start_time = now
                _win_activity.current_record_id = None
                _win_activity.state = _win_clf.classify(proc_name, title, stay_seconds=0)
                # Track recent windows for multi-monitor display
                key = (proc_name.lower(), title[:40].lower())
                if not _win_activity._recent or (_win_activity._recent[-1]['proc'] != proc_name or _win_activity._recent[-1]['title'] != title):
                    _win_activity._recent.append({
                        'proc': proc_name,
                        'title': title[:45],
                        'state': _win_activity.state,
                        'ts': now,
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
                        # 持续更新当前记录 duration
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
        if row:
            keyword = row['title'][:8].strip()
            list_type = 'white' if state == 'focus' else 'black'
            if keyword:
                _win_db.add_keyword(keyword, list_type, row['proc_name'])
        # 同步更新当前活动状态
        with _win_activity.lock:
            if _win_activity.title and _win_db:
                stay = time.time() - _win_activity.start_time
                _win_activity.state = _win_clf.classify(
                    _win_activity.proc, _win_activity.title, stay_seconds=stay
                )
        return json.dumps({'success': True}), 200, {'Content-Type': 'application/json; charset=utf-8'}
    except Exception as e:
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json; charset=utf-8'}

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

"""
DoNotPlay 视觉处理后端
=====================
架构：Python (YOLO + MediaPipe) → WebSocket → 浏览器前端

功能：
  1. OpenCV 捕获本地摄像头画面
  2. YOLOv8m (CUDA 加速) 检测手机、键盘、书籍等物体
  3. MediaPipe Holistic (CPU) 获取面部关键点 + 身体骨骼
  4. 计算偏航角、俯仰角、EAR、眨眼、手臂姿态
  5. WebSocket 推送 JSON 数据给前端
  6. MJPEG 视频流给前端显示处理后的画面

依赖安装：
  pip install opencv-python mediapipe websockets ultralytics flask flask-cors numpy
  # 确保已安装 CUDA 版本的 PyTorch:
  # pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
"""

import cv2
import numpy as np
import mediapipe as mp
import asyncio
import websockets
import json
import time
import threading
from flask import Flask, Response
from flask_cors import CORS
from ultralytics import YOLO
from PIL import Image, ImageDraw, ImageFont
import os
import sys
import argparse
import webbrowser
import signal

# ══════════════════════════════════════════
#  资源路径动态获取（兼容 PyInstaller 打包）
# ══════════════════════════════════════════
def get_resource_path(relative_path):
    """获取资源的绝对路径，兼容开发模式和 PyInstaller 打包后的环境"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

# 应用根目录（用于存放 config.json 等可写文件）
# 打包后 _MEIPASS 是临时只读目录，可写文件放在 exe 所在目录
def get_writable_path(relative_path):
    """获取可写文件路径（exe同级目录或脚本所在目录）"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, relative_path)

# ══════════════════════════════════════════
#  全局配置
# ══════════════════════════════════════════
CAMERA_INDEX = 0          # 摄像头索引（0 = 默认摄像头）
WS_PORT = 8765            # WebSocket 端口
MJPEG_PORT = 5001         # MJPEG 视频流端口
YOLO_INTERVAL = 0.4       # YOLO 检测间隔（秒），约 2.5 fps
YOLO_CONF_THRESHOLD = 0.20  # YOLO 最低置信度

# 我们关心的 COCO 类别 ID → 自定义标签
# COCO: 67=cell phone, 65=remote, 73=book, 66=keyboard, 63=laptop
YOLO_TARGET_CLASSES = {
    67: 'cell phone',
    65: 'remote',
    73: 'book',
    66: 'keyboard',
    63: 'laptop',
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

# 绘制颜色映射
BBOX_COLORS = {
    'cell phone': (68, 68, 255),     # 红色 (BGR)
    'remote':     (68, 68, 255),
    'book':       (102, 204, 68),    # 绿色
    'keyboard':   (0, 153, 255),     # 橙色
    'laptop':     (0, 153, 255),
}
BBOX_LABELS = {
    'cell phone': '手机',
    'remote':     '手机',
    'book':       '书籍',
    'keyboard':   '键盘',
    'laptop':     '电脑',
}

# ══════════════════════════════════════════
#  全局配置持久化
# ══════════════════════════════════════════
CONFIG_FILE = get_writable_path('config.json')
DEFAULT_CONFIG = {
    'sens_idx': 1,
    'camera_index': 0,
    'baseline_yaw': 0.0,
    'baseline_pitch': 0.0,
}

def load_config():
    cfg_path = CONFIG_FILE
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f)
    except: pass

# 初始化加载配置
current_config = load_config()

# 灵敏度预设 (与前端 SENS_PRESETS 对应)
SENS_TABLE = [
    {'yaw': 20, 'pitch': 18, 'delay': 5000},
    {'yaw': 35, 'pitch': 30, 'delay': 8000},
    {'yaw': 55, 'pitch': 40, 'delay': 10000},
    {'yaw': 75, 'pitch': 50, 'delay': 12000},
]

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
        self.is_calibrating = False
        self.calibration_samples = []
        
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
        self.shoulder_diff = 0.0
        self.posture_ratio = 0.0
        # 判定逻辑
        self.is_looking_down = False
        self.phone_gaze_start = 0
        self.phone_alert_active = False
        self.trigger_reason = ''
        # 眨眼追踪
        self.eye_open = True
        self.last_blink_time = 0
        self.blink_timestamps = []
        # MJPEG 帧
        self.jpeg_frame = None

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
    """在独立线程中运行 YOLO 物体检测（GPU 加速）"""

    def __init__(self, model_path='yolov8m.pt'):
        print("[YOLO] 正在加载模型...")
        self.model = YOLO(model_path)
        # 强制使用 CUDA
        self.model.to('cuda')
        print(f"[YOLO] 模型已加载，设备: {self.model.device}")

    def detect(self, frame):
        """对一帧执行检测，返回过滤后的结果列表"""
        results = self.model(frame, verbose=False, conf=YOLO_CONF_THRESHOLD)
        detections = []

        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                if cls_id in YOLO_TARGET_CLASSES:
                    label = YOLO_TARGET_CLASSES[cls_id]
                    conf = float(box.conf[0])
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    detections.append({
                        'class': label,
                        'score': round(conf, 2),
                        'bbox': [float(x1), float(y1), float(x2 - x1), float(y2 - y1)],
                    })
        return detections

# ══════════════════════════════════════════
#  主视觉处理循环
# ══════════════════════════════════════════
def vision_loop():
    """主循环：读取摄像头 → MediaPipe + YOLO → 更新 state"""
    global state

    # —— 从配置中获取最新的摄像头索引 ——
    cam_idx = current_config.get('camera_index', 0)
    print(f"[CAM] 正在尝试打开摄像头 {cam_idx}...")
    
    cap = cv2.VideoCapture(cam_idx)
    if not cap.isOpened():
        print(f"[CAM] 错误：无法打开索引为 {cam_idx} 的摄像头！正在尝试默认值 0...")
        cap = cv2.VideoCapture(0)
    
    if not cap.isOpened():
        print("[CAM] 致命错误：所有摄像头设备均不可用。")
        return
        
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[CAM] 摄像头配置成功：{w}x{h}")

    # —— 初始化 MediaPipe Holistic ——
    print("[MP] 正在初始化 MediaPipe Holistic...")
    mp_holistic = mp.solutions.holistic
    holistic = mp_holistic.Holistic(
        static_image_mode=False,
        model_complexity=1,
        smooth_landmarks=True,
        enable_segmentation=False,
        refine_face_landmarks=False,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    print("[MP] MediaPipe Holistic 已就绪")

    # —— 初始化 YOLO ——
    try:
        yolo = YOLODetector(get_resource_path('yolov8m.pt'))
    except Exception as e:
        print(f"[YOLO] 严重错误：无法加载模型，后端将仅运行 MediaPipe。 详情: {e}")
        yolo = None

    last_yolo_time = 0
    last_yolo_detections = []

    BLINK_EAR = 0.22
    BLINK_COOLDOWN = 0.35  # 秒

    fps_counter = 0
    fps_timer = time.time()

    print("\n" + "=" * 50)
    print("  DoNotPlay 视觉后端已启动！")
    print(f"  WebSocket: ws://localhost:{WS_PORT}")
    print(f"  视频流:    http://localhost:{MJPEG_PORT}/video_feed")
    print("=" * 50 + "\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            continue

        now = time.time()
        
        # —— 方向修正：顺时针旋转 270 度（等同于逆时针 90 度） + 镜像翻转 ——
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        frame = cv2.flip(frame, 1)
        
        display_frame = frame.copy()
        # 重新获取旋转后的宽高
        h, w = frame.shape[:2]

        # ════════════════════════════════════
        #  1. MediaPipe Holistic 处理
        # ════════════════════════════════════
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_results = holistic.process(rgb)

        face_detected = False
        yaw_val = 0.0
        pitch_val = 0.0
        ear_val = 0.0
        nose_y_val = 0.0
        face_h_val = 0.0
        hands_visible = False
        is_arm_up = False
        lwrist_y = 1.0
        rwrist_y = 1.0
        ls_y = 0.0
        rs_y = 0.0
        neck_length_val = 0.0
        shoulder_diff_val = 0.0

        if mp_results.face_landmarks:
            face_detected = True
            lm = mp_results.face_landmarks.landmark

            # 头部姿态
            yaw_val, pitch_val, nose_y_val, face_h_val = head_pose(lm)

            with state.lock:
                if state.is_calibrating:
                    state.calibration_samples.append((yaw_val, pitch_val))
                    if len(state.calibration_samples) >= 15:
                        state.baseline_yaw = sum(s[0] for s in state.calibration_samples) / 15.0
                        state.baseline_pitch = sum(s[1] for s in state.calibration_samples) / 15.0
                        state.is_calibrating = False
                        current_config['baseline_yaw'] = state.baseline_yaw
                        current_config['baseline_pitch'] = state.baseline_pitch
                        save_config(current_config)
                        print(f"[CAM] 校准完成: baseline_yaw={state.baseline_yaw:.1f}, baseline_pitch={state.baseline_pitch:.1f}")

                adj_yaw = yaw_val - state.baseline_yaw
                adj_pitch = pitch_val - state.baseline_pitch

            # EAR 眨眼
            le_ear = eye_aspect_ratio(lm, LE_IDX)
            re_ear = eye_aspect_ratio(lm, RE_IDX)
            ear_val = (le_ear + re_ear) / 2.0
            mar_val = mouth_aspect_ratio(lm)

            # 眨眼检测
            with state.lock:
                if ear_val < BLINK_EAR and state.eye_open:
                    state.eye_open = False
                elif ear_val >= BLINK_EAR and not state.eye_open:
                    state.eye_open = True
                    if now - state.last_blink_time > BLINK_COOLDOWN:
                        state.blinks += 1
                        state.last_blink_time = now
                        state.blink_timestamps.append(now)

                # 清理 60 秒前的眨眼记录
                state.blink_timestamps = [t for t in state.blink_timestamps if now - t < 60]
                blink_rate = len(state.blink_timestamps)

            # 在画面上绘制面部关键点
            for idx in LE_IDX + RE_IDX + [NOSE]:
                pt = lm[idx]
                cx, cy = int(pt.x * w), int(pt.y * h)
                cv2.circle(display_frame, (cx, cy), 2, (39, 159, 239), -1)

        # 身体骨骼 — 手臂检测
        if mp_results.pose_landmarks:
            pose = mp_results.pose_landmarks.landmark
            # 15 = 左手腕, 16 = 右手腕, 11 = 左肩, 12 = 右肩, 13 = 左肘, 14 = 右肘
            if pose[15].visibility > 0.5:
                lwrist_y = pose[15].y
                hands_visible = True
            if pose[16].visibility > 0.5:
                rwrist_y = pose[16].y
                hands_visible = True

            if lwrist_y < 0.7 or rwrist_y < 0.7:
                is_arm_up = True

            # 计算脖子长度 (颈部基准线)
            if pose[11].visibility > 0.5 and pose[12].visibility > 0.5:
                ls_y = pose[11].y
                rs_y = pose[12].y
                if pose[0].visibility > 0.5:
                    neck_length_val = (ls_y + rs_y) / 2.0 - pose[0].y
                if face_h_val > 0.001:
                    shoulder_diff_val = abs(ls_y - rs_y) / face_h_val

            # 绘制手臂骨骼线
            arm_pairs = [(11, 13, 15), (12, 14, 16)]
            for (shoulder, elbow, wrist) in arm_pairs:
                ps, pe, pw = pose[shoulder], pose[elbow], pose[wrist]
                if ps.visibility > 0.5 and pe.visibility > 0.5 and pw.visibility > 0.5:
                    pts_arm = [
                        (int(ps.x * w), int(ps.y * h)),
                        (int(pe.x * w), int(pe.y * h)),
                        (int(pw.x * w), int(pw.y * h)),
                    ]
                    cv2.line(display_frame, pts_arm[0], pts_arm[1], (213, 155, 91), 3)
                    cv2.line(display_frame, pts_arm[1], pts_arm[2], (213, 155, 91), 3)
                    cv2.circle(display_frame, pts_arm[2], 6, (0, 153, 255), -1)

        # ════════════════════════════════════
        #  2. YOLO 物体检测（节流）
        # ════════════════════════════════════
        if yolo and (now - last_yolo_time > YOLO_INTERVAL):
            last_yolo_time = now
            try:
                last_yolo_detections = yolo.detect(frame)
            except Exception as e:
                print(f"[YOLO] 检测线程保护：{e}")
                last_yolo_detections = []

        # 在画面上绘制 YOLO 检测框
        for det in last_yolo_detections:
            cls_name = det['class']
            score = det['score']
            bx, by, bw, bh = det['bbox']
            color = BBOX_COLORS.get(cls_name, (255, 255, 255))
            label_text = BBOX_LABELS.get(cls_name, cls_name)

            x1, y1 = int(bx), int(by)
            x2, y2 = int(bx + bw), int(by + bh)

            # 半透明填充
            overlay = display_frame.copy()
            cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            cv2.addWeighted(overlay, 0.15, display_frame, 0.85, 0, display_frame)

            # 边框
            cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)

            # 标签
            label_str = f"{label_text} {int(score * 100)}%"
            display_frame = draw_text_chinese(display_frame, label_str, (x1, y1 - 25), 18, (255, 255, 255))

        # ════════════════════════════════════
        #  3. 核心判定逻辑
        # ════════════════════════════════════
        if not face_detected:
            adj_yaw = 0.0
            adj_pitch = 0.0

        has_phone = any(d['class'] in PHONE_CLASSES for d in last_yolo_detections)
        has_keyboard = any(d['class'] in WORK_CLASSES and d['class'] != 'book' for d in last_yolo_detections)
        has_book = any(d['class'] == 'book' for d in last_yolo_detections)
        is_looking_down = adj_pitch > 8 and abs(adj_yaw) < 45

        phone_cx, phone_cy = -1, -1
        if has_phone:
            for det in last_yolo_detections:
                if det['class'] in PHONE_CLASSES and det.get('score', 1.0) > 0.6:
                    bx, by, bw, bh = det['bbox']
                    phone_cx, phone_cy = bx + bw / 2, by + bh / 2
                    break
        
        hand_phone_dist = 99999
        if phone_cx != -1 and mp_results.pose_landmarks:
            pose = mp_results.pose_landmarks.landmark
            if pose[15].visibility > 0.5:
                hand_phone_dist = min(hand_phone_dist, np.hypot(pose[15].x * w - phone_cx, pose[15].y * h - phone_cy))
            if pose[16].visibility > 0.5:
                hand_phone_dist = min(hand_phone_dist, np.hypot(pose[16].x * w - phone_cx, pose[16].y * h - phone_cy))

        trigger_a = phone_cx != -1 and face_h_val > 0.001 and hand_phone_dist < face_h_val * h * 2.0
        trigger_b = phone_cx != -1 and adj_pitch > 15 and phone_cy > h / 2
        is_phone_violation = trigger_a or trigger_b

        posture_ratio = 0.0
        if face_h_val > 0.001 and neck_length_val > 0:
            posture_ratio = round(neck_length_val / face_h_val, 4)

        with state.lock:
            state.posture_ratio = posture_ratio
            if is_phone_violation:
                if state.phone_gaze_start == 0:
                    state.phone_gaze_start = now
                if now - state.phone_gaze_start > 0.6:
                    if not state.phone_alert_active:
                        state.phone_alert_active = True
                        state.trigger_reason = '检测到手机使用'
                    computed_status = 'phone'
                else:
                    computed_status = 'distracted' if not face_detected else ('active' if abs(adj_yaw) < YAW_T * 1.5 and abs(adj_pitch) < PITCH_T else 'distracted')
            else:
                state.phone_gaze_start = 0
                state.phone_alert_active = False

                if not face_detected:
                    computed_status = 'away'
                elif abs(adj_yaw) < YAW_T * 1.5 and abs(adj_pitch) < PITCH_T:
                    computed_status = 'active'
                else:
                    if (has_keyboard or has_book) and 5 < adj_pitch < PITCH_T + 25 and abs(adj_yaw) < YAW_T * 1.5 + 10:
                        computed_status = 'active'
                    else:
                        computed_status = 'distracted'

            if state.phone_alert_active:
                computed_status = 'phone'

            state.status = computed_status
            state.yaw = round(adj_yaw, 1) if face_detected else 0.0
            state.pitch = round(adj_pitch, 1) if face_detected else 0.0
            state.ear = round(ear_val * 100, 0)
            state.mar = round(mar_val, 4)
            state.blink_rate = blink_rate if face_detected else 0
            state.nose_y = round(nose_y_val, 3)
            state.face_h = round(face_h_val, 3)
            state.face_detected = face_detected
            state.hands_visible = hands_visible
            state.detected_objects = last_yolo_detections
            state.has_phone = has_phone
            state.is_arm_up = is_arm_up
            state.left_shoulder_y = round(ls_y, 4)
            state.right_shoulder_y = round(rs_y, 4)
            state.neck_length = round(neck_length_val, 4)
            state.shoulder_diff = round(shoulder_diff_val, 4)
            state.is_looking_down = is_looking_down

        # ════════════════════════════════════
        #  4. 状态 HUD 叠加到画面上
        # ════════════════════════════════════
        status_color = {
            'active': (0, 200, 0),
            'distracted': (0, 165, 255),
            'phone': (0, 0, 255),
            'away': (128, 128, 128),
            'init': (200, 200, 200),
        }.get(computed_status, (255, 255, 255))

        status_text = {
            'active': '专注中',
            'distracted': '分心',
            'phone': '玩手机！',
            'away': '离开',
            'init': '初始化',
        }.get(computed_status, computed_status)

        display_frame = draw_text_chinese(display_frame, status_text, (10, 5), 28, status_color)
        display_frame = draw_text_chinese(display_frame, f"Yaw:{yaw_val:.1f} Pitch:{pitch_val:.1f}", (10, 45), 16, (200, 200, 200))
        
        if is_arm_up:
            display_frame = draw_text_chinese(display_frame, "[手臂抬起]", (10, 70), 16, (0, 165, 255))

        # FPS 计算
        fps_counter += 1
        if now - fps_timer > 1.0:
            fps = fps_counter / (now - fps_timer)
            fps_timer = now
            display_frame = draw_text_chinese(display_frame, f"FPS: {fps:.0f}", (w - 100, 10), 16, (200, 200, 200))

        # ════════════════════════════════════
        #  5. 编码为 JPEG 用于 MJPEG 推流
        # ════════════════════════════════════
        _, jpeg = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with state.lock:
            state.jpeg_frame = jpeg.tobytes()

        # 控制帧率约 30fps
        time.sleep(max(0, 1.0 / 30.0 - (time.time() - now)))

    cap.release()

# ══════════════════════════════════════════
#  WebSocket 服务器
# ══════════════════════════════════════════
async def ws_handler(websocket, path=None):
    """双向通信处理器：不仅推理状态，还接收前端控制指令"""
    print(f"[WS] 客户端已连接: {websocket.remote_address}")
    
    # 建立连接时先同步当前配置给前端
    await websocket.send(json.dumps({
        'type': 'config_sync',
        'sens_idx': SENS_IDX
    }))

    async def receive_msgs():
        global SENS_IDX, YAW_T, PITCH_T, STATE_COOLDOWN
        try:
            async for message in websocket:
                data = json.loads(message)
                if data.get('cmd') == 'set_sensitivity':
                    idx = data.get('idx', 1)
                    SENS_IDX = idx
                    # 应用新阈值
                    YAW_T = SENS_TABLE[idx]['yaw']
                    PITCH_T = SENS_TABLE[idx]['pitch']
                    STATE_COOLDOWN = SENS_TABLE[idx]['delay'] / 1000.0
                    # 保存到文件
                    save_config({'sens_idx': SENS_IDX, 'baseline_yaw': current_config.get('baseline_yaw', 0.0), 'baseline_pitch': current_config.get('baseline_pitch', 0.0)})
                    print(f"[WS] 灵敏度切换为: {SENS_TABLE[idx]}")
                elif data.get('action') == 'calibrate' or data.get('cmd') == 'calibrate':
                    with state.lock:
                        state.is_calibrating = True
                        state.calibration_samples = []
                    print("[WS] 收到校准指令，开始采集样本...")
        except: pass

    async def send_updates():
        try:
            while True:
                with state.lock:
                    payload = {
                        'status': state.status,
                        'yaw': state.yaw,
                        'pitch': state.pitch,
                        'ear': state.ear,
                        'mar': state.mar,
                        'blinks': state.blinks,
                        'blinkRate': state.blink_rate,
                        'noseY': state.nose_y,
                        'faceH': state.face_h,
                        'faceDetected': state.face_detected,
                        'handsVisible': state.hands_visible,
                        'hasPhone': state.has_phone,
                        'hasKeyboard': state.has_keyboard,
                        'hasBook': state.has_book,
                        'isArmUp': state.is_arm_up,
                        'isLookingDown': state.is_looking_down,
                        'neckLength': state.neck_length,
                        'postureRatio': state.posture_ratio,
                        'leftShoulderY': state.left_shoulder_y,
                        'rightShoulderY': state.right_shoulder_y,
                        'neck_length': state.neck_length,
                        'face_height': state.face_h,
                        'shoulder_diff': state.shoulder_diff,
                        'mar': state.mar,
                        'phoneAlertActive': state.phone_alert_active,
                        'triggerReason': state.trigger_reason,
                        'detectedObjects': state.detected_objects,
                        'timestamp': time.time(),
                    }
                await websocket.send(json.dumps(payload))
                await asyncio.sleep(0.1)
        except: pass

    # 并发运行接收和发送任务
    await asyncio.gather(receive_msgs(), send_updates())

async def ws_server():
    """启动 WebSocket 服务器"""
    print(f"[WS] WebSocket 服务器启动在 ws://localhost:{WS_PORT}")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await asyncio.Future()  # 永久运行

def run_ws_server():
    """在独立线程中运行 asyncio 事件循环"""
    asyncio.run(ws_server())

# ══════════════════════════════════════════
#  MJPEG 视频流服务器 (Flask)
# ══════════════════════════════════════════
flask_app = Flask(__name__)
CORS(flask_app)

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

def run_flask():
    """在独立线程中运行 Flask"""
    print(f"[MJPEG] 视频流服务器启动在 http://localhost:{MJPEG_PORT}/video_feed")
    flask_app.run(host='0.0.0.0', port=MJPEG_PORT, threaded=True, use_reloader=False)

# ══════════════════════════════════════════
#  全局停止标志（供安全退出使用）
# ══════════════════════════════════════════
_shutdown_event = threading.Event()

# ══════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description='DoNotPlay 视觉后端')
    parser.add_argument('--dev', action='store_true', help='开发模式：仅启动后端服务，在浏览器中打开页面')
    args = parser.parse_args()

    print("╔══════════════════════════════════════╗")
    print("║   DoNotPlay 视觉后端 正在启动...     ║")
    print("╚══════════════════════════════════════╝")

    # 启动 WebSocket 服务器线程
    ws_thread = threading.Thread(target=run_ws_server, daemon=True)
    ws_thread.start()

    # 启动 Flask MJPEG 服务器线程
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 启动视觉处理循环（始终在后台线程）
    vision_thread = threading.Thread(target=vision_loop, daemon=True)
    vision_thread.start()

    if args.dev:
        # ── 开发模式：在默认浏览器中打开页面，方便 F12 调试 ──
        html_path = get_resource_path('app.html')
        url = f'file:///{html_path.replace(os.sep, "/")}'
        print(f"[DEV] 开发模式启动，正在打开浏览器: {url}")
        webbrowser.open(url)
        try:
            while not _shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[SYS] 正在关闭...")
    else:
        # ── 生产模式：使用 pywebview 原生窗口 ──
        try:
            import webview
        except ImportError:
            print("[SYS] 未安装 pywebview，回退到浏览器模式...")
            html_path = get_resource_path('app.html')
            url = f'file:///{html_path.replace(os.sep, "/")}'
            webbrowser.open(url)
            try:
                while not _shutdown_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("\n[SYS] 正在关闭...")
            return

        html_path = get_resource_path('app.html')
        print(f"[SYS] 生产模式启动，加载窗口: {html_path}")
        window = webview.create_window(
            'DoNotPlay - 专注力监测',
            url=html_path,
            width=1400,
            height=900,
            min_size=(1000, 700),
            resizable=True,
            text_select=False,
        )
        # webview.start() 会阻塞主线程直到窗口关闭
        webview.start(debug=False)
        print("[SYS] 窗口已关闭，正在退出...")
        _shutdown_event.set()

    # 确保进程完全退出（清理残留的后台线程）
    os._exit(0)


if __name__ == '__main__':
    main()

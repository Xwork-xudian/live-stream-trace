"""人脸检测 / 关键点后端：YuNet(ONNX, cv2.dnn) 优先，退化 Haar。

为什么需要它（内容级音画同步的可用性瓶颈）：
  带货/商铺直播里，Haar 人脸框经常误检（锁定产品/背景）且不给出关键点，嘴部 ROI 只能
  按「人脸框下半部 62%~95%」的固定比例取，会把下巴/脸颊/画面运动一起圈进来，导致
  「嘴型特异性」判据不可靠、相关性被稀释。YuNet 给出 5 个关键点（右眼/左眼/鼻尖/
  右嘴角/左嘴角），可精确框定嘴唇区域，并支持「正脸度」等几何判据。

零新增依赖：走 cv2.dnn（opencv>=4.5.4 自带 `cv2.FaceDetectorYN_create`），不需要
onnxruntime；模型缺失时自动退化到 Haar，行为与旧版一致。
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
# 兼容两种布局：core/ 子目录（统一版）与扁平根目录（douyin/xiaohongshu 独立版）
_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
_MODEL_NAME_LMK = "2d106det.onnx"          # 106 点关键点（可选；用于唇开合几何信号）
MODEL_CANDIDATES = (_HERE / "models" / _MODEL_NAME, _HERE.parent / "models" / _MODEL_NAME)
LMK_MODEL_CANDIDATES = (_HERE / "models" / _MODEL_NAME_LMK, _HERE.parent / "models" / _MODEL_NAME_LMK)

LMK_INPUT = 192            # 2d106det 输入 192x192
LMK_MOUTH = slice(52, 72)  # 实测（0-based）52..71 = 嘴部 20 点
LMK_CROP_SCALE = 1.6       # 人脸框外扩倍数（方形裁剪）


def resolve_lmk_model_path() -> Optional[Path]:
    """106 点关键点模型路径（可选）。不存在则几何唇信号不可用，功能自动降级。"""
    cands: list[Path] = []
    env = os.getenv("LST_LMK_MODEL")
    if env:
        cands.append(Path(env))
    cands.extend(LMK_MODEL_CANDIDATES)
    for cand in cands:
        try:
            if cand.exists() and cand.stat().st_size > 100_000:
                return cand
        except OSError:
            continue
    return None


def face_square_crop(box_xywh, shape, scale: float = LMK_CROP_SCALE):
    """人脸框 (x,y,w,h) → 保比例方形裁剪窗 (x0, y0, side)（已夹到画面内）。"""
    h, w = shape
    fx, fy, fw, fh = (float(v) for v in box_xywh)
    cx, cy = fx + fw / 2.0, fy + fh / 2.0
    side = max(fw, fh) * float(scale)
    x0 = int(max(0.0, cx - side / 2.0))
    y0 = int(max(0.0, cy - side / 2.0))
    side = int(min(side, w - x0, h - y0))
    if side < 16:
        return None
    return (x0, y0, side)


def aperture_from_points(pts, eye_dist: float):
    """嘴部点集 → (唇开合度, 脸部纵向跨度)。开合度 = 下半唇均值 y − 上半唇均值 y，除以眼距归一化。

    几何量而非纹理量 → 不受美颜磨皮影响（这也是它比重美颜房鲁棒的原因）。
    """
    np = _np()
    if np is None or pts is None or len(pts) < 4:
        return None, None
    ys = np.sort(np.asarray(pts, dtype=np.float64)[:, 1])
    k = len(ys) // 2
    ap = (float(ys[k:].mean()) - float(ys[:k].mean())) / (float(eye_dist) or 1.0)
    return ap, None

# YuNet 每张脸 15 维输出：
# x, y, w, h, 右眼x, 右眼y, 左眼x, 左眼y, 鼻x, 鼻y, 右嘴角x, 右嘴角y, 左嘴角x, 左嘴角y, score
_YUNET_VEC = 15


def resolve_model_path() -> Optional[Path]:
    """YuNet 模型路径：环境变量 LST_FACE_MODEL 优先，其次 models/face_detection_yunet_2023mar.onnx。"""
    cands: list[Path] = []
    env = os.getenv("LST_FACE_MODEL")
    if env:
        cands.append(Path(env))
    cands.extend(MODEL_CANDIDATES)
    for cand in cands:
        try:
            if cand.exists() and cand.stat().st_size > 10_000:
                return cand
        except OSError:
            continue
    return None


class FaceBackend:
    """统一人脸检测后端。`detect_multi` 返回 list[list[face]]，face 见 `_face()`。"""

    def __init__(self, score_threshold: float = 0.6, nms: float = 0.3, top_k: int = 5000):
        self.score_threshold = score_threshold
        self.nms = nms
        self.yunet_top_k = top_k
        self.name = "none"
        self._cv2 = None
        self._yunet = None
        self._has_landmarks = False
        self._cascades: list = []
        self._model_path: Optional[Path] = None
        self._lmk = None
        self._lmk_path: Optional[Path] = None
        self._init()

    # ── 初始化 ──
    def _init(self) -> None:
        try:
            import cv2  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("cv2 不可用，人脸后端禁用: %s", exc)
            return
        self._cv2 = cv2
        self._load_lmk()          # 关键点模型独立于检测后端，先加载（YuNet 分支会提前 return）
        model = resolve_model_path()
        if model is not None and hasattr(cv2, "FaceDetectorYN_create"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    str(model), "", (320, 320), self.score_threshold, self.nms, self.yunet_top_k)
                self._model_path = model
                self.name = "yunet"
                self._has_landmarks = True
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("YuNet 初始化失败，退化 Haar: %s", exc)
        self._load_haar()

    def _load_lmk(self) -> None:
        """尝试加载 106 点关键点模型（可选；缺模型只影响几何唇信号）。"""
        cv2 = self._cv2
        if cv2 is None or not hasattr(cv2.dnn, "readNetFromONNX"):
            return
        path = resolve_lmk_model_path()
        if path is None:
            return
        try:
            self._lmk = cv2.dnn.readNetFromONNX(str(path))
            self._lmk_path = path
        except Exception as exc:  # noqa: BLE001
            logger.warning("关键点模型加载失败（几何唇信号不可用）: %s", exc)
            self._lmk = None

    @property
    def has_lip_landmarks(self) -> bool:
        return self._lmk is not None

    def aperture_series(self, color_frames, faces_multi):
        """彩色帧 (N,H,W,3) + 逐帧人脸 → (ap, ctrl, valid)。ap=唇开合/眼距，ctrl=脸部纵向跨度/眼距。"""
        np = _np()
        n = int(color_frames.shape[0]) if color_frames is not None else 0
        if np is None or not self.has_lip_landmarks or n == 0:
            return None, None, None
        cv2 = self._cv2
        h, w = color_frames.shape[1], color_frames.shape[2]
        ap = np.full(n, np.nan, dtype=np.float32)
        ctrl = np.full(n, np.nan, dtype=np.float32)
        for i in range(n):
            fl = faces_multi[i] if i < len(faces_multi) else []
            if not fl:
                continue
            f = fl[0]
            crop = face_square_crop(f["box"], (h, w))
            if crop is None:
                continue
            x0, y0, side = crop
            sq = color_frames[i][y0:y0 + side, x0:x0 + side]
            blob = cv2.dnn.blobFromImage(cv2.resize(sq, (LMK_INPUT, LMK_INPUT)),
                                         1.0 / 128.0, (LMK_INPUT, LMK_INPUT),
                                         (127.5, 127.5, 127.5), swapRB=True)
            try:
                self._lmk.setInput(blob)
                pts = (self._lmk.forward().reshape(-1, 2) + 1.0) * (LMK_INPUT / 2.0)
            except Exception:  # noqa: BLE001
                continue
            lm5 = f.get("lm")
            eye_d = 1.0
            if lm5 and len(lm5) >= 2:
                eye_d = float(np.hypot(lm5[0][0] - lm5[1][0], lm5[0][1] - lm5[1][1])) or 1.0
            a, _ = aperture_from_points(pts[LMK_MOUTH], eye_d)
            if a is not None:
                ap[i] = a
            # 对照：眼/眉带的纵向跨度（随头部姿态/景别变化，但**不随嘴部开合**）。
            # 不用「整脸跨度」——它会随说话时的点头一起变，导致对照也高、特异性判据失效。
            if len(pts) >= 8:
                yy = pts[:, 1]
                y_lo, y_hi = float(yy.min()), float(yy.max())
                span = max(1.0, y_hi - y_lo)
                band = pts[(yy > y_lo + 0.10 * span) & (yy < y_lo + 0.42 * span)]
                if len(band) >= 4:
                    by = np.sort(band[:, 1])
                    kk = max(1, len(by) // 5)
                    ctrl[i] = (float(by[-kk:].mean()) - float(by[:kk].mean())) / eye_d
        if not np.isfinite(ap).any():
            return None, None, None
        return ap, ctrl, np.isfinite(ap)

    def _load_haar(self) -> None:
        cv2 = self._cv2
        try:
            import cv2.data  # noqa: PLC0415
            root = Path(cv2.data.haarcascades)
        except Exception as exc:  # noqa: BLE001
            logger.warning("无法定位 Haar 级联目录: %s", exc)
            return
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml",
                     "haarcascade_profileface.xml"):
            path = root / name
            if not path.exists():
                continue
            try:
                c = cv2.CascadeClassifier(str(path))
                if not c.empty():
                    self._cascades.append(c)
            except Exception:  # noqa: BLE001
                continue
        self.name = "haar" if self._cascades else "none"

    # ── 属性 ──
    @property
    def available(self) -> bool:
        return self.name != "none"

    @property
    def has_landmarks(self) -> bool:
        return self._has_landmarks

    @property
    def model_path(self) -> Optional[str]:
        return str(self._model_path) if self._model_path else None

    # ── 检测 ──
    def detect_multi(self, frames, top_k: int = 2) -> list:
        """gray 帧 (N,H,W) → list[list[face_dict]]（按面积降序，最多 top_k 张）。"""
        n = int(frames.shape[0])
        if not self.available:
            return [[] for _ in range(n)]
        if self.name == "yunet":
            return self._detect_yunet(frames, top_k)
        return self._detect_haar(frames, top_k)

    def _detect_yunet(self, frames, top_k: int) -> list:
        cv2 = self._cv2
        n, h, w = frames.shape
        self._yunet.setInputSize((w, h))
        out: list = []
        for i in range(n):
            f = frames[i]
            bgr = f if f.ndim == 3 else cv2.cvtColor(f, cv2.COLOR_GRAY2BGR)
            try:
                _, faces = self._yunet.detect(bgr)
            except Exception:  # noqa: BLE001
                faces = None
            rows = [] if faces is None else [r for r in faces.tolist() if len(r) >= _YUNET_VEC]
            rows.sort(key=lambda r: r[2] * r[3], reverse=True)
            faces_k = []
            for r in rows[:top_k]:
                lm = [(r[4], r[5]), (r[6], r[7]), (r[8], r[9]), (r[10], r[11]), (r[12], r[13])]
                faces_k.append(_face(r[0], r[1], r[2], r[3], score=float(r[14]),
                                     lm=lm, source="yunet"))
            out.append(faces_k)
        return out

    def _detect_haar(self, frames, top_k: int) -> list:
        cv2 = self._cv2
        n, h, w = frames.shape
        params = [
            (1.10, 5, (max(12, int(w * 0.10)), max(12, int(h * 0.10)))),
            (1.05, 4, (max(10, int(w * 0.07)), max(10, int(h * 0.07)))),
        ]
        out: list = []
        for i in range(n):
            found_all: list = []
            for cascade in self._cascades:
                for scale, neighbors, min_size in params:
                    try:
                        found = cascade.detectMultiScale(frames[i], scaleFactor=scale,
                                                         minNeighbors=neighbors, minSize=min_size,
                                                         flags=cv2.CASCADE_SCALE_IMAGE)
                    except Exception:  # noqa: BLE001
                        continue
                    found_all.extend((int(x), int(y), int(bw), int(bh)) for x, y, bw, bh in found)
                    if found_all:
                        break
                if found_all:
                    break
            found_all.sort(key=lambda b: b[2] * b[3], reverse=True)
            out.append([_face(*b, score=None, lm=None, source="haar") for b in found_all[:top_k]])
        return out


def _face(x, y, w, h, *, score, lm, source) -> dict:
    return {"box": (float(x), float(y), float(w), float(h)),
            "score": score, "lm": lm, "source": source}


# ─────────────────────────── 几何 / 质量判据 ───────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def mouth_roi_from_face(face: dict, shape: tuple) -> tuple:
    """人脸（含关键点优先）→ 嘴部 ROI (x0, y0, x1, y1)（缩放后帧坐标，已夹到画面内）。

    有 5 点关键点时：以左右嘴角定宽、鼻尖→嘴中心定高，得到**紧贴嘴唇**的矩形；
    无关键点时：退回「人脸框下半部」固定比例（旧行为）。
    """
    h, w = shape
    lm = face.get("lm")
    if lm and len(lm) >= 5:
        (_rex, _rey), (_lex, _ley), (nx, ny), (rmx, rmy), (lmx, lmy) = lm[:5]
        cx, cy = (rmx + lmx) / 2.0, (rmy + lmy) / 2.0
        mw = max(6.0, abs(lmx - rmx) * 1.30)          # 嘴角距 ×1.3（含唇外缘）
        mh = max(6.0, abs(cy - ny) * 1.35)            # 鼻→嘴中心距 ×1.35（含上下唇）
        x0, x1 = cx - mw / 2.0, cx + mw / 2.0
        y0, y1 = cy - mh * 0.55, cy + mh * 0.55
    else:
        x, y, bw, bh = face["box"]
        x0, x1 = x + 0.25 * bw, x + 0.75 * bw
        y0, y1 = y + 0.62 * bh, y + 0.95 * bh
    x0 = max(0.0, min(float(w - 2), x0))
    y0 = max(0.0, min(float(h - 2), y0))
    x1 = max(x0 + 2.0, min(float(w), x1))
    y1 = max(y0 + 2.0, min(float(h), y1))
    return (x0, y0, x1, y1)


def frontality(face: dict) -> Optional[float]:
    """正脸度 0..1（仅当有 5 点关键点时可算）：偏航（鼻/嘴偏离双眼中点）+ 翻滚（双眼连线倾角）。

    ≈1 正脸朝向镜头；<0.35 多为大侧脸/转头/俯仰，此时嘴唇不可见或严重透视变形，
    唇形信号不可信。
    """
    lm = face.get("lm")
    if not lm or len(lm) < 5:
        return None
    (rex, rey), (lex, ley), (nx, ny), (rmx, rmy), (lmx, lmy) = lm[:5]
    d = math.hypot(lex - rex, ley - rey)
    if d < 1.0:
        return None
    eye_mid_x = (rex + lex) / 2.0
    mouth_mid_x = (rmx + lmx) / 2.0
    yaw_nose = abs(nx - eye_mid_x) / d              # 鼻尖水平偏移（偏航）
    yaw_mouth = abs(mouth_mid_x - eye_mid_x) / d    # 嘴中心水平偏移（偏航）
    roll = abs(math.degrees(math.atan2(ley - rey, lex - rex)))   # 头部左右倾斜
    s = 1.0
    s -= _clamp(yaw_nose / 0.35) * 0.45
    s -= _clamp(yaw_mouth / 0.35) * 0.25
    s -= _clamp(roll / 35.0) * 0.30
    return _clamp(s)


def mouth_texture(frame, roi: tuple, face: Optional[dict] = None) -> dict:
    """嘴部区域纹理/对比度画像（灰度帧 + ROI）。

    返回 {"contrast": 0..1, "edge": 0..1, "visible": 0..1}。
    `visible` 是相对「同脸下半部」的纹理归一化，用于在片段打分中给「糊/平/被挡」的嘴部降权
    （绝对遮挡判定在纯 CV 下不可靠，故只作相对软指标，不做硬门禁）。
    """
    np = _np()
    if np is None:
        return {"contrast": None, "edge": None, "visible": None}
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in roi)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, max(x0 + 2, x1)), min(h, max(y0 + 2, y1))
    crop = frame[y0:y1, x0:x1]
    if crop.size == 0:
        return {"contrast": None, "edge": None, "visible": None}
    c = crop.astype(np.float32)
    contrast = float(c.std()) / 64.0
    cx0, cy0, cx1, cy1 = _face_lower_half(face, (h, w))
    ref = frame[cy0:cy1, cx0:cx1]
    ref_std = float(ref.astype(np.float32).std()) if ref.size else 0.0
    try:
        lap = float(np.abs(_cv2_lap(c)).mean())
    except Exception:  # noqa: BLE001
        lap = 0.0
    ratio = (float(c.std()) / ref_std) if ref_std > 1e-6 else 1.0
    return {"contrast": round(_clamp(contrast), 3),
            "edge": round(_clamp(lap / 32.0), 3),
            "visible": round(_clamp(ratio), 3)}



def rois_from_faces_multi(faces_multi, shape: tuple, k: int = 0):
    """取每帧第 k 张脸 → (rois(N,4), coverage, jitter_px, frontality_median)。

    与旧 `boxes_to_mouth_rois` 一样：缺失帧用最近有效脸前向/后向填充；
    但 ROI 改用**关键点**推导（有 lm 时），并对关键点脸汇总「正脸度」中位数。
    """
    np = _np()
    n = len(faces_multi)
    picked = [(fl[k] if len(fl) > k else None) for fl in faces_multi]
    valid = [f for f in picked if f]
    coverage = len(valid) / max(1, n)
    if np is None or not valid:
        return None, coverage, 0.0, None
    last = None
    filled = []
    for f in picked:
        if f:
            last = f
        filled.append(last)
    first = next((f for f in filled if f), None)
    filled = [f if f else first for f in filled]
    rois = np.zeros((n, 4), dtype=np.float32)
    fronts: list[float] = []
    for i, f in enumerate(filled):
        rois[i] = mouth_roi_from_face(f, shape)
        fr = frontality(f)
        if fr is not None:
            fronts.append(fr)
    centers = np.array([[f["box"][0] + f["box"][2] / 2.0, f["box"][1] + f["box"][3] / 2.0]
                        for f in valid], dtype=np.float32)
    jitter = float(centers.std(axis=0).mean()) if len(centers) > 1 else 0.0
    fmed = float(np.median(fronts)) if fronts else None
    return rois, coverage, jitter, fmed


def visibility_series(frames, rois, faces_multi, k: int = 0, stride: int = 2):
    """逐帧嘴部「可见性」序列 → np.float32 数组（缺失为 nan）。

    可见性 = 嘴部 ROI 灰度标准差 / 人脸下半部标准差。手（同肤色、低纹理）或物体遮住嘴时，
    嘴部纹理相对人脸塌陷 → 该值下降。用于「遮挡」识别与片段降权（软指标）。
    """
    np = _np()
    n = int(frames.shape[0]) if frames is not None else 0
    if np is None or n == 0 or rois is None:
        return None
    out = np.full(n, np.nan, dtype=np.float32)
    for i in range(0, n, max(1, stride)):
        fl = faces_multi[i] if i < len(faces_multi) else []
        face = fl[k] if len(fl) > k else (fl[0] if fl else None)
        try:
            vis = mouth_texture(frames[i], tuple(rois[i]), face).get("visible")
        except Exception:  # noqa: BLE001
            vis = None
        if vis is not None:
            out[i] = float(vis)
    return out


def visibility_median(frames, rois, faces_multi, k: int = 0, stride: int = 5):
    """嘴部「可见性」中位数（相对人脸下半部纹理的比值，1.0=与脸同质、<1=偏平/可能被挡）。"""
    np = _np()
    ser = visibility_series(frames, rois, faces_multi, k=k, stride=stride)
    if ser is None:
        return None
    vals = ser[np.isfinite(ser)]
    return float(np.median(vals)) if vals.size else None


def _face_lower_half(face: Optional[dict], shape: tuple) -> tuple:
    """人脸框下半部（作为嘴部纹理的参照区）。"""
    h, w = shape
    if not face:
        return (0, int(h * 0.55), w, h)
    x, y, bw, bh = face["box"]
    x0 = int(max(0, x))
    x1 = int(min(w, x + bw))
    y0 = int(max(0, y + 0.55 * bh))
    y1 = int(min(h, y + bh))
    if x1 <= x0 or y1 <= y0:
        return (0, int(h * 0.55), w, h)
    return (x0, y0, x1, y1)


# 延迟导入，避免顶层 import 失败影响无 cv2/numpy 的场景
def _np():
    try:
        import numpy as np  # noqa: PLC0415
        return np
    except Exception:  # noqa: BLE001
        return None


def _cv2_lap(arr):
    cv2 = _Cv2Holder.cv2
    if cv2 is None:
        import cv2 as _cv2  # noqa: PLC0415
        _Cv2Holder.cv2 = _cv2
        cv2 = _cv2
    return cv2.Laplacian(arr, -1)


class _Cv2Holder:
    cv2 = None

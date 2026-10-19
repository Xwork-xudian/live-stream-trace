# 模型文件

- `face_detection_yunet_2023mar.onnx` — YuNet 人脸检测 + 5 点关键点。
  来源：OpenCV Zoo（https://github.com/opencv/opencv_zoo），Apache-2.0。
  本环境经 https://hf-mirror.com/opencv/face_detection_yunet 获取。
  由 `core/face_backend.py` 通过 `cv2.dnn`（`cv2.FaceDetectorYN_create`）加载，无需 onnxruntime。
  可用环境变量 `LST_FACE_MODEL` 覆盖路径。

- `2d106det.onnx` — **106 点人脸关键点**（用于「唇开合」几何信号，抗美颜磨皮）。
  来源：https://hf-mirror.com/artemonlysuno/2d106det （InsightFace 2d106det 的 ONNX 转存）。
  由 `core/face_backend.py` 通过 `cv2.dnn.readNetFromONNX` 加载，**无需 onnxruntime**。
  预处理：人脸方形裁剪（外扩 1.6×）→ 192×192 →
  `blobFromImage(..., 1/128, (192,192), (127.5,127.5,127.5), swapRB=True)`；
  输出 212 → `(p+1)*96` 得 192 空间坐标。**索引标定（实测）**：0-based 52..71 = 嘴部 20 点。
  可用环境变量 `LST_LMK_MODEL` 覆盖路径；**缺失时功能自动降级**（只用帧差信号）。

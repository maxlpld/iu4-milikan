#!/usr/bin/env python3
import sys
import os
import cv2
import time
import numpy as np
import subprocess
import re
import csv
from dataclasses import dataclass, field

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QToolBar, QPushButton,
    QStatusBar, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLineEdit,
    QDialog, QTextEdit, QSlider, QGroupBox, QSizePolicy,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage, QPixmap, QColor, QBrush


# ----------------------------
# Tracking data structure
# ----------------------------
@dataclass
class Track:
    tid: int
    color_bgr: tuple  # OpenCV color (B, G, R)

    # Current center used for tracking (None => do not draw circle)
    prev_center: tuple | None = None
    prev_time: float | None = None

    vx_samples: list = field(default_factory=list)
    vy_samples: list = field(default_factory=list)
    t_samples: list = field(default_factory=list)

    # Final mean velocities (computed when target exits / is lost)
    mean_vx: float | None = None
    mean_vy: float | None = None

    # Live mean velocities (periodically updated)
    last_report_time: float | None = None
    last_report_index: int = 0
    live_mean_vx: float | None = None
    live_mean_vy: float | None = None

    active: bool = True
    lost_frames: int = 0  # consecutive frames without valid detection


class MilikanApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("IU4 Millikan Experiment")
        self.resize(1200, 900)

        # If False, the image is NOT rotated.
        self.rotate_180 = True

        self.cap = None
        self.recording = False
        self.out = None
        self.detect_mode = False

        # ----------------------------
        # Tracking parameters (tune here)
        # ----------------------------
        self.ROI_WINDOW = 40            # ROI half-size around previous center (pixels)
        self.JUMP_MAX = 30              # max allowed center jump between frames (pixels)
        self.AREA_MIN = 6               # reject tiny blobs (noise)
        self.AREA_MAX = 4000            # reject huge blobs (reflections)
        self.LOST_MAX_FRAMES = 12       # if target not found for N frames -> finalize + stop tracking
        self.SMOOTH_ALPHA = 0.65        # 0..1 (higher = smoother, less jitter)

        # Click behavior / deletion
        self.DELETE_RADIUS_PX = 18      # clicking within this distance deletes the target

        # Multi-target behavior:
        # - Normal click: add new target (no limit)
        # - Click on circle: delete target
        # - SHIFT+click near an active target: replace that target (reset its samples)
        self.enable_shift_replace = True
        self.REPLACE_RADIUS_PX = 35

        # Multi-target management (NO LIMIT)
        self.tracks: list[Track] = []
        self.next_tid = 1

        # Color generation settings (avoid white/black via saturation/value)
        self._color_seed = 0

        # Periodic (live) mean velocities (px/s)
        self.report_interval = 1.0

        self.grid_step = 50

        self.default_brightness = 0
        self.default_contrast = 1.0
        self.default_gamma = 1.5

        self.brightness = self.default_brightness
        self.contrast = self.default_contrast
        self.gamma = self.default_gamma

        # ----------------------------
        # Central UI: video on top + table underneath
        # ----------------------------
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "#",
            "Status",
            "⟨vₓ⟩ live [px·s⁻¹]",
            "⟨vᵧ⟩ live [px·s⁻¹]",
            "⟨vₓ⟩ final [px·s⁻¹]",
            "⟨vᵧ⟩ final [px·s⁻¹]",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(200)

        central = QWidget()
        v = QVBoxLayout()
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)
        v.addWidget(self.video_label, stretch=4)
        v.addWidget(self.table, stretch=1)
        central.setLayout(v)
        self.setCentralWidget(central)

        toolbar = QToolBar()
        self.addToolBar(toolbar)

        # ---------------- CAMERA SELECTOR (NO INTEGRATED) ----------------
        toolbar.addWidget(QLabel("Camera:"))
        self.cam_combo = QComboBox()
        self.cam_devices = self.list_external_cameras()

        if not self.cam_devices:
            self.cam_combo.addItem("No external camera found")
            self.cam_combo.setEnabled(False)
        else:
            for d in self.cam_devices:
                self.cam_combo.addItem(d["label"])
            self.cam_combo.currentIndexChanged.connect(self.change_camera)

        toolbar.addWidget(self.cam_combo)
        toolbar.addSeparator()

        # Resolution selector
        self.res_combo = QComboBox()
        self.resolutions = [(1920, 1080), (1280, 720)]
        self.res_combo.addItems(["1920x1080", "1280x720"])
        self.res_combo.currentIndexChanged.connect(self.change_resolution)
        toolbar.addWidget(QLabel("Res:"))
        toolbar.addWidget(self.res_combo)
        toolbar.addSeparator()

        # Grid selector
        self.grid_combo = QComboBox()
        self.grid_values = [25, 50, 75, 100]
        self.grid_combo.addItems(["25 px", "50 px", "75 px", "100 px"])
        self.grid_combo.setCurrentText("50 px")
        self.grid_combo.currentIndexChanged.connect(self.change_grid)
        toolbar.addWidget(QLabel("Grid:"))
        toolbar.addWidget(self.grid_combo)
        toolbar.addSeparator()

        # --------- Periodic averaging interval selector ----------
        toolbar.addWidget(QLabel("Avg:"))
        self.avg_combo = QComboBox()
        self.avg_presets = [1, 3, 5, 10]
        self.avg_combo.addItems([f"{v} s" for v in self.avg_presets] + ["Custom"])
        self.avg_combo.setCurrentText("1 s")
        self.avg_combo.currentIndexChanged.connect(self.on_avg_combo_changed)
        toolbar.addWidget(self.avg_combo)

        self.avg_input = QLineEdit("1")
        self.avg_input.setFixedWidth(50)
        self.avg_input.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.avg_input.setToolTip("Custom averaging interval (seconds)")
        self.avg_input.editingFinished.connect(self.on_avg_custom_changed)
        self.avg_input.setEnabled(False)
        toolbar.addWidget(self.avg_input)
        toolbar.addSeparator()
        # -------------------------------------------------------------

        # Record
        self.rec_button = QPushButton()
        self.rec_button.setFixedSize(28, 28)
        self.rec_button.setStyleSheet(self.rec_style(False))
        self.rec_button.clicked.connect(self.toggle_record)
        toolbar.addWidget(self.rec_button)

        # Detect
        self.detect_button = QPushButton("Detect")
        self.detect_button.setCheckable(True)
        self.detect_button.clicked.connect(self.toggle_detect)
        toolbar.addWidget(self.detect_button)

        # Clear targets
        toolbar.addSeparator()
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_tracks)
        toolbar.addWidget(self.clear_button)

        # Export CSV (final means only)
        toolbar.addSeparator()
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self.export_csv_final_means)
        toolbar.addWidget(self.export_button)

        toolbar.addSeparator()

        # Image controls toggle
        self.image_btn = QPushButton("Image Controls")
        self.image_btn.clicked.connect(self.toggle_image_panel)
        toolbar.addWidget(self.image_btn)

        toolbar.addSeparator()

        # Help
        self.help_button = QPushButton("?")
        self.help_button.setFixedSize(28, 28)
        self.help_button.clicked.connect(self.open_readme)
        toolbar.addWidget(self.help_button)

        # Create floating image settings dialog
        self.create_image_panel()

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # IMPORTANT: do NOT open any camera automatically.
        if self.cam_devices:
            self.cam_combo.setCurrentIndex(0)
            self.change_camera(0)

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)

        self.video_label.mousePressEvent = self.mouse_click

    # ---------------- Color generation ----------------

    def next_color_bgr(self):
        """
        Generate a visually distinct color each time.
        Avoid white/black by enforcing saturation/value.
        """
        # Use golden ratio to spread hues nicely
        self._color_seed += 1
        hue = (self._color_seed * 0.61803398875) % 1.0

        # OpenCV HSV: H in [0,179], S in [0,255], V in [0,255]
        H = int(hue * 179)
        S = 220  # high saturation (avoid gray/white)
        V = 230  # high value but not 255 (avoid pure white-ish on some cameras)

        hsv = np.uint8([[[H, S, V]]])
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
        return (int(bgr[0]), int(bgr[1]), int(bgr[2]))

    # ---------------- HELP ----------------

    def open_readme(self):
        readme_path = os.path.join(os.path.dirname(__file__), "README.md")
        if not os.path.exists(readme_path):
            return
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()

        dialog = QDialog(self)
        dialog.setWindowTitle("Help - IU4 Millikan Experiment")
        dialog.resize(700, 600)

        layout = QVBoxLayout()
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(content)
        layout.addWidget(text)

        dialog.setLayout(layout)
        dialog.exec()

    # ---------------- CAMERA LISTING (EXCLUDE INTEGRATED) ----------------

    def list_external_cameras(self):
        """
        Build a list of selectable cameras excluding the integrated one.
        Preferred method: /dev/v4l/by-id (stable names).
        Fallback: v4l2-ctl --list-devices parsing.
        Returns list of dicts: {"label": str, "path": str | None, "index": int | None}
        """
        cams = []
        byid = "/dev/v4l/by-id"

        if os.path.isdir(byid):
            for name in sorted(os.listdir(byid)):
                if "Integrated" in name or "integrated" in name:
                    continue
                if "video-index0" not in name:
                    continue

                path = os.path.join(byid, name)
                try:
                    real = os.path.realpath(path)
                    m = re.search(r"/dev/video(\d+)", real)
                    idx = int(m.group(1)) if m else None
                except Exception:
                    real = path
                    idx = None

                clean_name = name
                clean_name = re.sub(r"^usb-", "", clean_name)
                clean_name = re.sub(r"-video-index\d+", "", clean_name)
                clean_name = clean_name.replace("_", " ")

                if len(clean_name) > 35:
                    clean_name = clean_name[:35] + "..."

                video_id = os.path.basename(real)
                label = f"{clean_name} ({video_id})"
                cams.append({"label": label, "path": path, "index": idx})

            if cams:
                return cams

        # Fallback: v4l2-ctl --list-devices
        try:
            p = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True, check=False)
            txt = p.stdout or ""
            lines = txt.splitlines()

            current_name = None
            current_videos = []

            def flush():
                nonlocal current_name, current_videos, cams
                if not current_name:
                    return
                if "Integrated" in current_name or "integrat" in current_name.lower():
                    current_name = None
                    current_videos = []
                    return
                for v in current_videos:
                    m = re.search(r"/dev/video(\d+)", v)
                    if m:
                        idx = int(m.group(1))
                        cams.append({"label": f"{current_name.strip()}  →  video{idx}",
                                     "path": None, "index": idx})
                        break
                current_name = None
                current_videos = []

            for line in lines:
                if line.strip() == "":
                    flush()
                    continue
                if not line.startswith("\t") and ":" in line:
                    flush()
                    current_name = line
                elif "/dev/video" in line:
                    current_videos.append(line.strip())
            flush()
        except Exception:
            pass

        return cams

    def change_camera(self, idx):
        if not self.cam_devices:
            return
        if idx < 0 or idx >= len(self.cam_devices):
            return
        if self.recording:
            self.status.showMessage("Stop recording before changing camera")
            return
        self.init_camera(self.cam_devices[idx])

    # ---------------- CAMERA ----------------

    def init_camera(self, cam):
        if self.cap:
            self.cap.release()
            self.cap = None

        if cam.get("path"):
            self.cap = cv2.VideoCapture(cam["path"])
        else:
            self.cap = cv2.VideoCapture(int(cam["index"]))

        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open selected camera: {cam.get('label', 'unknown')}")

        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        w, h = self.resolutions[self.res_combo.currentIndex()]
        self.set_resolution(w, h)

        self.clear_tracks()
        self.status.showMessage(f"Using camera: {cam['label']}")

    def set_resolution(self, w, h):
        if not self.cap:
            return
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.camera_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.camera_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def change_resolution(self, idx):
        if self.recording:
            self.status.showMessage("Stop recording before changing resolution")
            self.res_combo.blockSignals(True)
            self.res_combo.setCurrentIndex(0)
            self.res_combo.blockSignals(False)
            return
        if not self.cap:
            self.status.showMessage("Select a camera first")
            return
        w, h = self.resolutions[idx]
        self.set_resolution(w, h)
        self.clear_tracks()

    # ---------------- GRID ----------------

    def change_grid(self, idx):
        self.grid_step = self.grid_values[idx]
        self.status.showMessage(f"Grid set to {self.grid_step} px")

    def draw_grid(self, frame):
        h, w = frame.shape[:2]
        for x in range(0, w, self.grid_step):
            cv2.line(frame, (x, 0), (x, h), (255, 255, 255), 1)
        for y in range(0, h, self.grid_step):
            cv2.line(frame, (0, y), (w, y), (255, 255, 255), 1)
        return frame

    # ---------------- AVG INTERVAL UI ----------------

    def on_avg_combo_changed(self, idx):
        text = self.avg_combo.currentText().strip()
        if text.lower() == "custom":
            self.avg_input.setEnabled(True)
            self.on_avg_custom_changed()
        else:
            self.avg_input.setEnabled(False)
            try:
                val = float(text.replace("s", "").strip())
                self.set_report_interval(val)
            except Exception:
                pass

    def on_avg_custom_changed(self):
        if self.avg_combo.currentText().strip().lower() != "custom":
            return
        try:
            val = float(self.avg_input.text().strip())
        except ValueError:
            return
        self.set_report_interval(val)

    def set_report_interval(self, seconds: float):
        seconds = float(seconds)
        if seconds <= 0:
            return
        seconds = max(0.2, seconds)
        self.report_interval = seconds
        self.status.showMessage(f"Averaging interval set to {self.report_interval:.2f} s")

        now = time.time()
        for tr in self.tracks:
            if tr.active:
                tr.last_report_time = now
                tr.last_report_index = len(tr.vx_samples)
                tr.live_mean_vx = None
                tr.live_mean_vy = None

    # ---------------- IMAGE PANEL ----------------

    def create_image_panel(self):
        self.image_dialog = QDialog(self)
        self.image_dialog.setWindowTitle("Image Settings")
        self.image_dialog.setMinimumWidth(380)
        self.image_dialog.setMaximumWidth(420)
        self.image_dialog.setModal(False)

        outer = QVBoxLayout()
        outer.setContentsMargins(15, 15, 15, 15)
        outer.setSpacing(12)

        group = QGroupBox("Brightness / Contrast / Gamma")
        group_layout = QVBoxLayout()
        group_layout.setSpacing(12)
        group.setLayout(group_layout)

        b_row, self.b_slider, self.b_input = self.make_control(
            "Brightness", -100, 100, self.default_brightness, self.set_brightness, suffix=""
        )
        c_row, self.c_slider, self.c_input = self.make_control(
            "Contrast", 50, 200, int(self.default_contrast * 100), self.set_contrast, suffix=" %"
        )
        g_row, self.g_slider, self.g_input = self.make_control(
            "Gamma", 50, 300, int(self.default_gamma * 100), self.set_gamma, suffix=" %"
        )

        group_layout.addWidget(b_row)
        group_layout.addWidget(c_row)
        group_layout.addWidget(g_row)

        default_btn = QPushButton("Reset to Default")
        default_btn.clicked.connect(self.reset_image_defaults)

        outer.addWidget(group)
        outer.addWidget(default_btn)
        outer.addStretch()
        self.image_dialog.setLayout(outer)

    def toggle_image_panel(self):
        if self.image_dialog.isVisible():
            self.image_dialog.hide()
        else:
            self.image_dialog.move(self.geometry().center() - self.image_dialog.rect().center())
            self.image_dialog.show()

    def make_control(self, label, minv, maxv, value, callback, suffix=""):
        row = QWidget()
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        row.setLayout(h)

        lab = QLabel(label)
        lab.setFixedWidth(90)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(minv)
        slider.setMaximum(maxv)
        slider.setValue(value)
        slider.valueChanged.connect(callback)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        right = QWidget()
        r = QHBoxLayout()
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(6)
        right.setLayout(r)

        inp = QLineEdit(str(value))
        inp.setFixedWidth(70)
        inp.setAlignment(Qt.AlignmentFlag.AlignRight)

        suf = QLabel(suffix if suffix else "")
        suf.setFixedWidth(24)

        inp.editingFinished.connect(lambda: self._safe_set_slider(slider, inp, minv, maxv))
        slider.valueChanged.connect(lambda v: inp.setText(str(v)))

        r.addWidget(inp)
        r.addWidget(suf)

        h.addWidget(lab)
        h.addWidget(slider)
        h.addWidget(right)

        return row, slider, inp

    def _safe_set_slider(self, slider, inp, minv, maxv):
        try:
            v = int(inp.text().strip())
        except ValueError:
            v = slider.value()
        v = max(minv, min(maxv, v))
        slider.setValue(v)

    def set_brightness(self, v):
        self.brightness = v

    def set_contrast(self, v):
        self.contrast = v / 100

    def set_gamma(self, v):
        self.gamma = v / 100

    def reset_image_defaults(self):
        self.b_slider.setValue(self.default_brightness)
        self.c_slider.setValue(int(self.default_contrast * 100))
        self.g_slider.setValue(int(self.default_gamma * 100))

    # ---------------- RECORD ----------------

    def rec_style(self, active):
        if active:
            return "QPushButton { background-color: #ff3b3b; border-radius: 14px; }"
        return "QPushButton { background-color: #555; border-radius: 14px; }"

    def toggle_record(self):
        if not self.cap:
            self.status.showMessage("Select a camera first")
            return

        if not self.recording:
            home = os.path.expanduser("~")
            save_dir = os.path.join(home, "Documents", "IU4MillkanExp")
            os.makedirs(save_dir, exist_ok=True)

            filename = time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
            path = os.path.join(save_dir, filename)

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.out = cv2.VideoWriter(path, fourcc, 20.0, (self.camera_width, self.camera_height))
            self.recording = True
        else:
            self.recording = False
            if self.out:
                self.out.release()
                self.out = None

        self.rec_button.setStyleSheet(self.rec_style(self.recording))

    # ---------------- DETECT / TRACKING ----------------

    def toggle_detect(self):
        self.detect_mode = self.detect_button.isChecked()
        self.clear_tracks()

    def clear_tracks(self):
        self.tracks = []
        self.next_tid = 1
        self._color_seed = 0
        self.refresh_table()

    def _label_to_image_coords(self, mx: int, my: int):
        """
        Convert QLabel mouse coordinates to image pixel coordinates, accounting for KeepAspectRatio letterboxing.
        Returns (img_x, img_y) or None if outside displayed image.
        """
        lw = self.video_label.width()
        lh = self.video_label.height()

        scale = min(lw / self.camera_width, lh / self.camera_height)
        disp_w = int(self.camera_width * scale)
        disp_h = int(self.camera_height * scale)

        off_x = (lw - disp_w) // 2
        off_y = (lh - disp_h) // 2

        if mx < off_x or mx >= off_x + disp_w or my < off_y or my >= off_y + disp_h:
            return None

        img_x = int((mx - off_x) / scale)
        img_y = int((my - off_y) / scale)

        img_x = max(0, min(self.camera_width - 1, img_x))
        img_y = max(0, min(self.camera_height - 1, img_y))
        return img_x, img_y

    def _find_track_near(self, img_x: int, img_y: int, radius_px: float, active_only: bool = False):
        """
        Return index of the closest track whose current center is within radius_px.
        """
        best_i = None
        best_d = 1e18
        p = np.array([img_x, img_y], dtype=float)

        for i, tr in enumerate(self.tracks):
            if active_only and (not tr.active):
                continue
            if tr.prev_center is None:
                continue
            c = np.array(tr.prev_center, dtype=float)
            d = float(np.linalg.norm(p - c))
            if d < radius_px and d < best_d:
                best_d = d
                best_i = i

        return best_i

    def mouse_click(self, event):
        """
        FIXED MULTI-TRACK BEHAVIOR:

        - Normal click:
            * If on an existing circle => delete it (removes line from table too)
            * Else => ADD a new target (NO LIMIT)
        - SHIFT+click:
            * Replace the nearest ACTIVE target (if close enough), useful when you mis-clicked.
        """
        if not self.detect_mode or not self.cap:
            return

        mx = int(event.position().x())
        my = int(event.position().y())
        coords = self._label_to_image_coords(mx, my)
        if coords is None:
            return
        img_x, img_y = coords

        # 1) Delete if clicking on an existing circle
        idx_del = self._find_track_near(img_x, img_y, self.DELETE_RADIUS_PX, active_only=False)
        if idx_del is not None:
            del self.tracks[idx_del]
            self.refresh_table()
            return

        # 2) SHIFT+click => replace nearest ACTIVE target
        mods = event.modifiers()
        if self.enable_shift_replace and (mods & Qt.KeyboardModifier.ShiftModifier):
            idx_rep = self._find_track_near(img_x, img_y, self.REPLACE_RADIUS_PX, active_only=True)
            if idx_rep is not None:
                tr = self.tracks[idx_rep]
                now = time.time()

                tr.prev_center = (img_x, img_y)
                tr.prev_time = now
                tr.lost_frames = 0

                # Reset samples because the user selected a different particle
                tr.vx_samples = []
                tr.vy_samples = []
                tr.t_samples = []
                tr.mean_vx = None
                tr.mean_vy = None
                tr.live_mean_vx = None
                tr.live_mean_vy = None
                tr.last_report_time = now
                tr.last_report_index = 0

                self.refresh_table()
                return

        # 3) Create a new target (NO LIMIT)
        color = self.next_color_bgr()
        now = time.time()
        tr = Track(
            tid=self.next_tid,
            color_bgr=color,
            prev_center=(img_x, img_y),
            prev_time=now,
            last_report_time=now,
            last_report_index=0,
            active=True
        )
        self.next_tid += 1
        self.tracks.append(tr)
        self.refresh_table()

    # ---------------- Improved local tracker ----------------

    def track_local(self, frame, center, window=None):
        """
        Robust local tracking (blob centroid):
        - ROI around previous center
        - abs(high-pass) -> works for dark/bright droplets
        - Otsu threshold + morphology open
        - choose blob centroid nearest to previous center
        - gate by maximum jump distance
        """
        if window is None:
            window = self.ROI_WINDOW

        x, y = center
        h, w = frame.shape[:2]

        x1 = max(0, x - window)
        x2 = min(w, x + window)
        y1 = max(0, y - window)
        y2 = min(h, y + window)

        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return None

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        blurred = cv2.GaussianBlur(gray, (21, 21), 0)
        hp = cv2.absdiff(gray, blurred)
        hp = cv2.GaussianBlur(hp, (5, 5), 0)

        _, th = cv2.threshold(hp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)

        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        best = None
        best_dist = 1e18

        for c in contours:
            area = cv2.contourArea(c)
            if area < self.AREA_MIN or area > self.AREA_MAX:
                continue

            M = cv2.moments(c)
            if M["m00"] == 0:
                continue

            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            cand = (x1 + cx, y1 + cy)

            dist = float(np.linalg.norm(np.array(cand) - np.array(center)))
            if dist < best_dist:
                best_dist = dist
                best = cand

        if best is None:
            return None
        if best_dist > self.JUMP_MAX:
            return None
        return best

    # ---------------- IMAGE PROCESSING ----------------

    def apply_controls(self, frame):
        frame = cv2.convertScaleAbs(frame, alpha=self.contrast, beta=self.brightness)
        invGamma = 1.0 / max(self.gamma, 0.01)
        table = np.array([((i / 255.0) ** invGamma) * 255 for i in range(256)], dtype=np.float32)
        table = np.clip(table, 0, 255).astype("uint8")
        return cv2.LUT(frame, table)

    # ---------------- LIVE MEAN COMPUTATION ----------------

    def maybe_update_live_mean(self, tr: Track, now: float):
        if not tr.active:
            return

        if tr.last_report_time is None:
            tr.last_report_time = now
            tr.last_report_index = len(tr.vx_samples)
            return

        if (now - tr.last_report_time) < self.report_interval:
            return

        start = tr.last_report_index
        end = len(tr.vx_samples)
        if end - start <= 0:
            tr.last_report_time = now
            tr.last_report_index = end
            return

        window_vx = tr.vx_samples[start:end]
        window_vy = tr.vy_samples[start:end]

        tr.live_mean_vx = float(np.mean(window_vx)) if window_vx else None
        tr.live_mean_vy = float(np.mean(window_vy)) if window_vy else None

        tr.last_report_time = now
        tr.last_report_index = end

    def finalize_track(self, tr: Track):
        if tr.vx_samples:
            tr.mean_vx = float(np.mean(tr.vx_samples))
        if tr.vy_samples:
            tr.mean_vy = float(np.mean(tr.vy_samples))
        tr.active = False

        # Remove circle from live view, but keep data in table
        tr.prev_center = None
        tr.prev_time = None

    # ---------------- Table UI ----------------

    def refresh_table(self):
        self.table.setRowCount(len(self.tracks))

        for row, tr in enumerate(self.tracks):
            status = "Tracking" if tr.active else "Final"

            live_vx = "—" if tr.live_mean_vx is None else f"{tr.live_mean_vx:.2f}"
            live_vy = "—" if tr.live_mean_vy is None else f"{tr.live_mean_vy:.2f}"
            fin_vx = "—" if tr.mean_vx is None else f"{tr.mean_vx:.2f}"
            fin_vy = "—" if tr.mean_vy is None else f"{tr.mean_vy:.2f}"

            items = [
                QTableWidgetItem(f"{tr.tid}"),
                QTableWidgetItem(status),
                QTableWidgetItem(live_vx),
                QTableWidgetItem(live_vy),
                QTableWidgetItem(fin_vx),
                QTableWidgetItem(fin_vy),
            ]

            b, g, r = tr.color_bgr
            qcol = QColor(r, g, b)
            brush = QBrush(qcol)

            for col, it in enumerate(items):
                it.setForeground(brush)
                self.table.setItem(row, col, it)

        self.table.resizeRowsToContents()

    # ---------------- CSV EXPORT ----------------

    def export_csv_final_means(self):
        """
        Export ONLY final mean velocities for each target:
        columns: id, mean_vx_px_s, mean_vy_px_s
        If a target is still tracking and has no final mean, we write blanks.
        """
        if not self.tracks:
            QMessageBox.information(self, "Export CSV", "No data to export.")
            return

        home = os.path.expanduser("~")
        default_dir = os.path.join(home, "Documents", "IU4MillkanExp")
        os.makedirs(default_dir, exist_ok=True)
        default_name = f"millikan_final_means_{time.strftime('%Y-%m-%d_%H-%M-%S')}.csv"
        default_path = os.path.join(default_dir, default_name)

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export final mean velocities to CSV",
            default_path,
            "CSV Files (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["id", "mean vx [px/s]", "mean vy [px/s]"])
                for tr in self.tracks:
                    vx = "" if tr.mean_vx is None else f"{tr.mean_vx:.6g}"
                    vy = "" if tr.mean_vy is None else f"{tr.mean_vy:.6g}"
                    w.writerow([tr.tid, vx, vy])

            QMessageBox.information(self, "Export CSV", f"Saved:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export CSV", f"Failed to write CSV:\n{repr(e)}")

    # ---------------- MAIN LOOP ----------------

    def update_frame(self):
        if not self.cap:
            self.status.showMessage("Select a camera (top-left) to start.")
            return

        ret, frame = self.cap.read()
        if not ret:
            self.status.showMessage("Camera read failed")
            return

        if self.rotate_180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)

        frame = self.apply_controls(frame)
        display_frame = self.draw_grid(frame.copy())

        # Tracking update (multi-target)
        if self.detect_mode and self.tracks:
            margin = 15
            now = time.time()

            for tr in self.tracks:
                if not tr.active or tr.prev_center is None or tr.prev_time is None:
                    continue

                new_center = self.track_local(frame, tr.prev_center)

                if new_center is None:
                    tr.lost_frames += 1
                    if tr.lost_frames >= self.LOST_MAX_FRAMES:
                        self.finalize_track(tr)
                    continue

                tr.lost_frames = 0
                x, y = new_center

                # Stop tracking if target exits frame margins
                if (
                    x < margin
                    or x > self.camera_width - margin
                    or y < margin
                    or y > self.camera_height - margin
                ):
                    self.finalize_track(tr)
                    continue

                # Smooth the center to reduce jitter
                sx = int(self.SMOOTH_ALPHA * tr.prev_center[0] + (1 - self.SMOOTH_ALPHA) * x)
                sy = int(self.SMOOTH_ALPHA * tr.prev_center[1] + (1 - self.SMOOTH_ALPHA) * y)
                new_center_smooth = (sx, sy)

                dx_raw = (new_center_smooth[0] - tr.prev_center[0])
                dy_raw = (new_center_smooth[1] - tr.prev_center[1])

                # Correct sign if rotated
                if self.rotate_180:
                    dx = -dx_raw
                    dy = -dy_raw
                else:
                    dx = dx_raw
                    dy = dy_raw

                dt = now - tr.prev_time
                if dt > 0:
                    tr.vx_samples.append(dx / dt)
                    tr.vy_samples.append(dy / dt)
                    tr.t_samples.append(now)
                    self.maybe_update_live_mean(tr, now)

                tr.prev_center = new_center_smooth
                tr.prev_time = now

        # Draw circles only for active targets (no numbers, no text)
        for tr in self.tracks:
            if tr.active and tr.prev_center is not None:
                cv2.circle(display_frame, tr.prev_center, 10, tr.color_bgr, 2)

        # Update table
        self.refresh_table()

        # Status bar
        if self.detect_mode:
            active_n = sum(1 for tr in self.tracks if tr.active)
            msg = f"Detection ON | active targets: {active_n} | Click to add | Click circle to delete"
            if self.enable_shift_replace:
                msg += " | SHIFT+click near a target to replace"
            self.status.showMessage(msg)
        else:
            self.status.showMessage("Detection OFF")

        # Recording overlay (only on saved video)
        if self.recording and self.out:
            record_frame = display_frame.copy()
            y = 30
            for tr in self.tracks:
                if y > record_frame.shape[0] - 20:
                    break  # avoid drawing beyond bottom
                if tr.active:
                    vx = tr.live_mean_vx
                    vy = tr.live_mean_vy
                    if vx is None and vy is None:
                        line = f"{tr.tid}: tracking..."
                    else:
                        vx_txt = "—" if vx is None else f"{vx:.2f}"
                        vy_txt = "—" if vy is None else f"{vy:.2f}"
                        line = f"{tr.tid}: live(~{self.report_interval:g}s) vx={vx_txt} vy={vy_txt}"
                else:
                    vx_txt = "—" if tr.mean_vx is None else f"{tr.mean_vx:.2f}"
                    vy_txt = "—" if tr.mean_vy is None else f"{tr.mean_vy:.2f}"
                    line = f"{tr.tid}: FINAL vx={vx_txt} vy={vy_txt}"

                cv2.putText(record_frame, line, (20, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, tr.color_bgr, 2)
                y += 18

            self.out.write(record_frame)

        # Convert to Qt image for display
        rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
        qt_image = QImage(
            rgb.data,
            self.camera_width,
            self.camera_height,
            3 * self.camera_width,
            QImage.Format.Format_RGB888,
        )

        self.video_label.setPixmap(
            QPixmap.fromImage(qt_image).scaled(
                self.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
        )

    def closeEvent(self, event):
        if hasattr(self, "image_dialog") and self.image_dialog.isVisible():
            self.image_dialog.close()

        if self.out:
            self.out.release()
        if self.cap:
            self.cap.release()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MilikanApp()
    window.show()
    sys.exit(app.exec())
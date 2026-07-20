#!/usr/bin/env python3
"""Desktop viewer for tightly packed Bayer RAW8, RAW10 and RAW12 image streams."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

APP_NAME = "RAW Viewer"
RAW_FORMATS = ("RAW8", "RAW10", "RAW12")
BAYER_PATTERNS = ("GRBG", "GBRG", "RGGB", "BGGR")


def frame_bytes(width: int, height: int, raw_format: str) -> int:
    """Return packed bytes per frame; raises on a layout that cannot be packed."""
    if width <= 0 or height <= 0:
        raise ValueError("宽度和高度必须大于 0")
    pixels = width * height
    if raw_format == "RAW8":
        return pixels
    if raw_format == "RAW10":
        if width % 4:
            raise ValueError("RAW10 的宽度必须是 4 的倍数")
        return pixels * 5 // 4
    if raw_format == "RAW12":
        if width % 2:
            raise ValueError("RAW12 的宽度必须是 2 的倍数")
        return pixels * 3 // 2
    raise ValueError(f"不支持的格式：{raw_format}")


def unpack_raw8(data: np.ndarray, width: int, height: int) -> np.ndarray:
    expected = frame_bytes(width, height, "RAW8")
    if data.size != expected:
        raise ValueError(f"帧大小不正确：得到 {data.size:,} 字节，需要 {expected:,} 字节")
    return data.astype(np.uint16).reshape(height, width)


def unpack_raw10(data: np.ndarray, width: int, height: int) -> np.ndarray:
    """Unpack standard CSI-2 RAW10: four MSB bytes followed by packed 2-bit LSBs."""
    expected = frame_bytes(width, height, "RAW10")
    if data.size != expected:
        raise ValueError(f"帧大小不正确：得到 {data.size:,} 字节，需要 {expected:,} 字节")
    packed = data.reshape(-1, 5).astype(np.uint16)
    pixels = np.empty((packed.shape[0], 4), dtype=np.uint16)
    pixels[:, 0] = (packed[:, 0] << 2) | (packed[:, 4] & 0x03)
    pixels[:, 1] = (packed[:, 1] << 2) | ((packed[:, 4] >> 2) & 0x03)
    pixels[:, 2] = (packed[:, 2] << 2) | ((packed[:, 4] >> 4) & 0x03)
    pixels[:, 3] = (packed[:, 3] << 2) | ((packed[:, 4] >> 6) & 0x03)
    return pixels.reshape(height, width)


def unpack_raw12(data: np.ndarray, width: int, height: int) -> np.ndarray:
    """Unpack standard CSI-2 RAW12: two MSB bytes followed by packed nibbles."""
    expected = frame_bytes(width, height, "RAW12")
    if data.size != expected:
        raise ValueError(f"帧大小不正确：得到 {data.size:,} 字节，需要 {expected:,} 字节")
    packed = data.reshape(-1, 3).astype(np.uint16)
    pixels = np.empty((packed.shape[0], 2), dtype=np.uint16)
    pixels[:, 0] = (packed[:, 0] << 4) | (packed[:, 2] & 0x0F)
    pixels[:, 1] = (packed[:, 1] << 4) | ((packed[:, 2] >> 4) & 0x0F)
    return pixels.reshape(height, width)


def unpack_raw(data: np.ndarray, width: int, height: int, raw_format: str) -> tuple[np.ndarray, int]:
    if raw_format == "RAW8":
        return unpack_raw8(data, width, height), 255
    if raw_format == "RAW10":
        return unpack_raw10(data, width, height), 1023
    if raw_format == "RAW12":
        return unpack_raw12(data, width, height), 4095
    raise ValueError(f"不支持的格式：{raw_format}")


def _interpolate(raw: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Masked 3×3 bilinear interpolation without an OpenCV dependency."""
    kernel = ((1, 2, 1), (2, 4, 2), (1, 2, 1))
    height, width = raw.shape
    source = np.pad(raw * mask, 1, mode="reflect")
    samples = np.pad(mask.astype(np.float32), 1, mode="reflect")
    total = np.zeros((height, width), dtype=np.float32)
    weights = np.zeros((height, width), dtype=np.float32)
    for y in range(3):
        for x in range(3):
            weight = kernel[y][x]
            total += weight * source[y:y + height, x:x + width]
            weights += weight * samples[y:y + height, x:x + width]
    return total / np.maximum(weights, 1.0)


def demosaic_bilinear(raw: np.ndarray, pattern: str) -> np.ndarray:
    """Produce RGB using bilinear Bayer demosaicing for one of four common patterns."""
    pattern = pattern.upper()
    if pattern not in BAYER_PATTERNS:
        raise ValueError(f"不支持的 Bayer 排列：{pattern}")
    masks = {channel: np.zeros(raw.shape, dtype=bool) for channel in "RGB"}
    tile = ((pattern[0], pattern[1]), (pattern[2], pattern[3]))
    for y in range(2):
        for x in range(2):
            masks[tile[y][x]][y::2, x::2] = True
    image = raw.astype(np.float32)
    return np.stack([_interpolate(image, masks[channel]) for channel in "RGB"], axis=-1)


def tone_map(rgb: np.ndarray) -> np.ndarray:
    """Apply automatic gray-world WB, percentile stretch and 2.2 display gamma."""
    medians = np.median(rgb.reshape(-1, 3), axis=0)
    gains = np.clip(medians[1] / np.maximum(medians, 1.0), 0.35, 3.0)
    rgb = rgb * gains
    black, white = np.percentile(rgb, (0.5, 99.5))
    rgb = np.clip((rgb - black) / max(float(white - black), 1.0), 0.0, 1.0)
    return np.round(np.power(rgb, 1 / 2.2) * 255).astype(np.uint8)


def decode_frame(path: Path, width: int, height: int, frame: int, pattern: str, raw_format: str) -> tuple[np.ndarray, np.ndarray]:
    """Decode one frame, returning direct RGB and the optional ISP-enhanced RGB."""
    size = frame_bytes(width, height, raw_format)
    with path.open("rb") as stream:
        stream.seek(frame * size)
        data = np.frombuffer(stream.read(size), dtype=np.uint8)
    raw, maximum = unpack_raw(data, width, height, raw_format)
    rgb = demosaic_bilinear(raw, pattern)
    direct = np.clip(rgb * (255.0 / maximum), 0, 255).astype(np.uint8)
    return direct, tone_map(rgb)


class WorkerSignals(QObject):
    done = Signal(int, object, str)


class DecodeWorker(QRunnable):
    def __init__(self, serial: int, path: Path, width: int, height: int, frame: int, pattern: str, raw_format: str):
        super().__init__()
        self.serial, self.path = serial, path
        self.width, self.height, self.frame = width, height, frame
        self.pattern, self.raw_format = pattern, raw_format
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.done.emit(self.serial, decode_frame(self.path, self.width, self.height, self.frame, self.pattern, self.raw_format), "")
        except Exception as exc:  # UI presents errors rather than crashing a worker thread.
            self.signals.done.emit(self.serial, None, str(exc))


class DropCard(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event):  # noqa: N802 - Qt API name
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ImageView(QScrollArea):
    EMPTY_TEXT = "将 RAW 文件拖到窗口中\n\n或点击左侧的“打开文件”"

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignCenter)
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("imageArea")
        self.label = QLabel(self.EMPTY_TEXT)
        self.label.setObjectName("emptyPreview")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumSize(400, 300)
        self.setWidget(self.label)
        self.original: QPixmap | None = None
        self.fit_mode = True

    def set_image(self, pixmap: QPixmap) -> None:
        self.original = pixmap
        self.refresh()

    def clear(self) -> None:
        self.original = None
        self.label.setPixmap(QPixmap())
        self.label.setText(self.EMPTY_TEXT)
        self.label.setMinimumSize(400, 300)

    def set_fit(self, fit: bool) -> None:
        self.fit_mode = fit
        self.setWidgetResizable(fit)
        self.refresh()

    def refresh(self) -> None:
        if self.original is None:
            return
        if self.fit_mode:
            size = self.viewport().size()
            pixmap = self.original.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.label.setMinimumSize(1, 1)
            self.label.resize(size)
        else:
            pixmap = self.original
            self.label.setMinimumSize(pixmap.size())
            self.label.resize(pixmap.size())
        self.label.setText("")
        self.label.setPixmap(pixmap)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self.fit_mode:
            self.refresh()


class RawViewerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1380, 860)
        self.setMinimumSize(980, 650)
        self.setAcceptDrops(True)
        self.path: Path | None = None
        self.frame_count = self.serial = 0
        self.current_qimage: QImage | None = None
        self.direct_qimage: QImage | None = None
        self.processed_qimage: QImage | None = None
        self.pool = QThreadPool.globalInstance()
        self.decode_timer = QTimer(self)
        self.decode_timer.setSingleShot(True)
        self.decode_timer.timeout.connect(self.request_decode)
        self._build_ui()
        self._connect_events()

    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(330)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(26, 26, 26, 24)
        side.setSpacing(14)
        brand = QHBoxLayout()
        logo = QLabel("RV", objectName="logo")
        logo.setAlignment(Qt.AlignCenter)
        titles = QVBoxLayout()
        titles.addWidget(QLabel(APP_NAME, objectName="appTitle"))
        titles.addWidget(QLabel("Bayer RAW 图像预览工具", objectName="muted"))
        brand.addWidget(logo)
        brand.addLayout(titles, 1)
        side.addLayout(brand)
        self.drop_card = DropCard(objectName="dropCard")
        drop = QVBoxLayout(self.drop_card)
        drop.setContentsMargins(16, 17, 16, 17)
        icon = QLabel("＋", objectName="dropIcon")
        icon.setAlignment(Qt.AlignCenter)
        self.file_name = QLabel("拖入 RAW 文件", objectName="dropTitle")
        self.file_name.setAlignment(Qt.AlignCenter)
        self.file_name.setWordWrap(True)
        self.file_meta = QLabel("或点击此处选择", objectName="muted")
        self.file_meta.setAlignment(Qt.AlignCenter)
        self.file_meta.setWordWrap(True)
        drop.addWidget(icon); drop.addWidget(self.file_name); drop.addWidget(self.file_meta)
        side.addWidget(self.drop_card)
        side.addWidget(QLabel("解码设置", objectName="sectionTitle"))
        card = QFrame(objectName="settingsCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setHorizontalSpacing(10); grid.setVerticalSpacing(11)
        self.preset = QComboBox(); self.preset.addItems(["1920 × 1080", "3840 × 2160", "4208 × 3120", "自定义"])
        self.width_box = QSpinBox(); self.width_box.setRange(1, 20000); self.width_box.setValue(1920); self.width_box.setSingleStep(4)
        self.height_box = QSpinBox(); self.height_box.setRange(1, 20000); self.height_box.setValue(1080)
        self.pattern = QComboBox(); self.pattern.addItems(BAYER_PATTERNS)
        self.raw_format = QComboBox(); self.raw_format.addItems(RAW_FORMATS); self.raw_format.setCurrentText("RAW10")
        for row, (name, control) in enumerate((("预设", self.preset), ("宽度", self.width_box), ("高度", self.height_box), ("Bayer", self.pattern), ("格式", self.raw_format))):
            grid.addWidget(QLabel(name), row, 0); grid.addWidget(control, row, 1, 1, 2)
        self.isp_checkbox = QCheckBox("启用白平衡 + 亮度拉伸 + Gamma")
        self.isp_checkbox.setToolTip("统一启用自动灰度世界白平衡、0.5/99.5 百分位拉伸和 Gamma 2.2")
        grid.addWidget(self.isp_checkbox, 5, 0, 1, 3)
        side.addWidget(card)
        row = QHBoxLayout()
        row.addWidget(QLabel("帧选择", objectName="sectionTitle")); row.addStretch()
        self.frame_text = QLabel("0 / 0", objectName="accentText"); row.addWidget(self.frame_text)
        side.addLayout(row)
        frames = QHBoxLayout()
        self.prev_button = QPushButton("‹", objectName="squareButton")
        self.frame_slider = QSlider(Qt.Horizontal); self.frame_slider.setRange(0, 0)
        self.frame_box = QSpinBox(); self.frame_box.setRange(0, 0); self.frame_box.setFixedWidth(70)
        self.next_button = QPushButton("›", objectName="squareButton")
        for control in (self.prev_button, self.frame_slider, self.frame_box, self.next_button): frames.addWidget(control, 1 if control is self.frame_slider else 0)
        side.addLayout(frames)
        self.render_button = QPushButton("重新渲染", objectName="primaryButton")
        self.export_button = QPushButton("导出当前帧为 PNG")
        self.close_button = QPushButton("关闭文件")
        for button in (self.render_button, self.export_button, self.close_button): side.addWidget(button)
        side.addStretch()
        self.status = QLabel("准备就绪", objectName="status"); self.status.setWordWrap(True); side.addWidget(self.status)
        layout.addWidget(sidebar)
        main = QFrame(objectName="mainPanel")
        body = QVBoxLayout(main); body.setContentsMargins(22, 18, 22, 22); body.setSpacing(12)
        top = QHBoxLayout()
        self.header = QLabel("图像预览", objectName="pageTitle")
        self.info_badge = QLabel("未打开文件", objectName="badge")
        self.fit_button = QPushButton("适应窗口"); self.fit_button.setCheckable(True); self.fit_button.setChecked(True)
        self.actual_button = QPushButton("100%"); self.actual_button.setCheckable(True)
        for item in (self.header, self.info_badge): top.addWidget(item)
        top.addStretch(); top.addWidget(self.fit_button); top.addWidget(self.actual_button)
        body.addLayout(top)
        self.image_view = ImageView(); body.addWidget(self.image_view, 1)
        layout.addWidget(main, 1)

    def _connect_events(self) -> None:
        self.drop_card.clicked.connect(self.choose_file)
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.width_box.editingFinished.connect(self.settings_changed)
        self.height_box.editingFinished.connect(self.settings_changed)
        self.pattern.currentTextChanged.connect(lambda _: self.schedule_decode())
        self.raw_format.currentTextChanged.connect(self.settings_changed)
        self.frame_slider.valueChanged.connect(self.frame_box.setValue)
        self.frame_box.valueChanged.connect(self.frame_changed)
        self.prev_button.clicked.connect(lambda: self.frame_box.setValue(max(0, self.frame_box.value() - 1)))
        self.next_button.clicked.connect(lambda: self.frame_box.setValue(min(self.frame_box.maximum(), self.frame_box.value() + 1)))
        self.render_button.clicked.connect(self.request_decode); self.export_button.clicked.connect(self.export_png); self.close_button.clicked.connect(self.close_file)
        self.fit_button.clicked.connect(lambda: self.set_zoom(True)); self.actual_button.clicked.connect(lambda: self.set_zoom(False))
        self.isp_checkbox.toggled.connect(self.update_display_mode)
        QShortcut(QKeySequence.Open, self, activated=self.choose_file)
        QShortcut(QKeySequence.Save, self, activated=self.export_png)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.prev_button.click())
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.next_button.click())

    def dragEnterEvent(self, event: QDragEnterEvent):  # noqa: N802
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()): event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):  # noqa: N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths: self.open_path(paths[0]); event.acceptProposedAction()

    def choose_file(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "选择 RAW 文件", "", "RAW 文件 (*.raw *.raw8 *.raw10 *.raw12 *.bin);;所有文件 (*.*)")
        if value: self.open_path(Path(value))

    def open_path(self, path: Path) -> None:
        if not path.is_file(): QMessageBox.warning(self, APP_NAME, f"文件不存在：\n{path}"); return
        self.path = path; self.file_name.setText(path.name)
        self.file_meta.setText(f"{path.stat().st_size / (1024 * 1024):.1f} MiB\n{path.parent}")
        self.header.setText(path.name); self.frame_box.setValue(0); self.update_frame_count(); self.request_decode()

    def close_file(self) -> None:
        """Clear all state and invalidate any background decode result."""
        self.serial += 1; self.path = None; self.frame_count = 0
        self.current_qimage = self.direct_qimage = self.processed_qimage = None
        self.file_name.setText("拖入 RAW 文件"); self.file_meta.setText("或点击此处选择")
        self.header.setText("图像预览"); self.info_badge.setText("未打开文件")
        self.frame_slider.setRange(0, 0); self.frame_box.setRange(0, 0); self.frame_box.setValue(0); self.frame_text.setText("0 / 0")
        self.image_view.clear(); self.set_status("文件已关闭")

    def apply_preset(self, index: int) -> None:
        presets = ((1920, 1080), (3840, 2160), (4208, 3120))
        if index < len(presets):
            self.width_box.setValue(presets[index][0]); self.height_box.setValue(presets[index][1]); self.settings_changed()

    def settings_changed(self) -> None:
        match = {(1920, 1080): 0, (3840, 2160): 1, (4208, 3120): 2}
        self.preset.blockSignals(True); self.preset.setCurrentIndex(match.get((self.width_box.value(), self.height_box.value()), 3)); self.preset.blockSignals(False)
        self.update_frame_count(); self.schedule_decode()

    def update_frame_count(self) -> None:
        if not self.path: return
        try: size = frame_bytes(self.width_box.value(), self.height_box.value(), self.raw_format.currentText())
        except ValueError as exc: self.frame_count = 0; self.info_badge.setText(str(exc)); return
        complete, remainder = divmod(self.path.stat().st_size, size)
        self.frame_count = complete; maximum = max(0, complete - 1)
        self.frame_slider.setRange(0, maximum); self.frame_box.setRange(0, maximum)
        self.frame_text.setText(f"{self.frame_box.value()} / {maximum}")
        tail = " + 残帧" if remainder else ""
        self.info_badge.setText(f"{self.raw_format.currentText()} · {self.width_box.value()}×{self.height_box.value()} · {complete} 帧{tail}")

    def frame_changed(self, value: int) -> None:
        self.frame_slider.blockSignals(True); self.frame_slider.setValue(value); self.frame_slider.blockSignals(False)
        self.frame_text.setText(f"{value} / {max(0, self.frame_count - 1)}"); self.schedule_decode()

    def schedule_decode(self) -> None:
        if self.path: self.decode_timer.start(160)

    def request_decode(self) -> None:
        if not self.path: return
        width, height, frame, fmt = self.width_box.value(), self.height_box.value(), self.frame_box.value(), self.raw_format.currentText()
        try: size = frame_bytes(width, height, fmt)
        except ValueError as exc: self.show_error(str(exc)); return
        if (frame + 1) * size > self.path.stat().st_size: self.show_error("当前帧不完整或超出文件范围"); return
        self.serial += 1
        worker = DecodeWorker(self.serial, self.path, width, height, frame, self.pattern.currentText(), fmt)
        worker.signals.done.connect(self.decode_done); self.render_button.setEnabled(False); self.set_status(f"正在解码第 {frame} 帧…", "busy"); self.pool.start(worker)

    @Slot(int, object, str)
    def decode_done(self, serial: int, payload: object, error: str) -> None:
        if serial != self.serial: return
        self.render_button.setEnabled(True)
        if error: self.show_error(error); return
        direct, processed = payload
        height, width, _ = direct.shape
        self.direct_qimage = QImage(direct.data, width, height, direct.strides[0], QImage.Format_RGB888).copy()
        self.processed_qimage = QImage(processed.data, width, height, processed.strides[0], QImage.Format_RGB888).copy()
        self.update_display_mode()

    def set_zoom(self, fit: bool) -> None:
        self.fit_button.setChecked(fit); self.actual_button.setChecked(not fit); self.image_view.set_fit(fit)

    def update_display_mode(self) -> None:
        selected = self.processed_qimage if self.isp_checkbox.isChecked() else self.direct_qimage
        if selected is None: return
        self.current_qimage = selected; self.image_view.set_image(QPixmap.fromImage(selected))
        mode = "白平衡 + 拉伸 + Gamma" if self.isp_checkbox.isChecked() else "彩色直显"
        self.set_status(f"第 {self.frame_box.value()} 帧 · {self.raw_format.currentText()} · {self.pattern.currentText()} · {mode}", "ok")

    def export_png(self) -> None:
        if self.current_qimage is None: QMessageBox.information(self, APP_NAME, "请先打开并成功预览一个 RAW 文件。"); return
        value, _ = QFileDialog.getSaveFileName(self, "导出 PNG", f"{self.path.stem if self.path else 'raw'}_frame{self.frame_box.value():03d}.png", "PNG 图片 (*.png)")
        if value:
            if not value.lower().endswith(".png"): value += ".png"
            if self.current_qimage.save(value, "PNG"): self.set_status(f"已导出：{value}", "ok")
            else: self.show_error("PNG 导出失败")

    def set_status(self, text: str, state: str = "") -> None:
        self.status.setProperty("state", state); self.status.setText(text); self.status.style().unpolish(self.status); self.status.style().polish(self.status)

    def show_error(self, text: str) -> None:
        self.set_status(text, "error")


STYLESHEET = """
* { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QWidget#root { background: #07100c; color: #edf3ee; } QFrame#sidebar { background: #0b1710; border-right: 1px solid #233a2a; } QFrame#mainPanel { background: #06100b; } QLabel { color: #e7eee9; background: transparent; }
QLabel#logo { min-width: 48px; max-width: 48px; min-height: 48px; max-height: 48px; border-radius: 14px; background: #8add00; color: #102100; font-size: 16px; font-weight: 800; } QLabel#appTitle, QLabel#pageTitle { font-size: 20px; font-weight: 700; color: white; } QLabel#muted { color: #8d9b91; font-size: 12px; } QLabel#sectionTitle { color: #b7c5bb; font-size: 12px; font-weight: 700; padding-top: 3px; } QLabel#accentText { color: #99e62a; font-weight: 700; } QLabel#badge { color: #c4ec91; background: #1d351c; border: 1px solid #467334; border-radius: 10px; padding: 5px 10px; font-size: 11px; } QLabel#status { color: #a5b8aa; background: #13231a; border: 1px solid #294331; border-radius: 10px; padding: 10px 12px; } QLabel#status[state='busy'] { color: #f6d365; border-color: #6b5d29; } QLabel#status[state='ok'] { color: #9ae62b; border-color: #4a7f29; } QLabel#status[state='error'] { color: #fb7185; border-color: #71303c; }
QFrame#dropCard { background: #102218; border: 1px dashed #85d915; border-radius: 14px; } QFrame#dropCard:hover { background: #17301f; border: 1px solid #9bea26; } QLabel#dropIcon { color: #98e52a; font-size: 28px; } QLabel#dropTitle { color: white; font-weight: 700; } QFrame#settingsCard { background: #102018; border: 1px solid #294331; border-radius: 12px; }
QComboBox, QSpinBox { color: #edf3ee; background: #09150e; border: 1px solid #3b5942; border-radius: 8px; padding: 7px 9px; min-height: 20px; } QComboBox:hover, QSpinBox:hover { border-color: #6d9d57; } QComboBox:focus, QSpinBox:focus { border-color: #8add00; } QComboBox::drop-down { border: none; width: 24px; } QComboBox QAbstractItemView { color: white; background: #13231a; border: 1px solid #3b5942; selection-background-color: #5eaa00; }
QCheckBox { color: #e1ece3; background: #0b1710; border: 1px solid #3b5942; border-radius: 8px; padding: 9px 10px; spacing: 9px; } QCheckBox:checked { color: white; background: #1d351c; border-color: #8add00; } QPushButton { color: #e0e9e2; background: #14251a; border: 1px solid #3b5942; border-radius: 9px; padding: 9px 14px; font-weight: 600; } QPushButton:hover { background: #1d3522; border-color: #6d9d57; } QPushButton:disabled { color: #6e8273; background: #0e1911; border-color: #243529; } QPushButton#primaryButton { color: #132000; background: #8add00; border-color: #a4f12a; } QPushButton#primaryButton:hover { background: #a4f12a; } QPushButton#squareButton { min-width: 22px; max-width: 22px; padding: 7px; font-size: 19px; }
QSlider::groove:horizontal { height: 5px; background: #2d4934; border-radius: 2px; } QSlider::sub-page:horizontal { background: #8add00; border-radius: 2px; } QSlider::handle:horizontal { width: 15px; height: 15px; margin: -5px 0; background: #effbd7; border: 2px solid #8add00; border-radius: 7px; } QScrollArea#imageArea { background: #050b07; border: 1px solid #1f3527; border-radius: 14px; } QScrollArea#imageArea > QWidget > QWidget, QLabel#emptyPreview { background: #050b07; } QLabel#emptyPreview { color: #708a77; font-size: 16px; } QScrollBar:vertical { width: 11px; background: #0b1220; } QScrollBar::handle:vertical { background: #33425e; min-height: 30px; border-radius: 5px; } QScrollBar:horizontal { height: 11px; background: #0b1220; } QScrollBar::handle:horizontal { background: #33425e; min-width: 30px; border-radius: 5px; } QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
"""


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME); app.setStyle("Fusion"); app.setStyleSheet(STYLESHEET)
    window = RawViewerWindow(); window.show()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"): QTimer.singleShot(100, lambda: window.open_path(Path(sys.argv[1])))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

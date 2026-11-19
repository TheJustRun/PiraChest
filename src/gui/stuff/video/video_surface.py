from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QVideoFrame, QVideoSink
from PySide6.QtWidgets import QSizePolicy, QWidget


class VideoSurface(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setStyleSheet("background:black;")
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._frame = QImage()
        self._pending: Optional[QVideoFrame] = None
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)

    def videoSink(self) -> QVideoSink:
        return self._sink

    def _on_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            self._pending = None
            self._frame = QImage()
        else:
            self._pending = frame
        if self.isVisible():
            self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.update()

    def clear(self) -> None:
        self._pending = None
        self._frame = QImage()
        self.update()

    def paintEvent(self, _event) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            image = pending.toImage()
            if not image.isNull():
                self._frame = image
        painter = QPainter(self)
        frame = self._frame
        if frame.isNull():
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
            return
        w, h = self.width(), self.height()
        size = frame.size().scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (w - size.width()) / 2, (h - size.height()) / 2
        if size.width() < w or size.height() < h:
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if size != frame.size():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(QRectF(x, y, size.width(), size.height()), frame)

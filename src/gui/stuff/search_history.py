from __future__ import annotations

import json
import logging
import os

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QGraphicsDropShadowEffect, QHBoxLayout,
                               QSizePolicy, QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CaptionLabel, FluentIcon, FlyoutViewBase,
                            TransparentPushButton, TransparentToolButton)

from src.core.config import paths, settings
from src.core.translations import tr

logger = logging.getLogger(__name__)

_LIMIT = 10
_PATH: str | None = None
_DATA: dict[str, list[str]] | None = None


def _path() -> str:
    global _PATH
    if _PATH is None:
        _PATH = os.path.join(paths.config_dir, 'pirachest_search_history.json')
    return _PATH


def _load() -> dict[str, list[str]]:
    global _DATA
    if _DATA is not None:
        return _DATA
    data: dict[str, list[str]] = {}
    try:
        with open(_path(), 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    items = [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
                    if items:
                        data[str(key)] = items[:_LIMIT]
    except (OSError, ValueError):
        pass
    _DATA = data
    return data


def _save() -> None:
    if _DATA is None:
        return
    try:
        directory = os.path.dirname(_path())
        os.makedirs(directory, exist_ok=True)
        tmp = _path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_DATA, fh, ensure_ascii=False)
        os.replace(tmp, _path())
    except OSError as exc:
        logger.debug('Failed to save search history: %s', exc)


def enabled() -> bool:
    return bool(getattr(settings, 'search_history_enabled', True))


def entries(key: str) -> list[str]:
    return list(_load().get(key, ()))


def record(key: str, text: str) -> None:
    text = (text or '').strip()
    if not enabled() or not text or len(text) > 200:
        return
    data = _load()
    items = [v for v in data.get(key, ()) if v.casefold() != text.casefold()]
    items.insert(0, text)
    data[key] = items[:_LIMIT]
    _save()


def remove(key: str, text: str) -> None:
    data = _load()
    items = [v for v in data.get(key, ()) if v.casefold() != text.casefold()]
    if items:
        data[key] = items
    else:
        data.pop(key, None)
    _save()


def clear(key: str | None = None) -> None:
    data = _load()
    if key is None:
        data.clear()
    else:
        data.pop(key, None)
    _save()


class _HistoryView(FlyoutViewBase):
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self._owner = owner
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(1)

    def _clear(self) -> None:
        for i in reversed(range(self._layout.count())):
            item = self._layout.takeAt(i)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def reload(self) -> None:
        self._clear()

        head = QWidget(self)
        head.setFocusPolicy(Qt.NoFocus)
        head.setFixedHeight(28)
        row = QHBoxLayout(head)
        row.setContentsMargins(8, 2, 2, 2)
        row.setSpacing(0)
        title = CaptionLabel(tr('search.recent', default='Recent searches'), head)
        clear_btn = TransparentToolButton(FluentIcon.DELETE, head)
        clear_btn.setFixedSize(24, 24)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.setToolTip(tr('search.clear_all', default='Clear all'))
        clear_btn.clicked.connect(self._clear_all)
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(clear_btn)
        self._layout.addWidget(head)

        values = entries(self._owner.key)
        if not values:
            empty = CaptionLabel(tr('search.no_history', default='No recent searches yet'), self)
            empty.setContentsMargins(10, 6, 10, 8)
            empty.setFixedHeight(34)
            self._layout.addWidget(empty)
            return
        for value in values:
            self._layout.addWidget(self._row(value))

    def measure_height(self) -> int:
        margins = self._layout.contentsMargins()
        height = margins.top() + margins.bottom()
        count = self._layout.count()
        for i in range(count):
            widget = self._layout.itemAt(i).widget()
            if widget is not None:
                height += widget.height()
        return height + self._layout.spacing() * max(0, count - 1)

    def _row(self, value: str) -> QWidget:
        host = QWidget(self)
        host.setFocusPolicy(Qt.NoFocus)
        host.setFixedHeight(30)
        lay = QHBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        btn = TransparentPushButton('', host)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.clicked.connect(lambda _checked=False, v=value: self._choose(v))
        label = BodyLabel(value, btn)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        btn_lay = QHBoxLayout(btn)
        btn_lay.setContentsMargins(10, 0, 6, 0)
        btn_lay.addWidget(label)
        remove_btn = TransparentToolButton(FluentIcon.CLOSE, host)
        remove_btn.setFixedSize(24, 24)
        remove_btn.setFocusPolicy(Qt.NoFocus)
        remove_btn.setToolTip(tr('search.remove', default='Remove'))
        remove_btn.clicked.connect(lambda _checked=False, v=value: self._remove(v))
        lay.addWidget(btn, 1)
        lay.addWidget(remove_btn)
        return host

    def _choose(self, value: str) -> None:
        self._owner.pick(value)

    def _remove(self, value: str) -> None:
        remove(self._owner.key, value)
        if entries(self._owner.key):
            self.reload()
            self._owner.popup.refresh()
        else:
            self._owner.hide()

    def _clear_all(self) -> None:
        clear(self._owner.key)
        self._owner.hide()


class _HistoryPopup(QWidget):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self._width = 240
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(12, 8, 12, 16)
        self.view = _HistoryView(owner, self)
        shadow = QGraphicsDropShadowEffect(self.view)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.view.setGraphicsEffect(shadow)
        self._outer.addWidget(self.view)

    def reload(self) -> None:
        self.view.reload()

    def _resize(self, width: int) -> None:
        self._width = width
        self.view.setFixedWidth(width)
        height = self.view.measure_height()
        self.view.setFixedHeight(height)
        self.setFixedSize(width + 24, height + 24)

    def show_for(self, edit) -> None:
        window = edit.window()
        if self.parentWidget() is not window:
            self.setParent(window)
        self.reload()
        self._resize(max(240, edit.width()))
        self.position_for(edit)
        self.show()
        self.raise_()

    def position_for(self, edit) -> None:
        window = edit.window()
        if self.parentWidget() is not window:
            return
        point = edit.mapTo(window, QPoint(0, edit.height() + 6)) - QPoint(12, 8)
        x = max(0, min(point.x(), window.width() - self.width()))
        y = point.y()
        if y + self.height() > window.height():
            top = edit.mapTo(window, QPoint(0, 0)).y() - self.height() + 8
            y = max(0, top)
        self.move(x, y)

    def refresh(self) -> None:
        self._resize(self._width)


class _Attacher(QObject):
    def __init__(self, edit, key: str, on_pick=None):
        super().__init__(edit)
        self.edit = edit
        self.key = key
        self.on_pick = on_pick
        self.popup = _HistoryPopup(self)
        self._resettle = QTimer(self)
        self._resettle.setSingleShot(True)
        self._resettle.setInterval(250)
        self._resettle.timeout.connect(self._resettle_show)
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.setInterval(1200)
        self._record_timer.timeout.connect(self._record_pending)
        self._pending = ''
        edit.installEventFilter(self)
        edit.textChanged.connect(self._on_text_changed)
        edit.returnPressed.connect(self._record_now)
        if hasattr(edit, 'searchSignal'):
            edit.searchSignal.connect(self._record_now)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _record_now(self, *_args) -> None:
        self._record_timer.stop()
        text = self.edit.text().strip()
        if enabled() and text:
            record(self.key, text)

    def _record_pending(self) -> None:
        if not self._pending or self._pending != self.edit.text().strip():
            return
        self._record_now()

    def pick(self, value: str) -> None:
        self.hide()
        self.edit.setText(value)
        self._record_timer.stop()
        self.edit.setFocus(Qt.OtherFocusReason)
        if enabled():
            record(self.key, value)
        if self.on_pick is not None:
            self.on_pick(value)
        else:
            self.edit.returnPressed.emit()

    def hide(self) -> None:
        self._resettle.stop()
        self.popup.hide()

    def _resettle_show(self) -> None:
        if not self.popup.isVisible():
            self.show_history()

    def show_history(self) -> None:
        if (not enabled() or not entries(self.key) or not self.edit.isVisible()
                or not self.edit.hasFocus() or self.edit.text().strip()):
            return
        self.popup.show_for(self.edit)

    def _on_text_changed(self, text: str) -> None:
        text = text.strip()
        if text:
            self.hide()
            self._pending = text
            self._record_timer.start()
        else:
            self._record_timer.stop()
            self._pending = ''
            if enabled() and self.edit.hasFocus():
                QTimer.singleShot(0, self.show_history)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Move, QEvent.Type.Resize):
            if (self.popup.isVisible() and isinstance(obj, QWidget)
                    and (obj is self.edit or obj is self.edit.window()
                         or obj.isAncestorOf(self.edit))):
                self.hide()
                if (enabled() and not self.edit.text().strip()
                        and self.edit.hasFocus() and entries(self.key)):
                    self._resettle.start()
        elif obj is self.edit:
            if kind == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    self._record_now()
                    self.hide()
                elif event.key() == Qt.Key_Escape:
                    self.hide()
            elif kind == QEvent.Type.FocusIn:
                if enabled() and not self.edit.text().strip():
                    QTimer.singleShot(100, self.show_history)
            elif kind in (QEvent.Type.FocusOut, QEvent.Type.Hide):
                self.hide()
        return False


def attach(edit, key: str, on_pick=None) -> None:
    existing = getattr(edit, '_pira_search_history', None)
    if existing is not None:
        existing.key = key
        existing.on_pick = on_pick
        existing.popup._owner = existing
        existing.popup.view._owner = existing
        return
    edit._pira_search_history = _Attacher(edit, key, on_pick)

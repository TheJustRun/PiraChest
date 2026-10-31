from __future__ import annotations

import ctypes
import functools
import itertools
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import weakref
import zipfile
from collections import OrderedDict
from contextlib import suppress
from ctypes import byref, c_int, c_long, c_size_t, c_uint, c_void_p, POINTER, sizeof, Structure
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QFileSystemWatcher,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QFontMetricsF,
    QIcon,
    QIconEngine,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QPalette,
    QRadialGradient,
    QRegion,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, isDarkTheme, qconfig, Theme
from qfluentwidgets.common.style_sheet import setStyleSheet, styleSheetManager
from qfluentwidgets.window.fluent_window import FluentTitleBar
from qframelesswindow import TitleBarButton
from qframelesswindow.titlebar.title_bar_buttons import TitleBarButtonState
from qframelesswindow.utils import toggleMaxState


def _widget_global_pos(widget, point):
    p = point
    w = widget
    while w is not None:
        p = p + w.pos()
        w = w.parentWidget()
    return p


def _patch_combobox_menu_exec() -> None:
    try:
        from qfluentwidgets.components.widgets import combo_box as _cb
        from qfluentwidgets.components.widgets.menu import MenuAnimationType, MenuAnimationManager
        from PySide6.QtCore import QPoint
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QAction
    except Exception:
        return

    _ComboBox = getattr(_cb, "ComboBox", None)
    if _ComboBox is None or getattr(_ComboBox, "_ani_kwarg_patch_applied", False):
        return

    def _safe_show_combo_menu(self):
        if not self.items:
            return
        menu = self._createComboMenu()
        for item in self.items:
            action = QAction(item.icon, item.text)
            action.setEnabled(item.isEnabled)
            menu.addAction(action)

        menu.view.itemClicked.connect(lambda i: self._onItemClicked(self.findText(i.text().lstrip())))

        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()

        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        if self.currentIndex() >= 0 and self.items:
            menu.setDefaultAction(menu.actions()[self.currentIndex()])

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pd = _widget_global_pos(self, QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)

        pu = _widget_global_pos(self, QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.view.adjustSize(pd, MenuAnimationType.DROP_DOWN)
            target, aniType = pd, MenuAnimationType.DROP_DOWN
        else:
            menu.view.adjustSize(pu, MenuAnimationType.PULL_UP)
            target, aniType = pu, MenuAnimationType.PULL_UP

        menu.aniManager = MenuAnimationManager.make(menu, aniType)
        menu.aniManager.exec(target)
        menu.show()
        if menu.isSubMenu:
            menu.menuItem.setSelected(True)

    _ComboBox._showComboMenu = _safe_show_combo_menu
    _ComboBox._ani_kwarg_patch_applied = True

_patch_combobox_menu_exec()


LAYOUT: dict[str, str] = {
    "card_padding": "12px 16px",
    "card_radius": "10px",
    "input_padding": "7px 10px",
    "input_radius": "6px",
    "btn_padding": "7px 16px",
    "btn_radius": "6px",
    "group_title_pad_top": "8px",
    "group_title_pad_bottom": "4px",
    "input_border_width": "1px",
    "input_border_width_focus": "2px",
    "dialog_radius": "12px",
}


PALETTE_ROLES: tuple[str, ...] = (
    "page_bg", "card_bg", "card_hover", "card_border", "muted", "primary_text",
    "input_bg", "input_border", "input_border_hover", "input_border_focus",
    "btn_bg", "btn_border", "btn_hover", "btn_text", "accent",
    "detail_title", "detail_subtitle", "detail_meta", "detail_box_bg", "detail_box_border",
    "state_queued", "state_downloading", "state_verifying", "state_paused",
    "state_seeding", "state_completed", "state_error", "state_cancelled",
    "faint_text", "body_text", "surface_tint", "surface_tint_strong", "surface_border",
    "hover_tint", "inactive_dot", "poster_fallback_bg", "section_card_bg",
    "section_card_border", "scrollbar_handle", "scrollbar_handle_hover", "list_bg",
    "nav_bg", "nav_text", "nav_icon", "nav_selected_bg", "nav_selected_text",
    "nav_selected_icon", "nav_hover_bg",
    "dialog_bg", "dialog_border", "dialog_title_text", "dialog_body_text", "dialog_accent_text",
    "icon_color", "accent_text", "on_accent",
)


_ROOTS = ("page_bg", "card_bg", "primary_text", "muted", "accent", "state_queued",
          "state_downloading", "state_verifying", "state_seeding", "state_error")
_ACCENT_ROLES = ("nav_selected_icon", "nav_selected_text", "input_border_focus", "dialog_accent_text",
                 "detail_subtitle")
_RULES = {
    "card_bg": "lift(@page_bg,.035)", "muted": "mix(@primary_text,@page_bg,.4)",
    "accent_text": "@accent", "on_accent": "#ffffff",
    "card_hover": "mix(@card_bg,@primary_text,.06)", "card_border": "alpha(@primary_text,.08)",
    "input_bg": "@card_bg", "input_border": "alpha(@primary_text,.14)",
    "input_border_hover": "alpha(@primary_text,.22)", "input_border_focus": "@accent",
    "btn_bg": "mix(@card_bg,@primary_text,.04)", "btn_border": "@input_border",
    "btn_hover": "mix(@card_bg,@primary_text,.1)", "btn_text": "@primary_text",
    "detail_title": "@primary_text", "detail_subtitle": "@accent", "detail_meta": "@muted",
    "detail_box_bg": "@card_bg", "detail_box_border": "@card_border",
    "state_paused": "@state_queued", "state_cancelled": "@state_queued", "state_completed": "@state_seeding",
    "faint_text": "mix(@muted,@page_bg,.25)", "body_text": "mix(@primary_text,@page_bg,.15)",
    "surface_tint": "alpha(@primary_text,.05)", "surface_tint_strong": "alpha(@primary_text,.09)",
    "surface_border": "alpha(@primary_text,.1)", "hover_tint": "alpha(@primary_text,.06)",
    "inactive_dot": "alpha(@primary_text,.22)", "poster_fallback_bg": "@card_bg",
    "section_card_bg": "mix(@card_bg,@primary_text,.05)", "section_card_border": "@surface_border",
    "scrollbar_handle": "alpha(@primary_text,.16)", "scrollbar_handle_hover": "alpha(@primary_text,.26)",
    "list_bg": "alpha(@page_bg,.5)",
    "nav_bg": "@page_bg", "nav_text": "@primary_text", "nav_icon": "@primary_text",
    "nav_selected_bg": "alpha(@primary_text,.12)", "nav_selected_text": "@accent", "nav_selected_icon": "@accent",
    "nav_hover_bg": "alpha(@primary_text,.07)",
    "dialog_bg": "@page_bg", "dialog_border": "@card_border", "dialog_title_text": "@primary_text",
    "dialog_body_text": "@body_text", "dialog_accent_text": "@accent", "icon_color": "@primary_text",
}
_ROLE_SET = frozenset(PALETTE_ROLES)
_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", re.I)
_FN_RE = re.compile(r"(alpha|mix|lift)\((.*)\)")


def _parse_rgba(text):
    t = text.strip()
    m = _RGB_RE.fullmatch(t)
    if m:
        r, g, b = (max(0, min(255, round(float(m.group(i))))) for i in (1, 2, 3))
        a = 1.0 if m.group(4) is None else float(m.group(4))
        return r, g, b, max(0.0, min(1.0, a / 255 if a > 1 else a))
    if t.startswith("#"):
        h = t[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8):
            try:
                v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
            except ValueError:
                return None
            return v[0], v[1], v[2], (v[3] / 255 if len(v) == 4 else 1.0)
    return None


def _fmt(c) -> str:
    r, g, b, a = c
    if a >= 0.999:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"rgba({r}, {g}, {b}, {f'{a:.3f}'.rstrip('0').rstrip('.') or '0'})"


def _lin(v):
    v /= 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _lum(rgb) -> float:
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])


def _ratio(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def _flat(c, under) -> tuple:
    a = c[3] if len(c) > 3 else 1.0
    return tuple(round(c[i] * a + under[i] * (1 - a)) for i in range(3))


def _toward(rgb, pole: int, t: float) -> tuple:
    end = 255 if pole > 0 else 0
    return tuple(round(x + (end - x) * t) for x in rgb[:3])


def _smooth(lo, hi, x) -> float:
    t = max(0.0, min(1.0, (x - lo) / (hi - lo)))
    return t * t * (3 - 2 * t)


def _legible(fg, surfaces, target: float, pole: Optional[int] = None, flip: bool = True,
             floor: float = 0.0) -> tuple:
    rgb, a = tuple(fg[:3]), fg[3]

    def worst(c):
        return min(_ratio(_flat(tuple(c) + (a,), s), s) for s in surfaces)

    if worst(rgb) >= target:
        return fg
    if pole is None:
        pole = 1 if _lum(rgb) >= sum(_lum(s) for s in surfaces) / len(surfaces) else -1

    def search(p, level):
        best = worst(_toward(rgb, p, 1.0))
        if best < level:
            return None
        lo, hi = 0.0, 1.0
        for _ in range(18):
            mid = (lo + hi) / 2
            if worst(_toward(rgb, p, mid)) >= level:
                hi = mid
            else:
                lo = mid
        return _toward(rgb, p, hi) + (a,)

    got = search(pole, target)
    if got is None and floor:
        best = worst(_toward(rgb, pole, 1.0))
        if best >= floor:
            got = search(pole, max(floor, min(target, best * 0.92)))
    if got is None and flip:
        got = search(-pole, target)
    if got is None:
        got = max((_toward(rgb, p, 1.0) for p in ((pole, -pole) if flip else (pole,))), key=worst) + (a,)
    return got


_SURF = ("page_bg", "card_bg", "section_card_bg")
_NAV_SURF = ("page_bg", "nav_hover_bg", "nav_selected_bg")
_LEGIBILITY = (
    ("primary_text", _SURF, 4.5, True, 0), ("body_text", _SURF, 4.5, True, 0), ("detail_title", _SURF, 4.5, True, 0),
    ("muted", _SURF, 4.0, True, 3.5), ("detail_meta", _SURF, 4.0, True, 3.5), ("faint_text", _SURF, 3.0, True, 2.8),
    ("btn_text", ("btn_bg",), 4.5, True, 0), ("nav_text", _NAV_SURF, 4.5, True, 0), ("nav_icon", _NAV_SURF, 3.0, True, 0),
    ("icon_color", _SURF[:2], 3.0, True, 0),
    ("dialog_title_text", ("dialog_bg",), 4.5, True, 0), ("dialog_body_text", ("dialog_bg",), 4.5, True, 0),
    ("accent_text", _SURF, 4.5, False, 3.0), ("detail_subtitle", _SURF + ("detail_box_bg",), 4.0, False, 3.0),
    ("dialog_accent_text", ("dialog_bg",), 4.5, False, 3.0),
    ("nav_selected_text", ("page_bg", "nav_selected_bg"), 3.5, False, 2.8),
    ("nav_selected_icon", ("page_bg", "nav_selected_bg"), 3.0, False, 2.5),
    *((r, _SURF[:2], 2.5, False, 2.0) for r in ("state_queued", "state_downloading", "state_verifying", "state_paused",
                                              "state_seeding", "state_completed", "state_error", "state_cancelled")),
)


def _enforce_legibility(out: dict, src: dict) -> None:
    def parse(role):
        return _parse_rgba(str(out.get(role) or ""))
    page, prim = parse("page_bg"), parse("primary_text")
    if not page or not prim:
        return
    ink_light = _lum(prim) >= _lum(page)
    page_op = _flat(page, (0, 0, 0) if ink_light else (255, 255, 255))
    cache = {"page_bg": page_op}

    def surface(role):
        if role not in cache:
            c = parse(role)
            cache[role] = _flat(c, page_op) if c else None
        return cache[role]

    for role, roles, target, ink, floor in _LEGIBILITY:
        fg = parse(role)
        surfaces = [x for x in map(surface, roles) if x]
        if not fg or not surfaces:
            continue
        new = _legible(fg, surfaces, target, 1 if ink_light else -1, flip=not ink, floor=floor)
        if new != fg:
            out[role] = _fmt(new)
    if "on_accent" not in src:
        acc = parse("accent")
        if acc:
            a = _flat(acc, page_op)
            out["on_accent"] = "#ffffff" if _ratio((255, 255, 255), a) >= _ratio((17, 17, 17), a) else "#111111"


_SURFACE_ROOTS = frozenset({"page_bg", "card_bg", "primary_text"})
_REF_RE = re.compile(r"@(\w+)")


def _rederive_stale(src: dict, touched: set) -> None:
    if touched & {"page_bg", "card_bg"}:
        for role in ("nav_bg", "nav_selected_bg", "nav_hover_bg"):
            v = src.get(role)
            if role not in touched and isinstance(v, str) and _parse_rgba(v) is not None:
                del src[role]
    known = set(touched & _SURFACE_ROOTS)
    if not known:
        return
    grew = True
    while grew:
        grew = False
        for role, rule in _RULES.items():
            if role not in known and any(r in known for r in _REF_RE.findall(rule)):
                known.add(role)
                grew = True
    for role in known - touched - {"list_bg"}:
        v = src.get(role)
        if isinstance(v, str) and _parse_rgba(v) is not None:
            del src[role]


def _apply_fn(name, args, color):
    try:
        if name == "lift":
            c, t = color(args[0]), float(args[1])
            lum = _lum(c[:3])
            f = 1 + 1.2 * max(0.0, 1 - abs(lum - 0.25) / 0.25) + 11 * _smooth(0.25, 0.6, lum)
            return _fmt(_toward(c, 1, min(1.0, t * f)) + (c[3],))
        if name == "alpha":
            r, g, b, a = color(args[0])
            return _fmt((r, g, b, a * float(args[1])))
        c1, c2, t = color(args[0]), color(args[1]), float(args[2])
        rgb = tuple(round(x * (1 - t) + y * t) for x, y in zip(c1[:3], c2[:3], strict=True))
        return _fmt(rgb + (c1[3] * (1 - t) + c2[3] * t,))
    except (TypeError, ValueError, IndexError):
        return None


def resolve_palette(src: dict, errors: Optional[list] = None) -> dict:
    expr = {**_RULES, **src}
    out: dict = {}
    busy: set = set()

    def note(msg):
        if errors is not None:
            errors.append(msg)

    def color(arg):
        arg = arg.strip()
        return _parse_rgba(val(arg[1:]) if arg.startswith("@") else arg)

    def val(role):
        if role in out:
            return out[role]
        raw = expr.get(role)
        if raw is None or role in busy:
            note(f"{role}: {'circular reference' if raw is not None else 'undefined role'}")
            out[role] = "#ff00ff"
            return "#ff00ff"
        busy.add(role)
        text = str(raw).strip()
        if text.startswith("@"):
            res = val(text[1:])
        else:
            m = _FN_RE.fullmatch(text)
            res = _apply_fn(m.group(1), [a.strip() for a in m.group(2).split(",")], color) if m else None
            if res is None:
                parsed = _parse_rgba(text)
                if parsed:
                    res = _fmt(parsed)
                else:
                    res = text
                    if m:
                        note(f"{role}: cannot evaluate '{text}'")
        busy.discard(role)
        out[role] = res
        return res

    pg, pt = color("@page_bg"), color("@primary_text")
    if pg and pt:
        under = (0, 0, 0) if pg[3] >= 0.999 or _lum(pg[:3]) < 0.5 else (255, 255, 255)
        page_op = _flat(pg, under)
        if _ratio(_flat(pt, page_op), page_op) < 2.0:
            expr["primary_text"] = "#f4f4f5" if _ratio((244, 244, 245), page_op) >= _ratio((17, 19, 24), page_op) else "#111318"
            out.clear()
    res = {r: val(r) for r in PALETTE_ROLES}
    _enforce_legibility(res, src)
    return res


_SRC: dict = {
    "dark": {
        "page_bg": "#121212", "primary_text": "#eaeaea",
        "muted": "#9a9a9a", "accent": "#c9c9c9", "state_queued": "#7a7a7a",
        "state_downloading": "#3B9DF3", "state_verifying": "#E08838", "state_seeding": "#3FC240",
        "state_error": "#E5484D",
        "card_hover": "mix(@card_bg,@primary_text,.045)", "btn_hover": "mix(@card_bg,@primary_text,.07)",
        "section_card_bg": "mix(@card_bg,@primary_text,.03)",
        "list_bg": "rgba(0, 0, 0, 0.30)", "nav_selected_text": "@primary_text", "dialog_bg": "@card_bg",
    },
    "light": {
        "page_bg": "#F3F3F4", "card_bg": "#FFFFFF", "primary_text": "#101828",
        "muted": "#5B6472", "accent": "#0078D4", "state_queued": "#6B7280",
        "state_downloading": "#0078D4", "state_verifying": "#C15A0A", "state_seeding": "#0E7A0E",
        "state_error": "#C42B1C",
        "section_card_bg": "mix(@card_bg,@primary_text,.04)", "btn_bg": "@card_bg",
        "detail_box_bg": "@section_card_bg", "poster_fallback_bg": "@surface_tint",
        "nav_bg": "alpha(@primary_text,.02)",
        "list_bg": "rgba(0, 0, 0, 0.035)", "dialog_bg": "@card_bg",
    },
    "amoled": {
        "page_bg": "#000000", "card_bg": "#0A0A0A", "primary_text": "#F2F2F2",
        "muted": "#8A8F98", "accent": "#FFFFFF", "state_queued": "#7A7F87",
        "state_downloading": "#00B8D4", "state_verifying": "#E08838", "state_seeding": "#00E676",
        "state_error": "#FF5252", "card_hover": "#141414", "card_border": "rgba(255, 255, 255, 0.08)",
        "input_bg": "#0D0D0D", "input_border": "rgba(255, 255, 255, 0.12)", "input_border_hover": "rgba(255, 255, 255, 0.20)",
        "btn_bg": "#0D0D0D", "btn_border": "rgba(255, 255, 255, 0.12)", "btn_hover": "#161616",
        "detail_box_border": "rgba(255, 255, 255, 0.08)", "faint_text": "#6E7178", "body_text": "#D0D2D6",
        "surface_tint": "rgba(255, 255, 255, 0.05)", "surface_tint_strong": "rgba(255, 255, 255, 0.09)", "surface_border": "rgba(255, 255, 255, 0.10)",
        "hover_tint": "rgba(255, 255, 255, 0.06)", "inactive_dot": "rgba(255, 255, 255, 0.22)", "section_card_bg": "#0D0D0D",
        "section_card_border": "rgba(255, 255, 255, 0.10)", "scrollbar_handle": "rgba(255, 255, 255, 0.16)", "scrollbar_handle_hover": "rgba(255, 255, 255, 0.26)",
        "list_bg": "rgba(0, 0, 0, 0.35)", "nav_selected_bg": "#141414", "nav_hover_bg": "#0D0D0D",
        "dialog_bg": "#0A0A0A", "dialog_border": "rgba(255, 255, 255, 0.10)", "dialog_body_text": "#D0D2D6",
        "icon_color": "#FFFFFF",
    },
    "catppuccin_rose": {
        "page_bg": "#f2e6e8", "card_bg": "#f8f0f1", "primary_text": "#4a3238",
        "muted": "#8a6f76", "accent": "#ea76cb", "state_queued": "#a68d93",
        "state_downloading": "#ea76cb", "state_verifying": "#fe640b", "state_seeding": "#40a02b",
        "state_error": "#e64553", "card_hover": "#efe0e2", "card_border": "rgba(76, 44, 58, 0.12)",
        "input_border": "#e0c9cd", "input_border_hover": "#d0b0b6", "btn_bg": "#f8f0f1",
        "btn_border": "#e0c9cd", "btn_hover": "#efe0e2", "detail_subtitle": "#dd7878",
        "detail_box_bg": "#efe0e2", "detail_box_border": "#e0c9cd", "faint_text": "#a68d93",
        "body_text": "#5c4249", "surface_tint": "rgba(76, 44, 58, 0.05)", "surface_tint_strong": "rgba(76, 44, 58, 0.09)",
        "surface_border": "rgba(76, 44, 58, 0.12)", "hover_tint": "rgba(76, 44, 58, 0.06)", "inactive_dot": "rgba(76, 44, 58, 0.22)",
        "poster_fallback_bg": "#efe0e2", "section_card_bg": "#efe0e2", "section_card_border": "rgba(76, 44, 58, 0.12)",
        "scrollbar_handle": "rgba(76, 44, 58, 0.20)", "scrollbar_handle_hover": "rgba(76, 44, 58, 0.32)", "list_bg": "rgba(76, 44, 58, 0.04)",
        "nav_bg": "#f8f0f1", "nav_selected_bg": "#efe0e2", "nav_selected_icon": "#dd7878",
        "nav_hover_bg": "#f2e6e8", "dialog_bg": "#f8f0f1", "dialog_border": "#e0c9cd",
        "dialog_body_text": "#5c4249", "dialog_accent_text": "#dd7878", "icon_color": "#c98fae",
    },
    "dracula": {
        "page_bg": "#282a36", "card_bg": "#21222c", "primary_text": "#f8f8f2",
        "muted": "#6272a4", "accent": "#bd93f9", "state_queued": "#6272a4",
        "state_downloading": "#8be9fd", "state_verifying": "#ffb86c", "state_seeding": "#50fa7b",
        "state_error": "#ff5555", "card_hover": "#343746", "input_border": "#44475a",
        "input_border_hover": "#6272a4", "btn_bg": "#343746", "btn_border": "#44475a",
        "btn_hover": "#44475a", "detail_subtitle": "#ff79c6", "detail_box_border": "#44475a",
        "faint_text": "#6272a4", "body_text": "#d6d6e0", "section_card_bg": "#343746",
        "section_card_border": "#44475a", "list_bg": "rgba(33, 34, 44, 0.5)", "nav_bg": "#21222c",
        "nav_selected_bg": "#343746", "nav_selected_text": "#ff79c6", "nav_hover_bg": "#2b2d3a",
        "dialog_bg": "#21222c", "dialog_border": "#44475a", "dialog_body_text": "#d6d6e0",
        "dialog_accent_text": "#ff79c6",
    },
    "nord": {
        "page_bg": "#2e3440", "card_bg": "#3b4252", "primary_text": "#eceff4",
        "muted": "#7b88a1", "accent": "#88c0d0", "state_queued": "#4c566a",
        "state_downloading": "#81a1c1", "state_verifying": "#d08770", "state_seeding": "#a3be8c",
        "state_error": "#bf616a", "card_hover": "#434c5e", "card_border": "rgba(216, 222, 233, 0.08)",
        "input_border": "#434c5e", "input_border_hover": "#4c566a", "btn_bg": "#434c5e",
        "btn_border": "#4c566a", "btn_hover": "#4c566a", "detail_subtitle": "#81a1c1",
        "detail_box_border": "#434c5e", "faint_text": "#7b88a1", "body_text": "#d8dee9",
        "surface_tint": "rgba(216, 222, 233, 0.05)", "surface_tint_strong": "rgba(216, 222, 233, 0.09)", "surface_border": "rgba(216, 222, 233, 0.10)",
        "hover_tint": "rgba(216, 222, 233, 0.06)", "inactive_dot": "rgba(216, 222, 233, 0.22)", "section_card_bg": "#434c5e",
        "section_card_border": "#4c566a", "scrollbar_handle": "rgba(216, 222, 233, 0.16)", "scrollbar_handle_hover": "rgba(216, 222, 233, 0.26)",
        "nav_selected_bg": "#3b4252", "nav_selected_icon": "#81a1c1", "nav_hover_bg": "#333947",
        "dialog_border": "#4c566a", "dialog_body_text": "#d8dee9", "dialog_accent_text": "#81a1c1",
        "icon_color": "#8fbcbb",
    },
    "gruvbox_dark": {
        "page_bg": "#282828", "card_bg": "#32302f", "primary_text": "#ebdbb2",
        "muted": "#928374", "accent": "#fe8019", "state_queued": "#928374",
        "state_downloading": "#83a598", "state_verifying": "#fabd2f", "state_seeding": "#b8bb26",
        "state_error": "#fb4934", "card_hover": "#3c3836", "input_border": "#504945",
        "input_border_hover": "#665c54", "btn_bg": "#3c3836", "btn_border": "#504945",
        "btn_hover": "#504945", "detail_subtitle": "#d3869b", "detail_box_border": "#504945",
        "faint_text": "#7c6f64", "body_text": "#d5c4a1", "section_card_bg": "#3c3836",
        "section_card_border": "#504945", "nav_selected_bg": "#3c3836", "nav_selected_icon": "#fabd2f",
        "dialog_border": "#504945", "dialog_body_text": "#d5c4a1", "dialog_accent_text": "#d3869b",
        "icon_color": "#d3869b",
    },
    "tokyo_night": {
        "page_bg": "#1a1b26", "card_bg": "#16161e", "primary_text": "#c0caf5",
        "muted": "#565f89", "accent": "#7aa2f7", "state_queued": "#565f89",
        "state_downloading": "#7dcfff", "state_verifying": "#ff9e64", "state_seeding": "#9ece6a",
        "state_error": "#f7768e", "card_hover": "#232433", "input_border": "#292e42",
        "input_border_hover": "#414868", "btn_bg": "#292e42", "btn_border": "#414868",
        "btn_hover": "#414868", "detail_subtitle": "#bb9af7", "detail_box_border": "#292e42",
        "faint_text": "#565f89", "body_text": "#a9b1d6", "section_card_bg": "#292e42",
        "section_card_border": "#414868", "list_bg": "rgba(22, 22, 30, 0.5)", "nav_bg": "#16161e",
        "nav_selected_bg": "#292e42", "nav_selected_text": "#bb9af7", "nav_hover_bg": "#1e1f2c",
        "dialog_bg": "#16161e", "dialog_border": "#292e42", "dialog_body_text": "#a9b1d6",
        "dialog_accent_text": "#bb9af7",
    },
    "rose_pine": {
        "page_bg": "#191724", "card_bg": "#1f1d2e", "primary_text": "#e0def4",
        "muted": "#6e6a86", "accent": "#ebbcba", "state_queued": "#6e6a86",
        "state_downloading": "#9ccfd8", "state_verifying": "#f6c177", "state_seeding": "#31748f",
        "state_error": "#eb6f92", "card_hover": "#26233a", "input_border": "#403d52",
        "input_border_hover": "#524f67", "btn_bg": "#26233a", "btn_border": "#403d52",
        "btn_hover": "#403d52", "detail_subtitle": "#c4a7e7", "detail_meta": "#908caa",
        "detail_box_border": "#403d52", "faint_text": "#6e6a86", "body_text": "#908caa",
        "section_card_bg": "#26233a", "section_card_border": "#403d52", "nav_selected_bg": "#26233a",
        "nav_selected_icon": "#c4a7e7", "dialog_border": "#403d52", "dialog_body_text": "#908caa",
        "dialog_accent_text": "#c4a7e7",
    },
    "solarized_dark": {
        "page_bg": "#002b36", "card_bg": "#073642", "primary_text": "#eee8d5",
        "muted": "#93a1a1", "accent": "#2aa198", "state_queued": "#657b83",
        "state_downloading": "#268bd2", "state_verifying": "#b58900", "state_seeding": "#859900",
        "state_error": "#dc322f", "card_hover": "#0a4351", "card_border": "rgba(147, 161, 161, 0.10)",
        "input_border": "#0a4351", "input_border_hover": "#586e75", "btn_bg": "#0a4351",
        "btn_border": "#586e75", "btn_hover": "#0e5162", "detail_subtitle": "#268bd2",
        "detail_box_border": "#0a4351", "faint_text": "#657b83", "body_text": "#c3cfd1",
        "surface_tint": "rgba(147, 161, 161, 0.06)", "surface_tint_strong": "rgba(147, 161, 161, 0.10)", "surface_border": "rgba(147, 161, 161, 0.12)",
        "hover_tint": "rgba(147, 161, 161, 0.07)", "inactive_dot": "rgba(147, 161, 161, 0.24)", "section_card_bg": "#0a4351",
        "section_card_border": "#586e75", "scrollbar_handle": "rgba(147, 161, 161, 0.18)", "scrollbar_handle_hover": "rgba(147, 161, 161, 0.28)",
        "nav_selected_bg": "#073642", "nav_selected_icon": "#268bd2", "nav_hover_bg": "#04404e",
        "dialog_border": "#0a4351", "dialog_body_text": "#c3cfd1", "dialog_accent_text": "#268bd2",
        "icon_color": "#b58900",
    },
    "everforest": {
        "page_bg": "#2b3339", "card_bg": "#323c41", "primary_text": "#d3c6aa",
        "muted": "#9da9a0", "accent": "#a7c080", "state_queued": "#7a8478",
        "state_downloading": "#7fbbb3", "state_verifying": "#dbbc7f", "state_seeding": "#a7c080",
        "state_error": "#e67e80", "card_hover": "#3a454a", "card_border": "rgba(211, 198, 170, 0.09)",
        "input_border": "#4a555b", "input_border_hover": "#5c6a63", "btn_bg": "#3a454a",
        "btn_border": "#4a555b", "btn_hover": "#4a555b", "detail_subtitle": "#83c092",
        "detail_box_border": "#4a555b", "faint_text": "#7a8478", "body_text": "#c6d1b8",
        "section_card_bg": "#3a454a", "section_card_border": "#4a555b", "nav_selected_bg": "#3a454a",
        "nav_selected_icon": "#83c092", "nav_hover_bg": "#333e43", "dialog_border": "#4a555b",
        "dialog_body_text": "#c6d1b8", "dialog_accent_text": "#83c092",
    },
    "cyberpunk": {
        "page_bg": "#0d0221", "card_bg": "#170a33", "primary_text": "#f5f0ff",
        "muted": "#9d8ec9", "accent": "#ff2e88", "state_queued": "#7a6ba0",
        "state_downloading": "#00f0ff", "state_verifying": "#ffb800", "state_seeding": "#39ff88",
        "state_error": "#ff2e6e", "card_hover": "#231143", "card_border": "rgba(255, 0, 122, 0.14)",
        "input_border": "#3a1d63", "input_border_hover": "#5a2a8f", "input_border_focus": "#00f0ff",
        "btn_bg": "#231143", "btn_border": "#3a1d63", "btn_hover": "#331759",
        "detail_subtitle": "#00f0ff", "detail_box_border": "#3a1d63", "faint_text": "#7a6ba0",
        "body_text": "#d6c9ff", "surface_tint": "rgba(255, 46, 136, 0.06)", "surface_tint_strong": "rgba(255, 46, 136, 0.11)",
        "surface_border": "rgba(0, 240, 255, 0.14)", "hover_tint": "rgba(0, 240, 255, 0.07)", "inactive_dot": "rgba(245, 240, 255, 0.24)",
        "section_card_bg": "#231143", "section_card_border": "#3a1d63", "scrollbar_handle": "rgba(255, 46, 136, 0.22)",
        "scrollbar_handle_hover": "rgba(0, 240, 255, 0.32)", "list_bg": "rgba(13, 2, 33, 0.55)", "nav_selected_bg": "#231143",
        "nav_selected_text": "#00f0ff", "dialog_bg": "#170a33", "dialog_border": "#3a1d63",
        "dialog_body_text": "#d6c9ff", "dialog_accent_text": "#00f0ff",
    },
    "vgui": {
        "page_bg": "#333d2e", "card_bg": "#3f4a38", "primary_text": "#eef0e2",
        "muted": "#a7b08f", "accent": "#7a9c34", "state_queued": "#8a9478",
        "state_downloading": "#66c0f4", "state_verifying": "#d9a441", "state_seeding": "#7a9c34",
        "state_error": "#c15c4a", "card_hover": "#4a5641", "card_border": "rgba(140, 168, 88, 0.18)",
        "input_border": "#5a6b4e", "input_border_hover": "#6d8058", "input_border_focus": "#8db83c",
        "btn_bg": "#4a5641", "btn_border": "#5a6b4e", "btn_hover": "#56643f",
        "detail_subtitle": "#a8cf4a", "detail_box_border": "#5a6b4e", "faint_text": "#828c6f",
        "body_text": "#d3d8c3", "surface_tint": "rgba(140, 168, 88, 0.07)", "surface_tint_strong": "rgba(140, 168, 88, 0.13)",
        "surface_border": "rgba(140, 168, 88, 0.16)", "hover_tint": "rgba(140, 168, 88, 0.09)", "inactive_dot": "rgba(238, 240, 226, 0.24)",
        "section_card_bg": "#4a5641", "section_card_border": "#5a6b4e", "scrollbar_handle": "rgba(140, 168, 88, 0.22)",
        "scrollbar_handle_hover": "rgba(140, 168, 88, 0.34)", "list_bg": "rgba(51, 61, 46, 0.55)", "nav_bg": "#2b3327",
        "nav_selected_bg": "#4a5641", "nav_selected_text": "#a8cf4a", "nav_hover_bg": "#3a4534",
        "dialog_bg": "#3a4534", "dialog_border": "#5a6b4e", "dialog_body_text": "#d3d8c3",
        "dialog_accent_text": "#a8cf4a", "icon_color": "#d3d8c3",
    },
}


_BACKDROPS = ("none", "acrylic", "blur")
_CORNERS = ("default", "square", "round", "small")


def _clamp_int(value, low: int, high: int, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return max(low, min(high, number))


def _opt_str(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _num(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _pair(value, default: tuple) -> tuple:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_num(v, None) is not None for v in value):
        return int(value[0]), int(value[1])
    return default


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _layers(value) -> tuple:
    return tuple(v for v in value if isinstance(v, dict)) if isinstance(value, (list, tuple)) else ()


_TITLEBAR_DEFAULTS: dict = {
    "mode": "overlay",
    "height": 48,
    "icon": 20,
    "title": {"size": 14, "bold": True, "color": "@nav_text", "padding": 4},
    "chips": {
        "height": 22, "radius": 11, "padding": 10, "spacing": 5, "gap": 8, "icon": 13, "size": 12,
        "inherit_buttons": False,


        "style": {
            "normal": {"text": "@nav_text", "fill": "@surface_tint_strong"},
            "hover": {"fill": "@surface_border"},
            "pressed": {"fill": "@surface_border"},
        },
    },
    "nav": {
        "radius": 5,
        "text": "@nav_text", "selected_text": "@nav_selected_text",
        "icon": "@nav_icon", "selected_icon": "@nav_selected_icon",
        "indicator": "@nav_selected_icon", "separator": "@surface_border",
        "hover": {"fill": "@nav_hover_bg"},
        "selected": {"fill": "@nav_selected_bg"},

        "text_glow": {},
    },





    "chrome": "linked",
    "sidebar": {},
    "buttons": {
        "width": 46, "height": 32, "spacing": 0, "bleed": 0,
        "glyphs": {
            "min": [{"shape": "rect", "x": -5, "y": 0, "w": 10, "h": 1}],
            "max": [{"shape": "ring", "x": -5, "y": -5, "w": 10, "h": 10, "t": 1}],
            "restore": [
                {"shape": "ring", "x": -3, "y": -5, "w": 8, "h": 8, "t": 1, "clip_out": [-5, -3, 8, 8]},
                {"shape": "ring", "x": -5, "y": -3, "w": 8, "h": 8, "t": 1},
            ],
            "close": [
                {"shape": "line", "points": [[-5, -5], [5, 5]], "width": 1},
                {"shape": "line", "points": [[5, -5], [-5, 5]], "width": 1},
            ],
        },
        "style": {
            "normal": {"glyph": "@nav_text"},
            "hover": {"fill": "@surface_tint_strong"},
            "pressed": {"fill": "@surface_border"},
        },
        "kinds": {"close": {"style": {
            "hover": {"fill": "#E81123", "glyph": "#FFFFFF"},
            "pressed": {"fill": "#F1707A", "glyph": "#FFFFFF"},
        }}},
    },
}


@dataclass(frozen=True)
class TitleBarSpec:
    mode: str = "overlay"
    height: int = 48
    height_maximized: int = 0
    frame: int = 0
    frame_maximized: int = 0
    inset_left: int = 46
    padding: tuple = (0, 0)
    padding_maximized: tuple = (0, 0)
    icon: int = 20
    corner: Optional[str] = None
    dark: Optional[bool] = None
    backdrop_tint: Optional[str] = None
    force_fallback: bool = False
    title: dict = field(default_factory=dict)
    buttons: dict = field(default_factory=dict)
    chips: dict = field(default_factory=dict)
    nav: dict = field(default_factory=dict)
    hole: str = "client"
    nav_background: Optional[str] = None
    chrome: str = "linked"
    sidebar: dict = field(default_factory=dict)
    layers: tuple = ()
    fallback: tuple = ()

    @classmethod
    def from_dict(cls, data) -> Optional["TitleBarSpec"]:
        if not isinstance(data, dict):
            return None
        d = _deep_merge(_TITLEBAR_DEFAULTS, data)
        framed = str(d.get("mode")).strip().lower() == "frame"
        corner = (_opt_str(d.get("corner")) or "").lower()
        padding = _pair(d.get("padding"), (0, 0))
        chrome = str(d.get("chrome") or "linked").strip().lower()
        if chrome not in ("linked", "split"):
            chrome = "linked"
        return cls(
            mode="frame" if framed else "overlay",
            height=_clamp_int(d.get("height"), 16, 96, 48),
            height_maximized=_clamp_int(d.get("height_maximized"), 16, 96),
            frame=_clamp_int(d.get("frame"), 1, 24) if framed else 0,
            frame_maximized=_clamp_int(d.get("frame_maximized"), 1, 24) if framed else 0,
            inset_left=int(_num(d.get("inset_left"), 0 if framed else 46)),
            padding=padding,
            padding_maximized=_pair(d.get("padding_maximized"), padding),
            icon=_clamp_int(d.get("icon"), 8, 48, 20),
            corner=corner if corner in _CORNERS else None,
            dark=d["dark"] if isinstance(d.get("dark"), bool) else None,
            backdrop_tint=_opt_str(d.get("backdrop_tint")),
            force_fallback=bool(d.get("force_fallback", False)),
            title=_dict(d.get("title")),
            buttons=_dict(d.get("buttons")),
            chips=_dict(d.get("chips")),
            nav=_dict(d.get("nav")),
            hole="content" if d.get("hole") == "content" else "client",
            nav_background=_opt_str(d.get("nav_background")),
            chrome=chrome,
            sidebar=_dict(d.get("sidebar")),
            layers=_layers(d.get("layers")),
            fallback=_layers(d.get("fallback")),
        )


_DEFAULT_TITLEBAR = TitleBarSpec.from_dict({})


def _parse_backdrop(data: dict) -> Optional[str]:
    value = data.get("backdrop")
    if isinstance(value, str) and value.strip().lower() in _BACKDROPS:
        return value.strip().lower()
    return None


_BASE = {True: _SRC["dark"], False: _SRC["light"]}
_DARK = resolve_palette(_SRC["dark"])
_LIGHT = resolve_palette(_SRC["light"])
_BUILTIN_META = (
    ("amoled", "AMOLED", True, False), ("catppuccin_rose", "Catppuccin Rosé", False, True),
    ("dracula", "Dracula", True, True), ("nord", "Nord", True, True),
    ("gruvbox_dark", "Gruvbox Dark", True, True), ("tokyo_night", "Tokyo Night", True, True),
    ("rose_pine", "Rosé Pine", True, True), ("solarized_dark", "Solarized Dark", True, True),
    ("everforest", "Everforest", True, True), ("cyberpunk", "Cyberpunk", True, True),
    ("vgui", "VGUI", True, True),
)
_log = logging.getLogger(__name__)
_POPOUT_SFX_HOOK: list = [None]


def set_popout_sfx_hook(fn) -> None:
    _POPOUT_SFX_HOOK[0] = fn


def _accent_src(accent: str) -> dict:
    return {"accent": accent, **dict.fromkeys(_ACCENT_ROLES, "@accent")}


def _int(value, lo, hi, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _float(value, lo, hi, default):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n)) if n == n else default


@dataclass(frozen=True)
class ThemePreset:
    id: str
    label: str
    palette: dict
    base_dark: bool
    font_families: Optional[tuple] = None
    font_paths: Optional[tuple] = None
    locked_accent: bool = True
    background_path: Optional[str] = None
    background_blur: int = 12
    background_darken: int = 45
    background_scope: str = "client"
    sfx_paths: Optional[dict] = None
    background_type: str = "image"
    background_shader_path: Optional[str] = None
    background_shader_image_path: Optional[str] = None
    background_shader_fps: float = 0.0
    background_shader_aa: int = 0
    icons: Optional[dict] = None
    backdrop: Optional[str] = None
    titlebar: Optional[TitleBarSpec] = None
    transparent_window: bool = False
    src: dict = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    custom_qss: str = ""
    dir: Optional[str] = None
    rules: tuple = ()
    widget_icons: tuple = ()
    win7_frame: bool = False

    @property
    def accent(self) -> str:
        return self.palette["accent"]

    def with_accent(self, accent: Optional[str]) -> "ThemePreset":
        if not accent:
            return self
        return replace(self, palette=resolve_palette({**self.src, **_accent_src(accent)}))


BUILTIN_PRESETS: dict = {
    pid: ThemePreset(pid, label, resolve_palette(_SRC[pid]), dark, locked_accent=locked, src=_SRC[pid])
    for pid, label, dark, locked in _BUILTIN_META
}



@functools.lru_cache(maxsize=1)
def _themes_dir() -> str:
    try:
        from src.core.config import paths
        base = paths.app_data_dir
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".pirachest")
    d = os.path.join(base, "themes")
    with suppress(OSError):
        os.makedirs(d, exist_ok=True)
    return d


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_json(path):
    try:
        return _load_json(path)
    except (OSError, ValueError):
        return None


def _write_json(path, data) -> None:
    tmp = path + ".tmp"
    with suppress(OSError):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def _user_accent_color() -> Optional[str]:
    try:
        from src.core.config import settings
        c = str(getattr(settings, "accent_color", "") or "").strip()
        return c if _parse_rgba(c) else None
    except Exception:
        return None




_SFX_KEYS = ("hover", "click", "navigate", "ambiance", "popout_info", "popout_success",
             "popout_warning", "popout_error")


def _resolve_asset(theme_dir, value) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    path = value if os.path.isabs(value) else os.path.normpath(os.path.join(theme_dir, value))
    if not os.path.isabs(value) and not path.startswith(os.path.normpath(theme_dir) + os.sep):
        return None
    return path if os.path.isfile(path) else None


def _resolve_icon_image(theme_dir, value) -> Optional[str]:
    if isinstance(value, str) and value and not os.path.isabs(value) and os.sep not in value and "/" not in value:
        return _resolve_asset(theme_dir, os.path.join("icons", value)) or _resolve_asset(theme_dir, value)
    return _resolve_asset(theme_dir, value)


def _parse_icon_spec(theme_dir, spec) -> Optional[dict]:
    if isinstance(spec, str):
        name = spec.strip().upper()
        return {"type": "fluent", "value": name} if name else None
    if isinstance(spec, dict):
        image = _resolve_icon_image(theme_dir, spec.get("image"))
        if image:
            return {"type": "image", "value": image, "crop": bool(spec.get("crop", True))}
        fluent = spec.get("fluent")
        if isinstance(fluent, str) and fluent.strip():
            return {"type": "fluent", "value": fluent.strip().upper()}
    return None


def _parse_background(theme_dir, bg) -> dict:
    out = dict(background_type="image", background_path=None, background_blur=12, background_darken=45,
               background_scope="client", background_shader_path=None, background_shader_image_path=None,
               background_shader_fps=0.0, background_shader_aa=0)
    if not isinstance(bg, dict):
        return out
    scope = str(bg.get("scope") or "client").strip().lower()
    out["background_scope"] = scope if scope in ("content", "client", "window") else "client"
    kind = str(bg.get("type") or "").strip().lower()
    src = bg.get("file")
    if kind not in ("image", "shader") and isinstance(bg.get("shader"), str):
        kind = "shader"
    if kind == "shader" and not (isinstance(src, str) and src.strip()):
        src = bg.get("shader")
    path = _resolve_asset(theme_dir, src)
    if kind == "shader":
        if path:
            aa = _int(bg.get("aa"), 0, 8, 0)
            out.update(background_type="shader", background_shader_path=path,
                       background_shader_image_path=_resolve_asset(theme_dir, bg.get("image")),
                       background_shader_fps=_float(bg.get("fps"), 0.0, 240.0, 0.0),
                       background_shader_aa=aa if aa in (0, 2, 4, 8) else 0)
        return out
    if path:
        out.update(background_path=path, background_blur=_int(bg.get("blur"), 0, 100, 12),
                   background_darken=_int(bg.get("darken"), 0, 100, 45))
    return out


class PresetRegistry:

    def __init__(self):
        self.errors: dict = {}
        self.warnings: dict = {}
        self._all: dict = dict(BUILTIN_PRESETS)
        self.reload()

    def reload(self) -> None:
        merged, errors, warns = dict(BUILTIN_PRESETS), {}, {}
        root = _themes_dir()
        try:
            names = sorted(n for n in os.listdir(root)
                           if not n.startswith(("_", ".")) and os.path.isdir(os.path.join(root, n)))
        except OSError:
            names = []
        for name in names:
            w: list = []
            try:
                preset = self._load_dir(os.path.join(root, name), merged, w)
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
                continue
            if preset.id in BUILTIN_PRESETS:
                errors[name] = f"id '{preset.id}' collides with a built-in theme"
                continue
            if preset.id in merged:
                winner = os.path.basename(merged[preset.id].dir) or merged[preset.id].id
                errors[name] = f"id '{preset.id}' is already used by '{winner}'"
                continue
            merged[preset.id] = preset
            if w:
                warns[name] = w
        self._all, self.errors, self.warnings = merged, errors, warns
        for n, e in errors.items():
            _log.warning("theme '%s' skipped: %s", n, e)
        for n, ws in warns.items():
            _log.warning("theme '%s': %s", n, "; ".join(ws))

    @staticmethod
    def _load_dir(theme_dir, known, warns) -> ThemePreset:
        return PresetRegistry.from_data(theme_dir, _load_json(os.path.join(theme_dir, "theme.json")), known, warns)

    def set_draft(self, preset: ThemePreset) -> None:
        self._all = {**self._all, preset.id: preset}

    def clear_draft(self) -> None:
        self._all = {k: v for k, v in self._all.items() if k != "__draft__"}

    @staticmethod
    def from_data(theme_dir, data, known, warns) -> ThemePreset:
        if not isinstance(data, dict):
            raise ValueError("theme.json must be a JSON object")
        tid = str(data.get("id") or os.path.basename(theme_dir)).strip()
        if not tid:
            raise ValueError("empty theme id")
        parent = known.get(data.get("extends")) if isinstance(data.get("extends"), str) else None
        if data.get("extends") and parent is None:
            warns.append(f"extends '{data['extends']}' not found; using base theme")
        dark = bool(data.get("base_dark", parent.base_dark if parent else True))
        src = dict(parent.src if parent else _BASE[dark])
        raw = data.get("palette")
        touched: set = set()
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            if k in _ROLE_SET and isinstance(v, str):
                src[k] = v
                touched.add(k)
            else:
                warns.append(f"palette.{k}: unknown role or non-string value ignored")
        if isinstance(data.get("accent"), str) and data["accent"].strip():
            src["accent"] = data["accent"].strip()
            touched.add("accent")
        _rederive_stale(src, touched)
        palette = resolve_palette(src, warns)

        fam, fp = data.get("font_families"), data.get("font_paths")
        families = tuple(f for f in fam if isinstance(f, str)) if isinstance(fam, list) else None
        fonts = tuple(x if os.path.isabs(x) else os.path.join(theme_dir, x)
                      for x in fp if isinstance(x, str)) if isinstance(fp, list) else None
        sfx_raw = data.get("sfx") if isinstance(data.get("sfx"), dict) else {}
        sfx = {k: p for k in _SFX_KEYS if (p := _resolve_asset(theme_dir, sfx_raw.get(k))) is not None} or None
        icons_raw = data.get("icons") if isinstance(data.get("icons"), dict) else {}
        icons = {k: s for k, v in icons_raw.items()
                 if isinstance(k, str) and (s := _parse_icon_spec(theme_dir, v)) is not None}
        layout = {k: str(v) for k, v in (data.get("layout") or {}).items() if k in LAYOUT} \
            if isinstance(data.get("layout"), dict) else {}
        qss_path = os.path.join(theme_dir, "theme.qss")
        try:
            custom_qss = open(qss_path, encoding="utf-8").read(262144) if os.path.isfile(qss_path) else ""
        except OSError:
            custom_qss = ""
        rr, wi = data.get("rules"), data.get("widget_icons")
        rules = tuple({"selector": str(r["selector"]).strip(), "state": str(r.get("state") or ""), "css": str(r["css"]).strip()}
                      for r in (rr if isinstance(rr, list) else ())
                      if isinstance(r, dict) and r.get("selector") and r.get("css"))
        wicons = tuple({"match": str(i["match"]).strip(), "spec": s} for i in (wi if isinstance(wi, list) else ())
                       if isinstance(i, dict) and i.get("match") and (s := _parse_icon_spec(theme_dir, i.get("spec"))))
        custom_qss = "\n".join(x for x in (custom_qss, str(data.get("qss") or ""),
                                           *(f'{r["selector"]}{r["state"]} {{ {r["css"]} }}' for r in rules)) if x)
        return ThemePreset(
            id=tid, label=str(data.get("label") or tid), palette=palette, base_dark=dark,
            font_families=families, font_paths=fonts,
            locked_accent=bool(data.get("locked_accent", True)),
            sfx_paths=sfx, icons=icons or None, backdrop=_parse_backdrop(data),
            titlebar=TitleBarSpec.from_dict(data.get("titlebar")),
            transparent_window=bool(data.get("transparent_window", False)),
            src=src, layout=layout, custom_qss=custom_qss, dir=theme_dir, rules=rules, widget_icons=wicons, win7_frame=bool(data.get("win7_frame", False)),
            **_parse_background(theme_dir, data.get("background")),
        )

    def all(self) -> dict:
        return self._all

    def get(self, preset_id):
        return self._all.get(preset_id) if preset_id else None


TEXT_COLOR_ROLE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("settings.font_group_general", (
        ("primary_text", "settings.font_role_primary_text"),
        ("body_text", "settings.font_role_body_text"),
        ("muted", "settings.font_role_muted_text"),
        ("faint_text", "settings.font_role_faint_text"),
    )),
    ("settings.font_group_nav", (
        ("nav_text", "settings.font_role_nav_text"),
        ("nav_selected_text", "settings.font_role_nav_selected_text"),
    )),
    ("settings.font_group_dialogs", (
        ("dialog_title_text", "settings.font_role_dialog_title"),
        ("dialog_body_text", "settings.font_role_dialog_body"),
        ("dialog_accent_text", "settings.font_role_dialog_accent"),
    )),
    ("settings.font_group_details", (
        ("detail_title", "settings.font_role_detail_title"),
        ("detail_subtitle", "settings.font_role_detail_subtitle"),
        ("detail_meta", "settings.font_role_detail_meta"),
    )),
    ("settings.font_group_misc", (
        ("btn_text", "settings.font_role_btn_text"),
    )),
)

ICON_COLOR_ROLE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("settings.icon_group_nav", (
        ("nav_icon", "settings.icon_role_nav_icon"),
        ("nav_selected_icon", "settings.icon_role_nav_selected_icon"),
    )),
    ("settings.icon_group_misc", (
        ("icon_color", "settings.icon_role_icon_color"),
    )),
)

FONT_COLOR_ROLE_GROUPS = TEXT_COLOR_ROLE_GROUPS + ICON_COLOR_ROLE_GROUPS


def _roles_of(groups) -> tuple[str, ...]:
    return tuple(role for _label, entries in groups for role, _key in entries)


def font_color_roles() -> tuple[str, ...]:
    return _roles_of(FONT_COLOR_ROLE_GROUPS)


_FONT_ROLES = frozenset(font_color_roles())


class _Debounced:

    def __init__(self, fn, ms: int = 500):
        self._fn, self._ms, self._timer, self._dirty = fn, ms, None, False

    def poke(self) -> None:
        self._dirty = True
        app = QApplication.instance()
        if app is None:
            return self.flush()
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.setInterval(self._ms)
            self._timer.timeout.connect(self.flush)
            app.aboutToQuit.connect(self.flush)
        if not self._timer.isActive():
            self._timer.start()

    def cancel(self) -> None:
        self._dirty = False

    def flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self._fn()


class OverrideStore:

    def __init__(self) -> None:
        self._font: dict = {}
        self._tr_text: dict = {}
        self._tr_icon: dict = {}
        self._font_save = _Debounced(lambda: _write_json(self._font_path(), self._font))
        self._tr_save = _Debounced(lambda: _write_json(self._tr_path(), {"text": self._tr_text, "icon": self._tr_icon}))
        valid = _FONT_ROLES
        data = _read_json(self._font_path())
        if isinstance(data, dict):
            self._font.update({k: v for k, v in data.items() if k in valid and isinstance(v, str)})
        data = _read_json(self._tr_path())
        if isinstance(data, dict):
            for key, table in (("text", self._tr_text), ("icon", self._tr_icon)):
                sub = data.get(key)
                if isinstance(sub, dict):
                    table.update({k: v for k, v in sub.items() if isinstance(k, str) and isinstance(v, str)})

    @staticmethod
    def _font_path() -> str:
        return os.path.join(_themes_dir(), "_font_colors.json")

    @staticmethod
    def _tr_path() -> str:
        return os.path.join(_themes_dir(), "_tr_colors.json")

    @staticmethod
    def _set(table: dict, key: str, value: Optional[str]) -> bool:
        if value in (None, ""):
            return table.pop(key, None) is not None
        if table.get(key) == value:
            return False
        table[key] = value
        return True

    def set_font(self, role, color) -> bool:
        if role not in _FONT_ROLES or not self._set(self._font, role, color):
            return False
        self._font_save.poke()
        return True

    def clear_font(self) -> bool:
        if not self._font:
            return False
        self._font.clear()
        self._font_save.poke()
        return True

    def set_text_for_id(self, tr_id, color) -> bool:
        changed = self._set(self._tr_text, tr_id, color)
        if changed:
            self._tr_save.poke()
        return changed

    def set_icon_for_id(self, tr_id, color) -> bool:
        changed = self._set(self._tr_icon, tr_id, color)
        if changed:
            self._tr_save.poke()
        return changed

    def clear_tr(self) -> bool:
        if not self._tr_text and not self._tr_icon:
            return False
        self._tr_text.clear()
        self._tr_icon.clear()
        self._tr_save.poke()
        return True

    def apply_font_overrides(self, base: dict) -> None:
        for role, color in self._font.items():
            if role in base:
                base[role] = color

    def resolve_text(self, pal: dict, role: str, tr_id: Optional[str]) -> str:
        return (self._tr_text.get(tr_id) if tr_id is not None else None) or self._font.get(role) \
            or pal.get(role, pal.get("primary_text"))

    def resolve_icon(self, pal: dict, role: str, tr_id: Optional[str]) -> str:
        return (self._tr_icon.get(tr_id) if tr_id is not None else None) or self._font.get(role) \
            or pal.get(role, pal.get("icon_color"))


ICON_SLOTS: dict[str, tuple[str, str]] = {
    "nav.toggle": ("MENU", "settings.icon_slot_nav_toggle"),
    "nav.home": ("HOME", "settings.icon_slot_nav_home"),
    "nav.downloads": ("DOWNLOAD", "settings.icon_slot_nav_downloads"),
    "nav.settings": ("SETTING", "settings.icon_slot_nav_settings"),
    "nav.about": ("INFO", "settings.icon_slot_nav_about"),
    "nav.open_data_folder": ("FOLDER", "settings.icon_slot_nav_open_data_folder"),
    "nav.minerva": ("APPLICATION", "settings.icon_slot_nav_minerva"),
    "nav.pc_games": ("GAME", "settings.icon_slot_nav_pc_games"),
    "nav.music": ("MUSIC", "settings.icon_slot_nav_music"),
    "nav.books": ("BOOK_SHELF", "settings.icon_slot_nav_books"),
    "nav.youtube": ("PLAY", "settings.icon_slot_nav_youtube"),
    "nav.tv": ("PROJECTOR", "settings.icon_slot_nav_tv"),
    "nav.tbhb": ("SEARCH", "settings.icon_slot_nav_tbhb"),


    "home.category_repacks": ("GAME", "settings.icon_slot_home_category_repacks"),
    "home.category_music": ("MUSIC", "settings.icon_slot_home_category_music"),
    "home.category_anime": ("VIDEO", "settings.icon_slot_home_category_anime"),
    "home.category_youtube": ("PLAY", "settings.icon_slot_home_category_youtube"),
    "home.category_books": ("BOOK_SHELF", "settings.icon_slot_home_category_books"),
    "home.category_manga": ("BOOK_SHELF", "settings.icon_slot_home_category_manga"),
    "home.category_tbhb": ("SEARCH", "settings.icon_slot_home_category_tbhb"),
    "home.poster_placeholder": ("PHOTO", "settings.icon_slot_home_poster_placeholder"),
    "home.playlist_tile": ("MUSIC", "settings.icon_slot_home_playlist_tile"),
    "home.see_all_chevron": ("RIGHT_ARROW", "settings.icon_slot_home_see_all_chevron"),
    "home.card_chevron": ("RIGHT_ARROW", "settings.icon_slot_home_card_chevron"),


    "download.drop_zone": ("DOWNLOAD", "settings.icon_slot_download_drop_zone"),
    "download.donate_copy_address": ("COPY", "settings.icon_slot_download_donate_copy"),
    "download.item_pause": ("PAUSE", "settings.icon_slot_download_item_pause"),
    "download.item_resume": ("PLAY", "settings.icon_slot_download_item_resume"),
    "download.item_retry": ("SYNC", "settings.icon_slot_download_item_retry"),
    "download.item_open_file": ("DOCUMENT", "settings.icon_slot_download_item_open_file"),
    "download.item_install": ("PLAY", "settings.icon_slot_download_item_install"),
    "download.item_open_folder": ("FOLDER", "settings.icon_slot_download_item_open_folder"),
    "download.item_settings": ("SETTING", "settings.icon_slot_download_item_settings"),
    "download.item_cancel": ("CLOSE", "settings.icon_slot_download_item_cancel"),
    "download.item_remove": ("DELETE", "settings.icon_slot_download_item_remove"),
    "download.stat_active": ("SPEED_HIGH", "settings.icon_slot_download_stat_active"),
    "download.stat_download": ("DOWNLOAD", "settings.icon_slot_download_stat_download"),
    "download.stat_upload": ("SHARE", "settings.icon_slot_download_stat_upload"),
    "download.stat_queued": ("PAUSE_BOLD", "settings.icon_slot_download_stat_queued"),
    "download.stat_completed": ("ACCEPT", "settings.icon_slot_download_stat_completed"),
    "download.add_torrent": ("ADD", "settings.icon_slot_download_add_torrent"),
    "download.pause_all": ("PAUSE", "settings.icon_slot_download_pause_all"),
    "download.resume_all": ("PLAY", "settings.icon_slot_download_resume_all"),
    "download.delete_all": ("DELETE", "settings.icon_slot_download_delete_all"),
    "download.open_downloads_root": ("FOLDER", "settings.icon_slot_download_open_root"),
    "download.empty_state": ("DOWNLOAD", "settings.icon_slot_download_empty_state"),


    "minerva.tile_download": ("DOWNLOAD", "settings.icon_slot_minerva_tile_download"),
    "minerva.detail_download": ("DOWNLOAD", "settings.icon_slot_minerva_detail_download"),
    "minerva.download_selected": ("DOWNLOAD", "settings.icon_slot_minerva_download_selected"),
    "minerva.sync": ("SYNC", "settings.icon_slot_minerva_sync"),


    "repacks.card_fallback": ("GAME", "settings.icon_slot_repacks_card_fallback"),
    "repacks.latest_prev": ("LEFT_ARROW", "settings.icon_slot_repacks_latest_prev"),
    "repacks.latest_next": ("RIGHT_ARROW", "settings.icon_slot_repacks_latest_next"),
    "repacks.detail_back": ("RETURN", "settings.icon_slot_repacks_detail_back"),
    "repacks.detail_cover_fallback": ("GAME", "settings.icon_slot_repacks_detail_cover_fallback"),
    "repacks.site_link": ("LINK", "settings.icon_slot_repacks_site_link"),
    "repacks.donate": ("HEART", "settings.icon_slot_repacks_donate"),
    "repacks.upcoming": ("CALENDAR", "settings.icon_slot_repacks_upcoming"),
    "repacks.refresh": ("SYNC", "settings.icon_slot_repacks_refresh"),
    "repacks.discord": ("CHAT", "settings.icon_slot_repacks_discord"),
    "repacks.tab_fitgirl": ("GAME", "settings.tab_fitgirl_title"),
    "repacks.tab_gog": ("GAME", "settings.tab_gog_title"),
    "repacks.tab_tbhb": ("SEARCH", "nav.tbhb"),
    "repacks.browse_folder": ("FOLDER", "settings.icon_slot_repacks_browse_folder"),


    "music.lyrics_back": ("RETURN", "settings.icon_slot_music_lyrics_back"),
    "music.lyrics_align": ("ALIGNMENT", "settings.icon_slot_music_lyrics_align"),
    "music.lyrics_font_size": ("FONT_SIZE", "settings.icon_slot_music_lyrics_font_size"),
    "music.card_play": ("PLAY", "settings.icon_slot_music_card_play"),
    "music.card_download": ("DOWNLOAD", "settings.icon_slot_music_card_download"),
    "music.card_add_to_playlist": ("ADD_TO", "settings.icon_slot_music_card_add_to_playlist"),
    "music.card_more": ("MORE", "settings.icon_slot_music_card_more"),
    "music.bar_shuffle": ("ROTATE", "settings.icon_slot_music_bar_shuffle"),
    "music.bar_play": ("PLAY", "settings.icon_slot_music_bar_play"),
    "music.bar_pause": ("PAUSE", "settings.icon_slot_music_bar_pause"),
    "music.bar_repeat": ("SYNC", "settings.icon_slot_music_bar_repeat"),
    "music.bar_lyrics": ("LABEL", "settings.icon_slot_music_bar_lyrics"),
    "music.bar_volume": ("VOLUME", "settings.icon_slot_music_bar_volume"),
    "music.bar_mute": ("MUTE", "settings.icon_slot_music_bar_mute"),
    "music.album_fallback": ("ALBUM", "settings.icon_slot_music_album_fallback"),
    "music.row_play": ("PLAY", "settings.icon_slot_music_row_play"),
    "music.row_download": ("DOWNLOAD", "settings.icon_slot_music_row_download"),
    "music.row_remove": ("DELETE", "settings.icon_slot_music_row_remove"),
    "music.playlists_header": ("MENU", "settings.icon_slot_music_playlists_header"),
    "music.playlist_new": ("ADD", "settings.icon_slot_music_playlist_new"),
    "music.playlist_detail_back": ("RETURN", "settings.icon_slot_music_playlist_detail_back"),
    "music.playlist_play_all": ("PLAY", "settings.icon_slot_music_playlist_play_all"),
    "music.playlist_download_all": ("DOWNLOAD", "settings.icon_slot_music_playlist_download_all"),
    "music.playlist_rename": ("EDIT", "settings.icon_slot_music_playlist_rename"),
    "music.playlist_delete": ("DELETE", "settings.icon_slot_music_playlist_delete"),
    "music.playlist_chevron": ("RIGHT_ARROW", "settings.icon_slot_music_playlist_chevron"),
    "music.search_filters": ("FILTER", "settings.icon_slot_music_search_filters"),
    "music.search_playlists": ("MENU", "settings.icon_slot_music_search_playlists"),
    "music.search_cancel": ("CLOSE", "settings.icon_slot_music_search_cancel"),
    "music.search_prev_page": ("LEFT_ARROW", "settings.icon_slot_music_search_prev_page"),
    "music.search_next_page": ("RIGHT_ARROW", "settings.icon_slot_music_search_next_page"),


    "books.detail_back": ("RETURN", "settings.icon_slot_books_detail_back"),
    "books.site_link": ("LINK", "settings.icon_slot_books_site_link"),
    "books.chapter_download": ("DOWNLOAD", "settings.icon_slot_books_chapter_download"),
    "books.filter_sources": ("FILTER", "settings.icon_slot_books_filter_sources"),


    "tbhb.results_prev_page": ("LEFT_ARROW", "settings.icon_slot_tbhb_results_prev_page"),
    "tbhb.results_next_page": ("RIGHT_ARROW", "settings.icon_slot_tbhb_results_next_page"),
    "tbhb.detail_back": ("RETURN", "settings.icon_slot_tbhb_detail_back"),
    "tbhb.stat_category": ("TAG", "settings.icon_slot_tbhb_stat_category"),
    "tbhb.stat_size": ("DOCUMENT", "settings.icon_slot_tbhb_stat_size"),
    "tbhb.stat_seeders": ("UP", "settings.icon_slot_tbhb_stat_seeders"),
    "tbhb.stat_leechers": ("DOWN", "settings.icon_slot_tbhb_stat_leechers"),
    "tbhb.stat_uploader": ("PEOPLE", "settings.icon_slot_tbhb_stat_uploader"),
    "tbhb.stat_format": ("ALBUM", "settings.icon_slot_tbhb_stat_format"),
    "tbhb.new_search": ("RETURN", "settings.icon_slot_tbhb_new_search"),


    "yt.ffmpeg_gate": ("DOWNLOAD", "settings.icon_slot_yt_ffmpeg_gate"),
    "yt.empty_state": ("LINK", "settings.icon_slot_yt_empty_state"),
    "yt.download": ("PLAY", "settings.icon_slot_yt_download"),


    "tv.channels_add": ("ADD", "settings.icon_slot_tv_channels_add"),
    "tv.channels_manage": ("SETTING", "settings.icon_slot_tv_channels_manage"),
    "tv.channels_filter": ("FILTER", "settings.icon_slot_tv_channels_filter"),
    "tv.channels_refresh": ("SYNC", "settings.icon_slot_tv_channels_refresh"),
    "tv.channels_prev": ("LEFT_ARROW", "settings.icon_slot_tv_channels_prev"),
    "tv.channels_next": ("RIGHT_ARROW", "settings.icon_slot_tv_channels_next"),
    "tv.media_back": ("LEFT_ARROW", "settings.icon_slot_tv_media_back"),
    "tv.player_skip_back": ("LEFT_ARROW", "settings.icon_slot_tv_player_skip_back"),
    "tv.player_play": ("PLAY", "settings.icon_slot_tv_player_play"),
    "tv.player_pause": ("PAUSE", "settings.icon_slot_tv_player_pause"),
    "tv.player_skip_forward": ("RIGHT_ARROW", "settings.icon_slot_tv_player_skip_forward"),
    "tv.player_volume": ("VOLUME", "settings.icon_slot_tv_player_volume"),
    "tv.player_mute": ("MUTE", "settings.icon_slot_tv_player_mute"),
    "tv.player_fullscreen": ("VIEW", "settings.icon_slot_tv_player_fullscreen"),
    "tv.player_fullscreen_exit": ("CLOSE", "settings.icon_slot_tv_player_fullscreen_exit"),
    "tv.anime_back": ("LEFT_ARROW", "settings.icon_slot_tv_anime_back"),
    "tv.anime_download": ("DOWNLOAD", "settings.icon_slot_tv_anime_download"),
    "tv.tab_movies": ("MOVIE", "settings.icon_slot_tv_tab_movies"),
    "tv.tab_series": ("VIDEO", "settings.icon_slot_tv_tab_series"),
    "tv.tab_anime": ("VIDEO", "settings.icon_slot_tv_tab_anime"),
    "tv.tab_channels": ("TILES", "settings.icon_slot_tv_tab_channels"),


    "settings.theme_mode": ("BRUSH", "settings.icon_slot_settings_theme_mode"),
    "settings.accent_color": ("PALETTE", "settings.icon_slot_settings_accent_color"),
    "settings.background": ("PHOTO", "settings.icon_slot_settings_background"),
    "settings.background_blur": ("FILTER", "settings.icon_slot_settings_background_blur"),
    "settings.background_darken": ("QUIET_HOURS", "settings.icon_slot_settings_background_darken"),
    "settings.language": ("LANGUAGE", "settings.icon_slot_settings_language"),
    "settings.launch_at_startup": ("SPEED_HIGH", "settings.icon_slot_settings_launch_at_startup"),
    "settings.console_structure": ("IOT", "settings.icon_slot_settings_console_structure"),
    "settings.auto_download": ("MEDIA", "settings.icon_slot_settings_auto_download"),
    "settings.delete_torrent": ("DELETE", "settings.icon_slot_settings_delete_torrent"),
    "settings.download_limit": ("SPEED_HIGH", "settings.icon_slot_settings_download_limit"),
    "settings.upload_limit": ("SPEED_HIGH", "settings.icon_slot_settings_upload_limit"),
    "settings.seed_time": ("HISTORY", "settings.icon_slot_settings_seed_time"),
    "settings.on_finish": ("POWER_BUTTON", "settings.icon_slot_settings_on_finish"),
    "settings.close_to_tray": ("MINIMIZE", "settings.icon_slot_settings_close_to_tray"),
    "settings.folder_minerva": ("FOLDER", "settings.icon_slot_settings_folder_minerva"),
    "settings.folder_repacks": ("FOLDER", "settings.icon_slot_settings_folder_repacks"),
    "settings.folder_music": ("FOLDER", "settings.icon_slot_settings_folder_music"),
    "settings.folder_books": ("FOLDER", "settings.icon_slot_settings_folder_books"),
    "settings.folder_manga": ("DOCUMENT", "settings.icon_slot_settings_folder_manga"),
    "settings.folder_youtube": ("FOLDER", "settings.icon_slot_settings_folder_youtube"),
    "settings.folder_anime": ("VIDEO", "settings.icon_slot_settings_folder_anime"),
    "settings.folder_tbhb": ("FOLDER", "settings.icon_slot_settings_folder_tbhb"),
    "settings.tab_books": ("BOOK_SHELF", "settings.icon_slot_settings_tab_books"),
    "settings.tab_manga": ("DICTIONARY", "settings.icon_slot_settings_tab_manga"),
    "settings.notify_download": ("RINGER", "settings.icon_slot_settings_notify_download"),
    "settings.sfx_volume": ("VOLUME", "settings.icon_slot_settings_sfx_volume"),
    "settings.ambiance_volume": ("MUSIC", "settings.icon_slot_settings_ambiance_volume"),
    "settings.external_client": ("SEND", "settings.icon_slot_settings_external_client"),
    "settings.make_default": ("RINGER", "settings.icon_slot_settings_make_default"),
    "settings.repair_associations": ("SYNC", "settings.icon_slot_settings_repair_associations"),
    "settings.admin_mode": ("DEVELOPER_TOOLS", "settings.icon_slot_settings_admin_mode"),
    "settings.show_console": ("CODE", "settings.icon_slot_settings_show_console"),
    "settings.log_to_file": ("SAVE", "settings.icon_slot_settings_log_to_file"),
    "settings.software_updates": ("SYNC", "settings.icon_slot_settings_software_updates"),
    "settings.theme_maker": ("BRUSH", "settings.icon_slot_settings_theme_maker"),
    "settings.clear_cache": ("BROOM", "settings.icon_slot_settings_clear_cache"),
    "settings.wipe_all": ("DELETE", "settings.icon_slot_settings_wipe_all"),


    "about.github": ("GITHUB", "settings.icon_slot_about_github"),
    "about.discord": ("CHAT", "settings.icon_slot_about_discord"),
    "about.thanks": ("HEART", "settings.icon_slot_about_thanks"),


    "app.donate_heart": ("HEART", "settings.icon_slot_app_donate_heart"),
    "app.discord_chip": ("CHAT", "settings.icon_slot_app_discord_chip"),
    "app.sync_prompt_info": ("INFO", "settings.icon_slot_app_sync_prompt_info"),
    "app.sync_prompt_close": ("CLOSE", "settings.icon_slot_app_sync_prompt_close"),
}


def icon_slot_default_name(slot_id: str) -> Optional[str]:
    entry = ICON_SLOTS.get(slot_id)
    return entry[0] if entry else None


_FLUENT_ICON_BY_NAME: dict[str, "FluentIcon"] = {}


def _fluent_icon_by_name(name: str):
    if not _FLUENT_ICON_BY_NAME:
        for member in FluentIcon:
            _FLUENT_ICON_BY_NAME[member.name.upper()] = member
    return _FLUENT_ICON_BY_NAME.get(name.strip().upper())


class IconOverrideStore:


    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._load()

    def get(self, slot_id: str) -> Optional[dict]:
        return self._data.get(slot_id)

    def set_fluent(self, slot_id: str, fluent_name: str) -> bool:
        return self._set(slot_id, {"type": "fluent", "value": fluent_name.strip().upper()})

    def set_image(self, slot_id: str, path: str, crop: bool = True) -> bool:
        if not path or not os.path.isfile(path):
            return False
        return self._set(slot_id, {"type": "image", "value": path, "crop": bool(crop)})

    def clear(self, slot_id: str) -> bool:
        if slot_id not in self._data:
            return False
        del self._data[slot_id]
        self._save()
        return True

    def clear_all(self) -> bool:
        if not self._data:
            return False
        self._data.clear()
        self._save()
        return True

    def all(self) -> dict[str, dict]:
        return dict(self._data)

    def _set(self, slot_id: str, value: dict) -> bool:
        if self._data.get(slot_id) == value:
            return False
        self._data[slot_id] = value
        self._save()
        return True

    @staticmethod
    def _path() -> str:
        return os.path.join(_themes_dir(), "_icon_overrides.json")

    def _load(self) -> None:
        data = _read_json(self._path())
        if not isinstance(data, dict):
            return
        for slot_id, spec in data.items():
            if not isinstance(slot_id, str) or not isinstance(spec, dict):
                continue
            kind = spec.get("type")
            value = spec.get("value")
            if kind in ("fluent", "image") and isinstance(value, str) and value:
                entry = {"type": kind, "value": value}
                if kind == "image":
                    entry["crop"] = bool(spec.get("crop", True))
                self._data[slot_id] = entry

    def _save(self) -> None:
        _write_json(self._path(), self._data)



class _LRU(OrderedDict):
    def __init__(self, limit: int):
        super().__init__()
        self._limit = limit

    def fetch(self, key):
        value = self.get(key)
        if value is not None:
            self.move_to_end(key)
        return value

    def put(self, key, value) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self._limit:
            self.popitem(last=False)


class _ScaleToFillIconEngine(QIconEngine):

    def __init__(self, pixmap: QPixmap, cache: Optional[dict] = None) -> None:
        super().__init__()
        self._pixmap = pixmap
        self._cache = {} if cache is None else cache

    def paint(self, painter: QPainter, rect, mode, state) -> None:
        w, h = int(rect.width()), int(rect.height())
        if self._pixmap.isNull() or w <= 0 or h <= 0:
            return
        scaled = self._cache.get((w, h))
        if scaled is None:
            scaled = self._pixmap.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            if len(self._cache) >= 4:
                self._cache.pop(next(iter(self._cache)))
            self._cache[(w, h)] = scaled
        x, y = rect.x() + (w - scaled.width()) / 2, rect.y() + (h - scaled.height()) / 2
        painter.drawPixmap(QRectF(x, y, scaled.width(), scaled.height()), scaled, QRectF(scaled.rect()))

    def clone(self) -> "_ScaleToFillIconEngine":
        return _ScaleToFillIconEngine(self._pixmap, self._cache)


_pix_cache = _LRU(128)
_tinted = _LRU(256)


def _autocropped_pixmap(path: str) -> QPixmap:
    pix = QPixmap(path)
    img = pix.toImage()
    if pix.isNull() or not img.hasAlphaChannel():
        return pix
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    k = min(1.0, 64 / max(w, h))
    thumb = img if k >= 1.0 else img.scaled(max(1, int(w * k)), max(1, int(h * k)),
                                            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    tw, th = thumb.width(), thumb.height()
    alpha = bytes(thumb.constBits())[:tw * th * 4][3::4]
    rows = [y for y in range(th) if max(alpha[y * tw:(y + 1) * tw]) > 8]
    if not rows:
        return pix
    cols = [x for x in range(tw) if max(alpha[x::tw]) > 8]
    sx, sy = w / tw, h / th
    x0, y0 = max(0, int(cols[0] * sx)), max(0, int(rows[0] * sy))
    x1, y1 = min(w, int((cols[-1] + 1) * sx) + 1), min(h, int((rows[-1] + 1) * sy) + 1)
    return pix.copy(x0, y0, x1 - x0, y1 - y0)


def image_icon(path: str, size: int = 20, crop: bool = True) -> QIcon:
    try:
        key = (path, bool(crop), os.stat(path).st_mtime_ns)
    except OSError:
        return QIcon()
    pix = _pix_cache.fetch(key)
    if pix is None:
        pix = QPixmap(path) if not crop else _autocropped_pixmap(path)
        if pix.isNull():
            return QIcon()
        _pix_cache.put(key, pix)
    return QIcon(_ScaleToFillIconEngine(pix))


def _tinted_key(icon, color, bake):
    value = getattr(icon, "value", None)
    if hasattr(icon, "icon") and value is not None:
        return ("f", str(value), str(color), bake)
    try:
        q = icon if isinstance(icon, QIcon) else (icon.icon() if hasattr(icon, "icon") else QIcon(icon))
        return ("q", int(q.cacheKey()), str(color), bake)
    except Exception:
        return None


def tinted_icon(icon, color, size: int = 20) -> QIcon:
    bake = max(int(size) * 2, 64)
    key = _tinted_key(icon, color, bake)
    hit = _tinted.fetch(key) if key else None
    if hit is not None:
        return QIcon(hit)
    try:
        base = icon.icon() if hasattr(icon, "icon") else QIcon(icon)
    except Exception:
        base = icon if isinstance(icon, QIcon) else QIcon()
    src = base.pixmap(QSize(bake, bake))
    if src.isNull():
        return base
    out = QPixmap(src.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    icon_out = QIcon(_ScaleToFillIconEngine(out))
    if key:
        _tinted.put(key, icon_out)
    return QIcon(icon_out)


def filled_icon(icon, bake: int = 128) -> QIcon:
    from collections import deque
    try:
        base = icon.icon(color=QColor("white")) if hasattr(icon, "icon") else QIcon(icon)
    except Exception:
        base = icon if isinstance(icon, QIcon) else QIcon()
    mask = base.pixmap(QSize(bake, bake)).toImage().convertToFormat(QImage.Format.Format_Alpha8)
    if mask.isNull():
        return base
    w, h, bpl = mask.width(), mask.height(), mask.bytesPerLine()
    data = bytearray(mask.constBits())
    outside = bytearray(w * h)
    queue = deque(i for x in range(w) for i in (x, (h - 1) * w + x))
    queue.extend(i for y in range(h) for i in (y * w, y * w + w - 1))
    while queue:
        i = queue.popleft()
        if outside[i] or data[(i // w) * bpl + i % w] > 40:
            continue
        outside[i] = 1
        x, y = i % w, i // w
        if x:
            queue.append(i - 1)
        if x < w - 1:
            queue.append(i + 1)
        if y:
            queue.append(i - w)
        if y < h - 1:
            queue.append(i + w)
    for y in range(h):
        row = y * bpl
        for x in range(w):
            if not outside[y * w + x]:
                data[row + x] = 255
    solid = QImage(bytes(data), w, h, bpl, QImage.Format.Format_Alpha8).convertToFormat(QImage.Format.Format_ARGB32)
    return QIcon(QPixmap.fromImage(solid))


def trimmed_icon(pixmap: QPixmap, fill: float = 0.88, bake: int = 128) -> QIcon:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    box = QRect(0, 0, 0, 0)
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    if not alpha.isNull():
        w, h, bpl = alpha.width(), alpha.height(), alpha.bytesPerLine()
        data = bytes(alpha.constBits())
        rows = [y for y in range(h) if max(data[y * bpl:y * bpl + w]) > 8]
        cols = [x for x in range(w) if max(data[x:h * bpl:bpl]) > 8]
        if rows and cols:
            box = QRect(cols[0], rows[0], cols[-1] - cols[0] + 1, rows[-1] - rows[0] + 1)
    if box.isEmpty():
        return QIcon(pixmap)
    glyph = image.copy(box)
    side = round(bake * fill)
    glyph = glyph.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    canvas = QPixmap(bake, bake)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawImage((bake - glyph.width()) // 2, (bake - glyph.height()) // 2, glyph)
    p.end()
    return QIcon(canvas)


def _heart_pixmap(size: int, filled: bool) -> QPixmap:
    bake = max(int(size) * 2, 48)
    pix = QPixmap(bake, bake)
    pix.fill(Qt.GlobalColor.transparent)
    path = QPainterPath()
    path.moveTo(12.0, 20.7)
    path.cubicTo(10.4, 19.1, 2.8, 13.2, 2.8, 8.2)
    path.cubicTo(2.8, 5.2, 5.2, 2.8, 8.2, 2.8)
    path.cubicTo(10.0, 2.8, 11.4, 3.7, 12.0, 5.1)
    path.cubicTo(12.6, 3.7, 14.0, 2.8, 15.8, 2.8)
    path.cubicTo(18.8, 2.8, 21.2, 5.2, 21.2, 8.2)
    path.cubicTo(21.2, 13.2, 13.6, 19.1, 12.0, 20.7)
    path.closeSubpath()
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(bake / 24.0, bake / 24.0)
    if filled:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
    else:
        pen = QPen(QColor("#ffffff"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.end()
    return pix


def filled_heart_icon(size: int = 20) -> QIcon:
    return QIcon(_ScaleToFillIconEngine(_heart_pixmap(size, True)))


def outline_heart_icon(size: int = 20) -> QIcon:
    return QIcon(_ScaleToFillIconEngine(_heart_pixmap(size, False)))


class _RefreshBus(QObject):

    def __init__(self, mgr: "ThemeManager"):
        super().__init__()
        self._mgr = mgr
        self._subs: dict = {}
        qconfig.themeChanged.connect(self._on_changed)

    def add(self, widget, callback) -> None:
        key = id(widget)
        entry = self._subs.get(key)
        if entry is None:
            def _gone(ref, k=key):
                cur = self._subs.get(k)
                if cur is not None and cur[0] is ref:
                    del self._subs[k]
            entry = self._subs[key] = [weakref.ref(widget, _gone), [], 0]
            widget.installEventFilter(self)
        entry[1].append(callback)
        entry[2] = self._mgr.version

    def _run(self, key, entry) -> None:
        widget = entry[0]()
        if widget is None or not shiboken6.isValid(widget):
            self._subs.pop(key, None)
            return
        entry[2] = self._mgr.version
        for cb in list(entry[1]):
            try:
                cb()
            except RuntimeError:
                self._subs.pop(key, None)
                return
            except Exception:
                _log.exception("theme refresh callback failed")

    def _on_changed(self, *_args) -> None:
        ver = self._mgr.version
        for key, entry in list(self._subs.items()):
            if entry[2] == ver:
                continue
            widget = entry[0]()
            if widget is None or not shiboken6.isValid(widget):
                self._subs.pop(key, None)
                continue
            vis = getattr(widget, "isVisible", None)
            if vis is None or vis():
                self._run(key, entry)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Show:
            entry = self._subs.get(id(obj))
            if entry is not None and entry[2] != self._mgr.version:
                self._run(id(obj), entry)
        return False

    def refresh_all(self) -> None:
        for key, entry in list(self._subs.items()):
            widget = entry[0]()
            if widget is None or not shiboken6.isValid(widget):
                self._subs.pop(key, None)
                continue
            vis = getattr(widget, "isVisible", None)
            if vis is None or vis():
                self._run(key, entry)


_LOADED_FONT_FAMILIES: dict = {}


class ThemeManager:
    def __init__(self) -> None:
        self.presets = PresetRegistry()
        self.overrides = OverrideStore()
        self.icons = IconOverrideStore()
        self.version = 0
        self._active: Optional[str] = None
        self._meta: Optional[ThemePreset] = None
        self._pal: Optional[dict] = None
        self._layout: dict = dict(LAYOUT)
        self._qss: dict = {}
        self._sig = 0
        self._memo: dict = {}
        self._force = False
        self._notify_pending = False
        self._live = False
        self._applying = False
        self._reapply = False
        self._applied_version = -1
        self._watcher = None
        self._debounce = None
        qconfig.themeChanged.connect(self._on_external_change)
        self.bus = _RefreshBus(self)


    def _compute(self) -> dict:
        meta = self._meta
        if meta is not None:


            accent = None if meta.locked_accent else _user_accent_color()
            key = (meta.id, accent)
            pal = self._memo.get(key)
            if pal is None:
                pal = (meta.with_accent(accent) if accent else meta).palette
        else:
            dark, accent = isDarkTheme(), _user_accent_color()
            key = (None, dark, accent)
            pal = self._memo.get(key)
            if pal is None:
                pal = resolve_palette({**_BASE[dark], **_accent_src(accent)}) if accent else (_DARK if dark else _LIGHT)
        if len(self._memo) > 64:
            self._memo.clear()
        self._memo[key] = pal
        pal = dict(pal)
        self.overrides.apply_font_overrides(pal)
        return pal

    def _rebuild(self) -> dict:
        new, old = self._compute(), self._pal
        if self._force or old is None or new != old or self._pal_meta is not self._meta:
            self.version += 1
            self._layout = {**LAYOUT, **(self._meta.layout if self._meta else {})}
            self._sig = hash((tuple(new.items()), tuple(self._layout.items()), self._meta.custom_qss if self._meta else ""))
            self._force = False
            clear_nav_icon_cache()
        self._pal, self._pal_meta = new, self._meta
        return new

    _pal_meta: Optional[ThemePreset] = None

    def _on_external_change(self, *_args) -> None:
        self._rebuild()

    def go_live(self) -> None:
        self._live = True
        if self._notify_pending or self._applied_version != self.version:
            self._apply_loop()

    def _notify(self, force: bool = False) -> None:
        self._force = self._force or force
        self._rebuild()
        if self._live or QThread.currentThread().loopLevel() > 0:
            if self._applying:
                self._reapply = True
                return
            self._apply_loop()
            return
        if not self._notify_pending:
            self._notify_pending = True
            QTimer.singleShot(0, self._apply_loop)

    def _apply_loop(self) -> None:
        self._notify_pending = False
        self._applying = True
        app = QApplication.instance()
        frozen = []
        if app is not None:
            for window in app.topLevelWidgets():
                try:
                    if window.isVisible() and window.updatesEnabled():
                        window.setUpdatesEnabled(False)
                        frozen.append(window)
                except RuntimeError:
                    pass
        try:
            while True:
                self._emit()
                if not (self._reapply and self._applied_version != self.version):
                    break
                self._reapply = False
        finally:
            self._applying = False
            # Paint once with the finished palette instead of showing every
            # intermediate restyle pass.
            for window in frozen:
                with suppress(RuntimeError):
                    window.setUpdatesEnabled(True)
                    window.update()

    def _emit(self) -> None:
        if self._applied_version == self.version:
            return
        self._applied_version = self.version
        self._notify_pending = False
        accent = palette().get("accent")
        color_changed = False
        if accent:
            color = QColor(accent)
            if qconfig.themeColor.value != color:
                qconfig.themeColor.value = color
                color_changed = True
        apply_app_palette()
        _restyle_qfluent_widgets()
        apply_widget_rules()
        qconfig.themeChanged.emit(qconfig.theme)
        if color_changed:
            qconfig.themeColorChanged.emit(qconfig.themeColor.value)
        qconfig.themeChangedFinished.emit()
        _repaint_now()

    def all_presets(self) -> dict: return self.presets.all()
    def preset(self, preset_id): return self.presets.get(preset_id)
    def preset_ids(self) -> tuple: return tuple(self.presets.all())
    def active_preset_id(self): return self._active
    def layout(self) -> dict: return self._layout

    def preset_label(self, preset_id: str) -> str:
        meta = self.preset(preset_id)
        return meta.label if meta is not None else preset_id

    def accent_locked(self, preset_id: Optional[str] = None) -> bool:
        meta = self.preset(preset_id if preset_id is not None else self._active)
        return False if meta is None else meta.locked_accent

    def palette(self, dark: Optional[bool] = None) -> dict:
        if dark is not None:
            base = dict(_DARK if dark else _LIGHT)
            self.overrides.apply_font_overrides(base)
            return base
        return self._pal if self._pal is not None else self._rebuild()

    def qss(self, key, template) -> str:
        pal = self.palette()
        k = (self._sig, key)
        out = self._qss.get(k)
        if out is None:
            if len(self._qss) > 400:
                self._qss.clear()
            out = self._qss[k] = _render(template() if callable(template) else template, pal, self._layout)
        return out


    def _set_active(self, meta: Optional[ThemePreset]) -> None:
        self._meta, self._active = meta, (meta.id if meta else None)
        self._watch(meta)

    def activate(self, preset_id: Optional[str] = None, dark: Optional[bool] = None) -> None:
        # Hold painting for the whole switch. Setting qconfig.theme / themeColor
        # below restyles widgets and fires themeChanged *before* _notify() runs,
        # so without this the window can paint a half-switched frame (old and new
        # scrollbars / backgrounds on top of each other) for a few milliseconds.
        frozen = self._freeze_windows()
        try:
            self._activate_unfrozen(preset_id, dark)
        finally:
            self._thaw_windows(frozen)

    @staticmethod
    def _freeze_windows() -> list:
        frozen = []
        app = QApplication.instance()
        if app is not None:
            for window in app.topLevelWidgets():
                try:
                    if window.isVisible() and window.updatesEnabled():
                        window.setUpdatesEnabled(False)
                        frozen.append(window)
                except RuntimeError:
                    pass
        return frozen

    @staticmethod
    def _thaw_windows(frozen: list) -> None:
        for window in frozen:
            with suppress(RuntimeError):
                window.setUpdatesEnabled(True)
                window.update()

    def _activate_unfrozen(self, preset_id: Optional[str] = None, dark: Optional[bool] = None) -> None:
        meta = self.presets.get(preset_id)
        self._set_active(meta)
        if meta is not None:
            dark = meta.base_dark
            accent = meta.accent if meta.locked_accent else (_user_accent_color() or meta.accent)
        else:
            accent = _user_accent_color()
            dark = isDarkTheme() if dark is None else dark
        self._rebuild()
        target = Theme.DARK if dark else Theme.LIGHT
        if qconfig.theme != target:
            qconfig.theme = target
        if qconfig.themeMode.value != target:
            qconfig.themeMode.value = target
        if accent:
            color = QColor(accent)
            if qconfig.themeColor.value != color:
                qconfig.themeColor.value = color
        self._notify()

    def preview(self, data: dict, theme_dir: str) -> list:
        warns: list = []
        preset = PresetRegistry.from_data(theme_dir, {**data, "id": "__draft__"}, self.presets.all(), warns)
        self.presets.set_draft(preset)
        self._memo.clear()
        if self._active != "__draft__" or preset.base_dark != isDarkTheme():
            self.activate("__draft__")
        else:
            self._meta = preset
            self._notify(force=True)
        return warns

    def preview_light(self, data: dict, theme_dir: str) -> None:
        if self._active != "__draft__":
            self.preview(data, theme_dir)
            return
        preset = PresetRegistry.from_data(theme_dir, {**data, "id": "__draft__"}, self.presets.all(), [])
        self.presets.set_draft(preset)
        self._meta = preset
        apply_widget_rules()


        clear_nav_icon_cache()
        self.bus.refresh_all()
        chrome = _ACTIVE_CHROME.get("chrome")
        if chrome is not None:
            with suppress(Exception):
                chrome.apply()

    def end_preview(self, restore: Optional[str]) -> None:
        self.presets.clear_draft()
        self._memo.clear()
        self.activate(restore)

    def set_active_preset(self, preset_id: Optional[str]) -> None:
        meta = self.presets.get(preset_id)
        if (meta.id if meta else None) == self._active:
            return
        self._set_active(meta)
        self._notify()

    def notify_accent_changed(self) -> None:
        self._notify()

    def reload_custom_presets(self) -> None:
        self.presets.reload()
        self._memo.clear()
        self._meta = self.presets.get(self._active)
        if self._meta is None:
            self._active = None
        self._watch(self._meta)
        self._force = True
        self._rebuild()
        self._notify()


    def _watch(self, meta: Optional[ThemePreset]) -> None:

        shader = meta.background_shader_path if meta is not None else None
        paths = [p for p in ((meta.dir, os.path.join(meta.dir, "theme.json"), os.path.join(meta.dir, "theme.qss"), shader)
                             if meta is not None and meta.dir else ()) if p and os.path.exists(p)]
        if self._watcher is None:
            if not paths or QApplication.instance() is None:
                return
            self._watcher = QFileSystemWatcher()
            self._debounce = QTimer()
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(250)
            self._debounce.timeout.connect(self._hot_reload)
            self._watcher.directoryChanged.connect(lambda *_: self._debounce.start())
            self._watcher.fileChanged.connect(lambda *_: self._debounce.start())
        old = self._watcher.files() + self._watcher.directories()
        if old:
            self._watcher.removePaths(old)
        if paths:
            self._watcher.addPaths(paths)

    def _hot_reload(self) -> None:
        if self._meta is None or not self._meta.dir:
            return
        if self._active == "__draft__":


            self.activate("__draft__")
            return
        self.reload_custom_presets()
        self.activate(self._active)


    def set_font_color_override(self, role, color) -> None:
        if self.overrides.set_font(role, color):
            self._notify()

    def clear_font_color_overrides(self) -> None:
        if self.overrides.clear_font():
            self._notify()

    def set_text_color_for_id(self, tr_id, color) -> None:
        if self.overrides.set_text_for_id(tr_id, color):
            self._notify(force=True)

    def set_icon_color_for_id(self, tr_id, color) -> None:
        if self.overrides.set_icon_for_id(tr_id, color):
            self._notify(force=True)

    def clear_tr_color_overrides(self) -> None:
        if self.overrides.clear_tr():
            self._notify(force=True)

    def resolve_text_color(self, role: str, tr_id: Optional[str] = None) -> str:
        return self.overrides.resolve_text(self.palette(), role, tr_id)

    def resolve_icon_color(self, role: str = "icon_color", tr_id: Optional[str] = None) -> str:
        return self.overrides.resolve_icon(self.palette(), role, tr_id)

    def resolve_icon_spec(self, slot_id: str) -> Optional[dict]:
        spec = self.icons.get(slot_id)
        if spec is not None:
            return spec
        return self._meta.icons.get(slot_id) if self._meta is not None and self._meta.icons else None

    def set_icon_override(self, slot_id, kind, value, crop: bool = True) -> None:
        changed = self.icons.set_image(slot_id, value, crop=crop) if kind == "image" else self.icons.set_fluent(slot_id, value)
        if changed:
            self._notify(force=True)

    def clear_icon_override(self, slot_id) -> None:
        if self.icons.clear(slot_id):
            self._notify(force=True)

    def clear_all_icon_overrides(self) -> None:
        if self.icons.clear_all():
            self._notify(force=True)

    def resolve_font_families(self, meta: ThemePreset) -> Optional[tuple]:
        loaded: list = []
        for path in (meta.font_paths or ()):
            if not os.path.isfile(path):
                continue
            fams = _LOADED_FONT_FAMILIES.get(path)
            if fams is None:
                font_id = QFontDatabase.addApplicationFont(path)
                if font_id == -1:
                    continue
                fams = _LOADED_FONT_FAMILIES[path] = tuple(QFontDatabase.applicationFontFamilies(font_id))
            loaded.extend(f for f in fams if f not in loaded)
        loaded.extend(f for f in (meta.font_families or ()) if f not in loaded)
        return tuple(loaded) if loaded else None


manager = ThemeManager()



all_presets = manager.all_presets
all_preset_ids = manager.preset_ids
preset_meta = manager.preset
preset_label = manager.preset_label
active_preset_id = manager.active_preset_id
accent_locked = manager.accent_locked
set_active_preset = manager.set_active_preset
activate_theme = manager.activate
go_live_theme = manager.go_live
notify_accent_changed = manager.notify_accent_changed
resolve_font_families = manager.resolve_font_families
palette = manager.palette
reload_custom_themes = manager.reload_custom_presets
set_font_color_override = manager.set_font_color_override
clear_font_color_overrides = manager.clear_font_color_overrides
set_text_color_for_id = manager.set_text_color_for_id
set_icon_color_for_id = manager.set_icon_color_for_id
clear_tr_color_overrides = manager.clear_tr_color_overrides
resolve_text_color = manager.resolve_text_color
resolve_icon_color = manager.resolve_icon_color
set_icon_override = manager.set_icon_override
clear_icon_override = manager.clear_icon_override
clear_all_icon_overrides = manager.clear_all_icon_overrides


def register_theme_refresh(widget, callback) -> None:
    callback()
    manager.bus.add(widget, callback)


def _layout() -> dict:
    return manager.layout()


def _meta_if(preset_id, kind: str):
    meta = manager.preset(preset_id)
    return meta if meta is not None and meta.background_type == kind else None


def theme_background(preset_id):
    m = _meta_if(preset_id, "image")
    return (m.background_path, m.background_blur, m.background_darken) if m and m.background_path else None


def theme_background_info(preset_id) -> dict:
    m = _meta_if(preset_id, "shader")
    if m and m.background_shader_path:
        return {"shader": m.background_shader_path}
    m = _meta_if(preset_id, "image")
    if m and m.background_path:
        return {"path": m.background_path, "blur": m.background_blur, "darken": m.background_darken}
    return {}


def theme_shader_path(preset_id):
    m = _meta_if(preset_id, "shader")
    return m.background_shader_path if m else None


def theme_shader_image_path(preset_id):
    m = _meta_if(preset_id, "shader")
    return m.background_shader_image_path if m else None


def theme_shader_fps(preset_id) -> float:
    m = _meta_if(preset_id, "shader")
    return m.background_shader_fps if m else 0.0


def theme_shader_aa(preset_id) -> int:
    m = _meta_if(preset_id, "shader")
    return m.background_shader_aa if m else 0


def theme_sfx_path(preset_id, key):
    m = manager.preset(preset_id)
    return (m.sfx_paths or {}).get(key) if m else None


def theme_backdrop(preset_id):
    m = manager.preset(preset_id)
    return m.backdrop if m else None


def theme_background_scope(preset_id) -> str:
    m = manager.preset(preset_id)
    return m.background_scope if m else "client"


def theme_titlebar(preset_id):
    m = manager.preset(preset_id)
    return m.titlebar if m else None


def theme_transparent_window(preset_id) -> bool:
    m = manager.preset(preset_id)
    if m is None:
        return False
    if m.transparent_window:
        return True
    return bool(m.backdrop) and m.backdrop != "none" and DwmBackdrop.supports(m.backdrop)


def theme_win7_frame(preset_id) -> bool:
    m = manager.preset(preset_id)
    return bool(m and m.win7_frame)


def _transparent_window_enabled() -> bool:
    return theme_transparent_window(manager.active_preset_id())



_TOKEN = re.compile(r"@([a-z_]+)|\$([a-z_]+)")












_QSS_ROLE_BY_TOKEN = {
    "page_bg": "window",
    "primary_text": "text",
    "muted": "placeholder-text",
    "btn_bg": "button",
    "btn_text": "button-text",
    "btn_border": "link",
    "btn_hover": "link-visited",
    "card_bg": "alternate-base",
    "card_hover": "light",
    "dialog_bg": "tool-tip-base",
    "dialog_body_text": "tool-tip-text",
    "dialog_title_text": "bright-text",
    "dialog_border": "dark",
    "input_bg": "base",
    "input_border": "midlight",
    "input_border_hover": "mid",
    "input_border_focus": "shadow",
}

_QPALETTE_ROLE_BY_NAME = {
    "window": QPalette.ColorRole.Window,
    "window-text": QPalette.ColorRole.WindowText,
    "text": QPalette.ColorRole.Text,
    "placeholder-text": QPalette.ColorRole.PlaceholderText,
    "base": QPalette.ColorRole.Base,
    "alternate-base": QPalette.ColorRole.AlternateBase,
    "tool-tip-base": QPalette.ColorRole.ToolTipBase,
    "tool-tip-text": QPalette.ColorRole.ToolTipText,
    "button": QPalette.ColorRole.Button,
    "button-text": QPalette.ColorRole.ButtonText,
    "bright-text": QPalette.ColorRole.BrightText,
    "link": QPalette.ColorRole.Link,
    "link-visited": QPalette.ColorRole.LinkVisited,
    "light": QPalette.ColorRole.Light,
    "midlight": QPalette.ColorRole.Midlight,
    "dark": QPalette.ColorRole.Dark,
    "mid": QPalette.ColorRole.Mid,
    "shadow": QPalette.ColorRole.Shadow,
    "highlight": QPalette.ColorRole.Highlight,
    "highlighted-text": QPalette.ColorRole.HighlightedText,
}


def _render(tpl: str, pal: dict, layout: dict) -> str:
    def sub(m: re.Match) -> str:
        if not m.group(1):
            return str(layout.get(m.group(2), m.group(0)))
        role = _QSS_ROLE_BY_TOKEN.get(m.group(1))
        if role:
            return f"palette({role})"
        return str(pal.get(m.group(1), m.group(0)))
    return _TOKEN.sub(sub, tpl)


def _palette_color(pal: dict, token: str) -> Optional[QColor]:
    parsed = _parse_rgba(str(pal.get(token) or ""))
    if not parsed:
        return None
    r, g, b, a = parsed
    return QColor(r, g, b, round(a * 255))


def _on_accent_text(accent: QColor) -> QColor:
    lum = 0.299 * accent.red() + 0.587 * accent.green() + 0.114 * accent.blue()
    return QColor(0, 0, 0) if lum > 165 else QColor(255, 255, 255)


def _build_app_palette() -> QPalette:
    pal = palette()
    qp = QPalette()
    for token, role_name in _QSS_ROLE_BY_TOKEN.items():
        color = _palette_color(pal, token)
        role = _QPALETTE_ROLE_BY_NAME.get(role_name)
        if color is not None and role is not None:
            qp.setColor(QPalette.ColorGroup.All, role, color)

    text = _palette_color(pal, "primary_text")
    if text is not None:
        qp.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.WindowText, text)
    accent = _palette_color(pal, "accent")
    if accent is not None:
        qp.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.Highlight, accent)
        on_accent = _palette_color(pal, "on_accent")
        qp.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.HighlightedText,
                    on_accent if on_accent is not None else _on_accent_text(accent))
    return qp


def apply_app_palette() -> None:
    app = QApplication.instance()
    if app is None:
        return
    app.setPalette(_build_app_palette())


def _restyle_qfluent_widgets() -> None:
    # Setting a style sheet on every visible widget costs hundreds of
    # milliseconds. qfluentwidgets already ships a paint-time watcher for the
    # ``dirty-qss`` property, so mark every managed widget and let the next
    # paint restyle it. Painting is frozen by the caller, which coalesces all
    # of those style recalculations into one pass.
    removes = []
    for widget, _source in list(styleSheetManager.items()):
        try:
            widget.setProperty('dirty-qss', True)
            if widget.isVisible():
                widget.update()
        except RuntimeError:
            removes.append(widget)
    for widget in removes:
        styleSheetManager.deregister(widget)


def _repaint_now() -> None:
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        if w.isVisible():
            w.update()


def _sels(prefixes, widgets, suffix="") -> str:
    return ", ".join(f"{p} {w}{suffix}" for p in prefixes for w in widgets)


def _fill(tpl: str, sub: dict) -> str:
    return re.sub(r"%(\w+)%", lambda m: sub[m.group(1)], tpl)


_PIVOT_TPL = """
PivotItem { color: @muted; }
PivotItem[isSelected=true], PivotItem[isSelected=true]:hover, PivotItem[isSelected=true]:pressed,
PivotItem[isSelected=false]:hover { color: @primary_text; }
PivotItem[isSelected=false]:pressed { color: @muted; }
"""
_BASE_TEXT_TPL = """
QWidget { color: @primary_text; }
MutedLabel, CaptionLabel, BodyLabel, ItalicBodyLabel { color: @muted; }
StrongBodyLabel, SubtitleLabel, TitleLabel, LargeTitleLabel, DisplayLabel { color: @primary_text; }
PushButton, PrimaryPushButton, TransparentPushButton, ToolButton, TransparentToolButton { color: @btn_text; }
PrimaryPushButton, PrimaryToolButton, PrimaryDropDownPushButton, PrimarySplitPushButton { color: @on_accent; }
""" + _PIVOT_TPL + """
SegmentedItem[isSelected=true], SegmentedToolItem[isSelected=true] { color: @primary_text; }
SegmentedItem[isSelected=false], SegmentedToolItem[isSelected=false] { color: @muted; }
"""
_SCROLL_TPL = """
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @scrollbar_handle; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: @scrollbar_handle_hover; }
QScrollBar:horizontal { height: 0px; }
QScrollBar::add-line, QScrollBar::sub-line { background: transparent; }
"""
_CARD_TPL = """
%C% { background-color: @card_bg; border: none; border-radius: %R%px; }
%C%:hover { background-color: @card_hover; border: none; }
"""
_SETTINGS_TPL = """
%page% { background-color: @page_bg; border: none; }
%group% { color: @primary_text; font-weight: 600; font-size: 16px;
          padding-top: $group_title_pad_top; padding-bottom: $group_title_pad_bottom; }
%card% { background-color: @card_bg; border: none; border-radius: $card_radius; padding: $card_padding; }
%card_hover% { background-color: @card_hover; border: none; }
%title% { color: @primary_text; font-weight: 600; }
%content%, %caption% { color: @muted; }
%input% { background-color: @input_bg; border: $input_border_width solid @input_border;
          border-radius: $input_radius; color: @primary_text; padding: $input_padding; }
%input_hover% { border: $input_border_width solid @input_border_hover; }
%input_focus% { border: $input_border_width_focus solid @input_border_focus; }
%btn% { background-color: @btn_bg; border: $input_border_width solid @btn_border;
        border-radius: $btn_radius; color: @btn_text; padding: $btn_padding; }
%btn_hover% { background-color: @btn_hover; border: $input_border_width solid @input_border_hover; }
%subtitle% { color: @primary_text; }
%body% { color: @muted; }
"""
_DIALOG_TPL = """
%root% { background-color: @dialog_bg; border: 1px solid @dialog_border;
         border-radius: $dialog_radius; color: @dialog_body_text; }
%label% { color: @dialog_body_text; background: transparent; }
%titles% { color: @dialog_title_text; background: transparent; }
%caption% { color: @dialog_body_text; background: transparent; }
%btns% { border-radius: $btn_radius; padding: $btn_padding; }
%btn% { background-color: @btn_bg; border: $input_border_width solid @btn_border; color: @btn_text; }
%btn_hover% { background-color: @btn_hover; }
%inputs% { background-color: @input_bg; border: $input_border_width solid @input_border;
           border-radius: $input_radius; color: @primary_text; padding: $input_padding; }
%cardw% { background-color: @card_bg; border: none; border-radius: $card_radius; }
%bar% { background-color: @dialog_bg; border: none;
        border-bottom-left-radius: $dialog_radius; border-bottom-right-radius: $dialog_radius; }
#pirachestDialogBody { border-radius: $dialog_radius; }
"""


def base_text_qss() -> str:
    return manager.qss(("base_text",), _BASE_TEXT_TPL)


def scroll_area_qss() -> str:
    return manager.qss(("scroll",), _SCROLL_TPL)


def settings_qss(page_object_names: tuple = ("#settingsPage", "#aboutPage")) -> str:
    pages = tuple(page_object_names)

    def build() -> str:
        cards = ("SettingCard", "SwitchSettingCard", "CardWidget")
        labels = ("SettingCard", "SwitchSettingCard")
        inputs = ("LineEdit", "ComboBox", "SpinBox", "CompactSpinBox", "DoubleSpinBox")
        return _fill(_SETTINGS_TPL, {
            "page": ", ".join(pages), "group": _sels(pages, ("SettingCardGroup > QLabel",)),
            "card": _sels(pages, cards), "card_hover": _sels(pages, cards, ":hover"),
            "title": _sels(pages, labels, " QLabel#titleLabel"), "content": _sels(pages, labels, " QLabel#contentLabel"),
            "caption": _sels(pages, ("CaptionLabel",)), "input": _sels(pages, inputs),
            "input_hover": _sels(pages, inputs, ":hover"), "input_focus": _sels(pages, ("LineEdit", "ComboBox"), ":focus"),
            "btn": _sels(pages, ("PushButton",)), "btn_hover": _sels(pages, ("PushButton",), ":hover"),
            "subtitle": _sels(pages, ("SubtitleLabel",)), "body": _sels(pages, ("BodyLabel",)),
        })
    return manager.qss(("settings", pages), build)


def dialog_qss(selectors: tuple = ("MessageBoxBase", "QDialog", "Ui_MessageBox")) -> str:
    sel = tuple(selectors)
    return manager.qss(("dialog", sel), lambda: _fill(_DIALOG_TPL, {
        "root": ", ".join(sel), "label": _sels(sel, ("QLabel",)),
        "titles": _sels(sel, ("SubtitleLabel", "TitleLabel", "StrongBodyLabel")),
        "caption": _sels(sel, ("CaptionLabel",)), "btns": _sels(sel, ("PushButton", "PrimaryPushButton")),
        "btn": _sels(sel, ("PushButton",)), "btn_hover": _sels(sel, ("PushButton",), ":hover"),
        "inputs": _sels(sel, ("LineEdit", "ComboBox")), "cardw": _sels(sel, ("CardWidget",)),
        "bar": _sels(sel, ("#pirachestButtonBar",)),
    }))


def custom_theme_qss() -> str:
    meta = manager._meta
    return manager.qss(("custom", meta.id), meta.custom_qss) if meta is not None and meta.custom_qss else ""


_INTEGRATED = False


def _install_integration() -> None:
    global _INTEGRATED
    if _INTEGRATED:
        return
    _INTEGRATED = True
    for step in (_patch_qfluent_combo_menu_exec, _patch_message_box_theming,
                 _patch_infobar_theming, _patch_label_theming, _patch_spin_box_layout):
        try:
            step()
        except Exception:
            _log.exception("qfluentwidgets integration step %s failed", step.__name__)


def global_app_qss() -> str:
    _install_integration()
    return "\n".join(s for s in (base_text_qss(), dialog_qss(), custom_theme_qss()) if s)


def _qhex(c: QColor) -> str:
    return c.name(QColor.NameFormat.HexArgb) if c.alpha() < 255 else c.name()


def _nav_ink(key: str, fallback) -> str:
    chrome = _ACTIVE_CHROME.get("chrome")
    if chrome is not None:
        with suppress(Exception):
            color = (chrome._ensure_resolved().get("nav") or {}).get(key)
            if isinstance(color, QColor):
                return _qhex(color)
    return fallback()


def nav_icon_color() -> str:
    return _nav_ink("icon", resolve_icon_color)


def _palette_fingerprint(_palette_dict=None) -> int:
    return manager.version


def apply_pivot_theme(pivot) -> None:
    base = pivot.styleSheet()

    def _apply() -> None:
        qss = manager.qss(("pivot",), _PIVOT_TPL)
        pivot.setStyleSheet(base + qss)
        for item in pivot.items.values():
            item.setStyleSheet(qss)
    register_theme_refresh(pivot, _apply)


def _patch_spin_box_layout() -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QAbstractSpinBox
    from qfluentwidgets.components.widgets import spin_box as _spin_box

    if getattr(_spin_box, "_pirachest_layout_fix", False):
        return

    def _sync_edit_width(widget) -> None:
        edit = widget.lineEdit()
        if edit is None:
            return
        rect = edit.geometry()
        width = widget.width() - rect.x()
        if rect.width() != width:
            edit.setGeometry(rect.x(), rect.y(), width, rect.height())

    def _resize_event(self, event) -> None:
        QAbstractSpinBox.resizeEvent(self, event)
        _sync_edit_width(self)

    def _change_event(self, event) -> None:
        QAbstractSpinBox.changeEvent(self, event)
        if event.type() == QEvent.Type.StyleChange:
            _sync_edit_width(self)

    for base, right_inset in ((_spin_box.InlineSpinBoxBase, 80),
                               (_spin_box.CompactSpinBoxBase, 26)):
        original_init = base.__init__

        def _initialized(self, parent=None, _init=original_init,
                         _right=right_inset) -> None:
            _init(self, parent)
            self.lineEdit().setTextMargins(0, 0, _right, 0)
            _sync_edit_width(self)

        base.__init__ = _initialized
        base.resizeEvent = _resize_event
        base.changeEvent = _change_event

    _spin_box._pirachest_layout_fix = True


def _patch_qfluent_combo_menu_exec() -> None:
    from PySide6.QtWidgets import QMenu
    if getattr(QMenu, "_pirachest_exec_patched", False):
        return
    original_exec = QMenu.exec

    def _compatible_exec(self, *args, **kwargs):
        kwargs.pop("aniType", None)
        kwargs.pop("ani", None)
        return original_exec(self, *args, **kwargs)

    QMenu.exec = _compatible_exec
    QMenu._pirachest_exec_patched = True


def apply_tool_button_theme(button) -> None:
    base_qss = button.styleSheet()

    def _apply(*_args) -> None:
        p = palette()
        qss = f"""
            ToolButton, TransparentToolButton {{
                color: {p['btn_text']};
            }}
            ToolButton:disabled, TransparentToolButton:disabled {{
                color: {p['muted']};
            }}
        """
        button.setStyleSheet(base_qss + qss)

    register_theme_refresh(button, _apply)


def _apply_rounded_mask(widget, radius: int = 12) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(widget.rect()), radius, radius)
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


def _patch_message_box_theming() -> None:
    from qfluentwidgets import MessageBoxBase
    if getattr(MessageBoxBase, "_pirachest_themed", False):
        return

    original_init = MessageBoxBase.__init__
    original_resize_event = MessageBoxBase.resizeEvent

    def _themed_init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        base_qss = self.widget.styleSheet()
        self.widget.setObjectName("pirachestDialogBody")

        button_bar = None
        for attr_name in ("buttonGroup", "buttonBar"):
            candidate = getattr(self, attr_name, None)
            if candidate is not None:
                button_bar = candidate
                break
        if button_bar is None:
            for attr_name in ("yesButton", "cancelButton"):
                btn = getattr(self, attr_name, None)
                if btn is not None and btn.parentWidget() is not None:
                    button_bar = btn.parentWidget()
                    break

        import shiboken6

        def _disconnect() -> None:
            with suppress(RuntimeError, TypeError):
                qconfig.themeChanged.disconnect(_restyle)

        def _restyle(*_args) -> None:
            if not shiboken6.isValid(self) or not shiboken6.isValid(self.widget):
                _disconnect()
                return
            c = palette()
            self.widget.setStyleSheet(
                f"{base_qss}\n#pirachestDialogBody {{ background-color: {c['dialog_bg']}; "
                f"border-radius: {_layout()['dialog_radius']}; }}"
            )
            if button_bar is not None and shiboken6.isValid(button_bar):
                button_bar.setObjectName("pirachestButtonBar")
                button_bar.setStyleSheet(
                    f"#pirachestButtonBar {{ background-color: {c['dialog_bg']}; border: none; "
                    f"border-bottom-left-radius: {_layout()['dialog_radius']}; "
                    f"border-bottom-right-radius: {_layout()['dialog_radius']}; }}"
                )
            _apply_rounded_mask(self.widget)

        _restyle()
        qconfig.themeChanged.connect(_restyle)
        self.destroyed.connect(_disconnect)

    def _themed_resize(self, event) -> None:
        original_resize_event(self, event)
        widget = getattr(self, "widget", None)
        if widget is not None:
            _apply_rounded_mask(widget)

    MessageBoxBase.__init__ = _themed_init
    MessageBoxBase.resizeEvent = _themed_resize
    MessageBoxBase._pirachest_themed = True


_INFOBAR_KIND_BY_ICON_VALUE = {
    "success": "success",
    "error": "error",
    "warning": "warning",
    "info": "info",
    "information": "info",
}


def _patch_infobar_theming() -> None:
    from qfluentwidgets import InfoBar
    if getattr(InfoBar, "_pirachest_themed", False):
        return

    original_show_event = InfoBar.showEvent

    def _infobar_kind(self) -> str:
        icon = getattr(self, "icon", None)
        value = getattr(icon, "value", None)
        if isinstance(value, str):
            return _INFOBAR_KIND_BY_ICON_VALUE.get(value.lower(), "info")
        name = getattr(icon, "name", None)
        if isinstance(name, str):
            return _INFOBAR_KIND_BY_ICON_VALUE.get(name.lower(), "info")
        return "info"

    def _themed_show_event(self, event) -> None:
        original_show_event(self, event)
        hook = _POPOUT_SFX_HOOK[0]
        if hook is not None:
            with suppress(Exception):
                hook(_infobar_kind(self))

    InfoBar.showEvent = _themed_show_event
    InfoBar._pirachest_themed = True


_LABEL_ROLE_BY_CLASS = {
    "CaptionLabel": "muted",
    "MutedLabel": "muted",
    "BodyLabel": "body_text",
    "ItalicBodyLabel": "body_text",
    "StrongBodyLabel": "primary_text",
    "SubtitleLabel": "primary_text",
    "TitleLabel": "primary_text",
    "LargeTitleLabel": "primary_text",
    "DisplayLabel": "primary_text",
}


def _patch_label_theming() -> None:
    from qfluentwidgets.components.widgets.label import FluentLabelBase
    if getattr(FluentLabelBase, "_pirachest_patched", False):
        return

    def _role_for(label) -> str:
        override = getattr(label, "_pirachest_role", None)
        if override is not None:
            return override
        for cls in type(label).__mro__:
            role = _LABEL_ROLE_BY_CLASS.get(cls.__name__)
            if role is not None:
                return role
        return "primary_text"

    def _patched_apply_theme_color(self) -> None:
        color = palette().get(_role_for(self), palette()["primary_text"])
        self.setTextColor(color, color)

    FluentLabelBase._applyThemeColor = _patched_apply_theme_color
    FluentLabelBase._pirachest_patched = True


    original_show_event = FluentLabelBase.showEvent

    def _patched_show_event(self, event) -> None:
        if not getattr(self, "_pirachest_initial_color_applied", False):
            self._pirachest_initial_color_applied = True
            _patched_apply_theme_color(self)
        original_show_event(self, event)

    FluentLabelBase.showEvent = _patched_show_event


def apply_label_theme(label, role: str = "muted") -> None:
    _patch_label_theming()
    label._pirachest_role = role
    color = palette().get(role, palette()["primary_text"])
    label.setTextColor(color, color)


def themed_icon(slot_id: Optional[str], default_icon=None, size: int = 20, color: Optional[str] = None) -> QIcon:
    spec = manager.resolve_icon_spec(slot_id) if slot_id else None
    tint_color = color if color is not None else resolve_icon_color()
    if default_icon is None and slot_id:
        _n = icon_slot_default_name(slot_id)
        default_icon = _fluent_icon_by_name(_n) if _n else None

    if spec is not None and spec.get("type") == "image":
        icon = image_icon(spec["value"], size, crop=bool(spec.get("crop", True)))
        if not icon.isNull():
            return icon
    if spec is not None and spec.get("type") == "fluent":
        fluent = _fluent_icon_by_name(spec["value"])
        if fluent is not None:
            default_icon = fluent

    if hasattr(default_icon, "icon"):
        with suppress(Exception):
            return default_icon.icon(color=QColor(tint_color))
    return tinted_icon(default_icon, tint_color, size)


def accent_icon(slot_id: Optional[str], default_icon=None, size: int = 20) -> QIcon:
    return themed_icon(slot_id, default_icon, size=size, color=palette().get("on_accent", "#ffffff"))


def branded_icon(slot_id: Optional[str], default_icon=None, size: int = 20) -> QIcon:
    spec = manager.resolve_icon_spec(slot_id) if slot_id else None
    if spec is not None and spec.get("type") == "image":
        icon = image_icon(spec["value"], size, crop=bool(spec.get("crop", True)))
        if not icon.isNull():
            return icon
    if spec is not None and spec.get("type") == "fluent":
        fluent = _fluent_icon_by_name(spec["value"])
        if fluent is not None:
            return themed_icon(None, fluent, size=size)
    if isinstance(default_icon, QIcon):
        return QIcon(default_icon)
    if isinstance(default_icon, str):
        icon = QIcon(default_icon)
        if not icon.isNull():
            return icon
    return themed_icon(slot_id, default_icon, size=size)


def count_badge_qss(radius: int = 7, font_size: int = 10) -> str:
    pal = palette()
    return (f"background-color: {pal['accent']}; color: {pal['on_accent']}; "
            f"border-radius: {radius}px; font-size: {font_size}px; font-weight: 600;")


def themed_nav_icon(slot_id: Optional[str], default_icon, size: int = 20) -> QIcon:
    chrome = _ACTIVE_CHROME.get("chrome")
    if chrome is not None:
        with suppress(Exception):
            return chrome.themed_nav_icon(slot_id or "", default_icon, size=size)
    tint = nav_icon_color()
    return themed_icon(slot_id, default_icon, size=size, color=tint)


def apply_slot_icon_theme(widget, slot_id: str, default_icon, size: int = 16) -> None:

    def _apply(*_args) -> None:
        widget.setIcon(themed_icon(slot_id, default_icon, size))
    register_theme_refresh(widget, _apply)



try:
    _patch_label_theming()
except Exception:
    _log.exception('label theming patch failed')


IS_WIN = sys.platform == "win32"

_RGBA = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", re.I)


def to_color(value, fallback: str = "#000000") -> QColor:
    if isinstance(value, QColor):
        return QColor(value)
    if isinstance(value, str):
        text = value.strip()
        match = _RGBA.fullmatch(text)
        if match:
            r, g, b = (max(0, min(255, int(float(match.group(i))))) for i in (1, 2, 3))
            raw = match.group(4)
            if raw is None:
                alpha = 255
            else:
                number = float(raw)
                alpha = int(round(number * 255)) if number <= 1 else int(number)
            return QColor(r, g, b, max(0, min(255, alpha)))
        color = QColor(text)
        if color.isValid():
            return color
    return QColor(fallback)


def _linear_channel(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color: QColor) -> float:
    return (
        0.2126 * _linear_channel(color.red())
        + 0.7152 * _linear_channel(color.green())
        + 0.0722 * _linear_channel(color.blue())
    )


def contrast_ratio(fg: QColor, bg: QColor) -> float:
    def _composite(top: QColor, bottom: QColor) -> QColor:
        a = top.alpha() / 255.0
        if a >= 1.0:
            return QColor(top.red(), top.green(), top.blue())
        r = int(round(top.red() * a + bottom.red() * (1 - a)))
        g = int(round(top.green() * a + bottom.green() * (1 - a)))
        b = int(round(top.blue() * a + bottom.blue() * (1 - a)))
        return QColor(r, g, b)

    bg_opaque = bg
    if bg.alpha() < 255:

        try:
            base = QColor(palette().get("page_bg", "#202020"))
        except Exception:
            base = QColor("#202020")
        if base.alpha() < 255:
            base = QColor(18, 18, 18) if isDarkTheme() else QColor(255, 255, 255)
        bg_opaque = _composite(bg, base)
    fg_opaque = _composite(fg, bg_opaque)
    l1, l2 = _luminance(fg_opaque), _luminance(bg_opaque)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def ensure_contrast(fg: QColor, bg: QColor, min_ratio: float = 4.5) -> QColor:
    if fg is None or bg is None:
        return fg
    try:
        if contrast_ratio(fg, bg) >= min_ratio:
            return QColor(fg)
    except Exception:
        return fg

    for direction in (-1, 1):
        trial = QColor(fg)
        h, s, v, _a = trial.getHsvF()
        if h < 0:
            for _ in range(24):
                val = max(0.0, min(1.0, trial.valueF() + direction * 0.06))
                trial = QColor.fromHsvF(0.0, 0.0, val, fg.alphaF())

                try:
                    if contrast_ratio(trial, bg) >= min_ratio:
                        return trial
                except Exception:
                    return trial
                if (direction < 0 and val <= 0.0) or (direction > 0 and val >= 1.0):
                    break
            continue
        for _ in range(24):
            v = max(0.0, min(1.0, v + direction * 0.06))

            trial = QColor.fromHsvF(h, min(1.0, s), v, fg.alphaF())
            try:
                if contrast_ratio(trial, bg) >= min_ratio:
                    return trial
            except Exception:
                return trial
            if (direction < 0 and v <= 0.0) or (direction > 0 and v >= 1.0):
                break

    try:
        black = QColor(0, 0, 0, fg.alpha())
        white = QColor(255, 255, 255, fg.alpha())
        return white if contrast_ratio(white, bg) > contrast_ratio(black, bg) else black
    except Exception:
        return fg


def _surface_rgb(bg=None) -> tuple:
    pal = palette()
    page = _parse_rgba(pal.get("page_bg", "#000000")) or (0, 0, 0, 1.0)
    page_op = _flat(page, (0, 0, 0) if _lum(page[:3]) < 0.5 else (255, 255, 255))
    if bg is None:
        return page_op
    if isinstance(bg, QColor):
        c = (bg.red(), bg.green(), bg.blue(), bg.alphaF())
    else:
        c = _parse_rgba(str(pal.get(bg, bg)))
    return _flat(c, page_op) if c else page_op


def legible_text(fg, bg=None, min_ratio: float = 4.5) -> str:
    pal = palette()
    c = _parse_rgba(str(pal.get(fg, fg))) if not isinstance(fg, QColor) else (fg.red(), fg.green(), fg.blue(), fg.alphaF())
    if c is None:
        return str(fg)
    prim = _parse_rgba(pal.get("primary_text", "#ffffff")) or (255, 255, 255, 1.0)
    surf = _surface_rgb(bg)
    return _fmt(_legible(c, [surf], min_ratio, pole=1 if _lum(prim[:3]) >= _lum(surf) else -1,
                         floor=min(3.0, min_ratio * 0.7)))


def text_on(bg) -> str:
    s = _surface_rgb(bg)
    return "#ffffff" if _ratio((255, 255, 255), s) >= _ratio((17, 17, 17), s) else "#111111"


_ACTIVE_CHROME: dict = {}
_NAV_ICON_CACHE: dict[tuple, QIcon] = {}


def _register_nav_chrome(chrome: "WindowChrome") -> None:
    _ACTIVE_CHROME["chrome"] = chrome
    with suppress(Exception):
        _patch_nav_paint()


def clear_nav_icon_cache() -> None:
    _NAV_ICON_CACHE.clear()


try:
    from qfluentwidgets.components.navigation.navigation_widget import NavigationWidget
except Exception:
    try:
        from qfluentwidgets.components.navigation.navigation_panel import NavigationWidget
    except Exception:
        NavigationWidget = QWidget


_NAV_PAINT_PATCHED = False


def _nav_indicator_color(widget):
    try:
        light, dark = widget.lightIndicatorColor, widget.darkIndicatorColor
    except Exception:
        return None
    try:
        from qfluentwidgets.common.color import autoFallbackThemeColor
        color = autoFallbackThemeColor(light, dark)
    except Exception:
        try:
            from qfluentwidgets.common.color import isDarkTheme
            color = dark if isDarkTheme() else light
        except Exception:
            color = light
    try:
        return color if isinstance(color, QColor) and color.isValid() else None
    except Exception:
        return None


def _patch_nav_paint() -> None:
    global _NAV_PAINT_PATCHED
    if _NAV_PAINT_PATCHED:
        return
    try:
        from PySide6.QtGui import QPainter as _QP
        from PySide6.QtCore import Qt as _Qt
        from qfluentwidgets.common.icon import drawIcon as _drawIcon
        from qfluentwidgets.components.navigation.navigation_widget import (
            NavigationPushButton,
            NavigationSeparator,
        )
    except Exception:
        return

    orig_push = NavigationPushButton.paintEvent

    def _patched_push(self, e) -> None:


        chrome = _ACTIVE_CHROME.get("chrome")
        if chrome is None:
            orig_push(self, e)
            return

        try:
            res = chrome._res or {}
            glow_holder = res.get("nav_glow_holder") or (None, 0)
            nav = res.get("nav") or {}
            nav_radius = float(nav.get("radius", 5))
            hover_fill = chrome.color((nav.get("hover") or {}).get("fill"))
            selected_fill = chrome.color((nav.get("selected") or {}).get("fill"))
        except Exception:
            orig_push(self, e)
            return
        try:
            p = _QP(self)
            p.setRenderHints(_QP.RenderHint.Antialiasing | _QP.RenderHint.TextAntialiasing | _QP.RenderHint.SmoothPixmapTransform)
            p.setPen(_Qt.PenStyle.NoPen)
            pressed = bool(getattr(self, "isPressed", False))
            selected = bool(getattr(self, "isSelected", False) or getattr(self, "isAboutSelected", False))
            hovered = bool(getattr(self, "isEnter", False))
            if pressed:
                p.setOpacity(0.85)
            if not self.isEnabled():
                p.setOpacity(0.4)
            if selected or pressed or hovered:
                fill = selected_fill if selected else hover_fill
                if fill is None:
                    fill = hover_fill
                if fill is not None and fill.isValid():
                    if pressed and not selected:
                        fill = QColor(fill)
                        fill.setAlpha(min(255, fill.alpha() + 28))
                    p.setBrush(fill)
                    p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), nav_radius, nav_radius)
            with suppress(Exception):


                if self._canDrawIndicator():
                    bar = _nav_indicator_color(self)
                    if bar is not None:
                        p.setBrush(bar)
                        p.drawRoundedRect(self.indicatorRect(), 1.5, 1.5)
            m = self._margins()
            pl, pr = m.left(), m.right()
            with suppress(Exception):
                _drawIcon(self._icon, p, QRectF(11.5 + pl, 10, 16, 16))
            if getattr(self, "isCompacted", False):
                p.end()
                return
            try:
                p.setFont(self.font())
                glow, glow_radius = glow_holder
                if glow is not None and glow_radius > 0:
                    _draw_text(p, self.font(), QRectF(
                        (44 + pl if not self.icon().isNull() else pl + 16), 0,
                        self.width() - 13 - (44 + pl if not self.icon().isNull() else pl + 16) - pr,
                        self.height()), self.text(), self.textColor(), glow, glow_radius)
                else:
                    p.setPen(self.textColor())
                    left = 44 + pl if not self.icon().isNull() else pl + 16
                    p.drawText(QRectF(left, 0, self.width() - 13 - left - pr, self.height()),
                               _Qt.AlignmentFlag.AlignVCenter, self.text())
            except Exception:
                with suppress(Exception):
                    p.setPen(self.textColor())
                    left = 44 + pl if not self.icon().isNull() else pl + 16
                    p.drawText(QRectF(left, 0, self.width() - 13 - left - pr, self.height()),
                               _Qt.AlignmentFlag.AlignVCenter, self.text())
            p.end()
        except Exception:
            with suppress(Exception):
                p.end()
            orig_push(self, e)

    NavigationPushButton.paintEvent = _patched_push

    with suppress(Exception):
        orig_sep = NavigationSeparator.paintEvent

        def _patched_sep(self, e) -> None:
            chrome = _ACTIVE_CHROME.get("chrome")
            if chrome is None:
                orig_sep(self, e)
                return
            with suppress(Exception):
                sep = (chrome._res or {}).get("nav_separator")
                if isinstance(sep, QColor):
                    from PySide6.QtGui import QPainter as _QP2
                    from PySide6.QtGui import QPen as _QPen2
                    p = _QP2(self)
                    pen = _QPen2(sep)
                    pen.setCosmetic(True)
                    p.setPen(pen)
                    p.drawLine(0, 1, self.width(), 1)
                    p.end()
                    return
            orig_sep(self, e)

        NavigationSeparator.paintEvent = _patched_sep
    _NAV_PAINT_PATCHED = True


def _build() -> int:
    return sys.getwindowsversion().build if IS_WIN else 0


class _Accent(Structure):
    _fields_ = [("state", c_int), ("flags", c_int), ("color", c_uint), ("anim", c_int)]


class _CompAttr(Structure):
    _fields_ = [("attr", c_int), ("data", c_void_p), ("size", c_size_t)]


class _Margins(Structure):
    _fields_ = [("left", c_int), ("right", c_int), ("top", c_int), ("bottom", c_int)]


class _BlurBehind(Structure):
    _fields_ = [("flags", c_uint), ("enable", c_int), ("region", c_void_p), ("transition", c_int)]


_CORNER_PREF = {"default": 0, "square": 1, "round": 2, "small": 3}


class DwmBackdrop:

    def __init__(self) -> None:
        self._ready = False
        self._last_kind = None
        self._last_frame = None
        self._last_dark = None
        self._last_corner = None
        self._last_border = None
        if not IS_WIN:
            return
        try:
            user32 = ctypes.WinDLL("user32")
            dwm = ctypes.WinDLL("dwmapi")
            self._set_comp = user32.SetWindowCompositionAttribute
            self._set_comp.argtypes = [c_void_p, POINTER(_CompAttr)]
            self._set_comp.restype = c_int
            self._set_attr = dwm.DwmSetWindowAttribute
            self._set_attr.argtypes = [c_void_p, c_uint, c_void_p, c_uint]
            self._set_attr.restype = c_long
            self._extend = dwm.DwmExtendFrameIntoClientArea
            self._extend.argtypes = [c_void_p, POINTER(_Margins)]
            self._extend.restype = c_long
            self._blur = dwm.DwmEnableBlurBehindWindow
            self._blur.argtypes = [c_void_p, POINTER(_BlurBehind)]
            self._blur.restype = c_long
            self._pos = user32.SetWindowPos
            self._pos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
            self._pos.restype = c_int
            self._redraw = user32.RedrawWindow
            self._redraw.argtypes = [c_void_p, c_void_p, c_void_p, c_uint]
            self._redraw.restype = c_int
            self._ready = True
        except (OSError, AttributeError):
            self._ready = False

    @staticmethod
    def supports(kind: str) -> bool:
        if kind == "none":
            return True
        if not IS_WIN:
            return False
        build = _build()
        if kind == "acrylic":
            return build >= 17134
        if kind == "blur":
            return build >= 6000
        return False

    def refresh(self, hwnd: int) -> None:
        if not self._ready or not hwnd:
            return
        self._pos(int(hwnd), None, 0, 0, 0, 0, 0x37)
        self._redraw(int(hwnd), None, None, 0x585)

    def _attr(self, hwnd: int, attr: int, value: int) -> int:
        number = c_uint(value & 0xFFFFFFFF)
        return self._set_attr(hwnd, attr, byref(number), sizeof(number))

    def _accent(self, hwnd: int, state: int, color: int = 0, flags: int = 0) -> None:
        accent = _Accent(state, flags, color, 0)
        data = _CompAttr(19, ctypes.addressof(accent), sizeof(accent))
        self._set_comp(hwnd, byref(data))

    def _blur_behind(self, hwnd: int, enable: bool) -> None:
        blur = _BlurBehind(1, 1 if enable else 0, None, 0)
        self._blur(hwnd, byref(blur))

    @staticmethod
    def _abgr(color: QColor) -> int:
        return (color.alpha() << 24) | (color.blue() << 16) | (color.green() << 8) | color.red()

    def apply(
        self,
        hwnd: int,
        kind: str,
        dark: bool,
        tint: Optional[QColor] = None,
        corner: Optional[str] = None,
        border: Optional[QColor] = None,
        frame: bool = False,
    ) -> None:
        if not self._ready or not hwnd:
            return
        build = _build()
        h = int(hwnd)
        kind_changed = kind != self._last_kind

        # Only clear the backdrop when no new one replaces it below. Clearing
        # first and re-applying right after lets DWM composite one frame with
        # no backdrop, which shows up as a flash while switching themes.
        replaced = (kind == "acrylic" and build >= 17134) or (kind == "blur" and build >= 6000)
        if kind_changed and not replaced:
            self._accent(h, 0)
        if kind_changed and build < 10240 and kind != "blur":
            self._blur_behind(h, False)
        if build >= 22621:
            self._attr(h, 38, 1)
        elif build >= 22000:
            self._attr(h, 1029, 0)

        if build >= 17763 and dark != self._last_dark:
            if self._attr(h, 20, int(dark)) != 0:
                self._attr(h, 19, int(dark))

        if build >= 22000:
            corner_value = _CORNER_PREF.get(corner or "default", 0)
            if corner_value != self._last_corner:
                self._attr(h, 33, corner_value)
            border_value = (0xFFFFFFFF if border is None
                            else border.red() | (border.green() << 8) | (border.blue() << 16))
            if border_value != self._last_border:
                self._attr(h, 34, border_value)

        if build >= 6000 and frame != self._last_frame:

            self._extend(h, byref(_Margins(-1, -1, -1, -1) if frame else _Margins(0, 0, 0, 0)))

        if kind == "acrylic" and build >= 17134:
            color = tint if tint is not None else QColor(242, 242, 242, 153)
            self._accent(h, 4, self._abgr(color), 2)
        elif kind == "blur":
            if build >= 10240:
                self._accent(h, 3, 0, 0)
            elif build >= 6000:
                self._blur_behind(h, True)

        self._last_kind = kind
        self._last_frame = frame
        self._last_dark = dark
        if build >= 22000:
            self._last_corner = _CORNER_PREF.get(corner or "default", 0)
            self._last_border = (0xFFFFFFFF if border is None
                                 else border.red() | (border.green() << 8) | (border.blue() << 16))


_CHIP_BASE_STYLE: dict = {
    "normal": {"fill": "@surface_tint_strong"},
    "hover": {"fill": "@surface_border"},
    "pressed": {"fill": "@surface_border"},
}


def _round_shape(width: float, height: float, radius: float) -> Callable[[float, float], QPainterPath]:
    def shape(inset: float = 0.0, delta: float = 0.0) -> QPainterPath:
        r = max(radius + delta, 0.0)
        path = QPainterPath()
        path.addRoundedRect(QRectF(inset, inset, width - 2 * inset, height - 2 * inset), r, r)
        return path
    return shape


def _draw_text(p: QPainter, font: QFont, rect: QRectF, text: str, color: QColor,
               glow: Optional[QColor] = None, radius: int = 0, elide: bool = True) -> None:
    metrics = QFontMetrics(font)
    if elide:
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, int(rect.width())))
    if not text:
        return
    p.save()
    p.setFont(font)
    if glow is None or radius <= 0:
        p.setPen(color)
        p.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        p.restore()
        return
    path = QPainterPath()
    path.addText(rect.x(), rect.y() + (rect.height() - metrics.height()) / 2.0 + metrics.ascent(), font, text)
    for width in range(radius * 2 + 1, 2, -2):
        p.strokePath(path, QPen(glow, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.fillPath(path, color)
    p.restore()


class ChromeTitleLabel(QLabel):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("titleLabel")
        self._font = QFont(QApplication.font())
        self._color = QColor(0, 0, 0)
        self._glow: Optional[QColor] = None
        self._glow_radius = 0
        self._pad = 4

    def setChromeStyle(self, font: QFont, color: QColor, glow: Optional[QColor], glow_radius: int, pad: int) -> None:
        self._font = QFont(font)
        self._color = QColor(color)
        self._glow = QColor(glow) if glow is not None and glow_radius > 0 else None
        self._glow_radius = glow_radius if self._glow is not None else 0
        self._pad = pad
        self.updateGeometry()
        self.adjustSize()
        self.update()

    def _padding(self) -> int:
        return self._pad + self._glow_radius

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self._font)
        pad = self._padding()
        return QSize(metrics.horizontalAdvance(self.text()) + pad * 2, metrics.height() + pad)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def setText(self, text: str) -> None:
        super().setText(text)
        self.updateGeometry()
        self.adjustSize()

    def paintEvent(self, event) -> None:
        text = self.text()
        if not text:
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        pad = self._padding()
        _draw_text(p, self._font, QRectF(pad, 0, self.width() - pad, self.height()), text,
                   self._color, self._glow, self._glow_radius, elide=False)


class ChromeButton(TitleBarButton):

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._is_max = False
        self._chrome: Optional["WindowChrome"] = None
        self._corners = (0.0, 0.0, 0.0, 0.0)
        self._bleed = 0.0

    def setMaxState(self, is_max: bool) -> None:
        if self._is_max != is_max:
            self._is_max = is_max
            self.setState(TitleBarButtonState.NORMAL)
            self.update()

    def configure(self, chrome: "WindowChrome") -> None:
        self._chrome = chrome
        buttons = chrome.current.buttons
        own = _dict(_dict(buttons.get("kinds")).get(self._kind))
        corners = own.get("corners")
        valid = isinstance(corners, (list, tuple)) and len(corners) == 4
        self._corners = tuple(float(_num(c, 0)) for c in corners) if valid else (0.0,) * 4
        self._bleed = float(_num(buttons.get("bleed"), 0))
        width = _num(own.get("width"), _num(buttons.get("width"), 46))
        height = _num(own.get("height"), _num(buttons.get("height"), 32))
        self.setFixedSize(max(8, int(width)), max(8, int(height)))
        self.update()

    def _shape(self, inset: float, delta: float) -> QPainterPath:
        x0, y0 = inset, -self._bleed
        x1, y1 = self.width() - inset, self.height() - inset
        tl, tr, br, bl = (max(c + delta, 0.0) if c else 0.0 for c in self._corners)
        path = QPainterPath()
        path.moveTo(x0 + tl, y0)
        path.lineTo(x1 - tr, y0)
        if tr:
            path.arcTo(x1 - 2 * tr, y0, 2 * tr, 2 * tr, 90, -90)
        path.lineTo(x1, y1 - br)
        if br:
            path.arcTo(x1 - 2 * br, y1 - 2 * br, 2 * br, 2 * br, 0, -90)
        path.lineTo(x0 + bl, y1)
        if bl:
            path.arcTo(x0, y1 - 2 * bl, 2 * bl, 2 * bl, 270, -90)
        path.lineTo(x0, y0 + tl)
        if tl:
            path.arcTo(x0, y0, 2 * tl, 2 * tl, 180, -90)
        path.closeSubpath()
        return path

    @staticmethod
    def _glyph_path(shape: dict) -> QPainterPath:
        path = QPainterPath()
        kind = shape.get("shape")
        if kind == "line":
            points = [pt for pt in shape.get("points") or () if isinstance(pt, (list, tuple)) and len(pt) == 2]
            for index, (x, y) in enumerate(points):
                if index:
                    path.lineTo(float(x), float(y))
                else:
                    path.moveTo(float(x), float(y))
            return path
        rect = QRectF(*(float(_num(shape.get(key), 0)) for key in ("x", "y", "w", "h")))
        path.addRect(rect)
        if kind == "ring":
            thickness = float(_num(shape.get("t"), 1))
            path.addRect(rect.adjusted(thickness, thickness, -thickness, -thickness))
            path.setFillRule(Qt.FillRule.OddEvenFill)
        return path

    def _paint_glyph(self, p: QPainter, style: dict, chrome: "WindowChrome") -> None:
        fill = chrome.color(style.get("glyph"))
        if fill is None:
            return
        outline = chrome.color(style.get("glyph_outline"))
        name = "restore" if self._kind == "max" and self._is_max else self._kind
        shapes = [(self._glyph_path(shape), shape) for shape in chrome.glyph(name)]
        p.save()
        p.translate(self.width() / 2.0, self.height() / 2.0)
        for stage in (0, 1):
            for path, shape in shapes:
                reach = float(_num(shape.get("outline"), 0)) if outline is not None else 0.0
                if stage == 0 and not reach:
                    continue
                p.save()
                cut = shape.get("clip_out")
                if isinstance(cut, (list, tuple)) and len(cut) == 4:
                    keep = QPainterPath()
                    keep.addRect(QRectF(-4096, -4096, 8192, 8192))
                    keep.addRect(QRectF(*(float(_num(v, 0)) for v in cut)))
                    keep.setFillRule(Qt.FillRule.OddEvenFill)
                    p.setClipPath(keep)
                if shape.get("shape") == "line":
                    stroke = float(_num(shape.get("width"), 1))
                    width, color = (stroke + 2 * reach, outline) if stage == 0 else (stroke, fill)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                elif stage == 0:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(outline, 2 * reach))
                else:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(fill)
                p.drawPath(path)
                p.restore()
        p.restore()

    def paintEvent(self, event) -> None:
        chrome = self._chrome
        if chrome is None:
            return
        state = {TitleBarButtonState.HOVER: "hover", TitleBarButtonState.PRESSED: "pressed"}.get(self._state, "normal")
        style = chrome.button_style(self._kind, state)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        chrome.paint_surface(p, style, self._shape, float(self.width()), float(self.height()))
        self._paint_glyph(p, style, chrome)


class ChromeChip(QWidget):

    clicked = Signal()

    def __init__(self, text: str = "", icon=None, parent=None) -> None:
        super().__init__(parent)
        self._text = text
        self._icon_source = icon
        self._chrome: Optional["WindowChrome"] = None
        self._hover = False
        self._down = False
        self._font = QFont(QApplication.font())
        self._icon_px = 13
        self._icon_boost = 0
        self._pad = 10
        self._spacing = 5
        self._gap = 8
        self._height = 22
        self._radius = 11.0
        self._pixmap: Optional[QPixmap] = None
        self._glyphs: dict = {}
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def setText(self, text: str) -> None:
        self._text = text
        self._relayout()

    def text(self) -> str:
        return self._text

    def setIconSource(self, icon) -> None:
        self._icon_source = icon
        self._pixmap = None
        self._glyphs.clear()
        self._relayout()

    def setPixmap(self, pixmap: QPixmap) -> None:
        self._icon_source = None
        self._pixmap = QPixmap(pixmap) if pixmap is not None else None
        self._glyphs.clear()
        self._relayout()

    def _icon(self, color: QColor) -> Optional[QIcon]:
        if self._pixmap is not None and not self._pixmap.isNull():
            return QIcon(self._pixmap)
        source = self._icon_source
        if source is None:
            return None
        if callable(source):
            try:
                return source(color.name(QColor.NameFormat.HexArgb))
            except Exception:
                return None
        if isinstance(source, QPixmap):
            return QIcon(source)
        if isinstance(source, QIcon):


            try:
                return tinted_icon(source, color.name(), self._icon_px)
            except Exception:
                return source
        if hasattr(source, "icon"):
            with suppress(Exception):
                return source.icon(color=color)
        return source if isinstance(source, QIcon) else QIcon(source)

    def _glyph(self, color: QColor):
        dpr = self.devicePixelRatioF()
        side = max(1, round(self._icon_px * dpr))
        key = (color.rgba(), side)
        hit = self._glyphs.get(key)
        if hit is not None:
            return hit
        icon = self._icon(color)
        if icon is None or icon.isNull():
            return None
        pm = QPixmap(side, side)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        icon.paint(qp, QRect(0, 0, side, side))
        qp.end()
        alpha = pm.toImage().convertToFormat(QImage.Format.Format_Alpha8)
        bpl, data = alpha.bytesPerLine(), bytes(alpha.constBits())
        rows = [y for y in range(side) if max(data[y * bpl:y * bpl + side]) > 8]
        cols = [x for x in range(side) if max(data[x:side * bpl:bpl]) > 8]
        dx = dy = 0.0
        if rows and cols:
            dx = side / 2.0 - (cols[0] + cols[-1] + 1) / 2.0
            dy = side / 2.0 - (rows[0] + rows[-1] + 1) / 2.0
        pm.setDevicePixelRatio(dpr)
        hit = (pm, dx / dpr, dy / dpr, side / dpr)
        if len(self._glyphs) > 8:
            self._glyphs.clear()
        self._glyphs[key] = hit
        return hit

    def _has_icon(self) -> bool:
        return self._icon_source is not None or (
            self._pixmap is not None and not self._pixmap.isNull()
        )

    def configure(self, chrome: "WindowChrome") -> None:
        self._chrome = chrome
        spec = chrome.current
        chips = spec.chips
        title = spec.title
        bar = chrome.metrics_height()
        self._height = max(12, min(int(_num(chips.get("height"), 22)), max(12, bar - 3)))
        self._radius = float(_num(chips.get("radius"), self._height / 2.0))
        self._pad = int(_num(chips.get("padding"), 10))
        self._spacing = int(_num(chips.get("spacing"), 5))
        self._gap = int(_num(chips.get("gap"), 8))
        self._icon_px = max(12, round(self._height * 0.78)) + self._icon_boost
        self._glyphs.clear()
        font = QFont(QApplication.font())
        family = _opt_str(title.get("family"))
        if family:
            font.setFamily(family)
        font.setPixelSize(max(6, int(_num(chips.get("size"), 12))))
        font.setBold(bool(chips.get("bold", False)))
        self._font = font
        self._relayout()

    def _wanted_width(self) -> int:
        metrics = QFontMetrics(self._font)


        text_w = max(metrics.horizontalAdvance(self._text), metrics.boundingRect(self._text).width()) + 2
        width = self._gap + self._pad * 2 + text_w
        if self._has_icon():
            width += self._icon_px + self._spacing
        return max(8, int(width))

    def _relayout(self) -> None:
        self.setFixedSize(self._wanted_width(), self._height)
        self.update()

    def event(self, event) -> bool:

        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange,
                            QEvent.Type.DevicePixelRatioChange):
            QTimer.singleShot(0, self._relayout)
        return super().event(event)

    def _state(self) -> str:
        if self._down:
            return "pressed"
        return "hover" if self._hover else "normal"

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._down = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        was_down = self._down
        self._down = False
        self.update()
        try:
            inside = self.rect().contains(event.position().toPoint())
        except Exception:
            inside = False
        if was_down and event.button() == Qt.MouseButton.LeftButton and inside:
            self.clicked.emit()
        event.accept()

    def paintEvent(self, event) -> None:
        chrome = self._chrome
        if chrome is None:
            return
        state = self._state()
        try:
            res = chrome._res or {}
            style = (res.get("chips") or {}).get(state) or chrome.chip_style(state)
            color = (res.get("chip_text") or {}).get(state) or chrome.chip_text_color(style)
            glow = res.get("title_glow")
            glow_radius = min(int(res.get("title_glow_radius", 0)), 3)
            if glow is None or glow_radius <= 0:
                glow, glow_radius = chrome.title_glow(cap=3)
        except Exception:
            style = chrome.chip_style(state)
            color = chrome.chip_text_color(style)
            glow, glow_radius = chrome.title_glow(cap=3)
        if self._wanted_width() != self.width():
            QTimer.singleShot(0, self._relayout)
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        body_x = float(self._gap)
        w, h = float(self.width()) - body_x, float(self.height())
        p.translate(body_x, 0)
        chrome.paint_surface(p, style, _round_shape(w, h, self._radius), w, h)
        p.translate(-body_x, 0)
        x = body_x + self._pad
        if self._has_icon():
            glyph = self._glyph(color)
            if glyph is not None:
                pm, dx, dy, logical = glyph
                fm = QFontMetricsF(self._font)
                cap_mid = h / 2.0 - fm.height() / 2.0 + fm.ascent() - fm.capHeight() / 2.0
                p.drawPixmap(QPointF(x + self._icon_px / 2.0 - logical / 2.0 + dx, cap_mid - logical / 2.0 + dy), pm)
            x += self._icon_px + self._spacing
        _draw_text(p, self._font, QRectF(x, 0, self.width() - x - self._pad, h), self._text, color, glow, glow_radius)


class ChromeTitleBar(FluentTitleBar):

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._chrome: Optional["WindowChrome"] = None
        self._chips: list = []
        label = ChromeTitleLabel(self)
        old = self.titleLabel
        self.hBoxLayout.insertWidget(max(self.hBoxLayout.indexOf(old), 0), label, 0,
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.hBoxLayout.removeWidget(old)
        old.hide()
        old.deleteLater()
        self.titleLabel = label
        label.setText(self.window().windowTitle())
        window = self.window()
        buttons = tuple(ChromeButton(kind, self) for kind in ("min", "max", "close"))
        for before, after in zip((self.minBtn, self.maxBtn, self.closeBtn), buttons, strict=True):
            self.buttonLayout.insertWidget(self.buttonLayout.indexOf(before), after)
            self.buttonLayout.removeWidget(before)
            before.hide()
            before.deleteLater()
        self.minBtn, self.maxBtn, self.closeBtn = buttons
        self.minBtn.clicked.connect(window.showMinimized)
        self.maxBtn.clicked.connect(lambda: toggleMaxState(window))
        self.closeBtn.clicked.connect(window.close)

    def _buttons(self) -> tuple:
        return self.minBtn, self.maxBtn, self.closeBtn

    def addChip(self, chip: "ChromeChip") -> None:
        anchor = self._chips[-1] if self._chips else self.titleLabel
        index = self.hBoxLayout.indexOf(anchor)
        self.hBoxLayout.insertWidget(index + 1 if index >= 0 else 2, chip, 0,
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._chips.append(chip)
        if self._chrome is not None:
            chip.configure(self._chrome)

    def chips(self) -> tuple:
        return tuple(self._chips)

    def setIcon(self, icon) -> None:
        size = self.iconLabel.width()
        self.iconLabel.setPixmap(QIcon(icon).pixmap(size, size))

    def configure(self, chrome: "WindowChrome") -> None:
        self._chrome = chrome
        spec = chrome.current
        self.iconLabel.setFixedSize(spec.icon, spec.icon)
        self.setIcon(self.window().windowIcon())
        self.buttonLayout.setSpacing(int(_num(spec.buttons.get("spacing"), 0)))
        for button in self._buttons():
            button.configure(chrome)
        for chip in self._chips:
            chip.configure(chrome)
        self.restyle()

    def restyle(self) -> None:
        chrome = self._chrome
        if chrome is None:
            return
        spec = chrome.current.title
        font = QFont(QApplication.font())
        family = _opt_str(spec.get("family"))
        if family:
            font.setFamily(family)
        font.setPixelSize(max(6, int(_num(spec.get("size"), 14))))
        font.setBold(bool(spec.get("bold", False)))
        glow = _dict(spec.get("glow"))
        color = chrome.color(spec.get("color"))
        glow_color = chrome.color(glow.get("color"))
        alpha = chrome.resolve(glow.get("alpha"))
        if glow_color is not None and isinstance(alpha, (int, float)):
            glow_color.setAlpha(max(0, min(255, int(alpha))))
        self.titleLabel.setChromeStyle(
            font, color if color is not None else QColor(0, 0, 0), glow_color,
            int(_num(glow.get("radius"), 0)), int(_num(spec.get("padding"), 4)),
        )
        for button in self._buttons():
            button.update()
        for chip in self._chips:
            chip.update()
        self.update()

    def syncMaxState(self, is_max: bool) -> None:
        for button in self._buttons():
            button.setMaxState(is_max)

    def paintEvent(self, event) -> None:
        chrome = self._chrome
        if chrome is not None and chrome.current.mode != "frame" and chrome.current.layers:
            p = QPainter(self)
            chrome.paint_layers(p, QRectF(self.rect()), float(self.height()), chrome.current.layers)
            p.end()


class WindowChrome(QObject):

    applied = Signal()

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._backdrop = DwmBackdrop()
        self._kind = "none"
        self._spec = _DEFAULT_TITLEBAR
        self._native = False
        self._radius = 0.0
        self._shown = False
        self._active = True
        self._nav_sheet = ""
        self._stack_sheet = ""
        self._nav_slots: dict[str, tuple[str, object]] = {}
        self._transparent = False


        self._res: dict = {}
        self._res_key: tuple = ()

        self._frame_pixmap: Optional[QPixmap] = None
        self._frame_key: tuple = ()

        self._backdrop_last: tuple = ()
        self._nav_last = weakref.WeakKeyDictionary()
        self._last_spec_id: tuple = ()
        self._laid_spec = None

    @property
    def current(self) -> TitleBarSpec:
        return self._spec

    def native_window_recreated(self) -> None:
        self._backdrop_last = ()
        self._last_spec_id = ()
        self._frame_pixmap = None
        self._frame_key = ()

    def install(self) -> None:
        window = self._window
        if IS_WIN:
            before = int(window.winId())
            window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if int(window.winId()) != before and hasattr(window, "updateFrameless"):
                window.updateFrameless()
        window.setTitleBar(ChromeTitleBar(window))
        nav = getattr(window, "navigationInterface", None)
        if nav is not None:
            nav.displayModeChanged.connect(window.titleBar.raise_)
        window.installEventFilter(self)
        _register_nav_chrome(self)



    def spec(self) -> TitleBarSpec:
        return theme_titlebar(active_preset_id()) or _DEFAULT_TITLEBAR

    def resolve(self, value):
        if isinstance(value, dict):
            value = value.get("active" if self._active else "inactive")
        if isinstance(value, str) and value.startswith("@"):
            value = palette().get(value[1:])
        return value

    def color(self, value) -> Optional[QColor]:
        value = self.resolve(value)
        return None if value is None else to_color(value)

    def _stops(self, gradient: QLinearGradient, stops) -> None:
        for stop in stops or ():
            if isinstance(stop, (list, tuple)) and len(stop) == 2:
                gradient.setColorAt(max(0.0, min(1.0, float(_num(stop[0], 0)))), self.color(stop[1]) or QColor(0, 0, 0, 0))

    def brush(self, value, height: float) -> Optional[QBrush]:
        value = self.resolve(value)
        if isinstance(value, (list, tuple)):
            gradient = QLinearGradient(0, 0, 0, height)
            self._stops(gradient, value)
            return QBrush(gradient)
        return None if value is None else QBrush(to_color(value))

    def glyph(self, name: str) -> tuple:
        return _layers(_dict(self._spec.buttons.get("glyphs")).get(name))

    def button_style(self, kind: str, state: str) -> dict:
        buttons = self._spec.buttons
        out: dict = {}
        for part in (_dict(buttons.get("style")), _dict(_dict(_dict(buttons.get("kinds")).get(kind)).get("style"))):
            out.update(_dict(part.get("normal")))
            if state != "normal":
                out.update(_dict(part.get(state)))
        return out

    def paint_surface(self, p: QPainter, style: dict, shape: Callable[[float, float], QPainterPath], w: float, h: float) -> None:
        body = shape(0.0, 0.0)
        brush = self.brush(style.get("fill"), h)
        if brush is not None:
            p.fillPath(body, brush)
        glow = _dict(style.get("glow"))
        color = self.color(glow.get("color"))
        if color is not None:
            cx, cy = glow.get("center") or (0.5, 1.0)
            gradient = QRadialGradient(QPointF(w * cx, h * cy), w * float(_num(glow.get("radius"), 0.5)))
            edge = QColor(color)
            edge.setAlpha(0)
            gradient.setColorAt(0.0, color)
            gradient.setColorAt(1.0, edge)
            p.save()
            p.setClipPath(body)
            p.fillRect(QRectF(0, 0, w, h), gradient)
            p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for line in style.get("border") or ():
            line = _dict(line)
            color = self.color(line.get("color"))
            if color is not None:
                p.setPen(QPen(color, float(_num(line.get("width"), 1))))
                p.drawPath(shape(float(_num(line.get("inset"), 0.5)), float(_num(line.get("radius_delta"), 0))))

    def metrics_height(self) -> int:
        return self._metrics()[1]

    def title_glow(self, cap: int = 99) -> tuple:
        glow = _dict(self._spec.title.get("glow"))
        color = self.color(glow.get("color"))
        alpha = self.resolve(glow.get("alpha"))
        if color is not None and isinstance(alpha, (int, float)):
            color.setAlpha(max(0, min(255, int(alpha))))
        radius = min(int(_num(glow.get("radius"), 0)), cap)
        return (color, radius) if color is not None and radius > 0 else (None, 0)

    def chip_style(self, state: str) -> dict:
        chips = self._spec.chips
        base = _dict(self._spec.buttons.get("style")) if chips.get("inherit_buttons") else _CHIP_BASE_STYLE
        out: dict = {}
        for part in (base, _dict(chips.get("style"))):
            out.update(_dict(part.get("normal")))
            if state != "normal":
                out.update(_dict(part.get(state)))
        return out

    def chrome_background(self) -> QColor:
        def _opaque(color: QColor) -> QColor:
            if color.alpha() >= 250:
                out = QColor(color)
                out.setAlpha(255)
                return out
            try:
                base = to_color(palette().get("page_bg", "#202020"))
            except Exception:
                base = QColor("#202020")
            if base.alpha() < 255:
                base = QColor(18, 18, 18) if isDarkTheme() else QColor(255, 255, 255)
            a = color.alpha() / 255.0
            return QColor(
                int(round(color.red() * a + base.red() * (1 - a))),
                int(round(color.green() * a + base.green() * (1 - a))),
                int(round(color.blue() * a + base.blue() * (1 - a))),
            )
        with suppress(Exception):
            for layer in list(self._spec.layers) + list(self._spec.fallback):
                if not isinstance(layer, dict) or layer.get("type") != "fill":
                    continue

                if layer.get("target") in ("content", "client"):
                    continue
                color = self.color(layer.get("color"))
                if color is not None:
                    return _opaque(QColor(color))
        try:
            return _opaque(to_color(palette().get("page_bg", "#202020")))
        except Exception:
            return QColor("#202020")

    def chip_text_color(self, style: Optional[dict] = None) -> QColor:
        style = style if isinstance(style, dict) else self.chip_style("normal")
        color = self.color(style.get("text"))
        if color is None:
            color = self.color(self._spec.title.get("color"))
        if color is None:
            color = self.color(self._spec.nav.get("text"))
        if color is None:
            color = QColor(0, 0, 0)

        try:
            fill = self.brush(style.get("fill"), 22)
            bg = self.chrome_background()
            if fill is not None and isinstance(fill, QBrush) and fill.style() != Qt.BrushStyle.NoBrush:

                fill_color = fill.color() if hasattr(fill, "color") else None
                if isinstance(fill_color, QColor) and fill_color.isValid():
                    a = fill_color.alpha() / 255.0
                    if a >= 0.99:
                        bg = fill_color
                    elif a > 0.01:
                        bg = QColor(
                            int(round(fill_color.red() * a + bg.red() * (1 - a))),
                            int(round(fill_color.green() * a + bg.green() * (1 - a))),
                            int(round(fill_color.blue() * a + bg.blue() * (1 - a))),
                        )
            return ensure_contrast(QColor(color), bg)
        except Exception:
            return QColor(color)

    def nav_style(self) -> dict:
        nav = self._spec.nav
        hover, selected = _dict(nav.get("hover")), _dict(nav.get("selected"))
        bg = self.chrome_background()

        text = self.color(nav.get("text")) or QColor(0, 0, 0)
        selected_text = self.color(nav.get("selected_text")) or self.color(nav.get("text")) or QColor(0, 0, 0)
        icon = self.color(nav.get("icon")) or QColor(0, 0, 0)
        selected_icon = self.color(nav.get("selected_icon")) or self.color(nav.get("icon")) or QColor(0, 0, 0)
        with suppress(Exception):

            if self.is_nav_transparent():
                text = ensure_contrast(QColor(text), bg)
                selected_text = ensure_contrast(QColor(selected_text), bg)
                icon = ensure_contrast(QColor(icon), bg)
                selected_icon = ensure_contrast(QColor(selected_icon), bg)
        return {
            "radius": float(_num(nav.get("radius"), 5)),
            "text": text,
            "selected_text": selected_text,
            "icon": icon,
            "selected_icon": selected_icon,
            "indicator": self.color(nav.get("indicator")),
            "separator": self.color(nav.get("separator")),
            "hover": hover,
            "selected": selected,
        }

    def nav_glow(self) -> tuple:
        glow = _dict(_dict(self._spec.nav).get("text_glow"))
        color = self.color(glow.get("color"))
        alpha = self.resolve(glow.get("alpha"))
        if color is not None and isinstance(alpha, (int, float)):
            color.setAlpha(max(0, min(255, int(alpha))))
        radius = int(_num(glow.get("radius"), 0))
        return (color, radius) if color is not None and radius > 0 else (None, 0)

    def is_nav_transparent(self) -> bool:
        bg = (self._spec.nav_background or "").strip().lower()
        if bg in ("", "transparent", "none"):
            return True
        return False

    def resolved_nav_background(self) -> Optional[QColor]:
        if self.is_nav_transparent():
            return None
        color = self.color(self._spec.nav_background)
        if color is not None:
            return color
        try:
            return to_color(palette().get("nav_bg"))
        except Exception:
            return None

    def note_nav_item(self, route_key: str, slot_id: str, default_icon) -> None:
        if not route_key or not slot_id:
            return
        with suppress(Exception):
            self._nav_slots[str(route_key)] = (str(slot_id), default_icon)

    def themed_nav_icon(self, slot_id: str, default_icon, size: int = 20) -> QIcon:
        try:
            style = self._ensure_resolved(False).get("nav") or self.nav_style()
        except Exception:
            style = {}
        color = style.get("icon")
        if isinstance(color, QColor):
            tint = color.name(QColor.NameFormat.HexArgb) if color.alpha() < 255 else color.name()
        else:
            try:
                tint = resolve_icon_color()
            except Exception:
                tint = "#ffffff"
        key = (str(slot_id), str(tint), int(size))
        cached = _NAV_ICON_CACHE.get(key)
        if cached is not None:
            return QIcon(cached) if isinstance(cached, QIcon) else cached
        icon = themed_icon(slot_id, default_icon, size=size, color=tint)
        with suppress(Exception):
            _NAV_ICON_CACHE[key] = icon
            if len(_NAV_ICON_CACHE) > 256:
                _NAV_ICON_CACHE.pop(next(iter(_NAV_ICON_CACHE)))
        return icon

    def refresh_nav(self) -> None:
        nav_iface = getattr(self._window, "navigationInterface", None)
        panel = getattr(nav_iface, "panel", None)
        if panel is None:
            return
        try:
            res = self._ensure_resolved(False)
            style = res.get("nav") or self.nav_style()
        except Exception:
            try:
                style = self.nav_style()
            except Exception:
                style = {}
        text = style.get("text")
        indicator = style.get("indicator")
        try:
            text_rgba = text.rgba() if isinstance(text, QColor) else None
            ind_rgba = indicator.rgba() if isinstance(indicator, QColor) else None
        except Exception:
            text_rgba = ind_rgba = None
        try:
            widgets = panel.findChildren(NavigationWidget)
        except Exception:
            widgets = []

        route_of: dict[int, str] = {}
        with suppress(Exception):
            items = getattr(panel, "items", {}) or {}
            for key, item in items.items():
                w = getattr(item, "widget", None)
                if w is not None:
                    route_of[id(w)] = key
        for widget in widgets:
            try:
                wid = widget
                last = self._nav_last.get(wid)
                route = route_of.get(id(widget))
                slot = self._nav_slots.get(route) if route else None
                slot_key = slot[0] if slot else None
                if last == (text_rgba, ind_rgba, slot_key):
                    continue
                self._nav_last[wid] = (text_rgba, ind_rgba, slot_key)
                if isinstance(text, QColor) and hasattr(widget, "setTextColor"):
                    widget.setTextColor(text, text)
                elif isinstance(text, QColor):
                    if hasattr(widget, "lightTextColor"):
                        widget.lightTextColor = QColor(text)
                    if hasattr(widget, "darkTextColor"):
                        widget.darkTextColor = QColor(text)
                if indicator is not None and hasattr(widget, "setIndicatorColor"):
                    with suppress(Exception):
                        widget.setIndicatorColor(indicator, indicator)
                if slot is not None:
                    slot_id, default_icon = slot
                    with suppress(Exception):
                        widget.setIcon(self.themed_nav_icon(slot_id, default_icon))
            except Exception:
                continue
            with suppress(Exception):
                widget.update()
        btn = getattr(panel, "menuButton", None)
        if btn is not None:
            with suppress(Exception):
                btn.setIcon(self.themed_nav_icon("nav.toggle", FluentIcon.MENU))
            with suppress(Exception):
                if isinstance(text, QColor) and hasattr(btn, "setTextColor"):
                    btn.setTextColor(text, text)
            with suppress(Exception):
                btn.update()

    def text_color(self) -> str:
        color = self.color(self._spec.title.get("color")) or QColor(0, 0, 0)
        if color.alpha() == 255:
            return color.name()
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"

    def window_background(self, default: QColor) -> QColor:
        if _transparent_window_enabled():
            return QColor(0, 0, 0, 0)
        out = QColor(default)
        if out.alpha() < 255:
            try:
                base = to_color(palette().get("page_bg", "#202020"))
            except Exception:
                base = QColor("#202020")
            out = QColor(base.red(), base.green(), base.blue())
        out.setAlpha(255)
        return out

    def _maximized(self) -> bool:
        return self._window.isMaximized() or self._window.isFullScreen()

    def _metrics(self) -> tuple[int, int]:
        spec = self._spec
        if self._maximized():
            return spec.frame_maximized, spec.height_maximized or spec.height
        return spec.frame, spec.height

    def client_rect(self) -> QRect:
        window = self._window
        if self._spec.mode != "frame":
            return window.rect()
        frame, height = self._metrics()
        return QRect(frame, height, max(0, window.width() - 2 * frame), max(0, window.height() - height - frame))

    def apply(self) -> None:
        window = self._window
        spec = self.spec()
        pal = palette()
        try:
            pal_fp = _palette_fingerprint(pal)
        except Exception:
            pal_fp = 0
        spec_id = (id(spec), self._active, pal_fp)
        spec_changed = spec_id != self._last_spec_id
        self._spec = spec
        self._last_spec_id = spec_id
        kind = theme_backdrop(active_preset_id()) or "none"


        self._transparent = _transparent_window_enabled()
        self._kind = kind if DwmBackdrop.supports(kind) else "none"
        try:
            translucent = window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        except Exception:
            translucent = True
        self._native = IS_WIN and translucent and not spec.force_fallback and self._kind != "none"
        try:
            self._radius = {"square": 0.0, "small": 4.0}.get(spec.corner or "", 8.0) if _build() >= 22000 else 0.0
        except Exception:
            self._radius = 0.0
        try:
            self._active = window.isActiveWindow() or not window.isVisible()
        except Exception:
            self._active = True



        self._ensure_resolved(spec_changed)

        with suppress(Exception):
            window._isMicaEnabled = False
            window.setBackgroundColor(window._normalBackgroundColor())

        if IS_WIN:
            with suppress(Exception):
                tint = to_color(spec.backdrop_tint) if spec.backdrop_tint else to_color(pal.get("page_bg", "#202020"))
                if not spec.backdrop_tint:
                    tint.setAlpha(153)
                dark = isDarkTheme() if spec.dark is None else spec.dark
                frame = theme_win7_frame(active_preset_id())
                tint_key = tint.rgba() if self._kind == "acrylic" else 0
                key = (self._kind, dark, tint_key, spec.corner, frame)
                if key != self._backdrop_last:
                    last = self._backdrop_last
                    # Dark-mode and tint-only changes need no frame
                    # invalidation: re-applying SetWindowPos/RedrawWindow here
                    # makes DWM composite an extra frame, which shows up as a
                    # duplicated window flash on dark <-> light switches.
                    structural = not last or (last[0], last[3], last[4]) != (key[0], key[3], key[4])
                    self._backdrop.apply(int(window.winId()), self._kind, dark, tint=tint, corner=spec.corner, frame=frame)
                    if structural:
                        self._backdrop.refresh(int(window.winId()))
                    self._backdrop_last = key



        relayout = spec is not self._laid_spec
        with suppress(Exception):
            window.titleBar.configure(self) if relayout else window.titleBar.restyle()
        self._style_nav()
        self.refresh_nav()
        if relayout:
            self._laid_spec = spec
            self._relayout()
            with suppress(Exception):
                window.titleBar.syncMaxState(self._maximized())
        self._refresh()
        with suppress(Exception):
            self.applied.emit()


    def _ensure_resolved(self, force: bool = False) -> dict:
        key = self._last_spec_id
        if not force and self._res and self._res_key == key:
            return self._res
        spec = self._spec
        try:
            pal = palette()
        except Exception:
            pal = {}
        res: dict = {"palette": dict(pal) if isinstance(pal, dict) else {}}

        try:
            res["nav"] = self._build_nav_resolved()
        except Exception:
            res["nav"] = {}

        try:
            res["chips"] = {s: self.chip_style(s) for s in ("normal", "hover", "pressed")}
            res["chip_text"] = {s: self.chip_text_color(res["chips"][s]) for s in res["chips"]}
        except Exception:
            res["chips"] = {}
            res["chip_text"] = {}

        with suppress(Exception):
            from PySide6.QtGui import QFont as _QF
            from PySide6.QtWidgets import QApplication as _QA
            font = _QF(_QA.font())
            title = _dict(spec.title)
            fam = _opt_str(title.get("family"))
            if fam:
                font.setFamily(fam)
            font.setPixelSize(max(6, int(_num(title.get("size"), 14))))
            font.setBold(bool(title.get("bold", False)))
            res["title_font"] = font
            res["title_color"] = self.color(title.get("color")) or QColor(0, 0, 0)
            glow = _dict(title.get("glow"))
            gc = self.color(glow.get("color"))
            ga = self.resolve(glow.get("alpha"))
            if gc is not None and isinstance(ga, (int, float)):
                gc.setAlpha(max(0, min(255, int(ga))))
            res["title_glow"] = gc
            res["title_glow_radius"] = int(_num(glow.get("radius"), 0))
            res["title_pad"] = int(_num(title.get("padding"), 4))


        with suppress(Exception):
            nav = res.get("nav", {})
            sep = nav.get("separator")
            res["nav_separator"] = sep if isinstance(sep, QColor) else None
            res["nav_glow_holder"] = self.nav_glow()

        try:
            res["sidebar_split"] = spec.chrome == "split"
            side = _dict(getattr(spec, "sidebar", {}))
            res["sidebar_fill"] = self.color(side.get("fill")) if side.get("fill") else None
            res["sidebar_layers"] = _layers(side.get("layers"))
        except Exception:
            res["sidebar_split"] = False
            res["sidebar_fill"] = None
            res["sidebar_layers"] = ()
        self._res = res
        self._res_key = key

        return res

    def _build_nav_resolved(self) -> dict:

        return self.nav_style()

    def _style_nav(self) -> None:
        nav_iface = getattr(self._window, "navigationInterface", None)
        panel = getattr(nav_iface, "panel", None)
        if panel is None:
            return
        try:
            sheet = panel.styleSheet() or ""
        except Exception:
            sheet = ""
        if self._nav_sheet and self._nav_sheet in sheet:
            sheet = sheet.replace(self._nav_sheet, "")
        solid = self.resolved_nav_background()
        if solid is None:
            self._nav_sheet = (
                "NavigationPanel { background: transparent; border: none; }"
                "NavigationPanel QScrollArea, NavigationPanel #scrollWidget {"
                " background: transparent; border: none; }"
            )
        else:
            try:
                bg_text = solid.name(QColor.NameFormat.HexArgb) if solid.alpha() < 255 else solid.name()
            except Exception:
                bg_text = palette().get("nav_bg", "#202020")
            self._nav_sheet = (
                f"NavigationPanel {{ background: {bg_text}; border: none; }}"
                f"NavigationPanel QScrollArea, NavigationPanel #scrollWidget {{"
                f" background: {bg_text}; border: none; }}"
            )
        with suppress(Exception):
            if panel.styleSheet() != sheet + self._nav_sheet:
                panel.setStyleSheet(sheet + self._nav_sheet)

        for obj in (getattr(panel, "scrollArea", None), getattr(panel, "scrollWidget", None)):
            if obj is None:
                continue
            with suppress(Exception):
                obj.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                extra = "background: transparent; border: none;" if solid is None else f"background: {bg_text}; border: none;"
                base_ss = obj.styleSheet() or ""
                if getattr(obj, "_chrome_extra", None) and obj._chrome_extra in base_ss:
                    base_ss = base_ss.replace(obj._chrome_extra, "")
                obj._chrome_extra = extra
                if obj.styleSheet() != base_ss + extra:
                    obj.setStyleSheet(base_ss + extra)



        stacked = getattr(self._window, "stackedWidget", None)
        if stacked is not None:
            with suppress(Exception):
                base = stacked.styleSheet() or ""
                if self._stack_sheet and self._stack_sheet in base:
                    base = base.replace(self._stack_sheet, "")
                if self._spec.mode == "frame":
                    self._stack_sheet = (
                        "StackedWidget { background: transparent; border: none;"
                        " border-top-left-radius: 0px; }"
                        "StackedWidget[isTransparent=true] { background: transparent; border: none; }"
                    )
                else:
                    self._stack_sheet = ""
                if self._stack_sheet:
                    stacked.setStyleSheet(base + self._stack_sheet)
                elif base != stacked.styleSheet():
                    stacked.setStyleSheet(base)

    def _refresh(self) -> None:
        window = self._window


        with suppress(Exception):
            window.update()
            window.titleBar.update()

    def _relayout(self) -> None:
        window = self._window
        titlebar = window.titleBar
        spec = self._spec
        frame, height = self._metrics()
        left, right = spec.padding_maximized if self._maximized() else spec.padding
        titlebar.setFixedHeight(height)
        titlebar.hBoxLayout.setContentsMargins(left, 0, right, 0)
        if spec.mode == "frame":
            window.hBoxLayout.setContentsMargins(frame, height, frame, frame)
            window.widgetLayout.setContentsMargins(0, 0, 0, 0)
        else:
            window.hBoxLayout.setContentsMargins(0, 0, 0, 0)
            window.widgetLayout.setContentsMargins(0, height, 0, 0)
        window.hBoxLayout.activate()
        self.sync_geometry()

    def sync_geometry(self) -> None:
        window = self._window
        titlebar = getattr(window, "titleBar", None)
        if titlebar is None:
            return
        left = self._spec.inset_left
        titlebar.move(left, 0)
        titlebar.resize(max(0, window.width() - left), titlebar.height())
        titlebar.raise_()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._window:
            kind = event.type()
            if kind == QEvent.Type.ActivationChange:
                active = self._window.isActiveWindow()
                if active != self._active:
                    self._active = active
                    if self._last_spec_id:
                        self._last_spec_id = (self._last_spec_id[0], active, self._last_spec_id[2])
                    self._window.titleBar.restyle()
                    self.refresh_nav()
                    self._window.update()
            elif kind == QEvent.Type.WindowStateChange:
                QTimer.singleShot(0, self._on_state)
            elif kind == QEvent.Type.Show and not self._shown:
                self._shown = True
                QTimer.singleShot(0, self.apply)
        return False

    def _on_state(self) -> None:
        self._relayout()
        self._window.titleBar.syncMaxState(self._maximized())
        self._window.update()

    def paint_layers(self, p: QPainter, rect: QRectF, bar: float, layers, ring=None, outer=None, client=None, content=None) -> None:
        clips = {"chrome": ring, "window": outer}
        maximized = self._maximized()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for layer in layers:
            kind = layer.get("type")
            target = layer.get("target", "chrome")
            geom = {"client": client, "content": content}.get(target, rect)
            if geom is None or (layer.get("skip_maximized") and maximized):
                continue
            color = None if kind == "gradient" else self.color(layer.get("color"))
            if kind != "gradient" and color is None:
                continue
            clip = clips.get(target)
            p.save()
            if clip is not None and kind != "border":
                p.setClipPath(clip)
            if kind == "fill":
                p.fillRect(geom, color)
            elif kind == "gradient":
                horizontal = layer.get("direction") == "horizontal"
                span = float(_num(layer.get("span"), 0))
                area = QRectF(geom.x(), geom.y(), geom.width(), bar * span if span and not horizontal else geom.height())
                gradient = QLinearGradient(area.topLeft(), area.topRight() if horizontal else area.bottomLeft())
                self._stops(gradient, layer.get("stops"))
                p.fillRect(area, gradient)
            elif kind == "band":
                x = geom.x() + geom.width() * float(_num(layer.get("x"), 0))
                width = geom.width() * float(_num(layer.get("w"), 0.2))
                skew = bar * float(_num(layer.get("skew"), 0))
                top, bottom = geom.y(), geom.y() + bar + float(_num(layer.get("extra"), 0))
                fade = QLinearGradient(x, 0, x + width + skew, 0)
                edge = QColor(color)
                edge.setAlpha(0)
                fade.setColorAt(0.0, edge)
                fade.setColorAt(0.5, color)
                fade.setColorAt(1.0, edge)
                path = QPainterPath()
                path.addPolygon(QPolygonF([QPointF(x + skew, top), QPointF(x + width + skew, top), QPointF(x + width, bottom), QPointF(x, bottom)]))
                path.closeSubpath()
                p.fillPath(path, fade)
            elif kind == "border":
                inset = float(_num(layer.get("inset"), 0.5))
                radius = 0.0 if target in ("client", "content") else max((0.0 if maximized else self._radius) + float(_num(layer.get("radius_delta"), 0)), 0.0)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(color, float(_num(layer.get("width"), 1))))
                p.drawRoundedRect(geom.adjusted(inset, inset, -inset, -inset), radius, radius)
            p.restore()

    def sidebar_rect(self) -> Optional[QRect]:
        try:
            nav = getattr(self._window, "navigationInterface", None)
            if nav is None:
                return None
            geo = nav.geometry()


            return QRect(geo.x(), geo.y(), geo.width(), geo.height())
        except Exception:
            return None

    def paint_frame(self, window) -> bool:
        spec = self._spec
        if spec.mode != "frame":
            return False
        size = window.size()
        w, h = max(1, size.width()), max(1, size.height())
        maximized = self._maximized()
        try:
            pal_fp = self._res_key
        except Exception:
            pal_fp = ()
        try:
            side_w = int(getattr(getattr(window, "navigationInterface", None), "width", lambda: 0)())
        except Exception:
            side_w = 0
        key = (w, h, pal_fp, bool(maximized), bool(self._native), float(self._radius),
               str(spec.hole), int(side_w), bool(getattr(self, "_transparent", False)))
        cached = self._frame_pixmap
        if cached is not None and self._frame_key == key and not cached.isNull():
            p = QPainter(window)
            p.drawPixmap(0, 0, cached)
            p.end()
            return True
        if cached is not None and not cached.isNull() and getattr(window, '_resize_in_progress', False):
            p = QPainter(window)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawPixmap(window.rect(), cached)
            p.end()
            return True




        pix = QPixmap(w, h)
        if getattr(self, "_transparent", False):
            pix.fill(Qt.GlobalColor.transparent)
        else:
            try:
                pix.fill(to_color(palette().get("page_bg", "#202020")))
            except Exception:
                pix.fill(QColor("#202020"))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, w, h)
        radius = 0.0 if maximized else self._radius
        client = QRectF(self.client_rect())
        try:
            stacked = getattr(window, "stackedWidget", None)
            content = QRectF(stacked.geometry()) if stacked is not None else client

            if stacked is not None:
                with suppress(Exception):
                    top_left = stacked.mapTo(window, QPoint(0, 0))
                    content = QRectF(top_left.x(), top_left.y(), stacked.width(), stacked.height())
        except Exception:
            content = client
        hole = content if spec.hole == "content" else client
        outer = QPainterPath()
        outer.addRoundedRect(rect, radius, radius)
        cut = QPainterPath()
        cut.addRect(hole)
        ring = outer.subtracted(cut)
        bar = float(self._metrics()[1])
        if not self._native:
            self.paint_layers(p, rect, bar, spec.fallback, ring, outer, client, content)
        self.paint_layers(p, rect, bar, spec.layers, ring, outer, client, content)

        with suppress(Exception):
            res = self._res or {}
            if res.get("sidebar_split"):
                fill = res.get("sidebar_fill")
                side_rect = self.sidebar_rect()
                if side_rect is not None and fill is not None and isinstance(fill, QColor):
                    p.save()
                    p.setClipPath(outer)
                    p.fillRect(QRectF(side_rect), fill)
                    p.restore()
                layers = res.get("sidebar_layers") or ()
                if layers and side_rect is not None:
                    p.save()
                    p.setClipPath(outer)

                    self.paint_layers(p, QRectF(side_rect), bar, layers, None, None, None, None)
                    p.restore()
        p.end()
        self._frame_pixmap = pix
        self._frame_key = key

        pw = QPainter(window)
        pw.drawPixmap(0, 0, pix)
        pw.end()
        return True



_TM_MARK = "/*tm*/"
_tm_seq = itertools.count()
_tm_touched: "weakref.WeakSet" = weakref.WeakSet()


def _class_chain(obj):
    mo = obj.metaObject()
    while mo is not None:
        yield mo.className()
        mo = mo.superClass()


def _match(w, sel: str) -> bool:
    parts = sel.split()
    if not parts or len(parts) > 2:
        return False

    def one(p, o):
        return o.objectName() == p[1:] if p.startswith("#") else p in _class_chain(o)
    if not one(parts[-1], w):
        return False
    if len(parts) == 1:
        return True
    a = w.parent()
    while a is not None:
        if one(parts[0], a):
            return True
        a = a.parent()
    return False


def _spec_icon(spec, size: int = 20) -> QIcon:
    if not spec:
        return QIcon()
    if spec.get("type") == "image":
        return image_icon(spec["value"], size, bool(spec.get("crop", True)))
    f = _fluent_icon_by_name(spec.get("value", ""))
    return f.icon(color=QColor(manager.palette()["icon_color"])) if f is not None else QIcon()


class ScrollingTextDelegate(QStyledItemDelegate):
    """Scroll an overflowing cell's text while the pointer rests on it.

    The column keeps its normal width and ellipsis, so neighbouring columns do
    not get pushed off-screen. After a short hover delay the full text slides
    left until the end is visible, then loops.
    """

    def __init__(self, view, column: int = 0, padding: int = 16):
        super().__init__(view)
        self._view = view
        self._column = column
        self._padding = padding
        self._index = None
        self._offset = 0
        self._active = False
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(600)
        self._delay.timeout.connect(self._begin)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.setItemDelegateForColumn(column, self)

    def _alive(self) -> bool:
        return shiboken6.isValid(self._view)

    def eventFilter(self, obj, event):
        if not self._alive():
            return False
        if obj is self._view.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                index = self._view.indexAt(event.position().toPoint())
                if index.isValid() and index.column() == self._column:
                    if index != self._index:
                        self._stop()
                        self._index = index
                        self._delay.start()
                elif self._index is not None:
                    self._stop()
            elif kind in (QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.Wheel):
                self._stop()
        return super().eventFilter(obj, event)

    def _stop(self) -> None:
        self._delay.stop()
        self._timer.stop()
        self._active = False
        index, self._index = self._index, None
        self._offset = 0
        if index is not None and index.isValid() and self._alive():
            self._view.viewport().update(self._view.visualRect(index))

    def _begin(self) -> None:
        if not self._alive():
            return
        if self._index is None or not self._index.isValid():
            return
        self._active = True
        self._offset = 0
        self._timer.start()
        self._view.viewport().update(self._view.visualRect(self._index))

    def _tick(self) -> None:
        if not self._alive():
            self._timer.stop()
            self._delay.stop()
            return
        index = self._index
        if index is None or not index.isValid():
            self._stop()
            return
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = self._text_rect(index, self._view.visualRect(index))
        if rect.width() <= 0:
            self._stop()
            return
        font = index.data(Qt.ItemDataRole.FontRole) or self._view.font()
        overflow = QFontMetrics(font).horizontalAdvance(text) - rect.width()
        if overflow <= 0:
            self._stop()
            return
        self._offset += 2
        if self._offset > overflow + 32:
            self._timer.stop()
            self._delay.start()
        else:
            self._view.viewport().update(self._view.visualRect(index))

    def _text_rect(self, index, rect: QRect) -> QRect:
        rect = QRect(rect)
        left = self._padding
        if index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            width = QApplication.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth)
            left += width + 4
        rect.setLeft(rect.left() + left)
        rect.setRight(rect.right() - 6)
        return rect

    def paint(self, painter, option, index) -> None:
        if index.column() != self._column or index != self._index or not self._active:
            super().paint(painter, option, index)
            return
        text = option.text or str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = self._text_rect(index, option.rect)
        if rect.width() <= 0 or option.fontMetrics.horizontalAdvance(text) <= rect.width():
            super().paint(painter, option, index)
            return
        original = option.text
        option.text = ""
        super().paint(painter, option, index)
        option.text = original
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(option.font)
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))
        y = rect.center().y() + (option.fontMetrics.ascent() - option.fontMetrics.descent()) // 2
        painter.drawText(rect.left() - self._offset, y, text)
        painter.restore()


def apply_widget_rules() -> None:
    meta = manager._meta
    rules, icons = (meta.rules, meta.widget_icons) if meta is not None else ((), ())
    app = QApplication.instance()
    if app is None or not (rules or icons or _tm_touched):
        return
    targets = app.allWidgets() if (rules or icons) else list(_tm_touched)
    for w in targets:
        if not shiboken6.isValid(w):
            continue
        ss = w.styleSheet()
        base = ss.split(_TM_MARK)[0] if _TM_MARK in ss else ss
        hits = [r for r in rules if _match(w, r["selector"])]
        new = base
        if hits:
            tag = w.property("tmRule") or f"w{next(_tm_seq)}"
            w.setProperty("tmRule", tag)
            new = base + _TM_MARK + "\n" + "\n".join(f'*[tmRule="{tag}"]{r["state"]} {{ {r["css"]} }}' for r in hits)
            _tm_touched.add(w)
        if new != ss:
            w.setStyleSheet(new)
        if icons and hasattr(w, "setIcon"):
            for ic in icons:
                if _match(w, ic["match"]):
                    with suppress(Exception):
                        w.setIcon(_spec_icon(ic["spec"]))



_PROPS = (("Text color", "color", "color"), ("Background", "background-color", "color"),
          ("Border color", "border-color", "color"), ("Border width", "border-width", "px"),
          ("Corner radius", "border-radius", "px"), ("Font family", "font-family", "font"),
          ("Font size", "font-size", "px"), ("Font weight", "font-weight", "text"),
          ("Font style", "font-style", "text"), ("Padding", "padding", "px"), ("Margin", "margin", "px"),
          ("Min width", "min-width", "px"), ("Min height", "min-height", "px"),
          ("Max width", "max-width", "px"), ("Max height", "max-height", "px"))
_STATES = ("", ":hover", ":pressed", ":checked", ":focus", ":disabled", ":selected")


def _css_to_dict(css: str) -> dict:
    out = {}
    for part in css.replace("\n", ";").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-") or "my-theme"


def _ask_color(initial: str, parent) -> Optional[str]:
    p = _parse_rgba(initial or "") or (255, 255, 255, 1.0)
    c = QColorDialog.getColor(QColor(p[0], p[1], p[2], round(p[3] * 255)), parent, "Pick colour",
                              QColorDialog.ColorDialogOption.ShowAlphaChannel)
    return _fmt((c.red(), c.green(), c.blue(), c.alphaF())) if c.isValid() else None


class _IconPicker(QDialog):

    def __init__(self, parent=None, allow_reset: bool = True):
        super().__init__(parent)
        from qfluentwidgets import CaptionLabel, ListWidget, PushButton, SearchLineEdit
        self.setWindowTitle("Choose icon")
        self.resize(660, 560)
        self.spec: Optional[dict] = None
        self.reset = False
        lay = QVBoxLayout(self)
        self._search = SearchLineEdit()
        self._search.setPlaceholderText("Search built-in Fluent icons…")
        self._count = CaptionLabel("")
        self._grid = ListWidget()
        self._grid.setViewMode(QListView.ViewMode.IconMode)
        self._grid.setMovement(QListView.Movement.Static)
        self._grid.setResizeMode(QListView.ResizeMode.Adjust)
        self._grid.setIconSize(QSize(32, 32))
        self._grid.setGridSize(QSize(128, 100))
        self._grid.setSpacing(2)
        self._grid.setWordWrap(True)
        self._grid.setUniformItemSizes(True)
        self._grid.setTextElideMode(Qt.TextElideMode.ElideRight)
        row = QHBoxLayout()
        img = PushButton("Use a photo / image file…")
        img.clicked.connect(self._file)
        row.addWidget(img)
        if allow_reset:
            rst = PushButton("Reset to default")
            rst.clicked.connect(self._reset)
            row.addWidget(rst)
        lay.addWidget(self._search)
        lay.addWidget(self._count)
        lay.addWidget(self._grid, 1)
        lay.addLayout(row)
        self._names = [m.name for m in FluentIcon]
        self._search.textChanged.connect(self._fill)
        self._grid.itemDoubleClicked.connect(self._pick)
        self._fill("")

    def _fill(self, q: str = "") -> None:
        self._grid.clear()
        q = q.strip().lower().replace(" ", "_")
        col, n = QColor(manager.palette()["icon_color"]), 0
        for name in self._names:
            if q and q not in name.lower():
                continue
            try:
                icon = getattr(FluentIcon, name).icon(color=col)
            except Exception:
                continue
            nice = name.replace("_", " ").title()
            item = QListWidgetItem(icon, nice)
            item.setToolTip(nice)
            item.setSizeHint(QSize(124, 96))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._grid.addItem(item)
            n += 1
            if n >= 300:
                break
        self._count.setText(f"{n} icons shown" + (" — refine the search to see more" if n >= 300 else "") + " · double-click to choose")

    def _pick(self, item) -> None:
        self.spec = {"type": "fluent", "value": item.data(Qt.ItemDataRole.UserRole)}
        self.accept()

    def _file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.svg *.ico);;All files (*)")
        if path:
            self.spec = {"type": "image", "value": path, "crop": True}
            self.accept()

    def _reset(self) -> None:
        self.reset = True
        self.accept()


class _PickOverlay(QWidget):
    picked = Signal(object)
    cancelled = Signal()

    def __init__(self, top, maker, flash=None):
        super().__init__(top)
        self._top, self._maker, self._flash, self._hover, self._last = top, maker, flash, flash, 0.0
        if flash is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        else:
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setCursor(Qt.CursorShape.CrossCursor)
        top.installEventFilter(self)
        self.setGeometry(top.rect())
        self.raise_()
        self.show()
        if flash is None:
            self.setFocus()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._top and ev.type() == QEvent.Type.Resize:
            self.setGeometry(self._top.rect())
        return False

    def _visible_at(self, w, pos) -> bool:
        p = w
        while p is not self._top and p is not None:
            par = p.parent()
            if not isinstance(par, QWidget):
                break
            if not par.rect().contains(par.mapFrom(self._top, pos)):
                return False
            p = par
        return True

    def target_at(self, pos):
        cands = []
        for w in self._top.findChildren(QWidget):
            if w is self or w.isWindow() or not w.isVisible() or self._maker._mine(w):
                continue
            r = QRect(w.mapTo(self._top, QPoint(0, 0)), w.size())
            if r.contains(pos):
                cands.append((r.width() * r.height(), w))
        cands.sort(key=lambda c: c[0])
        return next((w for _, w in cands if self._visible_at(w, pos)), self._top)

    def mouseMoveEvent(self, e) -> None:
        now = time.monotonic()
        if now - self._last < 0.03:
            return
        self._last = now
        t = self.target_at(e.position().toPoint())
        if t is not self._hover:
            self._hover = t
            self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.target_at(e.position().toPoint()))
        else:
            self.cancelled.emit()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        if self._flash is None:
            p.fillRect(self.rect(), QColor(0, 0, 0, 28))
        w = self._hover
        if w is None or not shiboken6.isValid(w):
            return
        r = QRect(w.mapTo(self._top, QPoint(0, 0)), w.size())
        pink = QColor(255, 59, 130)
        p.fillRect(r, QColor(255, 59, 130, 55))
        p.setPen(QPen(pink, 2))
        p.drawRect(r.adjusted(1, 1, -1, -1))
        label = w.metaObject().className() + (f"  #{w.objectName()}" if w.objectName() else "")
        tw = p.fontMetrics().horizontalAdvance(label) + 12
        tag = QRect(r.left(), max(0, r.top() - 20), tw, 20)
        p.fillRect(tag, pink)
        p.setPen(QColor(255, 255, 255))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, label)


_QUICK = (("Pill", {"border-radius": "999px"}), ("Rounded", {"border-radius": "12px"}), ("Square", {"border-radius": "0px"}),
          ("Transparent", {"background-color": "transparent", "border-color": "transparent"}),
          ("Glass", {"background-color": "rgba(255, 255, 255, 0.08)", "border-width": "1px", "border-style": "solid",
                     "border-color": "rgba(255, 255, 255, 0.18)"}),
          ("Bold", {"font-weight": "700"}))
_GROUPS = (("Accent", ("accent",)),
           ("Surfaces", ("page_bg", "card_", "section_", "poster_", "list_bg", "surface_", "hover_tint", "inactive_dot", "scrollbar_")),
           ("Text", ("primary_text", "muted", "faint_text", "body_text", "detail_", "icon_color")),
           ("Buttons & inputs", ("btn_", "input_")), ("Navigation", ("nav_",)), ("Dialogs", ("dialog_",)), ("States", ("state_",)))
_PROP_GROUPS = (
    ("Colour", (("Text", "color", "color"), ("Background", "background-color", "color"), ("Border", "border-color", "color"))),
    ("Shape", (("Border width", "border-width", "px"), ("Corner radius", "border-radius", "px"),
               ("Padding", "padding", "px"), ("Margin", "margin", "px"))),
    ("Text", (("Font family", "font-family", "font"), ("Font size", "font-size", "px"),
              ("Weight", "font-weight", "text"), ("Style", "font-style", "text"))),
    ("Size", (("Min width", "min-width", "px"), ("Min height", "min-height", "px"),
              ("Max width", "max-width", "px"), ("Max height", "max-height", "px"))))
_TABS = (("element", "Element"), ("colours", "Colours"), ("icons", "Icons"), ("backdrop", "Backdrop"),
         ("titlebar", "Title bar"), ("fonts", "Fonts"), ("sounds", "Sounds"), ("code", "Code"))




_EXAMPLE_SHADER = """#version 440

// Background shaders paint straight into the window. Available uniforms:
//   time, deltaTime        seconds since load / since last frame
//   resolution             window size in pixels
//   mousePosition          cursor position in pixels
//   mouseVelocity          2d cursor movement
//   clickPosition, clickTime
//   windowPosition, windowVelocity, windowMovement, movementSpeed
//   scrollPosition         page scroll, accentColor  the theme/picker accent (rgba)
//   pageIndex, tiltAngle, tiltVelocity
layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float time;
    float deltaTime;
    vec2 resolution;
    vec2 mousePosition;
    vec2 mouseVelocity;
    vec2 clickPosition;
    float clickTime;
    vec2 windowPosition;
    vec2 windowVelocity;
    float windowMovement;
    float movementSpeed;
    float scrollPosition;
    vec4 accentColor;
    float pageIndex;
    float tiltAngle;
    float tiltVelocity;
};

void main() {
    vec2 uv = qt_TexCoord0;
    vec2 pixel = uv * resolution;

    // Soft diagonal wash from near-black to the theme accent.
    vec3 base = mix(vec3(0.02, 0.02, 0.03), accentColor.rgb * 0.55, uv.y);

    // Slowly drifting ripple bands.
    float bands = sin((uv.x * 6.0 + uv.y * 2.0) - time * 0.6) * 0.5 + 0.5;
    vec3 color = base + bands * accentColor.rgb * 0.08;

    // Cursor glow.
    float glow = exp(-distance(pixel, mousePosition) / max(resolution.y, 1.0) * 5.0);
    color += glow * accentColor.rgb * 0.12;

    fragColor = vec4(color, qt_Opacity);
}
"""


def _group_of(role: str) -> str:
    for name, pre in _GROUPS:
        if any(role == p or role.startswith(p) for p in pre):
            return name
    return "Other"


def _valid_color(v: str) -> bool:
    return bool(_parse_rgba(v)) or QColor(v).isValid()


def _valid_expr(v: str) -> bool:
    return v.startswith("@") or bool(_FN_RE.fullmatch(v)) or _valid_color(v)


class _ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Pick a colour")
        self._value = ""
        self.clicked.connect(self._pick)
        self.setValue("")

    def setValue(self, v: str) -> None:
        self._value = v or ""
        col = self._value if self._value and _valid_color(self._value) else "transparent"
        self.setStyleSheet(f"QPushButton {{ background: {col}; border: 1px solid rgba(128,128,128,.65); border-radius: 6px; }}"
                           "QPushButton:hover { border: 2px solid rgba(128,128,128,.95); }")

    def _pick(self) -> None:
        c = _ask_color(self._value, self.window())
        if c:
            self.setValue(c)
            self.changed.emit(c)


class _PropRow(QWidget):
    edited = Signal()

    def __init__(self, label: str, kind: str = "text", label_w: int = 104, placeholder: str = "inherit", parent=None):
        super().__init__(parent)
        from qfluentwidgets import BodyLabel, LineEdit, TransparentToolButton
        self.kind = kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.label = BodyLabel(label)
        self.label.setFixedWidth(label_w)
        self.line = LineEdit()
        self.line.setClearButtonEnabled(True)
        self.line.setPlaceholderText(placeholder)
        lay.addWidget(self.label)
        lay.addWidget(self.line, 1)
        self.swatch = None
        if kind == "color":
            self.swatch = _ColorButton()
            self.swatch.changed.connect(self.line.setText)
            lay.addWidget(self.swatch)
        elif kind in ("font", "file"):
            b = TransparentToolButton(FluentIcon.FONT if kind == "font" else FluentIcon.FOLDER)
            b.setToolTip("Choose font" if kind == "font" else "Choose file")
            b.clicked.connect(self._choose)
            lay.addWidget(b)
        self.line.textChanged.connect(self._on_text)

    def _on_text(self, text: str) -> None:
        if self.swatch is not None and text and _valid_color(text):
            self.swatch.setValue(text)
        elif self.swatch is not None and not text:
            self.swatch.setValue("")
        self.edited.emit()

    def _choose(self) -> None:
        if self.kind == "font":
            fam, ok = QInputDialog.getItem(self, "Font", "Family", QFontDatabase.families(), 0, True)
            if ok:
                self.line.setText(f'"{fam}"')
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose file", "", "All files (*)")
            if path:
                self.line.setText(path)

    def text(self) -> str:
        return self.line.text().strip()

    def setText(self, v: str) -> None:
        self.line.setText(v)

    def hint(self, text: str, swatch: Optional[str] = None) -> None:
        self.line.setPlaceholderText(text)
        if self.swatch is not None and not self.line.text():
            self.swatch.setValue(swatch or "")


def _card(title: str, parent=None):
    from qfluentwidgets import CaptionLabel, CardWidget
    card = CardWidget(parent)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(6)
    if title:
        t = CaptionLabel(title.upper())
        t.setStyleSheet("font-weight: 600; letter-spacing: 1px;")
        lay.addWidget(t)
    return card, lay


def _scrolled(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
    sa.setWidget(inner)
    return sa


class ThemeMaker(QWidget):
    _instance: Optional["ThemeMaker"] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setObjectName("themeMaker")
        self.setWindowTitle("Theme Maker")
        self.resize(520, 800)
        self._restore = manager.active_preset_id()
        self._saved = self._loading = self._picking = self._compare = False
        self._overlays: list = []
        self._flash_ov = None
        self._slot: Optional[str] = None
        self.workdir = os.path.join(tempfile.gettempdir(), "pirachest_theme_draft")
        self._draft_path = os.path.join(_themes_dir(), "_draft.json")
        self.target = None
        self._hier: list = []
        self._clip: dict = {}
        self._dirty = {"colours", "code"}
        self._pal_rows: dict = {}
        self._pal_heads: dict = {}
        self._undo: list = []
        self._redo: list = []
        self._full = QTimer(self)
        self._full.setSingleShot(True)
        self._full.setInterval(120)
        self._full.timeout.connect(self._preview_full)
        self._light = QTimer(self)
        self._light.setSingleShot(True)
        self._light.setInterval(25)
        self._light.timeout.connect(self._preview_light)
        self._draft_save = _Debounced(self._write_draft, 800)
        self._init_data()
        self._build()
        for seq, fn in (("Ctrl+Z", self._do_undo), ("Ctrl+Y", self._do_redo), ("Ctrl+Shift+Z", self._do_redo),
                        ("Ctrl+S", self._save)):
            QShortcut(QKeySequence(seq), self, activated=fn)
        sc = QShortcut(QKeySequence("Ctrl+Shift+P"), self, activated=lambda: self._set_picking(not self._picking))
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        register_theme_refresh(self, self._restyle)
        draft = self._read_draft()
        if draft is not None:

            if not draft.get("accent") and self.data.get("accent"):
                draft["accent"] = self.data["accent"]
            if not draft.get("extends") and self.data.get("extends"):
                draft["extends"] = self.data["extends"]
        self._load_data(draft or self.data, resumed=draft is not None)
        self._snapshot()


    def _init_data(self) -> None:
        restore = self._restore
        meta = manager.presets.get(restore)
        self.data = {"label": "My theme", "base_dark": isDarkTheme(), "palette": {}, "rules": [], "widget_icons": [], "icons": {}}
        if restore and meta is not None:
            self.data["extends"] = restore
        accent = _user_accent_color() if meta is None or not meta.locked_accent else None
        accent = accent or (meta.accent if meta is not None else None)
        if accent:
            self.data["accent"] = accent

    def _read_draft(self):
        d = _read_json(self._draft_path)
        return d if isinstance(d, dict) and (d.get("rules") or d.get("palette") or d.get("icons") or d.get("widget_icons")) else None

    def _write_draft(self) -> None:
        _write_json(self._draft_path, self.data)

    def _snapshot(self) -> None:
        s = json.dumps(self.data, sort_keys=True)
        if not self._undo or self._undo[-1] != s:
            self._undo.append(s)
            self._undo[:] = self._undo[-100:]
            self._redo.clear()
            self._sync_undo()

    def _sync_undo(self) -> None:
        self._b_undo.setEnabled(len(self._undo) > 1)
        self._b_redo.setEnabled(bool(self._redo))

    def _do_undo(self) -> None:
        if len(self._undo) > 1:
            self._redo.append(self._undo.pop())
            self._load_data(json.loads(self._undo[-1]))
            self._sync_undo()

    def _do_redo(self) -> None:
        if self._redo:
            s = self._redo.pop()
            self._undo.append(s)
            self._load_data(json.loads(s))
            self._sync_undo()

    def _changed(self, full: bool = True) -> None:
        if self._loading:
            return
        self._snapshot()
        self._draft_save.poke()
        self._dirty |= {"colours", "code"}
        (self._full if full else self._light).start()

    def _load_data(self, data: dict, resumed: bool = False) -> None:
        self._loading = True
        self.data = {"rules": [], "widget_icons": [], "icons": {}, "palette": {}, **data}
        d = self.data
        self._name.setText(d.get("label", "My theme"))
        self._mode.setCurrentIndex(0 if d.get("base_dark", True) else 1)
        for role, row in self._pal_rows.items():
            row.setText(d["palette"].get(role, ""))
        bg = d.get("background") or {}
        kind = str(bg.get("type") or "").strip().lower()
        if kind not in ("image", "shader"):
            kind = "shader" if isinstance(bg.get("shader"), str) else ("image" if bg else "none")
        self._bg_type.setCurrentText(kind)
        self._bg_file.setText(str(bg.get("file") or (bg.get("shader") if kind == "shader" else "") or ""))
        self._bg_img.setText(str(bg.get("image") or ""))
        self._bg_blur.setValue(int(bg.get("blur", 12) or 0))
        self._bg_dark.setValue(int(bg.get("darken", 45) or 0))
        self._bg_scope.setCurrentText(bg.get("scope", "client"))
        fps = int(_float(bg.get("fps"), 0.0, 240.0, 0.0)) if kind == "shader" else 0
        if self._bg_fps.findData(fps) < 0:
            self._bg_fps.addItem(f"{fps} FPS", userData=fps)
        self._bg_fps.setCurrentIndex(max(0, self._bg_fps.findData(fps)))
        aa = _int(bg.get("aa"), 0, 8, 0) if kind == "shader" else 0
        self._bg_aa.setCurrentIndex(max(0, self._bg_aa.findData(aa if aa in (0, 2, 4, 8) else 0)))
        self._update_bg_controls()
        self._backdrop.setCurrentText(d.get("backdrop") or "none")
        self._transparent.setChecked(bool(d.get("transparent_window")))
        self._tb.setPlainText(json.dumps(d["titlebar"], indent=2) if d.get("titlebar") else "")
        self._win7.setChecked(bool(d.get("win7_frame")))
        self._fam.setText(", ".join(d.get("font_families", [])))
        self._fpaths.setText(";".join(d.get("font_paths", [])))
        sfx = d.get("sfx") or {}
        for key, row in getattr(self, "_sfx_rows", {}).items():
            row.setText(str(sfx.get(key, "") or ""))
        for k, row in self._lay_rows.items():
            row.setText(d.get("layout", {}).get(k, ""))
        self._raw.setPlainText(d.get("qss", ""))
        self._loading = False
        self._fill_slots()
        self._dirty |= {"colours", "code"}
        self._load_rule()
        self._status.setText("Resumed your unsaved draft" if resumed else "Live preview — changes apply instantly")
        self._full.start()


    def _restyle(self) -> None:
        self.setStyleSheet(f"#themeMaker {{ background-color: {manager.palette()['page_bg']}; }}")

    def _build(self) -> None:
        from qfluentwidgets import (CaptionLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton,
                                    SegmentedWidget, StrongBodyLabel, ToolButton)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        head = QHBoxLayout()
        title = StrongBodyLabel("Theme Maker")
        self._status = CaptionLabel("")
        self._b_undo, self._b_redo = ToolButton(FluentIcon.RETURN), ToolButton(FluentIcon.SYNC)
        self._b_undo.setToolTip("Undo (Ctrl+Z)")
        self._b_redo.setToolTip("Redo (Ctrl+Y)")
        self._b_undo.clicked.connect(self._do_undo)
        self._b_redo.clicked.connect(self._do_redo)
        self._b_cmp = ToolButton(FluentIcon.VIEW)
        self._b_cmp.setCheckable(True)
        self._b_cmp.setToolTip("Compare with the original theme")
        self._b_cmp.toggled.connect(self._toggle_compare)
        head.addWidget(title)
        head.addStretch(1)
        for b in (self._b_undo, self._b_redo, self._b_cmp):
            head.addWidget(b)
        root.addLayout(head)
        self._name = LineEdit()
        self._name.setPlaceholderText("Theme name")
        self._mode = ComboBox()
        self._mode.addItems(["Dark base", "Light base"])
        self._name.textChanged.connect(lambda t: self._setkey("label", t))
        self._mode.currentIndexChanged.connect(lambda i: self._setkey("base_dark", i == 0))
        row = QHBoxLayout()
        row.addWidget(self._name, 1)
        row.addWidget(self._mode)
        root.addLayout(row)
        pr = QHBoxLayout()
        self._pick_btn = PrimaryPushButton("Pick an element")
        self._pick_btn.setToolTip("Then click anything in the app (Ctrl+Shift+P)")
        self._pick_btn.clicked.connect(lambda: self._set_picking(not self._picking))
        self._b_parent = PushButton("Parent ↑")
        self._b_parent.clicked.connect(self._select_parent)
        self._b_locate = PushButton("Locate")
        self._b_locate.clicked.connect(self._flash)
        pr.addWidget(self._pick_btn, 1)
        pr.addWidget(self._b_parent)
        pr.addWidget(self._b_locate)
        root.addLayout(pr)
        root.addWidget(self._status)
        self._seg = SegmentedWidget()
        self._stack = QStackedWidget()
        builders = (self._tab_element, self._tab_colours, self._tab_icons, self._tab_backdrop,
                    self._tab_titlebar, self._tab_fonts, self._tab_sfx, self._tab_code)
        for (key, text), build in zip(_TABS, builders, strict=True):
            self._stack.addWidget(build())
            self._seg.addItem(key, text, onClick=lambda _checked=False, k=key: self._goto(k))
        self._seg.setCurrentItem("element")
        root.addWidget(self._seg)
        root.addWidget(self._stack, 1)
        bar = QHBoxLayout()
        save = PrimaryPushButton("Save theme")
        save.setToolTip("Ctrl+S")
        save.clicked.connect(self._save)
        bar.addWidget(save, 1)
        for text, fn in (("Export", self._export), ("Import", self._import), ("Open saved", self._edit_saved), ("New", self._new)):
            b = PushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        root.addLayout(bar)

    def _goto(self, key: str) -> None:
        idx = [k for k, _ in _TABS].index(key)
        self._stack.setCurrentIndex(idx)
        self._seg.setCurrentItem(key)
        if key == "colours":
            self._ensure_palette()
            self._refresh_palette()
        elif key == "code":
            self._refresh_code()


    def _tab_element(self) -> QWidget:
        from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, ListWidget, PlainTextEdit, PushButton
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setSpacing(10)
        card, cl = _card("Selected element")
        self._info = BodyLabel("Nothing selected — press “Pick an element”, then click anything in the app.")
        self._info.setWordWrap(True)
        cl.addWidget(self._info)
        self._sel, self._state = ComboBox(), ComboBox()
        self._state.addItems([s or "normal" for s in _STATES])
        sr = QHBoxLayout()
        sr.addWidget(self._sel, 3)
        sr.addWidget(self._state, 2)
        cl.addLayout(sr)
        cl.addWidget(CaptionLabel("Applies to this selector, in this state. Change to affect one item or all of its kind."))
        self._hier_list = ListWidget()
        self._hier_list.setFixedHeight(120)
        self._hier_list.itemClicked.connect(lambda it: self._pick_hier(self._hier_list.row(it)))
        cl.addWidget(self._hier_list)
        lay.addWidget(card)
        self._sel.currentIndexChanged.connect(self._load_rule)
        self._state.currentIndexChanged.connect(self._load_rule)
        card, cl = _card("Quick styles")
        qr = QHBoxLayout()
        for name, props in _QUICK:
            b = PushButton(name)
            b.clicked.connect(lambda _=False, p=props: self._apply_quick(p))
            qr.addWidget(b)
        cl.addLayout(qr)
        ar = QHBoxLayout()
        for text, fn in (("Copy style", self._copy_style), ("Paste style", self._paste_style), ("Clear", self._clear_rule)):
            b = PushButton(text)
            b.clicked.connect(fn)
            ar.addWidget(b)
        self._icon_btn = PushButton("Change icon…")
        self._icon_btn.clicked.connect(self._pick_icon)
        ar.addWidget(self._icon_btn)
        cl.addLayout(ar)
        lay.addWidget(card)
        self._edits: dict = {}
        for gtitle, props in _PROP_GROUPS:
            card, cl = _card(gtitle)
            for label, prop, kind in props:
                r = _PropRow(label, kind)
                r.edited.connect(self._commit_rule)
                self._edits[prop] = r
                cl.addWidget(r)
            lay.addWidget(card)
        card, cl = _card("Any other QSS property — limitless")
        self._extra = PlainTextEdit()
        self._extra.setPlaceholderText("name: value   (one per line)")
        self._extra.setFixedHeight(90)
        self._extra.textChanged.connect(self._commit_rule)
        cl.addWidget(self._extra)
        lay.addWidget(card)
        lay.addStretch(1)
        return _scrolled(inner)


    def _tab_colours(self) -> QWidget:
        from qfluentwidgets import SearchLineEdit
        w = QWidget()
        lay = QVBoxLayout(w)
        self._pal_search = SearchLineEdit()
        self._pal_search.setPlaceholderText("Search colours (e.g. nav, button, card)…")
        self._pal_search.textChanged.connect(self._filter_palette)
        lay.addWidget(self._pal_search)
        self._pal_host = QWidget()
        self._pal_lay = QVBoxLayout(self._pal_host)
        self._pal_lay.setSpacing(4)
        lay.addWidget(_scrolled(self._pal_host), 1)
        return w

    def _ensure_palette(self) -> None:
        if self._pal_rows:
            return
        from qfluentwidgets import StrongBodyLabel
        last = None
        for role in sorted(PALETTE_ROLES, key=lambda r: ([g for g, _ in _GROUPS].index(_group_of(r)) if _group_of(r) != "Other" else 99, r)):
            g = _group_of(role)
            if g != last:
                head = StrongBodyLabel(g)
                head.setContentsMargins(0, 8, 0, 0)
                self._pal_heads[g] = head
                self._pal_lay.addWidget(head)
                last = g
            row = _PropRow(role.replace("_", " "), "color", label_w=150, placeholder="inherited")
            row.setText(self.data["palette"].get(role, ""))
            row.edited.connect(lambda r=role, rw=row: self._set_palette(r, rw.text()))
            self._pal_rows[role] = row
            self._pal_lay.addWidget(row)
        self._pal_lay.addStretch(1)

    def _filter_palette(self, q: str) -> None:
        q = q.strip().lower().replace(" ", "_")
        for role, row in self._pal_rows.items():
            row.setVisible(not q or q in role)
        for head in self._pal_heads.values():
            head.setVisible(not q)

    def _refresh_palette(self) -> None:
        if "colours" not in self._dirty or not self._pal_rows:
            return
        self._dirty.discard("colours")
        src = self._effective_src()
        res = resolve_palette(src)
        for role, row in self._pal_rows.items():
            row.hint(src.get(role, ""), res[role])

    def _effective_src(self) -> dict:
        parent = manager.presets.get(self.data.get("extends"))
        return {**(parent.src if parent else _BASE[bool(self.data.get("base_dark", True))]), **self.data.get("palette", {})}

    def _set_palette(self, role: str, text: str) -> None:
        if self._loading:
            return
        pal = self.data.setdefault("palette", {})
        if not text:
            pal.pop(role, None)
        elif _valid_expr(text):
            pal[role] = text
        else:
            return
        self._changed()


    def _tab_icons(self) -> QWidget:
        from qfluentwidgets import CaptionLabel, ListWidget, SearchLineEdit
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(CaptionLabel("Sidebar, settings and page icons — double-click one to change it."))
        self._slot_search = SearchLineEdit()
        self._slot_search.setPlaceholderText("Filter icon slots…")
        self._slot_search.textChanged.connect(self._fill_slots)
        self._slot_list = ListWidget()
        self._slot_list.setIconSize(QSize(22, 22))
        self._slot_list.itemDoubleClicked.connect(self._slot_pick)
        lay.addWidget(self._slot_search)
        lay.addWidget(self._slot_list, 1)
        return w

    def _fill_slots(self, q=None) -> None:
        q = (q if isinstance(q, str) else self._slot_search.text()).lower()
        self._slot_list.clear()
        icons = self.data.get("icons", {})
        for slot in ICON_SLOTS:
            if q and q not in slot.lower():
                continue
            s = icons.get(slot)
            spec = (_parse_icon_spec(self.workdir, s) if s is not None else None)
            if spec is None:
                n = icon_slot_default_name(slot)
                spec = {"type": "fluent", "value": n} if n else None
            item = QListWidgetItem(_spec_icon(spec, 22), slot + ("   ✎" if slot in icons else ""))
            item.setData(Qt.ItemDataRole.UserRole, slot)
            self._slot_list.addItem(item)

    def _slot_pick(self, item) -> None:
        slot = item.data(Qt.ItemDataRole.UserRole)
        dlg = _IconPicker(self)
        if not dlg.exec():
            return
        icons = self.data.setdefault("icons", {})
        if dlg.spec is not None:
            icons[slot] = {"image": dlg.spec["value"], "crop": True} if dlg.spec["type"] == "image" else dlg.spec["value"]
        else:
            icons.pop(slot, None)
        self._fill_slots()
        self._changed()


    def _tab_backdrop(self) -> QWidget:
        from qfluentwidgets import BodyLabel, CheckBox, ComboBox, PushButton, Slider
        inner = QWidget()
        lay = QVBoxLayout(inner)
        card, cl = _card("Background")
        self._bg_type = ComboBox()
        self._bg_type.addItems(["none", "image", "shader"])
        self._bg_file = _PropRow("File", "file", placeholder="photo, GIF or shader file")
        self._bg_img = _PropRow("Shader texture", "file", placeholder="optional image for shaders")
        self._bg_scope = ComboBox()
        self._bg_scope.addItems(["content", "client", "window"])
        self._bg_scope.setCurrentText("client")
        self._bg_fps, self._bg_aa = ComboBox(), ComboBox()
        for label, value in (("Unlimited (display rate)", 0), ("30 FPS", 30), ("60 FPS", 60), ("120 FPS", 120)):
            self._bg_fps.addItem(label, userData=value)
        for label, value in (("Anti-aliasing off", 0), ("2× MSAA", 2), ("4× MSAA", 4), ("8× MSAA", 8)):
            self._bg_aa.addItem(label, userData=value)
        fps_row = QHBoxLayout()
        for combo in (self._bg_fps, self._bg_aa):
            combo.setMinimumWidth(150)
            fps_row.addWidget(combo, 1)
        self._bg_example = PushButton("Write example shader")
        self._bg_example.setToolTip("Drops a commented starter .frag next to the draft and selects it")
        self._bg_example.clicked.connect(self._write_example_shader)
        self._bg_blur, self._bg_dark = Slider(Qt.Orientation.Horizontal), Slider(Qt.Orientation.Horizontal)
        for s, v in ((self._bg_blur, 12), (self._bg_dark, 45)):
            s.setRange(0, 100)
            s.setValue(v)
        self._bg_thumb = QLabel()
        self._bg_thumb.setFixedHeight(110)
        self._bg_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self._bg_type)
        cl.addWidget(self._bg_file)
        cl.addWidget(self._bg_img)
        cl.addLayout(fps_row)
        cl.addWidget(self._bg_example)
        cl.addWidget(BodyLabel("Blur"))
        cl.addWidget(self._bg_blur)
        cl.addWidget(BodyLabel("Darken"))
        cl.addWidget(self._bg_dark)
        cl.addWidget(BodyLabel("Cover"))
        cl.addWidget(self._bg_scope)
        cl.addWidget(self._bg_thumb)
        lay.addWidget(card)
        card, cl = _card("Window")
        self._backdrop = ComboBox()
        self._backdrop.addItems(["none", *sorted(b for b in _BACKDROPS if b != "none")])
        self._transparent = CheckBox("Transparent window")
        cl.addWidget(BodyLabel("Backdrop material"))
        cl.addWidget(self._backdrop)
        cl.addWidget(self._transparent)
        lay.addWidget(card)
        lay.addStretch(1)
        for sig in (self._bg_type.currentIndexChanged, self._bg_scope.currentIndexChanged, self._backdrop.currentIndexChanged,
                    self._bg_blur.valueChanged, self._bg_dark.valueChanged, self._bg_file.edited, self._bg_img.edited,
                    self._bg_fps.currentIndexChanged, self._bg_aa.currentIndexChanged, self._transparent.stateChanged):
            sig.connect(self._commit_bg)
        self._update_bg_controls()
        return _scrolled(inner)

    def _update_bg_controls(self) -> None:
        kind = self._bg_type.currentText()
        shader = kind == "shader"
        for w in (self._bg_fps, self._bg_aa, self._bg_example, self._bg_img):
            w.setEnabled(shader)
        for w in (self._bg_blur, self._bg_dark):
            w.setEnabled(kind == "image")

    def _write_example_shader(self) -> None:
        path = os.path.join(self.workdir, "example.frag")
        try:
            os.makedirs(self.workdir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(_EXAMPLE_SHADER)
        except OSError as exc:
            return self._toast(f"Could not write the example shader: {exc}", False)
        self._loading = True
        self._bg_type.setCurrentText("shader")
        self._bg_file.setText(path)
        self._loading = False
        self._commit_bg()
        self._toast("Wrote example.frag — edit it and the preview reloads")

    def _commit_bg(self, *_a) -> None:
        if self._loading:
            return
        kind, f = self._bg_type.currentText(), self._bg_file.text()
        self.data["backdrop"] = self._backdrop.currentText()
        self.data["transparent_window"] = self._transparent.isChecked()
        pm = QPixmap(f) if f and kind == "image" else QPixmap()
        self._bg_thumb.setPixmap(pm.scaledToHeight(104, Qt.TransformationMode.SmoothTransformation) if not pm.isNull() else QPixmap())
        self._update_bg_controls()
        if kind == "none" or not f:
            self.data.pop("background", None)
        else:
            bg = {"type": kind, "file": f, "image": self._bg_img.text(), "blur": self._bg_blur.value(),
                  "darken": self._bg_dark.value(), "scope": self._bg_scope.currentText()}
            if kind == "shader":
                bg["fps"] = self._bg_fps.currentData()
                bg["aa"] = self._bg_aa.currentData()
            self.data["background"] = bg
        self._changed()


    def _tab_titlebar(self) -> QWidget:
        from qfluentwidgets import CaptionLabel, PlainTextEdit, PushButton
        w = QWidget()
        lay = QVBoxLayout(w)
        from qfluentwidgets import CheckBox
        self._win7 = CheckBox("Windows 7 glass title bar frame")
        self._win7.setToolTip("Off (default): clean custom title bar in every backdrop mode (none, blur, acrylic, transparent).\n"
                              "On: keep the native Windows 7 / Aero glass frame.")
        self._win7.stateChanged.connect(lambda *_: self._setkey("win7_frame", self._win7.isChecked()))
        lay.addWidget(self._win7)
        lay.addWidget(CaptionLabel("Title bar DSL (JSON) — applies live as soon as it is valid."))
        self._tb = PlainTextEdit()
        self._tb.setPlaceholderText("{}  — layers, glyphs, chips, nav, mode, corner …")
        self._tb_err = CaptionLabel("")
        tpl = PushButton("Insert defaults to start from")
        tpl.clicked.connect(lambda: self._tb.setPlainText(json.dumps(_TITLEBAR_DEFAULTS, indent=2, default=str)))
        self._tb_t = QTimer(self)
        self._tb_t.setSingleShot(True)
        self._tb_t.setInterval(350)
        self._tb_t.timeout.connect(self._apply_titlebar)
        self._tb.textChanged.connect(lambda: None if self._loading else self._tb_t.start())
        lay.addWidget(self._tb, 1)
        lay.addWidget(self._tb_err)
        lay.addWidget(tpl)
        return w

    def _apply_titlebar(self) -> None:
        text = self._tb.toPlainText().strip()
        try:
            if text:
                self.data["titlebar"] = json.loads(text)
            else:
                self.data.pop("titlebar", None)
            self._tb_err.setText("")
            self._changed()
        except ValueError as exc:
            self._tb_err.setText(f"JSON error: {exc}")


    def _tab_fonts(self) -> QWidget:
        inner = QWidget()
        lay = QVBoxLayout(inner)
        card, cl = _card("Fonts")
        self._fam = _PropRow("Families", "font", placeholder="Segoe UI, Consolas …")
        self._fpaths = _PropRow("Font files", "file", placeholder="path.ttf;other.otf")
        for r in (self._fam, self._fpaths):
            r.edited.connect(self._commit_fonts)
            cl.addWidget(r)
        lay.addWidget(card)
        card, cl = _card("Layout")
        self._lay_rows = {}
        for key, default in LAYOUT.items():
            r = _PropRow(key.replace("_", " "), "text", label_w=170, placeholder=str(default))
            r.edited.connect(self._commit_layout)
            self._lay_rows[key] = r
            cl.addWidget(r)
        lay.addWidget(card)
        lay.addStretch(1)
        return _scrolled(inner)

    def _commit_fonts(self) -> None:
        if self._loading:
            return
        self.data["font_families"] = [f.strip().strip('"') for f in self._fam.text().split(",") if f.strip()]
        self.data["font_paths"] = [p.strip() for p in self._fpaths.text().split(";") if p.strip()]
        self._changed()

    def _commit_layout(self) -> None:
        if self._loading:
            return
        self.data["layout"] = {k: r.text() for k, r in self._lay_rows.items() if r.text()}
        self._changed()


    def _tab_sfx(self) -> QWidget:
        from qfluentwidgets import BodyLabel, PushButton
        inner = QWidget()
        lay = QVBoxLayout(inner)
        card, cl = _card("Sound effects")
        cl.addWidget(BodyLabel("Leave a row empty to inherit the built-in sound. Files are packed into the theme on save."))
        self._sfx_rows: dict = {}
        for key in _SFX_KEYS:
            row = _PropRow(key.replace("_", " ").title(), "file", placeholder="inherit")
            row.edited.connect(self._commit_sfx)
            self._sfx_rows[key] = row
            cl.addWidget(row)
        clear = PushButton("Clear all sounds")
        clear.clicked.connect(self._clear_sfx)
        cl.addWidget(clear)
        lay.addWidget(card)
        lay.addStretch(1)
        return _scrolled(inner)

    def _commit_sfx(self) -> None:
        if self._loading:
            return
        sfx = {k: row.text() for k, row in self._sfx_rows.items() if row.text()}
        if sfx:
            self.data["sfx"] = sfx
        else:
            self.data.pop("sfx", None)
        self._snapshot()
        self._draft_save.poke()

    def _clear_sfx(self) -> None:
        self._loading = True
        for row in self._sfx_rows.values():
            row.setText("")
        self._loading = False
        self._commit_sfx()


    def _tab_code(self) -> QWidget:
        from qfluentwidgets import CaptionLabel, ListWidget, PlainTextEdit, PushButton
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(CaptionLabel("Rules and icon overrides"))
        self._rule_list = ListWidget()
        self._rule_list.setFixedHeight(150)
        d = PushButton("Delete selected")
        d.clicked.connect(self._delete_rule)
        lay.addWidget(self._rule_list)
        lay.addWidget(d)
        lay.addWidget(CaptionLabel("Raw QSS added to the whole app"))
        self._raw = PlainTextEdit()
        self._raw.setFixedHeight(120)
        self._raw.textChanged.connect(lambda: self._setkey("qss", self._raw.toPlainText()))
        lay.addWidget(self._raw)
        lay.addWidget(CaptionLabel("theme.json (read-only preview)"))
        self._json_view = PlainTextEdit()
        self._json_view.setReadOnly(True)
        lay.addWidget(self._json_view, 1)
        return w

    def _refresh_code(self) -> None:
        if "code" not in self._dirty:
            return
        self._dirty.discard("code")
        self._rule_list.clear()
        for r in self.data["rules"]:
            self._rule_list.addItem(QListWidgetItem(f'{r["selector"]}{r.get("state", "")}  {{ {r["css"]} }}'))
        for i in self.data["widget_icons"]:
            self._rule_list.addItem(QListWidgetItem(f'icon  {i["match"]}  →  {i["spec"].get("value")}'))
        self._json_view.setPlainText(json.dumps(self.data, indent=2, ensure_ascii=False))

    def _delete_rule(self) -> None:
        row, nr = self._rule_list.currentRow(), len(self.data["rules"])
        if row < 0:
            return
        if row < nr:
            del self.data["rules"][row]
        else:
            del self.data["widget_icons"][row - nr]
        self._dirty.add("code")
        self._refresh_code()
        self._load_rule()
        self._changed(full=False)

    def _setkey(self, key, value) -> None:
        if not self._loading:
            self.data[key] = value
            self._changed()


    def _preview_full(self) -> None:
        if self._compare:
            return
        try:
            os.makedirs(self.workdir, exist_ok=True)
            warns = manager.preview(self.data, self.workdir)
            self._status.setText("⚠ " + "; ".join(warns[:2]) if warns else "Live preview — changes apply instantly")
        except Exception as exc:
            self._status.setText(f"Preview error: {exc}")
        if self._stack.currentIndex() == 1:
            self._refresh_palette()
        elif self._stack.currentIndex() == len(_TABS) - 1:
            self._refresh_code()

    def _preview_light(self) -> None:
        if self._compare:
            return
        try:
            manager.preview_light(self.data, self.workdir)
        except Exception as exc:
            self._status.setText(f"Preview error: {exc}")

    def _toggle_compare(self, on: bool) -> None:
        self._compare = on
        if on:
            manager.set_active_preset(self._restore)
            self._status.setText("Showing the ORIGINAL theme — toggle again to resume editing")
        else:
            self._preview_full()


    def _mine(self, w) -> bool:
        while w is not None:
            if w is self or isinstance(w, _PickOverlay):
                return True
            w = w.parent()
        return False

    def _set_picking(self, on: bool) -> None:
        for o in self._overlays:
            o.hide()
            o.deleteLater()
        self._overlays = []
        self._picking = False
        self._pick_btn.setText("Pick an element")
        if not on:
            return
        self._end_flash()
        for top in QApplication.topLevelWidgets():
            if (top.isVisible() and top.isWindow() and not self._mine(top) and top.width() > 80 and top.height() > 40
                    and top.windowType() not in (Qt.WindowType.ToolTip, Qt.WindowType.SplashScreen)):
                ov = _PickOverlay(top, self)
                ov.picked.connect(self._on_picked)
                ov.cancelled.connect(lambda: self._set_picking(False))
                self._overlays.append(ov)
        self._picking = bool(self._overlays)
        self._pick_btn.setText("Click any element…  (Esc or right-click cancels)" if self._picking else "Pick an element")

    def _on_picked(self, w) -> None:
        self._set_picking(False)
        try:
            self._set_target(w)
            self._flash()
        except Exception as exc:
            _log.exception("theme maker: selecting element failed")
            self._status.setText(f"Selected {w.metaObject().className()}, but the editor failed: {exc}")

    def _end_flash(self) -> None:
        if self._flash_ov is not None:
            self._flash_ov.hide()
            self._flash_ov.deleteLater()
            self._flash_ov = None

    def _flash(self) -> None:
        self._end_flash()
        w = self.target
        if w is not None and shiboken6.isValid(w) and w.isVisible():
            self._flash_ov = _PickOverlay(w.window(), self, flash=w)
            QTimer.singleShot(1000, self._end_flash)

    def _slot_of(self, w) -> Optional[str]:
        chrome = _ACTIVE_CHROME.get("chrome")
        slots = getattr(chrome, "_nav_slots", None) or {}
        panel = getattr(getattr(w.window(), "navigationInterface", None), "panel", None)
        for route, item in (getattr(panel, "items", None) or {}).items():
            btn = getattr(item, "widget", item)
            try:
                if btn is w or btn.isAncestorOf(w):
                    s = slots.get(route)
                    return s[0] if s else None
            except (RuntimeError, AttributeError):
                continue
        return None

    def _set_target(self, w) -> None:
        self.target = w
        cls = w.metaObject().className()
        self._slot = self._slot_of(w)
        self._info.setText(f"<b>{cls}</b>" + (f"  #{w.objectName()}" if w.objectName() else "  (no object name)")
                           + (f"<br>Icon slot: <b>{self._slot}</b>" if self._slot else ""))
        cands = ([f"#{w.objectName()}"] if w.objectName() else []) + [cls]
        a = w.parent()
        while a is not None and not a.objectName():
            a = a.parent()
        if a is not None:
            cands.append(f"#{a.objectName()} {cls}")
        self._loading = True
        self._sel.clear()
        self._sel.addItems(cands)
        self._loading = False
        self._icon_btn.setVisible(bool(self._slot) or hasattr(w, "setIcon"))
        self._icon_btn.setText("Change sidebar icon…" if self._slot else "Change icon…")
        self._fill_hierarchy()
        self._goto("element")
        self._load_rule()

    def _fill_hierarchy(self) -> None:
        w, chain, a = self.target, [], self.target.parent()
        while a is not None and len(chain) < 8:
            chain.append(a)
            a = a.parent()
        kids = [c for c in w.children() if hasattr(c, "isVisible") and c.isVisible()][:30]
        self._hier = list(reversed(chain)) + [w] + kids
        self._hier_list.clear()
        base = len(chain)
        for i, o in enumerate(self._hier):
            depth = i if i <= base else base + 1
            mark = "●  " if o is w else ""
            self._hier_list.addItem(QListWidgetItem("    " * min(depth, 6) + mark + o.metaObject().className()
                                                    + (f"#{o.objectName()}" if o.objectName() else "")))
        self._hier_list.setCurrentRow(base)

    def _pick_hier(self, row: int) -> None:
        if 0 <= row < len(self._hier) and shiboken6.isValid(self._hier[row]) and hasattr(self._hier[row], "isVisible"):
            self._set_target(self._hier[row])
            self._flash()

    def _select_parent(self) -> None:
        p = self.target.parent() if self.target is not None and shiboken6.isValid(self.target) else None
        while p is not None and not hasattr(p, "isVisible"):
            p = p.parent()
        if p is not None:
            self._set_target(p)
            self._flash()


    def _rule_key(self):
        return self._sel.currentText(), _STATES[max(0, self._state.currentIndex())]

    def _find_rule(self):
        sel, state = self._rule_key()
        return next((r for r in self.data["rules"] if r["selector"] == sel and r.get("state", "") == state), None)

    def _load_rule(self) -> None:
        if self._loading:
            return
        rule = self._find_rule()
        props = _css_to_dict(rule["css"]) if rule else {}
        hints = {}
        if self.target is not None and shiboken6.isValid(self.target):
            pal, f = self.target.palette(), self.target.font()
            px = f.pixelSize() if f.pixelSize() > 0 else round(f.pointSizeF() * 96 / 72)
            hints = {"color": pal.windowText().color().name(), "background-color": pal.window().color().name(),
                     "font-family": f.family(), "font-size": f"{px}px"}
        self._loading = True
        for prop, row in self._edits.items():
            row.setText(props.pop(prop, ""))
            h = hints.get(prop)
            row.hint(h or "inherit", h if prop in ("color", "background-color") else None)
        self._extra.setPlainText("\n".join(f"{k}: {v}" for k, v in props.items()))
        self._loading = False

    def _collect(self) -> dict:
        props = {}
        for prop, row in self._edits.items():
            v = row.text()
            if not v:
                continue
            if prop.endswith("color") and not _valid_color(v):
                continue
            props[prop] = v + "px" if re.fullmatch(r"-?\d+(\.\d+)?", v) and prop != "font-weight" else v
        props.update(_css_to_dict(self._extra.toPlainText()))
        return props

    def _commit_rule(self) -> None:
        if self._loading or not self._sel.currentText():
            return
        props = self._collect()
        sel, state = self._rule_key()
        rules = self.data["rules"]
        rules[:] = [r for r in rules if not (r["selector"] == sel and r.get("state", "") == state)]
        if props:
            rules.append({"selector": sel, "state": state, "css": " ".join(f"{k}: {v};" for k, v in props.items())})
        self._changed(full=False)

    def _apply_quick(self, props: dict) -> None:
        if not self._sel.currentText():
            self._status.setText("Pick an element first")
            return
        self._loading = True
        extra = _css_to_dict(self._extra.toPlainText())
        for k, v in props.items():
            if k in self._edits:
                self._edits[k].setText(v)
            else:
                extra[k] = v
        self._extra.setPlainText("\n".join(f"{k}: {v}" for k, v in extra.items()))
        self._loading = False
        self._commit_rule()

    def _copy_style(self) -> None:
        self._clip = self._collect()
        self._status.setText(f"Copied {len(self._clip)} properties")

    def _paste_style(self) -> None:
        if self._clip:
            self._apply_quick(self._clip)

    def _clear_rule(self) -> None:
        self._loading = True
        for row in self._edits.values():
            row.setText("")
        self._extra.clear()
        self._loading = False
        self._commit_rule()

    def _pick_icon(self) -> None:
        dlg = _IconPicker(self)
        if not dlg.exec():
            return
        if self._slot:
            icons = self.data.setdefault("icons", {})
            if dlg.spec is not None:
                icons[self._slot] = {"image": dlg.spec["value"], "crop": True} if dlg.spec["type"] == "image" else dlg.spec["value"]
            else:
                icons.pop(self._slot, None)
            self._fill_slots()
            return self._changed()
        sel = self._sel.currentText()
        if not sel:
            return
        lst = self.data["widget_icons"]
        lst[:] = [i for i in lst if i["match"] != sel]
        if dlg.spec is not None:
            lst.append({"match": sel, "spec": dlg.spec})
        self._changed(full=False)


    def _toast(self, text: str, ok: bool = True) -> None:
        self._status.setText(text)
        with suppress(Exception):
            from qfluentwidgets import InfoBar, InfoBarPosition
            (InfoBar.success if ok else InfoBar.error)("Theme Maker", text, duration=2600, position=InfoBarPosition.TOP, parent=self)

    def _pack(self) -> dict:
        out = json.loads(json.dumps(self.data))
        out["id"] = tid = _slug(out.get("label", "my-theme"))
        assets = os.path.join(_themes_dir(), tid, "assets")
        os.makedirs(assets, exist_ok=True)

        def grab(p):
            if not isinstance(p, str) or not os.path.isabs(p) or not os.path.isfile(p):
                return p
            dest = os.path.join(assets, os.path.basename(p))
            if os.path.abspath(dest) != os.path.abspath(p):
                shutil.copy2(p, dest)
            return "assets/" + os.path.basename(p)
        bg = out.get("background")
        if isinstance(bg, dict):
            bg["file"], bg["image"] = grab(bg.get("file")), grab(bg.get("image"))
        out["font_paths"] = [grab(p) for p in out.get("font_paths", [])]
        for s in out.get("icons", {}).values():
            if isinstance(s, dict):
                s["image"] = grab(s.get("image"))
        for i in out.get("widget_icons", []):
            if i["spec"].get("type") == "image":
                i["spec"]["value"] = grab(i["spec"]["value"])
        sfx = out.get("sfx")
        if isinstance(sfx, dict):
            out["sfx"] = {k: grab(v) for k, v in sfx.items() if isinstance(v, str) and v.strip()}
            if not out["sfx"]:
                out.pop("sfx", None)
        return out

    def _save(self) -> None:
        try:
            data = self._pack()
            folder = os.path.join(_themes_dir(), data["id"])
            with open(os.path.join(folder, "theme.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            return self._toast(f"Save failed: {exc}", False)
        self._saved = True
        manager.presets.clear_draft()
        manager.reload_custom_presets()
        manager.activate(data["id"])
        with suppress(Exception):
            from src.core.config import settings, save_settings
            settings.theme_preset = data["id"]
            save_settings(settings)
        with suppress(OSError):
            os.remove(self._draft_path)
        self._draft_save.cancel()
        self.workdir = folder
        self._toast(f"Saved “{data['label']}” — it is now your active theme")

    def _export(self) -> None:
        self._save()
        tid = _slug(self.data.get("label", "my-theme"))
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", f"{tid}.zip", "Theme package (*.zip)")
        if path:
            shutil.make_archive(path[:-4] if path.lower().endswith(".zip") else path, "zip", os.path.join(_themes_dir(), tid))
            self._toast(f"Exported → {path}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import theme", "", "Theme package (*.zip)")
        if not path:
            return
        try:
            with zipfile.ZipFile(path) as z:
                if "theme.json" not in z.namelist():
                    raise ValueError("package has no theme.json")
                dest = os.path.abspath(os.path.join(_themes_dir(), _slug(os.path.splitext(os.path.basename(path))[0])))
                for m in z.namelist():
                    if os.path.commonpath([dest, os.path.abspath(os.path.join(dest, m))]) != dest:
                        raise ValueError("unsafe path in package")
                z.extractall(dest)
            manager.reload_custom_presets()
            self._toast(f"Imported → {os.path.basename(dest)}")
        except Exception as exc:
            self._toast(f"Import failed: {exc}", False)

    def _edit_saved(self) -> None:
        custom = [p for p in manager.presets.all().values() if p.dir and p.id != "__draft__"]
        if not custom:
            return self._toast("No saved custom themes yet", False)
        labels = [p.label for p in custom]
        choice, ok = QInputDialog.getItem(self, "Open saved theme", "Theme", labels, 0, False)
        if not ok:
            return
        preset = custom[labels.index(choice)]
        raw = _read_json(os.path.join(preset.dir, "theme.json"))
        if isinstance(raw, dict):
            self.workdir = preset.dir
            self._load_data(raw)
            self._snapshot()

    def _new(self) -> None:
        self._init_data()
        self._load_data(self.data)
        self._snapshot()

    def closeEvent(self, event) -> None:
        self._set_picking(False)
        self._end_flash()
        self._draft_save.flush()
        ThemeMaker._instance = None
        manager.end_preview(manager.active_preset_id() if self._saved else self._restore)
        super().closeEvent(event)


def open_theme_maker(parent=None) -> ThemeMaker:
    if ThemeMaker._instance is None:
        ThemeMaker._instance = ThemeMaker(parent)
    ThemeMaker._instance.show()
    ThemeMaker._instance.raise_()
    return ThemeMaker._instance

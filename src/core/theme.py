from __future__ import annotations

from qfluentwidgets import isDarkTheme, qconfig

LAYOUT = {
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
}

_DARK = {
    "page_bg": "transparent",
    "card_bg": "rgba(255, 255, 255, 0.06)",
    "card_hover": "rgba(255, 255, 255, 0.09)",
    "card_border": "rgba(255, 255, 255, 0.10)",
    "muted": "#9CA3AF",
    "primary_text": "#E5E7EB",
    "input_bg": "rgba(255, 255, 255, 0.07)",
    "input_border": "rgba(255, 255, 255, 0.14)",
    "input_border_hover": "rgba(255, 255, 255, 0.22)",
    "input_border_focus": "#0078D4",
    "btn_bg": "rgba(255, 255, 255, 0.07)",
    "btn_border": "rgba(255, 255, 255, 0.14)",
    "btn_hover": "rgba(255, 255, 255, 0.11)",
    "accent": "#0078D4",
    "detail_title": "#E5E7EB",
    "detail_subtitle": "#7DA6E8",
    "detail_meta": "#9CA3AF",
    "detail_box_bg": "rgba(255, 255, 255, 0.06)",
    "detail_box_border": "rgba(255, 255, 255, 0.10)",
    "state_queued": "#8A8A8A",
    "state_downloading": "#3B9DF3",
    "state_verifying": "#E08838",
    "state_paused": "#8A8A8A",
    "state_seeding": "#3FC240",
    "state_completed": "#3FC240",
    "state_error": "#E5484D",
    "state_cancelled": "#8A8A8A",
    "faint_text": "#7A7F87",
    "body_text": "#D8DAE0",
    "surface_tint": "rgba(255, 255, 255, 0.06)",
    "surface_tint_strong": "rgba(255, 255, 255, 0.10)",
    "surface_border": "rgba(255, 255, 255, 0.13)",
    "hover_tint": "rgba(255, 255, 255, 0.07)",
    "inactive_dot": "rgba(255, 255, 255, 0.25)",
    "poster_fallback_bg": "rgba(255, 255, 255, 0.06)",
    "section_card_bg": "rgba(255, 255, 255, 0.10)",
    "section_card_border": "rgba(255, 255, 255, 0.13)",
    "scrollbar_handle": "rgba(255, 255, 255, 0.19)",
    "scrollbar_handle_hover": "rgba(255, 255, 255, 0.30)",
    "list_bg": "rgba(0, 0, 0, 0.14)",
}

_LIGHT = {
    "page_bg": "#F3F3F4",
    "card_bg": "#FFFFFF",
    "card_hover": "#F6F7F9",
    "card_border": "rgba(0, 0, 0, 0.09)",
    "muted": "#5B6472",
    "primary_text": "#101828",
    "input_bg": "#FFFFFF",
    "input_border": "#D5D8DE",
    "input_border_hover": "#B7BCC6",
    "input_border_focus": "#0078D4",
    "btn_bg": "#FFFFFF",
    "btn_border": "#D5D8DE",
    "btn_hover": "#EEF0F3",
    "accent": "#0078D4",
    "detail_title": "#101828",
    "detail_subtitle": "#0B5FA5",
    "detail_meta": "#5B6472",
    "detail_box_bg": "#F0F1F4",
    "detail_box_border": "#D5D8DE",
    "state_queued": "#6B7280",
    "state_downloading": "#0078D4",
    "state_verifying": "#C15A0A",
    "state_paused": "#6B7280",
    "state_seeding": "#0E7A0E",
    "state_completed": "#0E7A0E",
    "state_error": "#C42B1C",
    "state_cancelled": "#6B7280",
    "faint_text": "#6B7280",
    "body_text": "#1F2430",
    "surface_tint": "rgba(0, 0, 0, 0.045)",
    "surface_tint_strong": "rgba(0, 0, 0, 0.075)",
    "surface_border": "rgba(0, 0, 0, 0.11)",
    "hover_tint": "rgba(0, 0, 0, 0.05)",
    "inactive_dot": "rgba(0, 0, 0, 0.24)",
    "poster_fallback_bg": "rgba(0, 0, 0, 0.045)",
    "section_card_bg": "#EAEBEF",
    "section_card_border": "rgba(0, 0, 0, 0.11)",
    "scrollbar_handle": "rgba(0, 0, 0, 0.20)",
    "scrollbar_handle_hover": "rgba(0, 0, 0, 0.32)",
    "list_bg": "rgba(0, 0, 0, 0.035)",
}


def palette(dark: bool | None = None) -> dict:
    use_dark = isDarkTheme() if dark is None else dark
    return _DARK if use_dark else _LIGHT


def register_theme_refresh(widget, callback) -> None:
    callback()

    def _on_theme_changed(*_):
        _qss_cache.clear()
        callback()

    qconfig.themeChanged.connect(_on_theme_changed)


_qss_cache: dict[tuple, str] = {}

def settings_qss(page_object_names: tuple[str, ...] = ("#settingsPage", "#aboutPage")) -> str:
    cache_key = ("settings_qss", isDarkTheme(), page_object_names)
    cached = _qss_cache.get(cache_key)
    if cached is not None:
        return cached
    c = palette()
    l = LAYOUT
    sels = page_object_names
    page_sel = ", ".join(f"{s}" for s in sels)
    group_title_sel = ", ".join(f"{s} SettingCardGroup > QLabel" for s in sels)
    card_sel = ", ".join(
        f"{s} {w}" for s in sels for w in ("SettingCard", "SwitchSettingCard", "CardWidget")
    )
    card_hover_sel = ", ".join(
        f"{s} {w}:hover" for s in sels for w in ("SettingCard", "SwitchSettingCard", "CardWidget")
    )
    title_lbl_sel = ", ".join(
        f"{s} {w} QLabel#titleLabel" for s in sels for w in ("SettingCard", "SwitchSettingCard")
    )
    content_lbl_sel = ", ".join(
        f"{s} {w} QLabel#contentLabel" for s in sels for w in ("SettingCard", "SwitchSettingCard")
    )
    caption_sel = ", ".join(f"{s} CaptionLabel" for s in sels)
    input_sel = ", ".join(
        f"{s} {w}"
        for s in sels
        for w in ("LineEdit", "ComboBox", "SpinBox", "CompactSpinBox", "DoubleSpinBox")
    )
    input_hover_sel = ", ".join(
        f"{s} {w}:hover"
        for s in sels
        for w in ("LineEdit", "ComboBox", "SpinBox", "CompactSpinBox", "DoubleSpinBox")
    )
    input_focus_sel = ", ".join(f"{s} {w}:focus" for s in sels for w in ("LineEdit", "ComboBox"))
    btn_sel = ", ".join(f"{s} PushButton" for s in sels)
    btn_hover_sel = ", ".join(f"{s} PushButton:hover" for s in sels)
    subtitle_sel = ", ".join(f"{s} SubtitleLabel" for s in sels)
    body_sel = ", ".join(f"{s} BodyLabel" for s in sels)

    result = f"""
        {page_sel} {{
            background-color: {c['page_bg']};
            border: none;
        }}

        {group_title_sel} {{
            color: {c['primary_text']};
            font-weight: 600;
            font-size: 16px;
            padding-top: {l['group_title_pad_top']};
            padding-bottom: {l['group_title_pad_bottom']};
        }}

        {card_sel} {{
            background-color: {c['card_bg']};
            border: none;
            border-radius: {l['card_radius']};
            padding: {l['card_padding']};
        }}
        {card_hover_sel} {{
            background-color: {c['card_hover']};
            border: none;
        }}

        {title_lbl_sel} {{
            color: {c['primary_text']};
            font-weight: 600;
        }}
        {content_lbl_sel},
        {caption_sel} {{
            color: {c['muted']};
        }}

        {input_sel} {{
            background-color: {c['input_bg']};
            border: {l['input_border_width']} solid {c['input_border']};
            border-radius: {l['input_radius']};
            color: {c['primary_text']};
            padding: {l['input_padding']};
        }}
        {input_hover_sel} {{
            border: {l['input_border_width']} solid {c['input_border_hover']};
        }}
        {input_focus_sel} {{
            border: {l['input_border_width_focus']} solid {c['input_border_focus']};
        }}

        {btn_sel} {{
            background-color: {c['btn_bg']};
            border: {l['input_border_width']} solid {c['btn_border']};
            border-radius: {l['btn_radius']};
            color: {c['primary_text']};
            padding: {l['btn_padding']};
        }}
        {btn_hover_sel} {{
            background-color: {c['btn_hover']};
            border: {l['input_border_width']} solid {c['input_border_hover']};
        }}

        {subtitle_sel} {{
            color: {c['primary_text']};
        }}
        {body_sel} {{
            color: {c['muted']};
        }}
    """
    _qss_cache[cache_key] = result
    return result


def scroll_area_qss() -> str:
    cache_key = ("scroll_area_qss", isDarkTheme())
    cached = _qss_cache.get(cache_key)
    if cached is not None:
        return cached
    c = palette()
    result = f"""
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {c['scrollbar_handle']};
            border-radius: 5px;
            min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {c['scrollbar_handle_hover']};
        }}
        QScrollBar:horizontal {{
            height: 0px;
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{
            background: transparent;
        }}
    """
    _qss_cache[cache_key] = result
    return result


def card_qss(class_name: str, radius: int = 8) -> str:
    cache_key = ("card_qss", isDarkTheme(), class_name, radius)
    cached = _qss_cache.get(cache_key)
    if cached is not None:
        return cached
    c = palette()
    result = f"""
        {class_name} {{
            background-color: {c['card_bg']};
            border: none;
            border-radius: {radius}px;
        }}
        {class_name}:hover {{
            background-color: {c['card_hover']};
            border: none;
        }}
    """
    _qss_cache[cache_key] = result
    return result

from __future__ import annotations

from qfluentwidgets import isDarkTheme

LAYOUT = {
    "card_padding": "10px 14px",
    "card_radius": "12px",
    "input_padding": "6px 10px",
    "input_radius": "8px",
    "btn_padding": "6px 16px",
    "btn_radius": "8px",
    "group_title_pad_top": "8px",
    "group_title_pad_bottom": "4px",
    "input_border_width": "1px",
    "input_border_width_focus": "2px",
}

_DARK = {
    "page_bg": "transparent",
    "card_bg": "rgba(255, 255, 255, 0.04)",
    "card_hover": "rgba(255, 255, 255, 0.07)",
    "muted": "#9CA3AF",
    "primary_text": "#E5E7EB",
    "input_bg": "rgba(255, 255, 255, 0.06)",
    "input_border": "rgba(255, 255, 255, 0.10)",
    "input_border_hover": "rgba(255, 255, 255, 0.18)",
    "input_border_focus": "#0078D4",
    "btn_bg": "rgba(255, 255, 255, 0.05)",
    "btn_border": "rgba(255, 255, 255, 0.10)",
    "btn_hover": "rgba(255, 255, 255, 0.08)",
    "accent": "#0078D4",
    "detail_title": "#E5E7EB",
    "detail_subtitle": "#7DA6E8",
    "detail_meta": "#9CA3AF",
    "detail_box_bg": "rgba(255, 255, 255, 0.04)",
    "detail_box_border": "rgba(255, 255, 255, 0.10)",
    "state_queued": "#8a8a8a",
    "state_downloading": "#0078D4",
    "state_verifying": "#CA5010",
    "state_paused": "#8a8a8a",
    "state_seeding": "#107C10",
    "state_completed": "#107C10",
    "state_error": "#C42B1C",
    "state_cancelled": "#8a8a8a",
}

_LIGHT = {
    "page_bg": "rgba(242, 242, 242, 1)",
    "card_bg": "#FFFFFF",
    "card_hover": "rgba(248, 248, 250, 1)",
    "muted": "#6B7280",
    "primary_text": "#111827",
    "input_bg": "#F5F5F5",
    "input_border": "#E5E7EB",
    "input_border_hover": "#D1D5DB",
    "input_border_focus": "#0078D4",
    "btn_bg": "#FFFFFF",
    "btn_border": "#E5E7EB",
    "btn_hover": "#F5F5F5",
    "accent": "#0078D4",
    "detail_title": "#111827",
    "detail_subtitle": "#0B5FA5",
    "detail_meta": "#6B7280",
    "detail_box_bg": "#F5F5F5",
    "detail_box_border": "#E5E7EB",
    "state_queued": "#8a8a8a",
    "state_downloading": "#0078D4",
    "state_verifying": "#CA5010",
    "state_paused": "#8a8a8a",
    "state_seeding": "#107C10",
    "state_completed": "#107C10",
    "state_error": "#C42B1C",
    "state_cancelled": "#8a8a8a",
}

def palette(dark: bool | None = None) -> dict:
    use_dark = isDarkTheme() if dark is None else dark
    return _DARK if use_dark else _LIGHT

def settings_qss(page_object_names: tuple[str, ...] = ("#settingsPage", "#aboutPage")) -> str:
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

    return f"""
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
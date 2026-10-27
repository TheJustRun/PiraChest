from __future__ import annotations

import atexit
import base64
import importlib
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import types
import uuid
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from bisect import bisect_right
from functools import partial
from urllib.parse import urlparse

import httpx
from PySide6.QtCore import (QByteArray, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect,
                            QRectF, QRunnable, QSize, Qt, QUrl, QThreadPool, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
                               QListWidgetItem, QProxyStyle, QScrollArea, QSizePolicy, QSlider,
                               QStackedWidget, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout,
                               QWidget)
from qfluentwidgets import (Action, CaptionLabel, CardWidget, CheckBox, FluentIcon,
                            IndeterminateProgressBar, InfoBar, InfoBarPosition, LineEdit, ListWidget,
                            MessageBox, MessageBoxBase, Pivot, PrimaryPushButton, PushButton,
                            RoundMenu, ScrollArea, SearchLineEdit, Slider, StrongBodyLabel,
                            SubtitleLabel, Theme, TransparentToolButton, isDarkTheme, themeColor)

from src.core.artwork import images
from .grid import PosterGrid
from src.core.config import paths as app_paths, settings as app_settings
from src.core.models import MusicItem as Song
from src.core.net import LazyHttpClient
from src.core.translations import register_locale_refresh, tr
from src.core.worker import cancel, submit, wrap_callback

_USER_AGENT_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def install_fake_useragent_stub() -> None:
    if 'fake_useragent' in sys.modules:
        return
    try:
        importlib.import_module('fake_useragent')
        return
    except Exception:
        pass

    module = types.ModuleType('fake_useragent')

    class UserAgent:
        def __init__(self, *args, **kwargs):
            pass

        random = property(lambda self: _USER_AGENT_FALLBACK)

    module.UserAgent = UserAgent
    sys.modules['fake_useragent'] = module


install_fake_useragent_stub()

logger = logging.getLogger(__name__)

_ROW_H, _BAR_H, _CARD_W, _CARD_H, _COVER = 54, 96, 180, 244, 168
_AUDIO_EXT = ('.mp3', '.flac', '.m4a', '.mp4', '.aac', '.ogg', '.wav')
_MIME_EXT = {'audio/flac': '.flac', 'audio/x-flac': '.flac', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
             'audio/ogg': '.ogg', 'audio/wav': '.wav'}
_PLAYLISTS_DIR = os.path.join(app_paths.config_dir, 'music')
_PLAYLISTS_FILE = os.path.join(_PLAYLISTS_DIR, 'music_playlists.json')
_LRCLIB_HEADERS = {'Lrclib-Client': 'PiraChest v1.0 (https://github.com)'}
_RAW_KEEP = {'bitrate', 'bit_rate', 'br', 'sample_rate', 'samplerate', 'sr', 'sampling_rate', 'channels', 'channel',
             'nchannels'}
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/124.0.0.0 Safari/537.36')
_LRC_RE = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')
_http = LazyHttpClient(timeout=30, follow_redirects=True, headers={'User-Agent': _UA},
                       limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))


_SVG = {
    'Play': '<svg xmlns="http://www.w3.org/2000/svg" height="23" width="23" viewBox="0 0 23 23"><g><path transform="rotate(0,11.5,11.5) translate(-1,-0.5) scale(0.04791,0.04791) " fill="#FFFFFF" d="M160,93.5L160,418.5 392.25,256 160,93.5z M128,32L448,256 128,480 128,32z" /></g></svg>',
    'Pause': '<svg xmlns="http://www.w3.org/2000/svg" height="19" width="19" viewBox="0 0 19 19"><g><path transform="rotate(0,9.5,9.5) translate(-2,-1) scale(0.04241,0.04241) " fill="#FFFFFF" d="M320,64L352,64 352,448 320,448 320,64z M160,64L192,64 192,448 160,448 160,64z" /></g></svg>',
    'Next': '<svg xmlns="http://www.w3.org/2000/svg" height="20" width="20" viewBox="0 0 20 20"><g><path transform="rotate(0,10,10) translate(-1.5,-1.5) scale(0.04464,0.04464) " fill="#FFFFFF" d="M96,125.75L96,386.25 280.5,256 96,125.75z M416,64L448,64 448,448 416,448 416,64z M64,64L336,256 64,448 64,64z" /></g></svg>',
    'Previous': '<svg xmlns="http://www.w3.org/2000/svg" height="20" width="20" viewBox="0 0 20 20"><g><path transform="rotate(0,10,10) translate(-1.5,-1.5) scale(0.04464,0.04464) " fill="#FFFFFF" d="M416,125.75L231.5,256 416,386.25 416,125.75z M448,64L448,448 176,256 448,64z M64,64L96,64 96,448 64,448 64,64z" /></g></svg>',
    'Shuffle': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0) scale(0.05171,0.05171) " fill="#FFFFFF" d="M268.25,278.75L277.375,286.79687 286.75,294.4375 296.375,301.67187 306.25,308.5 316.36718,314.89062 326.71875,320.8125 337.30468,326.26562 348.125,331.25 359.15625,335.73437 370.375,339.6875 381.78125,343.10937 393.375,346 405.13281,348.34375 417.03125,350.125 429.07031,351.34375 441.25,352 404.75,315.25 427.25,292.75 502.75,368 427.25,443.25 404.75,420.75 441.5,384 427.82028,383.3125 414.28121,382 400.88281,380.0625 387.625,377.5 374.55468,374.33593 361.71875,370.59375 349.11718,366.27343 336.75,361.375 324.61718,355.91406 312.71875,349.90625 301.05468,343.35156 289.625,336.25 278.46093,328.64062 267.59375,320.5625 257.02343,312.01562 246.75,303 257.375,290.875 268.25,278.75z M-2.84217E-14,128L14.21484,128.33398 28.23437,129.33593 42.05859,131.00585 55.6875,133.34375 69.12109,136.34960 82.35937,140.02343 95.40234,144.36523 108.25,149.375 120.85937,154.98242 133.1875,161.11718 145.23437,167.77929 157,174.96875 168.48437,182.68554 179.6875,190.92968 190.60937,199.70117 201.25,209 190.625,221.125 179.75,233.25 170.35156,224.96095 160.65625,217.09376 150.66406,209.64843 140.375,202.625 129.82031,196.07032 119.03125,190.03126 108.00781,184.50781 96.75,179.5 85.28125,175.03126 73.625,171.12501 61.78125,167.78125 49.75000,165 37.54687,162.81251 25.1875,161.25 12.67187,160.3125 -2.84217E-14,160 -2.84217E-14,128z M427.25,68.75L502.75,144 427.25,219.25 404.75,196.75 441.25,160 425.58593,160.92970 410.34371,162.71875 395.52340,165.36718 381.125,168.875 367.125,173.1875 353.5,178.25 340.25,184.0625 327.375,190.625 314.85156,197.89843 302.65625,205.84375 290.78906,214.46095 279.25,223.75 268.03125,233.625 257.125,244 246.53123,254.875 236.25,266.25 219.25,285.25 210.49998,294.375 201.25,303.25 190.60937,312.54687 179.6875,321.3125 168.48437,329.54687 157,337.25 145.23437,344.42187 133.1875,351.0625 120.85937,357.17187 108.25,362.75 95.40234,367.73046 82.35937,372.04687 69.12109,375.69921 55.6875,378.6875 42.05859,381.01171 28.23437,382.67187 14.21484,383.66796 -2.84217E-14,384 -2.84217E-14,352 16.16406,351.52343 31.90625,350.09375 47.22656,347.71093 62.12500,344.375 76.61718,340.17187 90.71875,335.1875 104.42968,329.42187 117.75,322.875 130.69531,315.57812 143.28125,307.5625 155.50781,298.82812 167.375,289.375 178.91406,279.28906 190.15625,268.65625 201.10156,257.47656 211.75,245.75 229.375,226 238.53125,216.625 248.25,207.5 258.50781,198.71875 269.03125,190.375 279.82031,182.46876 290.875,175 302.1875,168.01562 313.75,161.5625 325.5625,155.64062 337.625,150.25 349.92187,145.41407 362.4375,141.15625 375.17187,137.47656 388.125,134.375 401.25781,131.86718 414.53125,129.96875 427.94531,128.67968 441.5,128 404.75,91.25 427.25,68.75z" /></g></svg>',
    'RepeatAll': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0.01904) scale(0.05078,0.05078) " fill="#FFFFFF" d="M446.75,112.5L454.28906,119.42968 461.40625,126.71875 468.10156,134.36718 474.375,142.375 480.20312,150.68751 485.5625,159.25 490.45312,168.0625 494.875,177.125 498.8125,186.41406 502.25,195.90625 505.1875,205.60157 507.625,215.5 509.53906,225.53125 510.90625,235.625 511.72656,245.78125 512,256 511.57031,269.125 510.28125,282 508.13281,294.625 505.125,307 501.33593,319.05468 496.84375,330.71875 491.64843,341.99218 485.75,352.875 479.18746,363.32031 471.99996,373.28125 464.18746,382.75781 455.75,391.75 446.75781,400.1875 437.28125,408 427.32031,415.1875 416.875,421.75 405.99218,427.64843 394.71875,432.84375 383.05468,437.33593 371,441.125 358.625,444.13281 346,446.28125 333.125,447.57031 320,448 158.5,448 199.25,488.75 176.75,511.25 97.5,432 176.75,352.75 199.25,375.25 158.5,416 320,416 330.90625,415.64062 341.625,414.5625 352.15625,412.76562 362.5,410.25 372.57812,407.08593 382.3125,403.34375 391.70312,399.02343 400.75,394.125 409.42968,388.67187 417.71871,382.6875 425.61715,376.17187 433.125,369.125 440.17184,361.61718 446.68746,353.71875 452.67187,345.42968 458.125,336.75 463.02343,327.70312 467.34375,318.3125 471.08593,308.57812 474.25,298.5 476.76562,288.15625 478.56246,277.625 479.64059,266.90625 480,256 479.76559,247.28906 479.06246,238.65625 477.89059,230.10156 476.25,221.625 474.16403,213.27343 471.65621,205.09375 468.72653,197.08593 465.375,189.25 461.60934,181.60157 457.43746,174.15625 452.85934,166.91406 447.875,159.875 436.78125,146.65625 424.25,134.75 446.75,112.5z M335.25,0.75L414.5,80 335.25,159.25 312.75,136.75 353.5,96 192,96 181.09375,96.35937 170.375,97.43750 159.84375,99.23438 149.5,101.75 139.42187,104.91407 129.6875,108.65625 120.29687,112.97657 111.25,117.875 102.57031,123.32813 94.28125,129.3125 86.38281,135.82812 78.875,142.875 71.82812,150.38281 65.3125,158.28125 59.32812,166.57032 53.875,175.25 48.97656,184.29687 44.65625,193.6875 40.91406,203.42187 37.75,213.5 35.23437,223.84376 33.4375,234.37501 32.35937,245.09375 32,256 32.24218,264.60156 32.96875,273.15625 34.17968,281.66406 35.875,290.125 38.01562,298.48437 40.5625,306.6875 43.51562,314.73437 46.875,322.625 50.64062,330.32031 54.81249,337.78125 59.39062,345.00781 64.375,352 75.46875,365.125 88,377 65.25,399.75 57.71875,392.71093 50.625,385.34375 43.96875,377.64843 37.75,369.625 31.96875,361.3125 26.625,352.75 21.71875,343.9375 17.25,334.875 13.25781,325.59375 9.78125,316.125 6.82031,306.46875 4.375,296.625 2.46093,286.63281 1.09375,276.53125 0.27343,266.32031 0,256 0.42968,242.875 1.71875,230 3.86718,217.375 6.875,205 10.66406,192.94532 15.15625,181.28125 20.35156,170.00781 26.25,159.125 32.8125,148.67968 40,138.71875 47.8125,129.24220 56.25,120.25 65.24218,111.81250 74.71875,104.00000 84.67968,96.81250 95.125,90.25 106.00781,84.35157 117.28125,79.15627 128.94531,74.66408 141,70.875 153.375,67.86719 166,65.71875 178.875,64.42968 192,64 353.5,64 312.75,23.25 335.25,0.75z" /></g></svg>',
    'Volume0': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0) scale(0.05078,0.05078) " fill="#FFFFFF" d="M160,118.75L86.75,192 32,192 32,320 86.75,320 160,393.25 160,118.75z M169.25,64L192,64 192,448 169.5,448 73.25,352 0,352 0,160 73.25,160 169.25,64z" /></g></svg>',
    'Volume1': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0) scale(0.05078,0.05078) " fill="#FFFFFF" d="M282.5,165.5L291.08593,174.93751 298.59375,185 305.02343,195.6875 310.375,207 314.58593,218.78125 317.59375,230.875 319.39843,243.28125 320,256 319.39843,268.71875 317.59375,281.125 314.58593,293.21875 310.375,305 305.02343,316.3125 298.59375,327 291.08593,337.0625 282.5,346.5 260,324 266.35937,316.99218 271.9375,309.46875 276.73437,301.42968 280.75,292.875 283.92187,283.96093 286.1875,274.84375 287.54687,265.52343 288,256 287.54687,246.47657 286.1875,237.15625 283.92187,228.03906 280.75,219.125 276.73437,210.57031 271.9375,202.53125 266.35937,195.00781 260,188 282.5,165.5z M160,118.75L86.75,192 32,192 32,320 86.75,320 160,393.25 160,118.75z M169.25,64L192,64 192,448 169.5,448 73.25,352 0,352 0,160 73.25,160 169.25,64z" /></g></svg>',
    'Volume2': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0) scale(0.05078,0.05078) " fill="#FFFFFF" d="M282.5,165.5L291.08593,174.93751 298.59375,185 305.02343,195.6875 310.375,207 314.58593,218.78125 317.59375,230.875 319.39843,243.28125 320,256 319.39843,268.71875 317.59375,281.125 314.58593,293.21875 310.375,305 305.02343,316.3125 298.59375,327 291.08593,337.0625 282.5,346.5 260,324 266.35937,316.99218 271.9375,309.46875 276.73437,301.42968 280.75,292.875 283.92187,283.96093 286.1875,274.84375 287.54687,265.52343 288,256 287.54687,246.47657 286.1875,237.15625 283.92187,228.03906 280.75,219.125 276.73437,210.57031 271.9375,202.53125 266.35937,195.00781 260,188 282.5,165.5z M160,118.75L86.75,192 32,192 32,320 86.75,320 160,393.25 160,118.75z M350.5,97.5L358.14843,105.63671 365.34375,114.04688 372.08593,122.73047 378.375,131.6875 384.21093,140.91796 389.59375,150.42187 394.52343,160.19921 399,170.25 402.98437,180.50390 406.4375,190.89064 409.35937,201.41017 411.75,212.0625 413.60937,222.84765 414.9375,233.76562 415.73437,244.81640 416,256 415.73437,267.18359 414.9375,278.23437 413.60937,289.15234 411.75,299.9375 409.35937,310.58984 406.4375,321.10937 402.98437,331.49609 399,341.75 394.52343,351.80078 389.59375,361.57812 384.21093,371.08203 378.375,380.3125 372.08593,389.26953 365.34375,397.95312 358.14843,406.36328 350.5,414.5 327.75,391.75 334.36328,384.81640 340.57812,377.64062 346.39453,370.22265 351.8125,362.5625 356.83203,354.66015 361.45312,346.51562 365.67578,338.12890 369.5,329.5 372.89843,320.69531 375.84375,311.78125 378.33593,302.75781 380.375,293.625 381.96093,284.38281 383.09375,275.03125 383.77343,265.57031 384,256 383.77343,246.42968 383.09375,236.96875 381.96093,227.61718 380.375,218.375 378.33593,209.24218 375.84375,200.21875 372.89843,191.30468 369.5,182.5 365.67578,173.87109 361.45312,165.48439 356.83203,157.33985 351.8125,149.43751 346.39453,141.77735 340.57812,134.35939 334.36328,127.18360 327.75,120.25 350.5,97.5z M169.25,64L192,64 192,448 169.5,448 73.25,352 0,352 0,160 73.25,160 169.25,64z" /></g></svg>',
    'Volume3': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,0) scale(0.05078,0.05078) " fill="#FFFFFF" d="M282.5,165.5L291.08593,174.93751 298.59375,185 305.02343,195.6875 310.375,207 314.58593,218.78125 317.59375,230.875 319.39843,243.28125 320,256 319.39843,268.71875 317.59375,281.125 314.58593,293.21875 310.375,305 305.02343,316.3125 298.59375,327 291.08593,337.0625 282.5,346.5 260,324 266.35937,316.99218 271.9375,309.46875 276.73437,301.42968 280.75,292.875 283.92187,283.96093 286.1875,274.84375 287.54687,265.52343 288,256 287.54687,246.47657 286.1875,237.15625 283.92187,228.03906 280.75,219.125 276.73437,210.57031 271.9375,202.53125 266.35937,195.00781 260,188 282.5,165.5z M160,118.75L86.75,192 32,192 32,320 86.75,320 160,393.25 160,118.75z M350.5,97.5L358.14843,105.63671 365.34375,114.04688 372.08593,122.73047 378.375,131.6875 384.21093,140.91796 389.59375,150.42187 394.52343,160.19921 399,170.25 402.98437,180.50390 406.4375,190.89064 409.35937,201.41017 411.75,212.0625 413.60937,222.84765 414.9375,233.76562 415.73437,244.81640 416,256 415.73437,267.18359 414.9375,278.23437 413.60937,289.15234 411.75,299.9375 409.35937,310.58984 406.4375,321.10937 402.98437,331.49609 399,341.75 394.52343,351.80078 389.59375,361.57812 384.21093,371.08203 378.375,380.3125 372.08593,389.26953 365.34375,397.95312 358.14843,406.36328 350.5,414.5 327.75,391.75 334.36328,384.81640 340.57812,377.64062 346.39453,370.22265 351.8125,362.5625 356.83203,354.66015 361.45312,346.51562 365.67578,338.12890 369.5,329.5 372.89843,320.69531 375.84375,311.78125 378.33593,302.75781 380.375,293.625 381.96093,284.38281 383.09375,275.03125 383.77343,265.57031 384,256 383.77343,246.42968 383.09375,236.96875 381.96093,227.61718 380.375,218.375 378.33593,209.24218 375.84375,200.21875 372.89843,191.30468 369.5,182.5 365.67578,173.87109 361.45312,165.48439 356.83203,157.33985 351.8125,149.43751 346.39453,141.77735 340.57812,134.35939 334.36328,127.18360 327.75,120.25 350.5,97.5z M169.25,64L192,64 192,448 169.5,448 73.25,352 0,352 0,160 73.25,160 169.25,64z M418.25,29.75L429.1875,41.23439 439.5,53.18752 449.1875,65.60939 458.25,78.5 466.64843,91.78126 474.34375,105.37503 481.33593,119.28127 487.625,133.5 493.21090,148.01564 498.09371,162.81251 502.27343,177.89062 505.75,193.25 508.48437,208.79689 510.4375,224.43751 511.60937,240.17187 512,256 511.60937,271.92968 510.4375,287.71875 508.48437,303.36718 505.75,318.875 502.27343,334.17968 498.09371,349.21875 493.21090,363.99218 487.625,378.5 481.33593,392.71875 474.34375,406.625 466.64843,420.21875 458.25,433.5 449.1875,446.39062 439.5,458.8125 429.1875,470.76562 418.25,482.25 395.5,459.5 405.34375,449.16406 414.625,438.40625 423.34375,427.22656 431.5,415.625 439.0625,403.67187 446,391.4375 452.3125,378.92187 458,366.125 463.04684,353.07031 467.43746,339.78125 471.17187,326.25781 474.25,312.5 476.65625,298.5625 478.375,284.5 479.40625,270.3125 479.75,256 479.40625,241.6875 478.375,227.5 476.65625,213.4375 474.25,199.5 471.17187,185.74218 467.43746,172.21875 463.04684,158.92968 458,145.875 452.3125,133.07812 446,120.5625 439.0625,108.32812 431.5,96.375 423.34375,84.77343 414.625,73.59375 405.34375,62.83593 395.5,52.5 418.25,29.75z" /></g></svg>',
    'Volumex': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,11.5,11.5) translate(0.12193,0) scale(0.05133,0.05133) " fill="#ffffff" d="M283.25,164.75L352,233.25 420.75,164.75 443.25,187.25 374.75,256 443.25,324.75 420.75,347.25 352,278.75 283.25,347.25 260.75,324.75 329.25,256 260.75,187.25 283.25,164.75z M160,118.75L86.75,192 32,192 32,320 86.75,320 160,393.25 160,118.75z M169.25,63.99999L192,63.99999 192,448 169.25,448 73.25,352 2.84217E-14,352 2.84217E-14,160 73.25,160 169.25,63.99999z" /></g></svg>',
    'ChevronUp': '<svg xmlns="http://www.w3.org/2000/svg" height="16" width="16" viewBox="0 0 16 16"><g><path transform="rotate(0,8,8) translate(0,2.02471) scale(0.16872,0.16872) " fill="#FFFFFF" d="M48,18.32812L94.82812,65.15625 89.15625,70.82812 48,29.67187 6.84374,70.82812 1.17187,65.15625 48,18.32812z" /></g></svg>',
    'Playlist': '<svg xmlns="http://www.w3.org/2000/svg" height="26" width="26" viewBox="0 0 26 26"><g><path transform="rotate(0,13,13) translate(0,-1) scale(0.05078,0.05078) " fill="#FFFFFF" d="M0,416L512,416 512,448 0,448 0,416z M0,320L512,320 512,352 0,352 0,320z M160,224L512,224 512,256 112,256 160,224z M26.5,147.25L26.5,236.75 89,192 26.5,147.25z M112,128L512,128 512,160 160,160 112,128z M0,96L134.5,192 0,288 0,96z" /></g></svg>',
    'l_play': '<svg xmlns="http://www.w3.org/2000/svg" height="19" width="19" viewBox="0 0 19 19"><g><path transform="rotate(0,9.5,9.5) translate(-0.63333,0) scale(0.03958,0.03958) " fill="#FFFFFF" d="M160,93.5L160,418.5 392.25,256 160,93.5z M128,32L448,256 128,480 128,32z" /></g></svg>',
    'l_add': '<svg xmlns="http://www.w3.org/2000/svg" height="19" width="19" viewBox="0 0 19 19"><g><path transform="rotate(0,9.5,9.5) translate(0,0) scale(0.03958,0.03958) " fill="#FFFFFF" d="M224,0L256,0 256,224 480,224 480,256 256,256 256,480 224,480 224,256 0,256 0,224 224,224 224,0z" /></g></svg>',
    'l_dl': '<svg xmlns="http://www.w3.org/2000/svg" height="20" width="20" viewBox="0 0 20 20"><g><path transform="rotate(0,10,10) translate(1,0) scale(0.03906,0.03906) " fill="#FFFFFF" d="M96,480L384,480 384,512 96,512 96,480z M224,0L256,0 256,362.25 372.75,244.75 395.25,267.25 240,423.75 84.75,267.25 107.25,244.75 224,362.25 224,0z" /></g></svg>',
    'l_rm': '<svg xmlns="http://www.w3.org/2000/svg" height="20" width="20" viewBox="0 0 20 20"><g><path transform="rotate(0,10,10) translate(0,0) scale(0.04166,0.04166) " fill="#FFFFFF" d="M0,224L480,224 480,256 0,256 0,224z" /></g></svg>',
}

_SVG.update({
    'm_play': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M8 5v14l11-7z"/></svg>',
    'm_pause': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>',
    'm_next': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg>',
    'm_prev': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg>',
    'm_shuffle': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg>',
    'm_repeat': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>',
    'm_volup': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>',
    'm_voldown': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z"/></svg>',
    'm_vol0': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M7 9v6h4l5 5V4l-5 5H7z"/></svg>',
    'm_volx': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>',
    'm_queue': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z"/></svg>',
    'm_pladd': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M14 10H2v2h12v-2zm0-4H2v2h12V6zm4 8v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zM2 16h8v-2H2v2z"/></svg>',
    'm_lyrics': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>',
})


try:
    from src.core.cache import cache
except Exception:
    cache = None

_MUSIC_CFG_DIR = os.path.join(app_paths.config_dir, 'music')
_MUSIC_SETTINGS_FILE = os.path.join(_MUSIC_CFG_DIR, 'music_settings.json')
# Providers verified to return complete, playable tracks (not previews) with a
# working direct URL.  Ordered alphabetically; the search pool starts all of
# them at once.  Slow or corrupt providers stay selectable in search filters
# but are intentionally not enabled by default.
DEFAULT_SOURCES = [
    "AudiusMusicClient",
    "FiveSingMusicClient",
    "JioSaavnMusicClient",
    "MituMusicClient",
    "NeteaseMusicClient",
    "YinyuekuMusicClient",
]
# The previous default list.  Configs that still contain exactly this list are
# treated as untouched and upgraded to the curated one on load.
LEGACY_DEFAULT_SOURCES = [
    "BodianMusicClient",
    "DeezerMusicClient",
    "FMAMusicClient",
    "FangpiMusicClient",
    "GequhaiMusicClient",
    "HTQYYMusicClient",
    "ITingWaMusicClient",
    "JBSouMusicClient",
    "JooxMusicClient",
    "KugouMusicClient",
    "KuwoMusicClient",
    "LRTSMusicClient",
    "LivePOOMusicClient",
    "MGMP3MusicClient",
    "MOOVMusicClient",
    "MiguMusicClient",
    "NeteaseMusicClient",
    "QQMusicClient",
    "QianqianMusicClient",
    "QobuzMusicClient",
    "SgogoMusicClient",
    "SodaMusicClient",
    "TuneHubMusicClient",
    "MituMusicClient",
]
SEARCH_TIMEOUT_S = 40.0  # Slowest curated source lands around 25-30s; keep headroom.
QUALITY_TIERS = ['lossless', 'mp3', 'aac', 'other']
SLOW_OR_GATED_SOURCES = ['AppleMusicClient', 'SoundCloudMusicClient', 'StreetVoiceMusicClient', 'TIDALMusicClient', 'YouTubeMusicClient', 'SpotifyMusicClient', 'MituMusicClient', 'GequbaoMusicClient', 'YinyuedaoMusicClient', 'BuguyyMusicClient']

@dataclass
class MusicSettings:
    download_dir: str = field(default_factory=lambda: os.path.join(app_paths.download_root, 'music'))
    preferred_sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    quality_filters: list[str] = field(default_factory=lambda: list(QUALITY_TIERS))
    search_size_per_source: int = 4
    max_concurrent_searches: int = 8
    lyrics_font_family: str = ''
    lyrics_custom_font_path: str = ''
    lyrics_font_scale: float = 1.0
    lyrics_align: str = 'center'

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict) -> 'MusicSettings':
        data = dict(data)
        saved = data.get('preferred_sources')
        if isinstance(saved, list) and sorted(saved) == sorted(LEGACY_DEFAULT_SOURCES):
            # Untouched config from before the provider cleanup: move it to the
            # verified defaults and drop the old (slow) search sizing.
            data['preferred_sources'] = list(DEFAULT_SOURCES)
            data['search_size_per_source'] = 4
            data['max_concurrent_searches'] = 8
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

def load_settings() -> MusicSettings:
    if os.path.isfile(_MUSIC_SETTINGS_FILE):
        try:
            with open(_MUSIC_SETTINGS_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            return MusicSettings.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning('Failed to load music settings: %s — using defaults', exc)
    return MusicSettings()

def save_settings(s: MusicSettings) -> None:
    os.makedirs(_MUSIC_CFG_DIR, exist_ok=True)
    try:
        with open(_MUSIC_SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(s.to_dict(), fh, indent=2)
    except OSError as exc:
        logger.error('Failed to save music settings: %s', exc)
settings = load_settings()

def apply_settings(**kwargs) -> None:
    for key, value in kwargs.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    save_settings(settings)

music_settings, apply_music_settings = settings, apply_settings


class _MusicdlNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith('musicdl') and 'quark_parser_config' in record.getMessage():
            record.levelno = logging.DEBUG
            record.levelname = 'DEBUG'
            return logging.getLogger().isEnabledFor(logging.DEBUG)
        return True


logging.getLogger('musicdl').addFilter(_MusicdlNoiseFilter())


MUSICDL_WORK_DIR = os.path.join(app_paths.cache_dir, 'musicdl')

_THREADS_PER_SOURCE = 2

_lock = threading.Lock()
_source_locks: dict[str, threading.Lock] = {}
_source_clients: dict[str, object] = {}
_dirs_ready = False


def _sources_key(sources: list[str]) -> str:
    return '|'.join(sorted(sources))


@lru_cache(maxsize=1)
def available_sources() -> list[str]:
    from musicdl.modules import MusicClientBuilder
    return sorted(MusicClientBuilder.REGISTERED_MODULES.keys())


def _registered(sources: list[str]) -> list[str]:
    known = set(available_sources())
    return [s for s in sources if s in known]


def _prepare_dirs() -> None:
    global _dirs_ready
    with _lock:
        if _dirs_ready:
            return
        shutil.rmtree(MUSICDL_WORK_DIR, ignore_errors=True)
        os.makedirs(MUSICDL_WORK_DIR, exist_ok=True)
        atexit.register(shutil.rmtree, MUSICDL_WORK_DIR, True)
        _dirs_ready = True


def _client_cfg(source: str) -> dict:
    return {source: {'search_size_per_source': settings.search_size_per_source, 'work_dir': MUSICDL_WORK_DIR}}


def _get_source_client(source: str):
    size = settings.search_size_per_source
    key = f'{source}#{size}'
    client = _source_clients.get(key)
    if client is not None:
        return client
    _prepare_dirs()
    with _lock:
        source_lock = _source_locks.setdefault(key, threading.Lock())
    with source_lock:
        client = _source_clients.get(key)
        if client is None:
            from musicdl.musicdl import MusicClient
            client = MusicClient(music_sources=[source], init_music_clients_cfg=_client_cfg(source)).music_clients.get(source)
            for stale in [k for k in _source_clients if k.startswith(f'{source}#') and k != key]:
                _source_clients.pop(stale, None)
            _source_clients[key] = client
    return client


def _filter_by_quality(songs: list[Song]) -> list[Song]:
    allowed = set(settings.quality_filters)
    if not allowed or allowed >= set(('lossless', 'mp3', 'aac', 'other')):
        return songs
    return [s for s in songs if getattr(s, 'quality_tier', None) in allowed]


def _search_source(source: str, query: str, progress, progress_lock):
    client = _get_source_client(source)
    if client is None:
        return []


    return client.search(keyword=query, num_threadings=_THREADS_PER_SOURCE, main_process_context=progress, main_progress_lock=progress_lock) or []


def search_streaming(query: str, on_source_done, sources: list[str] | None=None, use_cache: bool=True, is_cancelled=None, per_source_timeout: float | None=None) -> list[Song]:
    sources = _registered(sources or settings.preferred_sources)
    if not sources:
        return []
    key = _sources_key(sources)
    if use_cache:
        cached = cache.load('music_search', f'{query}:{key}')
        if cached:
            songs = _filter_by_quality([Song.from_dict(d) for d in cached])
            on_source_done(songs)
            return songs
    from rich.progress import Progress
    progress, progress_lock = Progress(disable=True), threading.Lock()
    all_songs: list[Song] = []
    workers = max(1, min(8, len(sources), int(settings.max_concurrent_searches)))
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='musicdl-search')
    try:
        ordered = sorted(sources, key=lambda source: source in SLOW_OR_GATED_SOURCES)
        futures = {ex.submit(_search_source, s, query, progress, progress_lock): s for s in ordered}
        pending = set(futures)
        timeout = SEARCH_TIMEOUT_S if per_source_timeout is None else per_source_timeout
        deadline = time.monotonic() + timeout
        while pending:
            if is_cancelled is not None and is_cancelled():
                return _filter_by_quality(all_songs)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning('Search deadline reached after %.1fs; abandoning %d source(s): %s',
                               timeout, len(pending), ', '.join(sorted(futures[f] for f in pending)))
                break

            done, pending = wait(pending, timeout=min(0.5, remaining), return_when=FIRST_COMPLETED)
            for future in done:
                source = futures[future]
                try:
                    song_infos = future.result()
                except Exception as exc:
                    logger.warning('Search failed for source %s: %s', source, exc)
                    continue
                batch = [Song.from_song_info(source, si) for si in song_infos]
                if not batch:
                    continue
                all_songs.extend(batch)
                filtered = _filter_by_quality(batch)
                if filtered:
                    on_source_done(filtered)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if all_songs:
        cache.save('music_search', f'{query}:{key}', [s.to_dict() for s in all_songs])
    return _filter_by_quality(all_songs)


def search(query: str, sources: list[str] | None=None, use_cache: bool=True) -> list[Song]:
    return search_streaming(query, lambda _songs: None, sources=sources, use_cache=use_cache)


FALLBACK_COVER_SOURCES = ('NeteaseMusicClient', 'DeezerMusicClient', 'QQMusicClient', 'KuwoMusicClient')
_cover_cache: OrderedDict[str, str | None] = OrderedDict()

_cover_slots = threading.Semaphore(2)


def fetch_fallback_cover_url(song: Song) -> str | None:
    if getattr(song, 'cover_url', None):
        return song.cover_url
    query = f"{song.song_name or ''} {(song.singers or '').split(',')[0]}".strip()
    if not query:
        return None
    if query in _cover_cache:
        return _cover_cache[query]
    with _cover_slots:
        if query in _cover_cache:
            return _cover_cache[query]
        url = None
        for source in _registered(list(FALLBACK_COVER_SOURCES)):
            if source == getattr(song, 'source', None):
                continue
            try:
                url = next((r.cover_url for r in search(query, sources=[source]) if r.cover_url), None)
            except Exception:
                logger.warning('Fallback cover lookup failed via %s', source)
            if url:
                break
    _cover_cache[query] = url
    while len(_cover_cache) > 512:
        _cover_cache.popitem(last=False)
    return url


music_service = types.SimpleNamespace(
    available_sources=available_sources, search_streaming=search_streaming, search=search,
    fetch_fallback_cover_url=fetch_fallback_cover_url, _get_source_client=_get_source_client)


try:
    if sys.platform != 'win32':
        raise ImportError('Windows only')
    from winrt.windows.foundation import Uri
    from winrt.windows.media import MediaPlaybackStatus, MediaPlaybackType, SystemMediaTransportControlsButton as Button, SystemMediaTransportControlsTimelineProperties
    from winrt.windows.media.playback import MediaPlayer
    from winrt.windows.storage.streams import RandomAccessStreamReference
except ImportError as exc:
    MediaPlayer = None
    if sys.platform == 'win32':
        logger.warning('Media overlay disabled, missing winrt package: %s', exc)


class MediaControls(QObject):
    _pressed = Signal(object)
    _seek_requested = Signal(int)

    def __init__(self, player):
        super().__init__(player)
        self._p = player
        self._ref_pos, self._ref_t = 0, time.monotonic()

        self._mp = MediaPlayer()
        self._mp.command_manager.is_enabled = False
        self._c = self._mp.system_media_transport_controls
        self._c.is_enabled = True
        self._c.is_play_enabled = self._c.is_pause_enabled = True
        self._c.add_button_pressed(lambda s, a: self._pressed.emit(a.button))
        self._c.add_playback_position_change_requested(lambda s, a: self._seek_requested.emit(int(a.requested_playback_position.total_seconds() * 1000)))
        self._actions = {Button.PLAY: player.resume, Button.PAUSE: player.pause, Button.NEXT: player.next, Button.PREVIOUS: player.previous, Button.STOP: player.stop}
        self._status = {'playing': MediaPlaybackStatus.PLAYING, 'paused': MediaPlaybackStatus.PAUSED, 'stopped': MediaPlaybackStatus.STOPPED}
        self._pressed.connect(lambda b: self._actions.get(b, lambda: None)())
        self._seek_requested.connect(player.seek)
        player.song_changed.connect(self._on_song)
        player.state_changed.connect(self._on_state)
        player.queue_changed.connect(self._sync_buttons)
        player.duration_changed.connect(lambda _: self._sync_timeline())
        player.position_changed.connect(self._on_position)

    def _on_song(self, song) -> None:
        u = self._c.display_updater
        u.type = MediaPlaybackType.MUSIC
        m = u.music_properties
        m.title, m.artist, m.album_title = song.song_name or '', song.singers or '', song.album or ''
        try:
            u.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(song.cover_url)) if song.cover_url else None
        except Exception:
            logger.debug('Media overlay cover failed for %r', song.cover_url)
        u.update()
        self._sync_buttons()

    def _on_state(self, state: str) -> None:
        self._c.playback_status = self._status.get(state, MediaPlaybackStatus.STOPPED)
        self._sync_buttons()
        self._sync_timeline()

    def _sync_buttons(self) -> None:
        self._c.is_next_enabled = self._p.has_next
        self._c.is_previous_enabled = self._p.has_previous

    def _sync_timeline(self) -> None:
        dur = self._p.duration
        if dur <= 0:
            return
        self._ref_pos, self._ref_t = self._p.position, time.monotonic()
        t = SystemMediaTransportControlsTimelineProperties()
        t.start_time = t.min_seek_time = timedelta(0)
        t.end_time = t.max_seek_time = timedelta(milliseconds=dur)
        t.position = timedelta(milliseconds=self._ref_pos)
        self._c.update_timeline_properties(t)

    def _on_position(self, pos: int) -> None:
        expected = self._ref_pos + (time.monotonic() - self._ref_t) * 1000 * self._p.is_playing
        if abs(pos - expected) > 1500:
            self._sync_timeline()


def _attach_media_controls(player):
    if MediaPlayer is None:
        return None
    try:
        return MediaControls(player)
    except Exception:
        logger.warning('Media overlay init failed', exc_info=True)
        return None


_PLAYBACK_STATE_LABELS = {QMediaPlayer.PlaybackState.PlayingState: 'playing', QMediaPlayer.PlaybackState.PausedState: 'paused', QMediaPlayer.PlaybackState.StoppedState: 'stopped'}

class MusicPreviewPlayer(QObject):
    state_changed = Signal(str)
    position_changed = Signal(int)
    duration_changed = Signal(int)
    song_changed = Signal(object)
    queue_changed = Signal()
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self.position_changed.emit)
        self._player.durationChanged.connect(self.duration_changed.emit)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._current_song: Song | None = None
        self._queue: list[Song] = []
        self._urls: dict[str, str] = {}
        self._order: list[int] = []
        self._index = -1
        self._repeat = False
        self._shuffle = False
        self._media_controls = _attach_media_controls(self)

    def set_queue(self, songs: list[Song], urls: dict[str, str]) -> None:
        current = self._current_song
        self._queue = list(songs)
        self._urls.update(urls)
        self._rebuild_order(current)
        self.queue_changed.emit()

    def _rebuild_order(self, keep_song: Song | None=None) -> None:
        n = len(self._queue)
        self._order = list(range(n))
        if self._shuffle:
            random.shuffle(self._order)
        if keep_song is not None and keep_song in self._queue:
            song_idx = self._queue.index(keep_song)
            self._index = self._order.index(song_idx)
        else:
            self._index = -1

    def set_repeat(self, enabled: bool) -> None:
        self._repeat = enabled

    @property
    def repeat(self) -> bool:
        return self._repeat

    def set_shuffle(self, enabled: bool) -> None:
        self._shuffle = enabled
        self._rebuild_order(self._current_song)

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    def play_song(self, song: Song, url: str, queue: list[Song] | None=None, urls: dict[str, str] | None=None) -> None:
        if queue is not None:
            self.set_queue(queue, urls or {song.key: url})
        elif song not in self._queue:
            self._queue.append(song)
            self._rebuild_order(self._current_song)
        if song in self._queue:
            song_idx = self._queue.index(song)
            self._index = self._order.index(song_idx)
        self._urls[song.key] = url
        self._play_current()

    def _play_current(self) -> None:
        if not 0 <= self._index < len(self._order):
            return
        song = self._queue[self._order[self._index]]
        url = self._urls.get(song.key)
        if not url:
            self.error.emit('No preview available for this track')
            return
        self._current_song = song
        self._player.setSource(QUrl(url))
        self._player.play()
        self.song_changed.emit(song)

    def next(self) -> None:
        if not self._order:
            return
        nxt = self._index + 1
        if nxt >= len(self._order):
            if not self._repeat:
                return
            nxt = 0
        self._index = nxt
        self._play_current()

    def previous(self) -> None:
        if not self._order:
            return
        prv = self._index - 1
        if prv < 0:
            if not self._repeat:
                prv = 0
            else:
                prv = len(self._order) - 1
        self._index = prv
        self._play_current()

    @property
    def has_next(self) -> bool:
        return self._index + 1 < len(self._order) or self._repeat

    @property
    def has_previous(self) -> bool:
        return self._index > 0 or self._repeat

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.play()

    def toggle_play_pause(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self.resume()

    def stop(self) -> None:
        self._player.stop()
        self._current_song = None

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(position_ms)

    def set_volume(self, volume: float) -> None:
        self._audio_output.setMuted(False)
        self._audio_output.setVolume(max(0.0, min(1.0, volume)))

    def set_muted(self, muted: bool) -> None:
        self._audio_output.setMuted(muted)

    @property
    def is_muted(self) -> bool:
        return self._audio_output.isMuted()

    @property
    def volume(self) -> float:
        return self._audio_output.volume()

    @property
    def position(self) -> int:
        return self._player.position()

    @property
    def duration(self) -> int:
        return self._player.duration()

    @property
    def current_song(self) -> Song | None:
        return self._current_song

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _on_state_changed(self, state) -> None:
        self.state_changed.emit(_PLAYBACK_STATE_LABELS.get(state, 'stopped'))

        try:
            from src.gui.settings_page import get_sound_manager
            get_sound_manager().set_media_playing('music', state == QMediaPlayer.PlaybackState.PlayingState, owner=self)
        except Exception:
            logger.debug('Ambiance ducking update failed', exc_info=True)

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.has_next:
                self.next()
            else:
                self.stop()

    def _on_error(self, error, error_string: str) -> None:
        logger.warning('Music preview player error: %s', error_string)
        self.error.emit(error_string)


def close_http_client() -> None:
    try:
        _http.close()
    except Exception:
        pass


def _t(key: str, default: str) -> str:
    try:
        v = tr(key)
    except Exception:
        return default
    return default if not v or v == key else v


def _format_ms(ms: int) -> str:
    s = max(0, ms) // 1000
    return f'{s // 60}:{s % 60:02d}'


def _format_source(src: str) -> str:
    for suf in ('MusicClient', 'Client'):
        if src and src.endswith(suf):
            return src[:-len(suf)] or src
    return src or ''


def _first_artist(song) -> str:
    return (song.singers or '').split(',')[0].strip()


def _playable(song) -> bool:
    return isinstance(song.download_url, str) and bool(song.download_url)


def _parse_lrc(text: str | None) -> list[tuple[int, str]]:
    out = []
    for line in (text or '').splitlines():
        ms = list(_LRC_RE.finditer(line))
        if ms:
            body = _LRC_RE.sub('', line).strip()
            out += [(int((int(m.group(1)) * 60 + float(m.group(2))) * 1000), body) for m in ms]
    out.sort(key=lambda x: x[0])
    return out


def _fetch_lrc_text(song) -> str:
    try:
        artist = _first_artist(song)
        params = {'track_name': song.song_name or '', 'artist_name': artist}
        if song.album:
            params['album_name'] = song.album
        if song.duration_s:
            params['duration'] = song.duration_s
        r = _http.get('https://lrclib.net/api/get', params=params, timeout=8, headers=_LRCLIB_HEADERS)
        if r.status_code == 200:
            data = r.json()
        else:
            r = _http.get('https://lrclib.net/api/search', timeout=8, headers=_LRCLIB_HEADERS,
                          params={'track_name': params['track_name'], 'artist_name': artist})
            r.raise_for_status()
            res = r.json()
            data = res[0] if res else None
        if data:
            return data.get('syncedLyrics') or data.get('plainLyrics') or ''
    except Exception as exc:
        logger.debug('Lyrics fetch failed for %r: %s', song.song_name, exc)
    return ''


class _LrcSignals(QObject):
    finished = Signal(str, str)


class _LrcTask(QRunnable):
    def __init__(self, song):
        super().__init__()
        self.song, self.signals = song, _LrcSignals()

    def run(self) -> None:
        self.signals.finished.emit(self.song.key, _fetch_lrc_text(self.song))


def _unique_path(directory: str, base: str, ext: str) -> str:
    path, n = os.path.join(directory, base + ext), 1
    while os.path.exists(path):
        path = os.path.join(directory, f'{base} ({n}){ext}')
        n += 1
    return path


def _fetch_cover(song) -> tuple[bytes | None, str]:
    if not getattr(song, 'cover_url', None):
        return None, ''
    try:
        r = _http.get(song.cover_url, timeout=10)
        r.raise_for_status()
        return r.content, 'image/png' if r.content[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
    except Exception:
        return None, ''


def _embed_metadata(path: str, song, lrc: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    title, artist, album = song.song_name or '', song.singers or '', song.album or ''
    cover, mime = _fetch_cover(song)
    try:
        if ext == '.mp3':
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1, USLT
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            for cls, v in ((TIT2, title), (TPE1, artist), (TALB, album)):
                if v:
                    tags.setall(cls.__name__, [cls(encoding=3, text=v)])
            if lrc:
                tags.delall('USLT')
                tags.add(USLT(encoding=3, lang='eng', desc='', text=lrc))
            if cover:
                tags.delall('APIC')
                tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=cover))
            tags.save(path)
        elif ext in ('.flac', '.ogg'):
            from mutagen.flac import FLAC, Picture
            from mutagen.oggvorbis import OggVorbis
            audio = FLAC(path) if ext == '.flac' else OggVorbis(path)
            for k, v in (('title', title), ('artist', artist), ('album', album), ('LYRICS', lrc)):
                if v:
                    audio[k] = v
            if cover:
                pic = Picture()
                pic.data, pic.type, pic.mime, pic.desc = cover, 3, mime, 'Cover'
                if ext == '.flac':
                    audio.clear_pictures()
                    audio.add_picture(pic)
                else:
                    audio['metadata_block_picture'] = [base64.b64encode(pic.write()).decode('ascii')]
            audio.save()
        elif ext in ('.m4a', '.mp4', '.aac'):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(path)
            for k, v in (('©nam', title), ('©ART', artist), ('©alb', album), ('©lyr', lrc)):
                if v:
                    audio[k] = [v]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
                audio['covr'] = [MP4Cover(cover, imageformat=fmt)]
            audio.save()
    except Exception:
        logger.exception('Failed to embed metadata into %s', path)


def _match_key(song) -> tuple[str, str]:
    norm = lambda s: re.sub(r'\s+', ' ', s.strip().lower())
    return norm(song.song_name or ''), norm(_first_artist(song))


class _DlSignals(QObject):
    progress = Signal(int, int)
    finished = Signal(bool, str, str)


_AUTH_ERRORS = (401, 403, 404, 410)


def _site_referer(url: str) -> str:
    parts = (urlparse(url).hostname or '').split('.')
    return f'https://www.{".".join(parts[-2:])}/' if len(parts) >= 2 else ''


def _client_headers(song) -> dict:
    headers = getattr(song, 'download_headers', None)
    if isinstance(headers, dict) and headers:
        return dict(headers)
    try:
        client = music_service._get_source_client(song.source)
        return dict(getattr(client, 'default_download_headers', None) or {})
    except Exception:
        return {}


def _client_cookies(song) -> dict:
    cookies = getattr(song, 'download_cookies', None)
    return dict(cookies) if isinstance(cookies, dict) else {}


def _refresh_url(song) -> str | None:
    try:
        res = music_service.search(f"{song.song_name or ''} {_first_artist(song)}".strip(),
                                   sources=[song.source], use_cache=False)
        key = _match_key(song)
        for r in res:
            if _match_key(r) == key and _playable(r):
                song.download_url = r.download_url
                if getattr(r, 'download_headers', None):
                    song.download_headers = dict(r.download_headers)
                if getattr(r, 'download_cookies', None):
                    song.download_cookies = dict(r.download_cookies)
                return r.download_url
    except Exception as exc:
        logger.debug('URL refresh failed for %r: %s', song.song_name, exc)
    return None


class _DownloadTask(QRunnable):
    def __init__(self, songs: list, dest: str):
        super().__init__()
        self.songs, self.dest, self.signals, self._cancelled = songs, dest, _DlSignals(), False

    def cancel(self) -> None:
        self._cancelled = True

    def _save(self, song, url: str, headers: dict, cookies: dict | None = None) -> str:
        tmp = None
        try:
            with _http.stream('GET', url, headers=headers, cookies=cookies or None, timeout=30) as r:
                r.raise_for_status()
                ext = os.path.splitext(url.split('?')[0])[1].lower()
                if ext not in _AUDIO_EXT:
                    ext = _MIME_EXT.get(r.headers.get('Content-Type', '').split(';')[0].strip().lower(), '')
                if ext not in _AUDIO_EXT:
                    ext = f'.{(getattr(song, "ext", "") or "").lower().lstrip(".")}'
                if ext not in _AUDIO_EXT:
                    ext = '.mp3'
                artist = _first_artist(song)
                name = f'{artist} - {song.song_name}' if artist else song.song_name or 'Unknown'
                name = re.sub(r'[\\/:*?"<>|]', '_', name).strip().strip('.') or 'Unknown'
                os.makedirs(self.dest, exist_ok=True)
                final = _unique_path(self.dest, name, ext)
                tmp = final + '.part'
                total, done = int(r.headers.get('Content-Length', 0) or 0), 0
                with open(tmp, 'wb') as fh:
                    for chunk in r.iter_bytes(262144):
                        if self._cancelled:
                            raise RuntimeError('cancelled')
                        fh.write(chunk)
                        done += len(chunk)
                        self.signals.progress.emit(done, total)
            os.replace(tmp, final)
            return final
        except BaseException:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _fetch(self, song) -> str:
        url, refreshed, err = song.download_url, False, None
        while True:
            for headers, cookies in ((lambda: {}, lambda: {}),
                                     (lambda u=url: {'Referer': _site_referer(u)}, lambda: {}),
                                     (lambda: _client_headers(song), lambda: _client_cookies(song))):
                try:
                    return self._save(song, url, headers(), cookies())
                except httpx.HTTPStatusError as exc:
                    err = exc
                    if exc.response.status_code not in _AUTH_ERRORS:
                        raise
            fresh = None if refreshed else _refresh_url(song)
            refreshed = True
            if not fresh or fresh == url:
                raise err
            url = fresh

    def run(self) -> None:
        cands = [s for s in self.songs if _playable(s)]
        if not cands:
            self.signals.finished.emit(False, '', 'No download URL available for this track.')
            return
        err = ''
        for song in cands:
            if self._cancelled:
                break
            try:
                final = self._fetch(song)
            except Exception as exc:
                if self._cancelled:
                    break
                err = f'HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else str(exc)[:300]
                logger.warning('Download failed for %r via %s (%s)', song.song_name, song.source, err)
                continue
            if not getattr(song, 'cover_url', None):
                try:
                    song.cover_url = music_service.fetch_fallback_cover_url(song) or song.cover_url
                except Exception:
                    pass
            _embed_metadata(final, song, _fetch_lrc_text(song))
            self.signals.finished.emit(True, final, '')
            return
        self.signals.finished.emit(False, '', 'Cancelled' if self._cancelled else err or 'All sources failed.')


class Playlist:
    __slots__ = ('id', 'name', 'songs')

    def __init__(self, id: str, name: str, songs: list | None = None):
        self.id, self.name, self.songs = id, name, songs if songs is not None else []


class PlaylistStore(QObject):
    playlists_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pl: dict[str, Playlist] = {}
        try:
            with open(_PLAYLISTS_FILE, 'r', encoding='utf-8') as fh:
                entries = json.load(fh).get('playlists', [])
        except (OSError, json.JSONDecodeError):
            entries = []
        for e in entries:
            try:
                pid = e.get('id') or uuid.uuid4().hex
                self._pl[pid] = Playlist(pid, e.get('name') or _t('music.untitled', 'Untitled'),
                                         [Song.from_dict(d) for d in e.get('songs', [])])
            except Exception:
                logger.exception('Failed to parse a saved playlist entry')

    def _commit(self) -> None:
        try:
            os.makedirs(_PLAYLISTS_DIR, exist_ok=True)
            payload = {'playlists': [{'id': p.id, 'name': p.name, 'songs': [s.to_dict() for s in p.songs]}
                                     for p in self._pl.values()]}
            with open(_PLAYLISTS_FILE + '.tmp', 'w', encoding='utf-8') as fh:
                json.dump(payload, fh)
            os.replace(_PLAYLISTS_FILE + '.tmp', _PLAYLISTS_FILE)
        except OSError as exc:
            logger.error('Failed to save music playlists: %s', exc)
        self.playlists_changed.emit()

    def all_playlists(self) -> list[Playlist]:
        return list(self._pl.values())

    def get(self, pid) -> Playlist | None:
        return self._pl.get(pid)

    def create(self, name: str) -> Playlist:
        pl = Playlist(uuid.uuid4().hex, (name or '').strip() or _t('music.untitled_playlist', 'Untitled playlist'))
        self._pl[pl.id] = pl
        self._commit()
        return pl

    def rename(self, pid: str, name: str) -> None:
        if pid in self._pl and name.strip():
            self._pl[pid].name = name.strip()
            self._commit()

    def delete(self, pid: str) -> None:
        if self._pl.pop(pid, None) is not None:
            self._commit()

    def add_song(self, pid: str, song) -> bool:
        pl = self._pl.get(pid)
        if pl is None or any(s.key == song.key for s in pl.songs):
            return False
        pl.songs.append(song)
        self._commit()
        return True

    def remove_song(self, pid: str, key: str) -> None:
        pl = self._pl.get(pid)
        if pl is not None:
            pl.songs = [s for s in pl.songs if s.key != key]
            self._commit()


_icon_cache: dict = {}


def icon_pm(name: str, color: str = '#FFFFFF', size: int = 20) -> QPixmap:
    key = (name, color, size)
    pm = _icon_cache.get(key)
    if pm is None:
        pm = QPixmap(size * 2, size * 2)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        QSvgRenderer(QByteArray(_SVG[name].encode())).render(p)
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(pm.rect(), QColor(color))
        p.end()
        pm.setDevicePixelRatio(2)
        _icon_cache[key] = pm
    return pm


def _fg() -> QColor:
    return QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0)


def _dim(alpha: float = 0.62) -> QColor:
    c = _fg()
    c.setAlphaF(alpha)
    return c


def _tone(c: QColor) -> QColor:
    h, s, l, _ = c.getHsl()
    return QColor.fromHsl(max(h, 0), min(s, 210), min(l, 84))


def _vivid(colors: list) -> QColor:
    return max(colors, key=lambda c: c.getHsv()[1] * c.getHsv()[2]) if colors else QColor('#3a2a4a')


def _placeholder(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor('#4a3a63'))
    g.setColorAt(1, QColor('#2a2038'))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.fillRect(pm.rect(), g)
    f = QFont()
    f.setPixelSize(size // 2)
    p.setFont(f)
    p.setPen(QColor(255, 255, 255, 110))
    p.drawText(pm.rect(), Qt.AlignCenter, '♪')
    p.end()
    return pm


def _font(px: int, weight=QFont.Normal, family: str = '') -> QFont:
    f = QFont(family) if family else QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


_ph_cache: dict = {}


def _ph(size: int) -> QPixmap:
    return _ph_cache.setdefault(size, _placeholder(size))


class ElidedLabel(QLabel):
    def __init__(self, px=14, weight=QFont.Normal, color=None, parent=None):
        super().__init__(parent)
        self._text, self._color = '', QColor('white' if color is None else color)
        self._font = QFont(self.font())
        self._font.setPixelSize(px)
        self._font.setWeight(weight)
        self.setFixedHeight(QFontMetrics(self._font).height() + 2)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def setText(self, t: str) -> None:
        self._text = t or ''
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(self._font)
        p.setPen(self._color)
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(self._font).elidedText(self._text, Qt.ElideRight, self.width()))


class ClickWidget(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class IconButton(QToolButton):
    def __init__(self, name: str, size=40, px=20, rotate=0, parent=None):
        super().__init__(parent)
        self._name, self._px, self._rot = name, px, rotate
        self._hover = self._down = False
        self.tint, self.fill, self.accent = QColor('white'), None, QColor('white')
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def setIconName(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.update()

    def set_colors(self, tint, fill=None, accent=None) -> None:
        self.tint, self.fill = QColor(tint), QColor(fill) if fill else None
        if accent is not None:
            self.accent = QColor(accent)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = self._down = False
        self.update()

    def mousePressEvent(self, e):
        self._down = True
        self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._down = False
        self.update()
        super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        if self.fill is not None:
            c = self.fill.lighter(108) if self._hover else QColor(self.fill)
            p.setBrush(c.darker(108) if self._down else c)
            p.drawEllipse(r)
        elif self._hover or self._down:
            p.setBrush(QColor(255, 255, 255, 46 if self._down else 28))
            p.drawEllipse(r)
        checked = self.isCheckable() and self.isChecked()
        if not self.isEnabled():
            p.setOpacity(0.35)
        elif self.isCheckable() and not checked:
            p.setOpacity(0.55)
        if self._rot:
            p.translate(self.width() / 2, self.height() / 2)
            p.rotate(self._rot)
            p.translate(-self.width() / 2, -self.height() / 2)
        color = self.accent if checked else self.tint
        p.drawPixmap((self.width() - self._px) // 2, (self.height() - self._px) // 2,
                     icon_pm(self._name, color.name(), self._px))


class _HollowStyle(QProxyStyle):
    def __init__(self):
        super().__init__()
        self.h = 4
        self.size = QSize(16, 16)

    def subControlRect(self, cc, opt, sc, widget):
        if cc != QStyle.CC_Slider or sc == QStyle.SC_SliderTickmarks:
            return super().subControlRect(cc, opt, sc, widget)
        rect = opt.rect
        if sc == QStyle.SC_SliderGroove:
            return QRect(0, (rect.height() - self.h) // 2, rect.width(), self.h)
        if sc == QStyle.SC_SliderHandle:
            x = QStyle.sliderPositionFromValue(opt.minimum, opt.maximum, opt.sliderPosition, rect.width())
            x = int(x * (rect.width() - self.size.width()) / max(1, rect.width()))
            return QRect(x, (rect.height() - self.size.height()) // 2, self.size.width(), self.size.height())
        return super().subControlRect(cc, opt, sc, widget)

    def drawComplexControl(self, cc, opt, painter, widget):
        if cc != QStyle.CC_Slider:
            return super().drawComplexControl(cc, opt, painter, widget)
        g = self.subControlRect(cc, opt, QStyle.SC_SliderGroove, widget)
        hd = self.subControlRect(cc, opt, QStyle.SC_SliderHandle, widget)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        cx = hd.center().x()
        accent = getattr(widget, 'accent', QColor('white'))
        painter.setBrush(accent)
        painter.drawRoundedRect(QRectF(g.x(), g.y(), max(0, cx - g.x()), self.h), 2, 2)
        painter.setBrush(QColor(255, 255, 255, 70))
        painter.drawRoundedRect(QRectF(cx, g.y(), max(0, g.right() - cx), self.h), 2, 2)
        painter.setBrush(accent)
        painter.drawEllipse(QPoint(cx, hd.center().y()), 8 if widget.isSliderDown() else 7, 8 if widget.isSliderDown() else 7)


_HOLLOW = _HollowStyle()


class HollowSlider(QSlider):
    seeked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.accent = QColor('white')
        self.setStyle(_HOLLOW)
        self.setFixedHeight(22)
        self.setCursor(Qt.PointingHandCursor)

    def _val(self, e) -> int:
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()), max(1, self.width()))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._val(e))
            self.sliderMoved.emit(self.value())

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setValue(self._val(e))
            self.sliderMoved.emit(self.value())

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self.setSliderDown(False)
            self.seeked.emit(self.value())


class Cover(QWidget):
    loaded = Signal()

    def __init__(self, size: int, radius: int = 6, parent=None):
        super().__init__(parent)
        self._r, self._url, self._token, self.pm = radius, None, 0, QPixmap()
        self.setFixedSize(size, size)

    def set_song(self, song) -> None:
        self._token += 1
        self.set_url(None)
        if song is None:
            return
        if song.cover_url:
            self.set_url(song.cover_url)
        else:
            tok = self._token
            submit(music_service.fetch_fallback_cover_url, args=(song,),
                   on_done=lambda url, s=song, t=tok: self._fallback(s, url, t))

    def _fallback(self, song, url, tok) -> None:
        if url:
            song.cover_url = url
            if tok == self._token:
                self.set_url(url)

    def set_url(self, url) -> None:
        self._url = url
        if url:
            images.get(url, (self.width(), self.height()), partial(self._got, url), owner=self)
        else:
            self.pm = QPixmap()
            self.update()

    def _got(self, url, pm) -> None:
        if url == self._url and not pm.isNull():
            self.pm = pm
            self.update()
            self.loaded.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._r, self._r)
        p.setClipPath(path)
        p.drawPixmap(self.rect(), self.pm if not self.pm.isNull() else _ph(self.width()))


def _columns(w: int) -> tuple:
    pad, dur, meta = 20, 60, (120 if w >= 640 else 0)
    rest, wide = w - 2 * pad - dur - meta, w >= 760
    nw, aw = int(rest * (0.42 if wide else 0.6)), int(rest * (0.29 if wide else 0.4))
    alw = rest - nw - aw if wide else 0
    ax, alx = pad + nw, pad + nw + aw
    mx = alx + alw
    return (pad, nw), (ax, aw), (alx, alw), (mx, meta), (mx + meta, dur)


class _Delegate(QStyledItemDelegate):
    def setHoverRow(self, *_): pass
    def setPressedRow(self, *_): pass
    def setSelectedRows(self, *_): pass


_ICON = {'play': 'l_play', 'add': 'l_add', 'dl': 'l_dl', 'rm': 'l_rm'}


class SongDelegate(_Delegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._title, self._body, self._pill = _font(14, QFont.DemiBold), _font(13), _font(11)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), _ROW_H)

    def paint(self, p, opt, idx):
        song = idx.data(Qt.UserRole)
        r = opt.rect
        hover = bool(opt.state & QStyle.State_MouseOver)
        playing = song.key == self.view.playing_key
        cols = _columns(r.width())
        accent = themeColor()
        p.save()
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.setPen(Qt.NoPen)
        if hover:
            p.setBrush(QColor(255, 255, 255, 14) if isDarkTheme() else QColor(0, 0, 0, 10))
            p.drawRoundedRect(QRectF(r).adjusted(4, 2, -4, -2), 6, 6)
        if playing:
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(r.x() + 5, r.y() + 14, 3, r.height() - 28), 1.5, 1.5)

        def cell(i, text, font, color, align=Qt.AlignLeft, shrink=0):
            x, w = cols[i]
            rect = QRect(r.x() + x, r.y(), max(0, w - 12 - shrink), r.height())
            p.setFont(font)
            p.setPen(color)
            p.drawText(rect, Qt.AlignVCenter | align, QFontMetrics(font).elidedText(text, Qt.ElideRight, rect.width()))

        btns = self.view.button_rects(r, hover)
        shrink = cols[0][1] - (min(b.x() for b in btns.values()) - r.x() - cols[0][0]) if btns else 0
        cell(0, song.song_name or '', self._title, accent if playing else _fg(), shrink=shrink)
        cell(1, song.singers or '', self._body, _dim())
        if cols[2][1]:
            cell(2, song.album or '', self._body, _dim())
        if cols[3][1]:
            mx, end = r.x() + cols[3][0], r.x() + cols[3][0] + cols[3][1]
            fm = QFontMetrics(self._pill)
            p.setFont(self._pill)
            if song.quality_label:
                tw = fm.horizontalAdvance(song.quality_label) + 12
                box = QRectF(mx, r.center().y() - 9, tw, 18)
                p.setPen(_dim(0.4))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box, 9, 9)
                p.setPen(_dim(0.8))
                p.drawText(box, Qt.AlignCenter, song.quality_label)
                mx += tw + 6
            p.setPen(_dim(0.5))
            p.drawText(QRect(mx, r.y(), max(0, end - mx), r.height()), Qt.AlignVCenter,
                       fm.elidedText(_format_source(song.source), Qt.ElideRight, max(0, end - mx)))
        cell(4, song.duration or '', self._body, _dim(), Qt.AlignRight)
        for name, br in btns.items():
            col = accent.name() if name == 'play' and playing else _fg().name()
            p.drawPixmap(br.x() + (br.width() - 16) // 2, br.y() + (br.height() - 16) // 2, icon_pm(_ICON[name], col, 16))
        p.restore()


def _song_menu(view, song, global_pos) -> None:
    m = RoundMenu(parent=view)
    m.addAction(Action(FluentIcon.PLAY, _t('music.play', 'Play'), triggered=lambda: view.play.emit(song)))
    if view.removable:
        m.addAction(Action(FluentIcon.DELETE, _t('music.remove', 'Remove'), triggered=lambda: view.remove.emit(song)))
    else:
        m.addAction(Action(FluentIcon.ADD_TO, tr('music.save_to_playlist'), triggered=lambda: view.add.emit(song)))
    m.addAction(Action(FluentIcon.DOWNLOAD, _t('music.download', 'Download'), triggered=lambda: view.download.emit(song)))
    m.exec(global_pos)


class _SongView:
    def _init_view(self, removable: bool) -> None:
        self.removable, self.playing_key = removable, None
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setStyleSheet('QListWidget{background:transparent;border:none;}')

    def songs(self) -> list:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def set_songs(self, songs: list) -> None:
        self.clear()
        self.add_songs(songs)

    def add_songs(self, songs: list) -> None:
        self.setUpdatesEnabled(False)
        for s in songs:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, s)
            self.addItem(it)
        self.setUpdatesEnabled(True)

    def set_playing(self, key) -> None:
        self.playing_key = key
        self.viewport().update()

    def _hit(self, pos):
        it = self.itemAt(pos)
        if it is None:
            return None, None
        for n, r in self.button_rects(self.visualItemRect(it), True).items():
            if r.contains(pos):
                return it.data(Qt.UserRole), n
        return it.data(Qt.UserRole), None

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            song, name = self._hit(e.position().toPoint())
            if name:
                {'play': self.play, 'add': self.add, 'dl': self.download, 'rm': self.remove}[name].emit(song)
                return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        song, _ = self._hit(e.position().toPoint())
        if song is not None:
            self.play.emit(song)

    def contextMenuEvent(self, e):
        it = self.itemAt(e.pos())
        if it is None:
            return
        _song_menu(self, it.data(Qt.UserRole), e.globalPos())


class SongList(_SongView, ListWidget):
    play, add, download, remove = Signal(object), Signal(object), Signal(object), Signal(object)

    def __init__(self, removable=False, parent=None):
        ListWidget.__init__(self, parent)
        self.setItemDelegate(SongDelegate(self))
        self._init_view(removable)

    def button_rects(self, row: QRect, hover: bool) -> dict:
        if not hover:
            return {}
        nx, nw = _columns(row.width())[0]
        names = ['play', 'rm' if self.removable else 'add', 'dl']
        x0 = row.x() + nx + nw - 12 - 28 * len(names)
        return {n: QRect(x0 + 28 * i, row.center().y() - 14, 28, 28) for i, n in enumerate(names)}


class _CoverGrid(PosterGrid):
    MIN_W = 190
    _HOVER_CURSOR = Qt.CursorShape.ArrowCursor

    def __init__(self, key_attr: str, title_attr: str, parent=None):
        super().__init__(parent, cell_w=_CARD_W, poster_h=_COVER, cover_attrs=('cover_url',), key_attr=key_attr,
                         title_attr=title_attr, tooltip_fn=lambda _entry: '', img_size=(240, 240),
                         cell_h=_COVER + 76, side=0, top=0)
        self.card_w, self.cover_px = _CARD_W, _COVER

    def _grid_metrics(self, avail: int) -> tuple:
        w = max(self.MIN_W, avail - 2)
        cols = max(1, w // self.MIN_W)
        self.card_w = w // cols
        self.cover_px = self.card_w - 12
        return cols, 0, 0, self.card_w, self.cover_px + 76, self.cover_px + 76

    def _target(self, pos: int) -> float:
        return 0.0

    def _paint_art(self, p: QPainter, pos: int, cell: QRect, shade: int, hover: bool) -> QRectF:
        cover = QRectF(cell.x() + 6, cell.y() + 6, self.cover_px, self.cover_px)
        src = self._src(pos)
        pixmap = images.peek(src, self._img) if src else None
        path = QPainterPath()
        path.addRoundedRect(cover, 8, 8)
        p.setClipPath(path)
        p.drawPixmap(cover.toRect(), pixmap if pixmap is not None and not pixmap.isNull() else _ph(240))
        if hover:
            p.fillRect(cover, QColor(0, 0, 0, shade))
        p.setClipping(False)
        return cover


class SongGrid(_CoverGrid):
    play, add, download, remove = Signal(object), Signal(object), Signal(object), Signal(object)
    removable = False

    def __init__(self, parent=None):
        super().__init__('key', 'song_name', parent)
        self.playing_key = None
        self._title, self._sub = _font(14, QFont.DemiBold), _font(12)
        self.context_requested.connect(lambda song, pos: _song_menu(self, song, pos))

    def songs(self) -> list:
        return list(self._entries)

    def set_songs(self, songs: list) -> None:
        self.set_entries(songs)

    add_songs = _CoverGrid.append_entries
    count = _CoverGrid.count_entries

    def set_playing(self, key) -> None:
        self.playing_key = key
        self.viewport().update()

    def button_rects(self, row: QRect, hover: bool) -> dict:
        if not hover:
            return {}
        d, gap, cp = 38, 8, self.cover_px
        x0 = row.x() + 6 + (cp - 3 * d - 2 * gap) // 2
        y = row.y() + 6 + (cp - d) // 2
        return {n: QRect(x0 + i * (d + gap), y, d, d) for i, n in enumerate(('play', 'add', 'dl'))}

    def _paint_card(self, p: QPainter, pos: int, cell: QRect) -> None:
        song = self._entry(pos)
        hover = pos == self._hover
        playing = song.key == self.playing_key
        accent = themeColor()
        cp = self.cover_px
        p.save()
        cover = self._paint_art(p, pos, cell, 100, hover)
        if playing:
            p.setPen(QPen(accent, 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(cover.adjusted(1, 1, -1, -1), 8, 8)
        for name, br in self.button_rects(cell, hover).items():
            p.setPen(QColor(255, 255, 255, 230))
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawEllipse(br)
            icon, px = ('Play', 20) if name == 'play' else (_ICON[name], 16)
            p.drawPixmap(br.x() + (br.width() - px) // 2, br.y() + (br.height() - px) // 2, icon_pm(icon, '#FFFFFF', px))
        y = cell.y() + cp + 14
        for text, font, color, dy in (
                (song.song_name or '', self._title, accent if playing else _fg(), 0),
                (song.singers or '', self._sub, _dim(0.6), 22),
                (' · '.join(x for x in (song.quality_label, song.duration, _format_source(song.source)) if x),
                 self._sub, _dim(0.4), 40)):
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRect(cell.x() + 6, y + dy, cp, 22 if dy == 0 else 18), Qt.AlignVCenter,
                       QFontMetrics(font).elidedText(text, Qt.ElideRight, cp))
        p.restore()

    def mouseDoubleClickEvent(self, e) -> None:
        pos = self._hit(e.position().toPoint())
        if pos is not None:
            self.play.emit(self._entry(pos))


class ListHeader(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)

    def paintEvent(self, _):
        p = QPainter(self)
        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(_dim(0.5))
        heads = [_t('music.col_title', 'Title'), _t('music.col_artist', 'Artist'), _t('music.col_album', 'Album'),
                 _t('music.col_quality', 'Quality'), _t('music.col_time', 'Time')]
        for i, ((x, w), t) in enumerate(zip(_columns(self.width()), heads, strict=True)):
            if w:
                p.drawText(QRect(x, 0, w - 12, self.height()),
                           Qt.AlignVCenter | (Qt.AlignRight if i == 4 else Qt.AlignLeft), t)
        p.setPen(_dim(0.12))
        p.drawLine(16, self.height() - 1, self.width() - 16, self.height() - 1)


class PlaylistGrid(_CoverGrid):
    open, play, rename, delete = Signal(str), Signal(str), Signal(str), Signal(str)

    def __init__(self, parent=None):
        super().__init__('id', 'name', parent)
        self.context_requested.connect(self._menu)

    def set_playlists(self, pls: list) -> None:
        self.set_entries(pls)

    def play_rect(self, r: QRect) -> QRect:
        return QRect(r.x() + 6 + (self.cover_px - 44) // 2, r.y() + 6 + (self.cover_px - 44) // 2, 44, 44)

    def _src(self, pos: int) -> str:
        return next((s.cover_url for s in self._entry(pos).songs if s.cover_url), '')

    def _paint_card(self, p: QPainter, pos: int, cell: QRect) -> None:
        pl = self._entry(pos)
        hover = pos == self._hover
        cp = self.cover_px
        p.save()
        self._paint_art(p, pos, cell, 90, hover)
        if hover:
            c = self.play_rect(cell)
            p.setPen(QColor(255, 255, 255, 230))
            p.setBrush(QColor(0, 0, 0, 80))
            p.drawEllipse(c)
            p.drawPixmap(c.x() + 11, c.y() + 11, icon_pm('Play', '#FFFFFF', 22))
        f = _font(14, QFont.DemiBold)
        p.setFont(f)
        p.setPen(_fg())
        p.drawText(QRect(cell.x() + 6, cell.y() + cp + 14, cp, 22), Qt.AlignVCenter,
                   QFontMetrics(f).elidedText(pl.name, Qt.ElideRight, cp))
        p.setFont(_font(12))
        p.setPen(_dim(0.55))
        p.drawText(QRect(cell.x() + 6, cell.y() + cp + 36, cp, 20), Qt.AlignVCenter,
                   _t('music.n_songs', '{n} songs').format(n=len(pl.songs)))
        p.restore()

    def _menu(self, playlist, global_pos) -> None:
        m = RoundMenu(parent=self)
        m.addAction(Action(FluentIcon.PLAY, _t('music.play', 'Play'), triggered=lambda: self.play.emit(playlist.id)))
        m.addAction(Action(FluentIcon.EDIT, _t('music.rename', 'Rename'), triggered=lambda: self.rename.emit(playlist.id)))
        m.addAction(Action(FluentIcon.DELETE, _t('music.delete', 'Delete'), triggered=lambda: self.delete.emit(playlist.id)))
        m.exec(global_pos)


class NameDialog(MessageBoxBase):
    def __init__(self, title: str, text: str = '', parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        self.edit = LineEdit(self)
        self.edit.setPlaceholderText(tr('music.playlist_name_placeholder'))
        self.edit.setText(text)
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self.yesButton.click)
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(340)

    def text(self) -> str:
        return self.edit.text().strip()


class SaveToPlaylistDialog(MessageBoxBase):
    def __init__(self, store: PlaylistStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.viewLayout.addWidget(SubtitleLabel(tr('music.save_to_playlist'), self))
        self.list = ListWidget(self)
        self.list.setFixedHeight(220)
        self.list.itemDoubleClicked.connect(lambda _: self.yesButton.click())
        self.viewLayout.addWidget(self.list)
        row = QHBoxLayout()
        self.new_edit = LineEdit(self)
        self.new_edit.setPlaceholderText(tr('music.playlist_name_placeholder'))
        self.new_edit.returnPressed.connect(self._create)
        btn = PushButton(FluentIcon.ADD, tr('music.new_playlist'), self)
        btn.clicked.connect(self._create)
        row.addWidget(self.new_edit, 1)
        row.addWidget(btn)
        self.viewLayout.addLayout(row)
        self.widget.setMinimumWidth(380)
        self._fill()

    def _fill(self, select=None) -> None:
        self.list.clear()
        for pl in self.store.all_playlists():
            it = QListWidgetItem(f'{pl.name}  ·  {len(pl.songs)}')
            it.setData(Qt.UserRole, pl.id)
            self.list.addItem(it)
            if pl.id == select:
                self.list.setCurrentItem(it)

    def _create(self) -> None:
        name = self.new_edit.text().strip()
        if name:
            self._fill(self.store.create(name).id)
            self.new_edit.clear()

    def selected_playlist_id(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None


class FilterDialog(MessageBoxBase):
    def __init__(self, sources: list, sel_sources: list, sel_quality: list, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(tr('music.search_filters'), self))
        self.viewLayout.addWidget(StrongBodyLabel(tr('music.quality'), self))
        labels = {'lossless': tr('music.quality_lossless'), 'mp3': 'MP3', 'aac': 'AAC / M4A',
                  'other': tr('music.quality_other')}
        self._q = {}
        for t in QUALITY_TIERS:
            cb = CheckBox(labels.get(t, t), self)
            cb.setChecked(t in sel_quality)
            self._q[t] = cb
            self.viewLayout.addWidget(cb)
        self.viewLayout.addWidget(StrongBodyLabel(tr('music.sources'), self))
        sc = ScrollArea(self)
        sc.setFixedHeight(220)
        sc.setWidgetResizable(True)
        sc.setStyleSheet('background:transparent;border:none;')
        box = QWidget(sc)
        box.setStyleSheet('background:transparent;')
        lay = QVBoxLayout(box)
        lay.setSpacing(4)
        self._s = {}
        for s in sorted(sources, key=lambda s: (s in SLOW_OR_GATED_SOURCES, s)):
            cb = CheckBox(tr('music.source_slow_setup', source=s) if s in SLOW_OR_GATED_SOURCES else s, box)
            cb.setChecked(s in sel_sources)
            self._s[s] = cb
            lay.addWidget(cb)
        lay.addStretch(1)
        sc.setWidget(box)
        self.viewLayout.addWidget(sc)
        self.yesButton.setText(tr('music.apply'))
        self.cancelButton.setText(tr('music.cancel'))
        self.widget.setMinimumWidth(360)

    def selected_sources(self) -> list:
        return [s for s, c in self._s.items() if c.isChecked()]

    def selected_quality(self) -> list:
        return [t for t, c in self._q.items() if c.isChecked()]


def _audio_info(song, meta_kbps: int = 0) -> str:
    raw = getattr(song, 'raw', None) or {}

    def num(*keys):
        for k in keys:
            try:
                v = float(raw.get(k))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        return 0

    ext = (getattr(song, 'ext', '') or os.path.splitext((song.download_url or '').split('?')[0])[1]).lstrip('.').upper()
    parts = [ext or getattr(song, 'quality_label', '')]
    br = num('bitrate', 'bit_rate', 'br')
    kbps = (br / 1000 if br > 3000 else br) or meta_kbps
    if kbps:
        parts.append(f'{kbps:.0f} kb/s')
    sr = num('sample_rate', 'samplerate', 'sr', 'sampling_rate')
    if sr:
        parts.append(f'{sr / 1000 if sr > 1000 else sr:g} kHz')
    ch = int(num('channels', 'channel', 'nchannels'))
    if ch:
        parts.append({1: 'Mono', 2: 'Stereo'}.get(ch, f'{ch} ch'))
    return ' • '.join(p for p in parts if p)


def _accent(c: QColor) -> QColor:
    h, s, _, _ = c.getHsl()
    return QColor.fromHsl(max(h, 0), min(s, 150), 175)


class PlayBar(QWidget):
    open_playing = Signal(int)
    add_requested = Signal()

    def __init__(self, player: MusicPreviewPlayer, parent=None):
        super().__init__(parent)
        self.player, self._song, self._meta_kbps = player, None, 0
        self._color, self._dark = QColor('#1e2a3a'), QColor('#1e2a3a')
        self._ani = QVariantAnimation(self, duration=250, valueChanged=self._set_color)
        self.setFixedHeight(_BAR_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 24, 0)
        lay.setSpacing(0)

        left = ClickWidget(self)
        left.clicked.connect(lambda: self.open_playing.emit(0))
        ll = QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(16)
        self.cover = Cover(_BAR_H, 0, left)
        ll.addWidget(self.cover)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = ElidedLabel(16, QFont.DemiBold, parent=left)
        self.artist = ElidedLabel(14, color=QColor(255, 255, 255, 215), parent=left)
        self.detail = ElidedLabel(13, color=QColor(255, 255, 255, 215), parent=left)
        col.addStretch(1)
        for w in (self.title, self.artist, self.detail):
            col.addWidget(w)
        col.addStretch(1)
        ll.addLayout(col, 1)
        lay.addWidget(left, 3)

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 10, 0, 6)
        mid.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(18)
        row.addStretch(1)
        self.b_shuffle = IconButton('m_shuffle', 38, 22)
        self.b_prev = IconButton('m_prev', 40, 24)
        self.b_play = IconButton('m_play', 54, 30)
        self.b_next = IconButton('m_next', 40, 24)
        self.b_repeat = IconButton('m_repeat', 38, 22)
        for b, on in ((self.b_shuffle, player.shuffle), (self.b_repeat, player.repeat)):
            b.setCheckable(True)
            b.setChecked(on)
        for b in (self.b_shuffle, self.b_prev, self.b_play, self.b_next, self.b_repeat):
            row.addWidget(b)
        row.addStretch(1)
        mid.addLayout(row)
        prog = QHBoxLayout()
        prog.setSpacing(12)
        self.t_cur, self.t_tot = CaptionLabel('0:00', self), CaptionLabel('0:00', self)
        for t in (self.t_cur, self.t_tot):
            t.setStyleSheet('color:rgba(255,255,255,215);background:transparent;font-size:13px;')
            t.setFixedWidth(46)
        self.t_cur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.seek = HollowSlider(self)
        self.seek.setRange(0, 0)
        prog.addWidget(self.t_cur)
        prog.addWidget(self.seek, 1)
        prog.addWidget(self.t_tot)
        mid.addLayout(prog)
        lay.addLayout(mid, 5)

        right = QHBoxLayout()
        right.setSpacing(4)
        right.addStretch(1)
        self.b_add = IconButton('m_pladd', 42, 24)
        self.b_queue = IconButton('m_queue', 42, 24)
        self.b_vol = IconButton('m_volup', 42, 24)
        self.vol = HollowSlider(self)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(player.volume * 100))
        self.vol.setFixedWidth(110)
        self.b_lyrics = IconButton('m_lyrics', 42, 24)
        for w in (self.b_add, self.b_queue, self.b_vol, self.vol):
            right.addWidget(w)
        right.addSpacing(10)
        right.addWidget(self.b_lyrics)
        lay.addLayout(right, 3)

        self._btns = [self.b_shuffle, self.b_prev, self.b_next, self.b_repeat, self.b_add, self.b_queue,
                      self.b_vol, self.b_lyrics]
        self.b_play.clicked.connect(player.toggle_play_pause)
        self.b_prev.clicked.connect(player.previous)
        self.b_next.clicked.connect(player.next)
        self.b_shuffle.toggled.connect(player.set_shuffle)
        self.b_repeat.toggled.connect(player.set_repeat)
        self.b_add.clicked.connect(self.add_requested)
        self.b_queue.clicked.connect(lambda: self.open_playing.emit(1))
        self.b_lyrics.clicked.connect(lambda: self.open_playing.emit(0))
        self.b_vol.clicked.connect(self._mute)
        self.vol.valueChanged.connect(self._volume)
        self.seek.sliderMoved.connect(lambda v: self.t_cur.setText(_format_ms(v)))
        self.seek.seeked.connect(player.seek)
        player.state_changed.connect(lambda s: self.b_play.setIconName('m_pause' if s == 'playing' else 'm_play'))
        player.position_changed.connect(self._pos)
        player.duration_changed.connect(self._dur)
        qp = getattr(player, '_player', None)
        if qp is not None:
            qp.metaDataChanged.connect(lambda: self._on_meta(qp))
        self._apply_accent(self._color)
        self.set_song(None)

    def _on_meta(self, qp) -> None:
        try:
            br = int(qp.metaData().value(QMediaMetaData.Key.AudioBitRate) or 0)
        except (TypeError, ValueError):
            br = 0
        if br and self._song is not None and br // 1000 != self._meta_kbps:
            self._meta_kbps = br // 1000
            self.detail.setText(_audio_info(self._song, self._meta_kbps))

    def set_song(self, song) -> None:
        self._song, self._meta_kbps = song, 0
        has = song is not None
        self.title.setText((song.song_name or tr('download.unknown')) if has else '')
        self.artist.setText((song.singers or tr('music.unknown_artist')) if has else '')
        self.detail.setText(_audio_info(song) if has else '')
        self.cover.set_song(song)

    def _apply_accent(self, c: QColor) -> None:
        self._dark, acc = _tone(c), _accent(c)
        self.b_play.set_colors(self._dark, acc)
        for b in self._btns:
            b.accent = acc
            b.update()
        for sl in (self.seek, self.vol):
            sl.accent = acc
            sl.update()

    def set_color(self, c: QColor) -> None:
        self._apply_accent(c)
        self._ani.stop()
        self._ani.setStartValue(self._color)
        self._ani.setEndValue(self._dark)
        self._ani.start()

    def _set_color(self, c) -> None:
        self._color = QColor(c)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), self._color)
        p.fillRect(0, 0, self.width(), 1, QColor(255, 255, 255, 28))

    def _pos(self, ms: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
            t = _format_ms(ms)
            if t != self.t_cur.text():
                self.t_cur.setText(t)

    def _dur(self, ms: int) -> None:
        self.seek.setRange(0, max(0, ms))
        self.t_tot.setText(_format_ms(ms))

    def _volume(self, v: int) -> None:
        self.player.set_volume(v / 100)
        self._vol_icon(v, False)

    def _mute(self) -> None:
        m = not self.player.is_muted
        self.player.set_muted(m)
        self._vol_icon(self.vol.value(), m)

    def _vol_icon(self, v: int, muted: bool) -> None:
        self.b_vol.setIconName('m_volx' if muted else 'm_vol0' if v == 0 else 'm_voldown' if v < 50 else 'm_volup')


_ALIGN = {'center': Qt.AlignCenter, 'left': Qt.AlignLeft | Qt.AlignVCenter, 'right': Qt.AlignRight | Qt.AlignVCenter}
_LINE_STYLE = ((44, 800, 255), (30, 600, 165), (26, 500, 115), (24, 500, 85))
_FONT_EXT = ('.ttf', '.otf', '.ttc')


class LyricLine(QLabel):
    clicked = Signal(int)
    hovered = Signal(object)

    def __init__(self, text: str, ms: int, parent=None):
        super().__init__(text, parent)
        self.ms, self.dist, self.hover = ms, 9, False
        self.setWordWrap(True)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        self.hover = True
        self.hovered.emit(self)

    def leaveEvent(self, e):
        self.hover = False
        self.hovered.emit(self)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.ms)


class LyricView(QScrollArea):
    seek = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.family = ''
        self.scale = max(0.7, min(1.5, music_settings.lyrics_font_scale or 1.0))
        self.align = music_settings.lyrics_align if music_settings.lyrics_align in _ALIGN else 'center'
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet('QScrollArea,QScrollArea>QWidget>QWidget{background:transparent;}')
        self._box = QWidget()
        self._lay = QVBoxLayout(self._box)
        self._lay.setSpacing(34)
        self.setWidget(self._box)
        self._labels: list = []
        self._times: list = []
        self._active = -1
        self._free_until = 0.0
        self._ani = QPropertyAnimation(self.verticalScrollBar(), b'value', self)
        self.status = QLabel('', self)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.status.setStyleSheet('color:rgba(255,255,255,150);font-size:14px;background:transparent;')

    def resizeEvent(self, e):
        super().resizeEvent(e)
        m = self.height() // 2
        self._lay.setContentsMargins(40, m, 40, m)
        self.status.setGeometry(0, 0, self.width(), self.height())

    def wheelEvent(self, e):
        sb = self.verticalScrollBar()
        self._free_until = time.monotonic() + 3
        base = self._ani.endValue() if self._ani.state() == QPropertyAnimation.Running else sb.value()
        self._animate(max(sb.minimum(), min(sb.maximum(), int(base) - e.angleDelta().y())), 170, QEasingCurve.OutCubic)
        e.accept()

    def _animate(self, target: int, ms: int, curve) -> None:
        self._ani.stop()
        self._ani.setDuration(ms)
        self._ani.setEasingCurve(curve)
        self._ani.setStartValue(self.verticalScrollBar().value())
        self._ani.setEndValue(target)
        self._ani.start()

    def clear(self, msg: str = '') -> None:
        for l in self._labels:
            self._lay.removeWidget(l)
            l.deleteLater()
        self._labels, self._times, self._active, self._free_until = [], [], -1, 0.0
        self.status.setText(msg)
        self.verticalScrollBar().setValue(0)

    def set_lines(self, lines: list) -> None:
        self.clear('' if lines else _t('music.no_lyrics_found', 'No lyrics found'))
        for ms, text in lines:
            l = LyricLine(text or '♪', ms, self._box)
            l.setAlignment(_ALIGN[self.align])
            l.clicked.connect(self._on_click)
            l.hovered.connect(lambda lab: self._style(lab, lab.dist))
            self._style(l, 9)
            self._lay.addWidget(l)
            self._labels.append(l)
            self._times.append(ms)

    def _on_click(self, ms: int) -> None:
        self._free_until = 0.0
        self.seek.emit(ms)
        self.set_position(ms)

    def _style(self, l: LyricLine, dist: int) -> None:
        l.dist = dist
        size, weight, alpha = _LINE_STYLE[min(dist, 3)]
        l.setFont(_font(max(8, int(size * self.scale)), QFont.Weight(weight), self.family))
        pal = l.palette()
        pal.setColor(QPalette.WindowText, QColor(255, 255, 255, max(alpha, 200) if l.hover else alpha))
        l.setPalette(pal)

    def restyle(self) -> None:
        for i, l in enumerate(self._labels):
            self._style(l, abs(i - self._active) if self._active >= 0 else 9)
        self._center(self._active, animate=False, force=True)

    def set_align(self, align: str) -> None:
        self.align = align
        for l in self._labels:
            l.setAlignment(_ALIGN[align])

    def set_position(self, ms: int) -> None:
        if not self._times:
            return
        i = bisect_right(self._times, ms) - 1
        if i == self._active:
            return
        prev, self._active = self._active, i
        touched = {j for k in (prev, i) if k >= 0 for j in range(k - 3, k + 4) if 0 <= j < len(self._labels)}
        for j in touched:
            self._style(self._labels[j], abs(j - i) if i >= 0 else 9)
        self._center(i)

    def _center(self, i: int, animate: bool = True, force: bool = False) -> None:
        if not 0 <= i < len(self._labels) or (not force and time.monotonic() < self._free_until):
            return
        self._box.adjustSize()
        l = self._labels[i]
        target = l.y() + l.height() // 2 - self.height() // 2
        if animate:
            self._animate(target, 380, QEasingCurve.OutCubic)
        else:
            self._ani.stop()
            self.verticalScrollBar().setValue(target)


class FontPopup(CardWidget):
    chosen = Signal(str, str)
    reset = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(300, 392)
        self.setBorderRadius(16)
        self.setAcceptDrops(True)
        self._custom: list[tuple[str, str]] = []
        self._filled = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText(_t('music.search_fonts_placeholder', 'Search fonts'))
        self.search.textChanged.connect(self._filter)
        self.preview = QLabel('AaBbCc 123', self)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(38)
        self.preview.setStyleSheet('color:white;background:rgba(255,255,255,18);border-radius:8px;font-size:15px;')
        self.list = ListWidget(self)
        self.list.setFixedHeight(190)
        self.list.setUniformItemSizes(True)
        self.list.itemClicked.connect(lambda it: self.chosen.emit(it.data(Qt.UserRole), it.data(Qt.UserRole + 1) or ''))
        self.list.currentItemChanged.connect(lambda cur, _: cur and self.preview.setFont(QFont(cur.data(Qt.UserRole), 15)))
        row = QHBoxLayout()
        btn = PushButton(FluentIcon.FOLDER_ADD, _t('music.load_font_file', 'Load font file'), self)
        btn.clicked.connect(self._browse)
        rst = TransparentToolButton(FluentIcon.CANCEL, self)
        rst.setFixedSize(30, 30)
        rst.setToolTip(_t('music.reset_font', 'Reset font'))
        rst.clicked.connect(self.reset)
        row.addWidget(btn, 1)
        row.addWidget(rst)
        hint = CaptionLabel(_t('music.drop_font_hint', 'Drop a .ttf / .otf file here'), self)
        hint.setWordWrap(True)
        for w in (self.search, self.preview):
            lay.addWidget(w)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        lay.addWidget(hint)

    def fill(self) -> None:
        if not self._filled:
            self._filled = True
            for fam, path in self._custom:
                self.list.addItem(self._item(fam, path))
            for fam in sorted(QFontDatabase.families()):
                self.list.addItem(self._item(fam))

    @staticmethod
    def _item(fam: str, path: str = '') -> QListWidgetItem:
        it = QListWidgetItem(fam)
        it.setData(Qt.UserRole, fam)
        it.setData(Qt.UserRole + 1, path)
        return it

    def select(self, family: str) -> None:
        self.fill()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == family:
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i))
                return
        self.list.setCurrentRow(-1)

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(q) and q not in it.text().lower())

    def register(self, path: str) -> str | None:
        fid = QFontDatabase.addApplicationFont(path)
        fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
        if not fams:
            return None
        if all(f != fams[0] for f, _ in self._custom):
            self._custom.insert(0, (fams[0], path))
            if self._filled:
                self.list.insertItem(0, self._item(fams[0], path))
        return fams[0]

    def load(self, path: str) -> bool:
        fam = self.register(path)
        if fam:
            self.chosen.emit(fam, path)
        return bool(fam)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, _t('music.load_font_file', 'Load font file'), '',
                                              'Fonts (*.ttf *.otf *.ttc)')
        if path and not self.load(path):
            InfoBar.warning(_t('music.font_load_failed_title', 'Font failed to load'),
                            _t('music.font_load_failed_content', 'The file is not a valid font.'),
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith(_FONT_EXT) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(_FONT_EXT):
                self.load(u.toLocalFile())
                break
        e.acceptProposedAction()


class PlayingView(QWidget):
    back = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base, self._blur = QColor('#2b2233'), QPixmap()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 14, 24, 8)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.b_back = IconButton('ChevronUp', 36, 18, rotate=180)
        self.b_back.clicked.connect(self.back)
        self.b_queue = IconButton('m_queue', 36, 20)
        self.b_queue.setCheckable(True)
        self.b_queue.toggled.connect(lambda on: self.stack.setCurrentIndex(1 if on else 0))
        fi = lambda *names: next((getattr(FluentIcon, n) for n in names if hasattr(FluentIcon, n)), FluentIcon.EDIT)
        self.b_align, self.b_size, self.b_font = (
            TransparentToolButton(fi(*n).icon(Theme.DARK), self) for n in (('ALIGNMENT',), ('FONT_SIZE',), ('FONT',)))
        for b, tip in ((self.b_align, 'music.text_alignment'), (self.b_size, 'music.font_size'),
                       (self.b_font, 'music.font_family')):
            b.setFixedSize(36, 36)
            b.setIconSize(QSize(18, 18))
            b.setToolTip(_t(tip, tip.split('.')[1].replace('_', ' ').title()))
        top.addWidget(self.b_back)
        top.addStretch(1)
        for b in (self.b_align, self.b_size, self.b_font, self.b_queue):
            top.addWidget(b)
        root.addLayout(top)

        self.cover = Cover(64, 0, self)
        self.cover.hide()
        self.cover.loaded.connect(self._rebuild_bg)
        self.stack = QStackedWidget()
        self.lyrics = LyricView()
        self.queue = SongList()
        self.queue.setStyleSheet('QListWidget{background:rgba(0,0,0,70);border:none;border-radius:10px;}')
        self.stack.addWidget(self.lyrics)
        self.stack.addWidget(self.queue)
        body = QHBoxLayout()
        body.addStretch(1)
        body.addWidget(self.stack, 5)
        body.addStretch(1)
        root.addLayout(body, 1)


        self._size_pop = QWidget(self)
        self._size_pop.setFixedSize(132, 34)
        self._size_pop.setStyleSheet('background:rgba(20,14,24,190);border:1px solid rgba(255,255,255,40);border-radius:17px;')
        sl = QHBoxLayout(self._size_pop)
        sl.setContentsMargins(12, 0, 12, 0)
        self._size_slider = Slider(Qt.Horizontal, self._size_pop)
        self._size_slider.setRange(70, 150)
        self._size_slider.setValue(int(self.lyrics.scale * 100))
        self._size_slider.setFixedWidth(108)
        self._size_slider.valueChanged.connect(self._on_scale)
        sl.addWidget(self._size_slider)
        self._font_pop = FontPopup(self)
        self._font_pop.chosen.connect(self._on_font)
        self._font_pop.reset.connect(lambda: self._on_font('', ''))
        self._size_pop.hide()
        self._font_pop.hide()
        self.b_align.clicked.connect(self._cycle_align)
        self.b_size.clicked.connect(lambda: self._toggle(self._size_pop, self.b_size))
        self.b_font.clicked.connect(lambda: self._toggle(self._font_pop, self.b_font))
        self._restore_font()

    def show_tab(self, i: int) -> None:
        self.b_queue.setChecked(i == 1)

    def _toggle(self, pop: QWidget, anchor: QWidget) -> None:
        other = self._font_pop if pop is self._size_pop else self._size_pop
        other.hide()
        if pop.isVisible():
            pop.hide()
            return
        if pop is self._font_pop:
            pop.fill()
            pop.search.clear()
            pop.select(self.lyrics.family)
        pt = anchor.mapTo(self, anchor.rect().bottomRight())
        pop.move(pt.x() - pop.width(), pt.y() + 6)
        pop.raise_()
        pop.show()

    def _cycle_align(self) -> None:
        order = list(_ALIGN)
        a = order[(order.index(self.lyrics.align) + 1) % 3]
        self.lyrics.set_align(a)
        apply_music_settings(lyrics_align=a)

    def _on_scale(self, v: int) -> None:
        self.lyrics.scale = v / 100
        self.lyrics.restyle()
        apply_music_settings(lyrics_font_scale=self.lyrics.scale)

    def _on_font(self, family: str, path: str) -> None:
        self.lyrics.family = family
        self.lyrics.restyle()
        self._font_pop.select(family) if family else self._font_pop.list.setCurrentRow(-1)
        apply_music_settings(lyrics_font_family=family, lyrics_custom_font_path=path)

    def _restore_font(self) -> None:
        path, fam = music_settings.lyrics_custom_font_path, music_settings.lyrics_font_family
        if path and fam and os.path.isfile(path):
            fam = self._font_pop.register(path) or ''
        elif fam and fam not in QFontDatabase.families():
            fam = ''
        self.lyrics.family = fam

    def hideEvent(self, e):
        self._size_pop.hide()
        self._font_pop.hide()
        super().hideEvent(e)

    def set_song(self, song) -> None:
        self.cover.set_song(song)
        if song is None:
            self._blur = QPixmap()
            self.update()

    def set_colors(self, colors: list) -> None:
        self._base = _tone(_vivid(colors)).darker(130)
        self.update()

    def _rebuild_bg(self) -> None:
        self._blur = self.cover.pm.scaled(28, 28, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), self._base)
        if not self._blur.isNull():
            p.setOpacity(0.55)
            p.drawPixmap(self.rect(), self._blur)
            p.setOpacity(1)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(0, 0, 0, 60))
        g.setColorAt(1, QColor(0, 0, 0, 120))
        p.fillRect(self.rect(), g)


class SlideStack(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.base = self.over = None
        self._p, self.is_open = 0.0, False
        self._ani = QVariantAnimation(self, valueChanged=self._set)

    def set_base(self, w: QWidget) -> None:
        self.base = w
        w.setParent(self)
        w.setGeometry(self.rect())
        w.show()

    def set_over(self, w: QWidget) -> None:
        self.over = w
        w.setParent(self)
        w.hide()

    def set_open(self, on: bool, animate: bool = True) -> None:
        self.is_open = on
        self._ani.stop()
        if on:
            self.over.show()
            self.over.raise_()
        end = 1.0 if on else 0.0
        if not animate or not self.isVisible():
            self._set(end)
            return
        self._ani.setDuration(260 if on else 200)
        self._ani.setEasingCurve(QEasingCurve.OutQuart)
        self._ani.setStartValue(self._p)
        self._ani.setEndValue(end)
        self._ani.start()

    def _set(self, p) -> None:
        self._p = float(p)
        if self.over is None:
            return
        self.over.setGeometry(0, int((1 - self._p) * self.height()), self.width(), self.height())
        if self._p <= 0 and not self.is_open:
            self.over.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.base is not None:
            self.base.setGeometry(self.rect())
        self._set(self._p)


class MusicPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = MusicPreviewPlayer(self)
        self._store = PlaylistStore(self)
        self._dl_manager = None
        self._dl_speed: dict = {}
        self._songs: list = []
        self._keys: set = set()
        self._queue: list = []
        self._token = None
        self._cur_pl = None
        self._cur_key = None
        self._lrc: dict = {}
        self._lrc_pending: set = set()
        self._lrc_shown = None
        self._cancel_ev = threading.Event()
        self._sid = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._main = SlideStack(self)
        root.addWidget(self._main, 1)
        self.bar = PlayBar(self._player, self)
        root.addWidget(self.bar)

        lib = QWidget()
        ll = QVBoxLayout(lib)
        ll.setContentsMargins(24, 12, 24, 0)
        ll.setSpacing(8)
        top = QHBoxLayout()
        self._pivot = Pivot(lib)
        self._pivot.addItem('songs', _t('music.songs', 'Songs'), lambda: self._tabs.setCurrentIndex(0))
        self._pivot.addItem('playlists', tr('music.playlists'), lambda: self._tabs.setCurrentIndex(1))
        self._pivot.setCurrentItem('songs')
        self._search = SearchLineEdit(lib)
        self._search.setPlaceholderText(tr('music.search_placeholder'))
        self._search.setFixedWidth(340)
        self._search.returnPressed.connect(self._on_search)
        self._search.searchSignal.connect(lambda _q: self._on_search())
        self._b_filter = TransparentToolButton(FluentIcon.FILTER, lib)
        self._b_filter.setToolTip(tr('music.filter'))
        self._b_filter.clicked.connect(self._on_filters)
        self._b_view = TransparentToolButton(getattr(FluentIcon, 'APPLICATION', FluentIcon.MENU), lib)
        self._b_view.setToolTip(_t('music.grid_view', 'Grid view'))
        self._b_view.clicked.connect(self._toggle_view)
        self._b_cancel = TransparentToolButton(FluentIcon.CLOSE, lib)
        self._b_cancel.hide()
        self._b_cancel.clicked.connect(self._on_cancel)
        top.addWidget(self._pivot)
        top.addStretch(1)
        for w in (self._b_cancel, self._search, self._b_filter, self._b_view):
            top.addWidget(w)
        ll.addLayout(top)
        self._loading = IndeterminateProgressBar(lib, start=False)
        self._loading.setFixedHeight(3)
        ll.addWidget(self._loading)
        self._tabs = QStackedWidget(lib)
        ll.addWidget(self._tabs, 1)

        st = QWidget()
        sl = QVBoxLayout(st)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self._res_stack = QStackedWidget(st)
        lw = QWidget()
        lv = QVBoxLayout(lw)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(ListHeader(lw))
        self.results = SongList(parent=lw)
        lv.addWidget(self.results, 1)
        self.grid_songs = SongGrid()
        self._res_stack.addWidget(lw)
        self._res_stack.addWidget(self.grid_songs)
        self._grid_mode = False
        sl.addWidget(self._res_stack, 1)
        self._empty = CaptionLabel(tr('music.search_hint'), st)
        self._empty.setAlignment(Qt.AlignCenter)
        sl.addWidget(self._empty)
        self._tabs.addWidget(st)

        pt = QStackedWidget()
        self.grid = PlaylistGrid()
        pt.addWidget(self.grid)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.setSpacing(18)
        b_back = TransparentToolButton(FluentIcon.RETURN, detail)
        b_back.clicked.connect(self._close_playlist)
        self._pl_cover = Cover(120, 8, detail)
        info = QVBoxLayout()
        self._pl_name = SubtitleLabel('', detail)
        self._pl_count = CaptionLabel('', detail)
        brow = QHBoxLayout()
        b_play = PrimaryPushButton(FluentIcon.PLAY, _t('music.play_all', 'Play all'), detail)
        b_dlall = PushButton(FluentIcon.DOWNLOAD, _t('music.download_all', 'Download all'), detail)
        b_ren = TransparentToolButton(FluentIcon.EDIT, detail)
        b_del = TransparentToolButton(FluentIcon.DELETE, detail)
        for w in (b_play, b_dlall, b_ren, b_del):
            brow.addWidget(w)
        brow.addStretch(1)
        info.addStretch(1)
        info.addWidget(self._pl_name)
        info.addWidget(self._pl_count)
        info.addSpacing(6)
        info.addLayout(brow)
        info.addStretch(1)
        head.addWidget(b_back, 0, Qt.AlignTop)
        head.addWidget(self._pl_cover)
        head.addLayout(info, 1)
        dl.addLayout(head)
        dl.addSpacing(8)
        dl.addWidget(ListHeader(detail))
        self.pl_songs = SongList(removable=True, parent=detail)
        dl.addWidget(self.pl_songs, 1)
        pt.addWidget(detail)
        self._pl_stack = pt
        self._tabs.addWidget(pt)
        self._main.set_base(lib)
        self.view = PlayingView()
        self._main.set_over(self.view)

        for name, lst in (('results', self.results), ('results', self.grid_songs), ('pl', self.pl_songs),
                          ('queue', self.view.queue)):
            lst.play.connect(partial(self._on_play_song, name))
            lst.download.connect(self._on_download)
            lst.add.connect(self._on_add_to_playlist)
        self.pl_songs.remove.connect(lambda s: self._store.remove_song(self._cur_pl, s.key))
        self.grid.open.connect(self._open_playlist)
        self.grid.play.connect(self._play_playlist)
        self.grid.rename.connect(self._rename_playlist)
        self.grid.delete.connect(self._delete_playlist)
        b_play.clicked.connect(lambda: self._play_playlist(self._cur_pl))
        b_dlall.clicked.connect(lambda: self._download_all(self._pl_songs_now()))
        b_ren.clicked.connect(lambda: self._rename_playlist(self._cur_pl))
        b_del.clicked.connect(lambda: self._delete_playlist(self._cur_pl))
        self._store.playlists_changed.connect(self._refresh_playlists)
        self.bar.open_playing.connect(self._open_playing)
        self.bar.add_requested.connect(self._add_current)
        self.bar.hide()
        self._player.state_changed.connect(lambda _s: QTimer.singleShot(0, self._check_idle))
        self.view.back.connect(lambda: self._main.set_open(False))
        self.view.lyrics.seek.connect(self._player.seek)
        self._player.song_changed.connect(self._on_song_changed)
        self._player.position_changed.connect(self._on_position)
        self._player.error.connect(lambda m: self._toast('error', tr('music.playback_error_title'), m[:120], 4000))
        register_locale_refresh(self, self._apply_locale)
        self._refresh_playlists()

    def _add_current(self) -> None:
        song = self._player.current_song
        if song is not None:
            self._on_add_to_playlist(song)

    def _check_idle(self) -> None:
        if self._player.current_song is None and not self._player.is_playing:
            self.bar.hide()
            self._main.set_open(False, animate=False)
            self._on_song_changed(None)

    def _open_playing(self, tab: int = 0) -> None:
        self.view.show_tab(tab)
        self._main.set_open(True)
        self._want_lyrics()


    def _apply_locale(self, *_):
        self._search.setPlaceholderText(tr('music.search_placeholder'))
        self._pivot.setItemText('playlists', tr('music.playlists'))
        if not self._songs:
            self._empty.setText(tr('music.search_hint'))

    def _toast(self, kind: str, title: str, content: str = '', ms: int = 3000) -> None:
        getattr(InfoBar, kind)(title=title, content=content, orient=Qt.Horizontal, isClosable=True,
                               position=InfoBarPosition.TOP_RIGHT, duration=ms, parent=self.window())

    def set_download_manager(self, manager) -> None:
        self._dl_manager = manager

    def shutdown(self) -> None:
        self._cancel_ev.set()
        close_http_client()
        if self._token is not None:
            cancel(self._token)
            self._token = None
        try:
            self._player.stop()
        except RuntimeError:
            pass
        images.release(self)


    def _on_search(self) -> None:
        q = self._search.text().strip()
        if not q:
            return
        if self._token is not None:
            cancel(self._token)
        self._cancel_ev.set()
        self._cancel_ev = ev = threading.Event()
        self._sid = sid = self._sid + 1
        self._songs, self._keys = [], set()
        self.results.clear()
        self.grid_songs.clear()
        self._pivot.setCurrentItem('songs')
        self._tabs.setCurrentIndex(0)
        self._loading.start()
        self._b_cancel.show()
        self._empty.hide()
        self._token = submit(music_service.search_streaming, args=(q,),
                             kwargs={'on_source_done': wrap_callback(partial(self._on_batch, sid)),
                                     'sources': music_settings.preferred_sources, 'use_cache': True,
                                     'is_cancelled': ev.is_set},
                             on_done=partial(self._on_done, sid), on_error=partial(self._on_error, sid))

    def _on_batch(self, sid: int, songs) -> None:
        if sid != self._sid:
            return
        new = [s for s in songs if s.key not in self._keys]
        self._keys.update(s.key for s in new)
        for s in new:
            try:
                if isinstance(s.raw, dict) and s.raw:
                    s.raw = {k: v for k, v in s.raw.items() if k in _RAW_KEEP}
            except Exception:
                pass
        self._songs.extend(new)
        (self.grid_songs if self._grid_mode else self.results).add_songs(new)

    def _toggle_view(self) -> None:
        self._grid_mode = not self._grid_mode
        view = self.grid_songs if self._grid_mode else self.results
        if view.count() != len(self._songs):
            view.set_songs(self._songs)
        view.set_playing(self._cur_key)
        self._res_stack.setCurrentIndex(1 if self._grid_mode else 0)
        self._b_view.setIcon(FluentIcon.MENU if self._grid_mode else getattr(FluentIcon, 'APPLICATION', FluentIcon.MENU))
        self._b_view.setToolTip(_t('music.list_view', 'List view') if self._grid_mode else _t('music.grid_view', 'Grid view'))

    def _end_search(self) -> None:
        self._token = None
        self._loading.stop()
        self._b_cancel.hide()

    def _on_done(self, sid: int, _songs) -> None:
        if sid != self._sid:
            return
        self._end_search()
        if not self._songs:
            self._empty.setText(tr('music.no_results_try_different'))
            self._empty.show()

    def _on_error(self, sid: int, error: str) -> None:
        if sid != self._sid:
            return
        self._end_search()
        self._toast('error', tr('music.search_failed_title'), str(error)[:120], 4000)

    def _on_cancel(self) -> None:
        self._cancel_ev.set()
        if self._token is not None:
            cancel(self._token)
        self._end_search()

    def _on_filters(self) -> None:
        dlg = FilterDialog(music_service.available_sources(), music_settings.preferred_sources,
                           music_settings.quality_filters, self.window())
        if not dlg.exec():
            return
        src, qual = dlg.selected_sources(), dlg.selected_quality()
        if not src or not qual:
            self._toast('warning', tr('music.filters_not_applied_title'), tr('music.filters_not_applied_content'))
            return
        apply_music_settings(preferred_sources=src, quality_filters=qual)
        if self._search.text().strip():
            self._on_search()


    def _play(self, song, queue: list) -> None:
        if not _playable(song):
            self._toast('warning', tr('music.no_preview_title'), tr('music.no_preview_content'))
            return
        q = [s for s in queue if _playable(s)]
        self._queue = q
        self.view.queue.set_songs(q)
        self._player.play_song(song, song.download_url, queue=q, urls={s.key: s.download_url for s in q})

    def _on_play_song(self, src: str, song) -> None:
        self._play(song, {'results': self._songs, 'pl': self.pl_songs.songs(), 'queue': self._queue}[src])

    def _on_song_changed(self, song) -> None:
        key = song.key if song else None
        self._cur_key = key
        for lst in (self.results, self.grid_songs, self.pl_songs, self.view.queue):
            lst.set_playing(key)
        self.bar.set_song(song)
        self.bar.setVisible(song is not None)
        self.view.set_song(song)
        self._lrc_shown = None
        self.view.lyrics.clear()
        if song is None:
            return
        if song.cover_url:
            images.dominant(song.cover_url, partial(self._on_colors, key), count=5, owner=self)
        self._want_lyrics()

    def _want_lyrics(self) -> None:
        song = self._player.current_song
        if song is None or not self._main.is_open or self._lrc_shown == song.key:
            return
        if song.key in self._lrc:
            self._apply_lrc(song.key, self._lrc[song.key])
        elif song.key not in self._lrc_pending:
            self._lrc_pending.add(song.key)
            self.view.lyrics.clear(_t('music.searching_lyrics', 'Searching lyrics…'))
            task = _LrcTask(song)
            task.signals.finished.connect(self._on_lrc)
            QThreadPool.globalInstance().start(task)

    def _on_colors(self, key, colors) -> None:
        if key == self._cur_key and colors:
            self.bar.set_color(_vivid(colors))
            self.view.set_colors(colors)

    def _on_lrc(self, key: str, text: str) -> None:
        self._lrc_pending.discard(key)
        cur = self._player.current_song
        if not _parse_lrc(text) and cur is not None and cur.key == key:
            fb = getattr(cur, 'lyric', None)
            if fb and _parse_lrc(fb):
                text = fb
        self._lrc[key] = text
        if len(self._lrc) > 50:
            self._lrc.pop(next(iter(self._lrc)))
        self._want_lyrics()

    def _apply_lrc(self, key: str, text: str) -> None:
        self._lrc_shown = key
        self.view.lyrics.set_lines(_parse_lrc(text))
        self.view.lyrics.set_position(self._player.position)

    def _on_position(self, ms: int) -> None:
        if self._main.is_open and self.view.stack.currentIndex() == 0:
            self.view.lyrics.set_position(ms)


    def _refresh_playlists(self) -> None:
        self.grid.set_playlists(self._store.all_playlists())
        if self._cur_pl:
            pl = self._store.get(self._cur_pl)
            if pl is None:
                self._close_playlist()
            else:
                self._fill_detail(pl)

    def _fill_detail(self, pl) -> None:
        self._pl_name.setText(pl.name)
        self._pl_count.setText(_t('music.n_songs', '{n} songs').format(n=len(pl.songs)))
        self._pl_cover.set_url(next((s.cover_url for s in pl.songs if s.cover_url), None))
        self.pl_songs.set_songs(pl.songs)
        self.pl_songs.set_playing(self._cur_key)

    def _pl_songs_now(self) -> list:
        pl = self._store.get(self._cur_pl) if self._cur_pl else None
        return pl.songs if pl else []

    def _open_playlist(self, pid: str) -> None:
        pl = self._store.get(pid)
        if pl is not None:
            self._cur_pl = pid
            self._fill_detail(pl)
            self._pl_stack.setCurrentIndex(1)

    def _close_playlist(self) -> None:
        self._cur_pl = None
        self._pl_stack.setCurrentIndex(0)

    def _play_playlist(self, pid) -> None:
        pl = self._store.get(pid) if pid else None
        if pl is None or not pl.songs:
            return
        first = next((s for s in pl.songs if _playable(s)), None)
        if first is None:
            self._toast('warning', tr('music.no_preview_title'), tr('music.no_preview_in_playlist_content'))
        else:
            self._play(first, pl.songs)

    def _rename_playlist(self, pid) -> None:
        pl = self._store.get(pid) if pid else None
        if pl is None:
            return
        dlg = NameDialog(_t('music.rename_playlist', 'Rename playlist'), pl.name, self.window())
        if dlg.exec() and dlg.text():
            self._store.rename(pid, dlg.text())

    def _delete_playlist(self, pid) -> None:
        pl = self._store.get(pid) if pid else None
        if pl is not None and MessageBox(_t('music.delete_playlist', 'Delete playlist'),
                                         _t('music.delete_playlist_confirm', 'Delete "{name}"?').format(name=pl.name),
                                         self.window()).exec():
            self._store.delete(pid)

    def _on_add_to_playlist(self, song) -> None:
        if not self._store.all_playlists():
            dlg = NameDialog(tr('music.new_playlist'), '', self.window())
            if not (dlg.exec() and dlg.text()):
                return
            pid = self._store.create(dlg.text()).id
        else:
            dlg = SaveToPlaylistDialog(self._store, self.window())
            if not dlg.exec():
                return
            pid = dlg.selected_playlist_id()
            if not pid:
                self._toast('warning', tr('music.no_playlist_selected_title'), tr('music.no_playlist_selected_content'))
                return
        name = self._store.get(pid).name
        if self._store.add_song(pid, song):
            self._toast('success', tr('music.saved_title'), tr('music.added_to_playlist', name=name), 2500)
        else:
            self._toast('info', tr('music.already_saved_title'), tr('music.already_in_playlist', name=name), 2500)


    def _on_download(self, song) -> None:
        if not _playable(song):
            self._toast('warning', tr('music.no_download_title'), tr('music.no_download_content'))
            return
        dest = getattr(app_settings, 'download_dir_music', None) or os.path.join(app_settings.download_dir, 'music')
        iid = None
        if self._dl_manager is not None:
            iid = self._dl_manager.add_external(game_name=song.song_name or tr('download.unknown'),
                                                console=song.singers or '', source='Music', category='music')
            self._dl_speed[iid] = (time.monotonic(), 0)
        key = _match_key(song)
        alts = [s for s in self._songs if s is not song and _playable(s) and _match_key(s) == key]
        task = _DownloadTask([song] + alts, dest)
        task.signals.progress.connect(lambda d, t, i=iid: self._dl_progress(i, d, t))
        task.signals.finished.connect(lambda ok, path, err, i=iid: self._dl_done(i, ok, path, err))
        if iid is not None and self._dl_manager is not None:
            self._dl_manager.register_external_cancel(iid, task.cancel)
        QThreadPool.globalInstance().start(task)

    def _download_all(self, songs: list) -> None:
        ok = [s for s in songs if _playable(s)]
        if not ok:
            self._toast('warning', tr('music.no_downloads_title'), tr('music.no_downloads_content'))
        for s in ok:
            self._on_download(s)

    def _dl_progress(self, iid, done: int, total: int) -> None:
        if iid is None or self._dl_manager is None:
            return
        kw = {'downloaded_bytes': done, 'total_bytes': total}
        prev, now = self._dl_speed.get(iid), time.monotonic()
        if prev and now - prev[0] >= 0.2:
            kw['speed_down_kbps'] = max(0.0, (done - prev[1]) / (now - prev[0]) / 1024)
            self._dl_speed[iid] = (now, done)
        self._dl_manager.update_external(iid, **kw)

    def _dl_done(self, iid, ok: bool, path: str, err: str) -> None:
        self._dl_speed.pop(iid, None)
        managed = iid is not None and self._dl_manager is not None
        if ok:
            if managed:
                self._dl_manager.complete_external(iid, path)
            return
        if err == 'Cancelled':
            return
        if managed:
            self._dl_manager.fail_external(iid, err or tr('music.download_failed_generic'))
        self._toast('error', tr('music.download_error_title'), (err or tr('music.status_failed'))[:120], 4000)

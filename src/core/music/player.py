from __future__ import annotations
import logging
import random
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from src.core.models import MusicItem as Song
logger = logging.getLogger(__name__)
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
    def queue_keys(self) -> set[str]:
        return {s.key for s in self._queue}

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _on_state_changed(self, state) -> None:
        self.state_changed.emit(_PLAYBACK_STATE_LABELS.get(state, 'stopped'))

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.has_next:
                self.next()
            else:
                self.stop()

    def _on_error(self, error, error_string: str) -> None:
        logger.warning('Music preview player error: %s', error_string)
        self.error.emit(error_string)
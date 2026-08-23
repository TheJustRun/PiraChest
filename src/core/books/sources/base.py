from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ...models import BookItem, MediaPage

IsCancelled = Optional[Callable[[], bool]]


class Cancelled(Exception):
    """Raised internally when a caller-supplied is_cancelled() check trips.

    Providers and manager functions raise this to unwind early out of
    in-progress network/parsing work. Background tasks run through
    core.worker.TaskManager, whose cancel_event is already set whenever this
    fires from a stale/obsolete request, so the exception is swallowed
    without ever reaching an on_error callback -- it's purely a fast exit,
    not a user-visible error.
    """


class BookProvider(ABC):
    """Common interface every book source (Gutenberg, Open Library, ...) implements.

    priority controls provider ordering when the manager fans a request out
    to every provider and merges the results (lower value = higher priority,
    i.e. listed/searched first).
    """

    key: str = ""
    display_name: str = ""
    priority: int = 100

    @abstractmethod
    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage: ...

    @abstractmethod
    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage: ...

    @abstractmethod
    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> Optional[BookItem]: ...

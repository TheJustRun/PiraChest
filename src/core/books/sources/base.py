from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ...models import BookItem, MediaPage

IsCancelled = Optional[Callable[[], bool]]


class Cancelled(Exception):
    pass


class BookProvider(ABC):
    key: str = ""
    display_name: str = ""
    priority: int = 100

    @abstractmethod
    def search(self, query: str, page: int, is_cancelled: IsCancelled = None) -> MediaPage: ...

    @abstractmethod
    def browse(self, page: int, is_cancelled: IsCancelled = None) -> MediaPage: ...

    @abstractmethod
    def details(self, book_id: str, is_cancelled: IsCancelled = None) -> Optional[BookItem]: ...

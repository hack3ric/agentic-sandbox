from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence


class Backend(ABC):
    name = "backend"

    @abstractmethod
    def create(self, identity, cwd: Path, wait: bool = False) -> None:
        raise NotImplementedError

    @abstractmethod
    def ssh(self, identity, cwd: Path, extra_args: Sequence[str]) -> int:
        raise NotImplementedError

    @abstractmethod
    def stop(
        self,
        identity,
        force: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def rebuild(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_running(self, identity) -> bool:
        raise NotImplementedError

    @abstractmethod
    def is_known(self, identity) -> bool:
        raise NotImplementedError

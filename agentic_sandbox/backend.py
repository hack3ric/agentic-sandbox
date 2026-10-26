from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from .mounts import DirectoryMount, recorded_mounts


class Backend(ABC):
    name = "backend"
    error_type: type[RuntimeError] = RuntimeError

    @abstractmethod
    def create(
        self,
        identity,
        cwd: Path,
        wait: bool = True,
        backend_args: Sequence[str] = (),
        mounts: Sequence[DirectoryMount] = (),
    ) -> None:
        raise NotImplementedError

    def require_mounts(self, identity, mounts: Sequence[DirectoryMount]) -> None:
        if {str(mount) for mount in mounts} != set(recorded_mounts(identity.state_file)):
            raise self.error_type(
                f"{identity.machine_name} has different or unrecorded mounts; "
                "stop and recreate it with the requested --mount options "
                "(stopping discards the sandbox's writable layer)"
            )

    @abstractmethod
    def wait_until_ready(self, identity) -> None:
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

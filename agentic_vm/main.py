#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import string
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from agentic_vm.consts import (
    APP_NAME,
    DEFAULT_STOP_POLL_INTERVAL_SECONDS,
    DEFAULT_STOP_TIMEOUT_SECONDS,
)

from .mkosi_backend import MkosiBackend


class AgenticVMError(RuntimeError):
    pass


MAX_MACHINE_NAME_LENGTH = 64


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    template_dir: Path
    home: Path
    data_dir: Path
    state_dir: Path
    image_dir: Path
    build_marker: Path

    @classmethod
    def detect(cls) -> "Paths":
        package_root = Path(__file__).resolve().parent
        repo_root = package_root.parent
        home = Path(os.environ.get("HOME", "~")).expanduser().resolve()
        data_home = Path(
            os.environ.get("XDG_DATA_HOME", home / ".local" / "share")
        ).expanduser()
        state_home = Path(
            os.environ.get("XDG_STATE_HOME", home / ".local" / "state")
        ).expanduser()
        data_dir = data_home / APP_NAME
        state_dir = state_home / APP_NAME
        image_dir = data_dir / "base-image"
        return cls(
            repo_root=repo_root,
            template_dir=package_root / "mkosi",
            home=home,
            data_dir=data_dir,
            state_dir=state_dir,
            image_dir=image_dir,
            build_marker=image_dir / ".image-built.json",
        )


@dataclass(frozen=True)
class VMIdentity:
    cwd: Path
    vm_id: str
    unit_name: str
    machine_name: str
    state_file: Path


@dataclass(frozen=True)
class VMState:
    cwd: str
    vm_id: str
    unit_name: str
    machine_name: str
    image_dir: str
    created_at: str


class AgenticVM:
    def __init__(
        self,
        paths: Paths,
        cwd: Path,
        backend=None,
        runner=None,
        sleeper=None,
        status_stream=None,
        spinner_enabled: bool | None = None,
        spinner_frame_interval_seconds=None,
    ):
        self.paths = paths
        self.cwd = cwd.resolve()
        self.backend = backend or MkosiBackend(
            paths,
            runner=runner,
            sleeper=sleeper,
            status_stream=status_stream,
            spinner_enabled=spinner_enabled,
            spinner_frame_interval_seconds=spinner_frame_interval_seconds
            if spinner_frame_interval_seconds is not None
            else 0.1,
            error_type=AgenticVMError,
        )

    def identity_for(self, cwd: Path | None = None) -> VMIdentity:
        resolved = (cwd or self.cwd).resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
        machine_name = self.machine_name_for(resolved, digest)
        return VMIdentity(
            cwd=resolved,
            vm_id=digest,
            unit_name=f"{machine_name}.service",
            machine_name=machine_name,
            state_file=self.paths.state_dir / f"{digest}.json",
        )

    def machine_name_for(self, cwd: Path, digest: str) -> str:
        prefix = f"{APP_NAME}-"
        suffix = f"-{digest}"
        max_slug_length = MAX_MACHINE_NAME_LENGTH - len(prefix) - len(suffix)
        encoded_path = self.encode_path_for_name(cwd)
        if len(encoded_path) > max_slug_length:
            encoded_path = encoded_path[:max_slug_length]
        return f"{prefix}{encoded_path}{suffix}"

    def encode_path_for_name(self, path: Path) -> str:
        safe_chars = set(string.ascii_lowercase + string.digits)
        encoded_parts: list[str] = []
        for part in path.parts:
            if part in (path.root, path.anchor):
                continue
            encoded = []
            for char in part.lower():
                if char in safe_chars:
                    encoded.append(char)
                else:
                    encoded.append(f"_{ord(char):02x}")
            encoded_parts.append("".join(encoded) or "_")
        return "-".join(encoded_parts) or "root"

    def create(self, wait: bool = False) -> None:
        identity = self.identity_for()
        self.ensure_directories()
        self.prune_stale_state(identity)
        if self.backend.is_running(identity):
            self.write_state(identity)
            print(f"{identity.unit_name} is already active")
            return
        self.backend.create(identity, identity.cwd, wait=wait)
        self.write_state(identity)
        print(f"created {identity.unit_name}")

    def run_vm(self, extra_args: Sequence[str]) -> int:
        self.create(wait=True)
        return self.ssh(extra_args)

    def ssh(self, extra_args: Sequence[str]) -> int:
        identity = self.identity_for()
        self.prune_stale_state(identity)
        return self.backend.ssh(identity, identity.cwd, extra_args)

    def stop(
        self,
        all_vms: bool = False,
        force: bool = False,
        timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        if all_vms:
            self.stop_all(
                force=force,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            return
        identity = self.identity_for()
        unit_exists = self.stop_identity(
            identity,
            force=force,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        print(
            f"stopped {identity.unit_name}"
            if unit_exists
            else f"{identity.unit_name} was not running"
        )

    def stop_all(
        self,
        force: bool = False,
        timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        stopped_any = False
        for identity in self.managed_identities():
            unit_exists = self.stop_identity(
                identity,
                force=force,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            stopped_any = stopped_any or unit_exists
            print(
                f"stopped {identity.unit_name}"
                if unit_exists
                else f"{identity.unit_name} was not running"
            )
        if not stopped_any:
            print("no managed VMs were running")

    def stop_identity(
        self,
        identity: VMIdentity,
        force: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        unit_exists = self.backend.stop(
            identity,
            force=force,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        identity.state_file.unlink(missing_ok=True)
        return unit_exists

    def rebuild(self) -> None:
        self.ensure_directories()
        active = self.active_managed_units()
        if active:
            joined = ", ".join(active)
            raise AgenticVMError(f"refusing rebuild while VMs are active: {joined}")
        self.backend.rebuild()
        print("rebuilt shared image")

    def ensure_directories(self) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)

    def active_managed_units(self) -> list[str]:
        active: list[str] = []
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        for state_file in sorted(self.paths.state_dir.glob("*.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                unit_name = state["unit_name"]
            except (OSError, ValueError, KeyError):
                continue
            identity = self.identity_from_state_file(state_file)
            if identity is None:
                continue
            if self.backend.is_running(identity):
                active.append(unit_name)
            else:
                state_file.unlink(missing_ok=True)
        return active

    def managed_identities(self) -> list[VMIdentity]:
        identities: list[VMIdentity] = []
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        for state_file in sorted(self.paths.state_dir.glob("*.json")):
            identity = self.identity_from_state_file(state_file)
            if identity is None:
                continue
            if self.backend.is_known(identity):
                identities.append(identity)
            else:
                state_file.unlink(missing_ok=True)
        return identities

    def identity_from_state_file(self, state_file: Path) -> VMIdentity | None:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cwd = Path(state["cwd"]).resolve()
            vm_id = state["vm_id"]
            unit_name = state["unit_name"]
            machine_name = state["machine_name"]
        except (OSError, ValueError, KeyError):
            return None
        return VMIdentity(
            cwd=cwd,
            vm_id=vm_id,
            unit_name=unit_name,
            machine_name=machine_name,
            state_file=state_file,
        )

    def prune_stale_state(self, identity: VMIdentity) -> None:
        if identity.state_file.exists() and not self.backend.is_running(identity):
            identity.state_file.unlink(missing_ok=True)

    def write_state(self, identity: VMIdentity) -> None:
        state = VMState(
            cwd=str(identity.cwd),
            vm_id=identity.vm_id,
            unit_name=identity.unit_name,
            machine_name=identity.machine_name,
            image_dir=str(self.paths.image_dir),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        identity.state_file.write_text(
            json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser(
        "create",
        help="Build the shared image if needed and create the VM for the current directory",
    )
    create_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the guest to become reachable before returning",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Create the VM for the current directory if needed, then connect via ssh",
    )
    run_parser.add_argument("ssh_args", nargs=argparse.REMAINDER)
    ssh_parser = subparsers.add_parser(
        "ssh", help="Connect to the VM for the current directory"
    )
    ssh_parser.add_argument("ssh_args", nargs=argparse.REMAINDER)
    stop_parser = subparsers.add_parser(
        "stop", help="Gracefully stop the VM for the current directory"
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Force-stop the transient unit without waiting for an in-guest shutdown",
    )
    stop_parser.add_argument(
        "--all",
        action="store_true",
        help="Stop all managed agentic VMs recorded in the state directory",
    )
    subparsers.add_parser("rebuild", help="Rebuild the shared mkosi image")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = AgenticVM(Paths.detect(), Path.cwd())
    try:
        if args.command == "create":
            app.create(wait=args.wait)
            return 0
        if args.command == "run":
            return app.run_vm(args.ssh_args)
        if args.command == "ssh":
            return app.ssh(args.ssh_args)
        if args.command == "stop":
            app.stop(all_vms=args.all, force=args.force)
            return 0
        if args.command == "rebuild":
            app.rebuild()
            return 0
    except AgenticVMError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

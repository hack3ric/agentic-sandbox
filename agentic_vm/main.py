#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, TextIO

from .spinner import DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS, Spinner

APP_NAME = "agentic-vm"
DEFAULT_RUNTIME_SIZE = "32G"
DEFAULT_BOOT_TIMEOUT_SECONDS = 60.0
DEFAULT_BOOT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_POLL_INTERVAL_SECONDS = 1.0
HOST_PACMAN_MIRRORLIST = Path("/etc/pacman.d/mirrorlist")
HOST_MIRRORLIST_TARGETS = (
    Path("mkosi.sandbox/etc/pacman.d/mirrorlist"),
    Path("mkosi.extra/etc/pacman.d/mirrorlist"),
)
HOST_BIND_MOUNTS = (
    Path(".local/share/opencode"),
    Path(".local/state/opencode"),
    Path(".config/opencode"),
    Path(".codex"),
    Path(".claude"),
)

DEFAULT_PACKAGES = [
    "base",
    "linux",
    "linux-headers",
    "openssh",
    # Development
    "nodejs",
    "npm",
    "rust",
    "rust-analyzer",
    "rustfmt",
    "python",
    "pyright",
    "base-devel",
    "clang",
    "cmake",
    "ninja",
    "typst",
    "tinymist",
    "ripgrep",
    # Agents
    "openai-codex",
    "opencode",
]


class AgenticVMError(RuntimeError):
    pass


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
        repo_root = Path(__file__).resolve().parent.parent
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
            template_dir=repo_root / "mkosi",
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
        runner=None,
        sleeper=None,
        status_stream: TextIO | None = None,
        spinner_enabled: bool | None = None,
        spinner_frame_interval_seconds: float = DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS,
    ):
        self.paths = paths
        self.cwd = cwd.resolve()
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.status_stream = status_stream or sys.stderr
        self.spinner_enabled = (
            spinner_enabled
            if spinner_enabled is not None
            else hasattr(self.status_stream, "isatty") and self.status_stream.isatty()
        )
        self.spinner_frame_interval_seconds = spinner_frame_interval_seconds

    def identity_for(self, cwd: Path | None = None) -> VMIdentity:
        resolved = (cwd or self.cwd).resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
        return VMIdentity(
            cwd=resolved,
            vm_id=digest,
            unit_name=f"{APP_NAME}-{digest}.service",
            machine_name=f"{APP_NAME}-{digest}",
            state_file=self.paths.state_dir / f"{digest}.json",
        )

    def create(self, wait: bool = False) -> None:
        identity = self.identity_for()
        self.ensure_directories()
        self.ensure_mkosi_workspace()
        self.ensure_ssh_credentials()
        self.prune_stale_state(identity)
        if self.is_unit_active(identity.unit_name):
            self.write_state(identity)
            print(f"{identity.unit_name} is already active")
            return
        self.ensure_image_built()
        command = [
            *self.mkosi_cmd(
            "--vmm=qemu",
            "--machine",
            identity.machine_name,
            "--ephemeral=yes",
            "--console=read-only",
            "--runtime-size",
            DEFAULT_RUNTIME_SIZE,
            "--runtime-network=user",
            "--register=no",
            "vm",
            ),
            *self.runtime_tree_args(identity.cwd),
        ]
        self.run(
            [
                "systemd-run",
                "--user",
                "--unit",
                identity.unit_name,
                "--description",
                f"{APP_NAME} VM for {identity.cwd}",
                "--collect",
                "--service-type=exec",
                "--working-directory",
                str(self.paths.image_dir),
                *command,
            ]
        )
        self.write_state(identity)
        print(f"created {identity.unit_name}")
        if wait:
            self.wait_for_machine()

    def runtime_tree_args(self, cwd: Path) -> list[str]:
        args = ["--runtime-tree", f"{cwd}:{cwd}"]
        for relative in HOST_BIND_MOUNTS:
            source = self.paths.home / relative
            if source.exists():
                args.extend(["--runtime-tree", f"{source}:{Path('/root') / relative}"])
        return args

    def run_vm(self, extra_args: Sequence[str]) -> int:
        self.create(wait=True)
        return self.ssh(extra_args)

    def ssh(self, extra_args: Sequence[str]) -> int:
        identity = self.identity_for()
        self.prune_stale_state(identity)
        if not self.is_unit_active(identity.unit_name):
            raise AgenticVMError(f"{identity.unit_name} is not active")
        command = self.mkosi_cmd("--machine", identity.machine_name, "ssh")
        command.extend(["--", *self.ssh_remote_args(identity, extra_args)])
        return self.run(command).returncode

    def should_allocate_ssh_tty(self) -> bool:
        return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    def ssh_remote_args(
        self, identity: VMIdentity, extra_args: Sequence[str]
    ) -> list[str]:
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        remote_cwd = shlex.quote(str(identity.cwd))
        if not forwarded:
            return [
                "-t",
                f"cd {remote_cwd} && exec ${{SHELL:-/bin/bash}} -l",
            ]
        remote_command = " ".join(shlex.quote(arg) for arg in forwarded)
        if self.should_allocate_ssh_tty():
            return ["-t", f"cd {remote_cwd} && exec {remote_command}"]
        return [f"cd {remote_cwd} && exec {remote_command}"]

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
        unit_exists = self.unit_known(identity.unit_name)
        if unit_exists:
            if not force:
                self.request_graceful_stop(identity)
                if not self.wait_for_unit_inactive(
                    identity,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                ):
                    self.force_stop_unit(identity)
            else:
                self.force_stop_unit(identity)
        identity.state_file.unlink(missing_ok=True)
        return unit_exists

    def rebuild(self) -> None:
        self.ensure_directories()
        active = self.active_managed_units()
        if active:
            joined = ", ".join(active)
            raise AgenticVMError(f"refusing rebuild while VMs are active: {joined}")
        self.ensure_mkosi_workspace(force=True)
        self.ensure_ssh_credentials()
        self.run(self.mkosi_cmd("-f", "build"))
        self.write_build_marker()
        print("rebuilt shared image")

    def ensure_directories(self) -> None:
        self.paths.image_dir.mkdir(parents=True, exist_ok=True)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)

    def ensure_mkosi_workspace(self, force: bool = False) -> None:
        for source in sorted(self.paths.template_dir.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(self.paths.template_dir)
            if relative.suffix == ".in":
                relative = relative.with_suffix("")
                content = self.render_template(source)
            else:
                content = source.read_text(encoding="utf-8")
            target = self.paths.image_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                force
                or not target.exists()
                or target.read_text(encoding="utf-8") != content
            ):
                target.write_text(content, encoding="utf-8")
            source_mode = source.stat().st_mode & 0o777
            if target.stat().st_mode & 0o777 != source_mode:
                target.chmod(source_mode)
        self.sync_host_mirrorlist(force=force)

    def sync_host_mirrorlist(self, force: bool = False) -> None:
        if not HOST_PACMAN_MIRRORLIST.exists():
            raise AgenticVMError(
                f"host pacman mirrorlist is missing: {HOST_PACMAN_MIRRORLIST}"
            )
        content = HOST_PACMAN_MIRRORLIST.read_text(encoding="utf-8")
        for relative in HOST_MIRRORLIST_TARGETS:
            target = self.paths.image_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                force
                or not target.exists()
                or target.read_text(encoding="utf-8") != content
            ):
                target.write_text(content, encoding="utf-8")

    def render_template(self, source: Path) -> str:
        template = source.read_text(encoding="utf-8")
        return template.replace("@PACKAGES@", ",".join(DEFAULT_PACKAGES))

    def ensure_ssh_credentials(self) -> None:
        key = self.paths.image_dir / "mkosi.key"
        crt = self.paths.image_dir / "mkosi.crt"
        if key.exists() and crt.exists():
            return
        self.run(["mkosi", "--directory", str(self.paths.image_dir), "genkey"])

    def ensure_image_built(self) -> None:
        if self.paths.build_marker.exists():
            return
        self.run(self.mkosi_cmd("build"))
        self.write_build_marker()

    def wait_for_machine(
        self,
        timeout_seconds: float = DEFAULT_BOOT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_BOOT_POLL_INTERVAL_SECONDS,
    ) -> None:
        identity = self.identity_for()
        deadline = time.monotonic() + timeout_seconds
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to boot")
        while True:
            if not self.is_unit_active(identity.unit_name):
                spinner.finish()
                raise AgenticVMError(f"{identity.unit_name} is not active")
            result = self.run(
                self.mkosi_cmd("--machine", identity.machine_name, "ssh", "--", "true"),
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                spinner.finish()
                return
            if time.monotonic() >= deadline:
                spinner.finish()
                raise AgenticVMError(
                    f"timed out waiting for {identity.machine_name} to become reachable"
                )
            self.sleeper(poll_interval_seconds)

    def request_graceful_stop(self, identity: VMIdentity) -> None:
        self.run(
            self.mkosi_cmd(
                "--machine",
                identity.machine_name,
                "ssh",
                "--",
                "poweroff",
            ),
            check=False,
            capture_output=True,
            text=True,
        )

    def wait_for_unit_inactive(
        self,
        identity: VMIdentity,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        remaining = max(timeout_seconds, 0.0)
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to power off")
        while True:
            if not self.is_unit_active(identity.unit_name):
                spinner.finish()
                return True
            if remaining <= 0:
                spinner.finish()
                return False
            sleep_duration = min(poll_interval_seconds, remaining)
            self.sleeper(sleep_duration)
            remaining -= sleep_duration

    def force_stop_unit(self, identity: VMIdentity) -> None:
        stop_result = self.run(
            ["systemctl", "--user", "stop", identity.unit_name], check=False
        )
        if stop_result.returncode != 0 or self.is_unit_failed(identity.unit_name):
            self.run(
                ["systemctl", "--user", "reset-failed", identity.unit_name],
                check=False,
            )

    def active_managed_units(self) -> list[str]:
        active: list[str] = []
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        for state_file in sorted(self.paths.state_dir.glob("*.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                unit_name = state["unit_name"]
            except (OSError, ValueError, KeyError):
                continue
            if self.is_unit_active(unit_name):
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
            if self.unit_known(identity.unit_name):
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
        if identity.state_file.exists() and not self.is_unit_active(identity.unit_name):
            identity.state_file.unlink(missing_ok=True)

    def unit_known(self, unit_name: str) -> bool:
        result = self.run(
            ["systemctl", "--user", "status", unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 or "Loaded:" in (result.stdout or "") + (
            result.stderr or ""
        )

    def is_unit_active(self, unit_name: str) -> bool:
        result = self.run(
            ["systemctl", "--user", "is-active", unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "active"

    def is_unit_failed(self, unit_name: str) -> bool:
        result = self.run(
            ["systemctl", "--user", "is-failed", unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "failed"

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

    def write_build_marker(self) -> None:
        payload = {
            "image_dir": str(self.paths.image_dir),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.paths.build_marker.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def mkosi_cmd(self, *args: str) -> list[str]:
        return ["mkosi", "--directory", str(self.paths.image_dir), *args]

    def run(self, command: Sequence[str], **kwargs):
        kwargs.setdefault("check", True)
        return self.runner(list(command), **kwargs)

    def make_spinner(self, message: str) -> Spinner:
        spinner = Spinner(
            self.status_stream,
            message,
            self.spinner_frame_interval_seconds,
        )
        if self.spinner_enabled:
            spinner.start()
        return spinner


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

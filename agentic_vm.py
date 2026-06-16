#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

APP_NAME = "agentic-vm"
DEFAULT_PACKAGES = "base,linux,openssh"
DEFAULT_RUNTIME_SIZE = "32G"


class AgenticVMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    home: Path
    data_dir: Path
    state_dir: Path
    image_dir: Path
    config_file: Path
    build_marker: Path

    @classmethod
    def detect(cls) -> "Paths":
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
            home=home,
            data_dir=data_dir,
            state_dir=state_dir,
            image_dir=image_dir,
            config_file=image_dir / "mkosi.conf",
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
    def __init__(self, paths: Paths, cwd: Path, runner=None):
        self.paths = paths
        self.cwd = cwd.resolve()
        self.runner = runner or subprocess.run

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

    def start(self) -> None:
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
        command = self.mkosi_cmd(
            "--vmm=qemu",
            "--machine",
            identity.machine_name,
            "--ephemeral=yes",
            "--console=read-only",
            "--runtime-size",
            DEFAULT_RUNTIME_SIZE,
            "--runtime-network=user",
            "--runtime-tree",
            f"{identity.cwd}:{identity.cwd}",
            "--register=no",
            "vm",
        )
        # print(" ".join(command))
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
        print(f"started {identity.unit_name}")

    def ssh(self, extra_args: Sequence[str]) -> int:
        identity = self.identity_for()
        self.prune_stale_state(identity)
        if not self.is_unit_active(identity.unit_name):
            raise AgenticVMError(f"{identity.unit_name} is not active")
        command = self.mkosi_cmd("--machine", identity.machine_name, "ssh")
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        if forwarded:
            command.extend(["--", *forwarded])
        return self.run(command).returncode

    def stop(self) -> None:
        identity = self.identity_for()
        unit_exists = self.unit_known(identity.unit_name)
        if unit_exists:
            self.run(["systemctl", "--user", "stop", identity.unit_name], check=False)
            self.run(
                ["systemctl", "--user", "reset-failed", identity.unit_name], check=False
            )
        if identity.state_file.exists():
            identity.state_file.unlink()
        print(
            f"stopped {identity.unit_name}"
            if unit_exists
            else f"{identity.unit_name} was not running"
        )

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
        if force or not self.paths.config_file.exists():
            self.paths.config_file.write_text(
                self.default_mkosi_config(), encoding="utf-8"
            )

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

    @staticmethod
    def default_mkosi_config() -> str:
        return "\n".join(
            [
                "[Distribution]",
                "Distribution=arch",
                "",
                "[Output]",
                "Format=disk",
                "",
                "[Content]",
                f"Packages={DEFAULT_PACKAGES}",
                "Bootable=yes",
                # "Autologin=yes",
                "",
            ]
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "start",
        help="Build the shared image if needed and start the VM for the current directory",
    )
    ssh_parser = subparsers.add_parser(
        "ssh", help="Connect to the VM for the current directory"
    )
    ssh_parser.add_argument("ssh_args", nargs=argparse.REMAINDER)
    subparsers.add_parser("stop", help="Stop the VM for the current directory")
    subparsers.add_parser("rebuild", help="Rebuild the shared mkosi image")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = AgenticVM(Paths.detect(), Path.cwd())
    try:
        if args.command == "start":
            app.start()
            return 0
        if args.command == "ssh":
            return app.ssh(args.ssh_args)
        if args.command == "stop":
            app.stop()
            return 0
        if args.command == "rebuild":
            app.rebuild()
            return 0
    except AgenticVMError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"{APP_NAME}: command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        return exc.returncode or 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

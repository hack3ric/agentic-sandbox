from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from agentic_sandbox.consts import (
    HOST_BIND_MOUNTS,
    HOST_PACMAN_MIRRORLIST,
    PODMAN_PACKAGES,
)

from .backend import Backend
from .spinner import DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS, Spinner

DEFAULT_BOOT_TIMEOUT_SECONDS = 30.0
DEFAULT_BOOT_POLL_INTERVAL_SECONDS = 1.0
ARCH_LINUX_IMAGE = "docker.io/archlinux:latest"
GUI_LABEL = "io.agentic-sandbox.gui"
GUI_IMAGE_VERSION = 1
GUI_RUNTIME_DIR = Path("/run/agentic-sandbox-gui")
GUI_WAYLAND_DISPLAY = "wayland-0"
GUI_X_DISPLAY = ":0"
KVM_DEVICE = Path("/dev/kvm")
KVM_LABEL = "io.agentic-sandbox.kvm"


class PodmanBackend(Backend):
    name = "podman"

    def __init__(
        self,
        paths,
        runner=None,
        sleeper=None,
        status_stream: TextIO | None = None,
        spinner_enabled: bool | None = None,
        spinner_frame_interval_seconds: float = DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS,
        error_type: type[RuntimeError] = RuntimeError,
    ):
        self.paths = paths
        self.image_dir = paths.podman_image_dir
        self.template_dir = paths.podman_template_dir
        self.build_marker = paths.podman_build_marker
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.status_stream = status_stream or sys.stderr
        self.spinner_enabled = (
            spinner_enabled
            if spinner_enabled is not None
            else hasattr(self.status_stream, "isatty") and self.status_stream.isatty()
        )
        self.spinner_frame_interval_seconds = spinner_frame_interval_seconds
        self.error_type = error_type

    def create(
        self,
        identity,
        cwd: Path,
        wait: bool = True,
        backend_args: Sequence[str] = (),
        gui: bool = False,
        kvm: bool = False,
    ) -> None:
        known = self.is_known(identity)
        if gui and known:
            self.require_gui(identity)
        if kvm and known:
            self.require_kvm(identity)
        command = (
            self.create_cmd(identity, cwd, backend_args, gui=gui, kvm=kvm)
            if not known
            else None
        )
        self.ensure_image_dir()
        self.ensure_workspace()
        self.ensure_image_built(gui=gui)
        if command is not None:
            self.run(command)
        if not self.is_running(identity):
            self.run(["podman", "start", identity.machine_name])
        if wait:
            self.wait_for_container(identity)

    def wait_until_ready(self, identity) -> None:
        self.wait_for_container(identity)

    def ssh(self, identity, cwd: Path, extra_args: Sequence[str]) -> int:
        if not self.is_running(identity):
            raise self.error_type(f"{identity.unit_name} is not active")
        if self.gui_enabled(identity):
            self.wait_for_container(identity)
        return self.run(self.exec_cmd(identity, cwd, extra_args)).returncode

    def stop(
        self,
        identity,
        force: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        del poll_interval_seconds
        if not self.is_known(identity):
            return False
        if force:
            self.remove_container(identity, force=True)
            return True
        stop_result = self.run(
            [
                "podman",
                "stop",
                "--time",
                str(max(int(timeout_seconds), 0)),
                identity.machine_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.remove_container(identity, force=stop_result.returncode != 0)
        return True

    def rebuild(self) -> None:
        self.ensure_image_dir()
        self.ensure_workspace(force=True)
        self.run(["podman", "pull", ARCH_LINUX_IMAGE])
        self.run(self.build_cmd())
        self.write_build_marker()

    def is_running(self, identity) -> bool:
        result = self.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                "{{.State.Running}}",
                identity.machine_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "true"

    def is_known(self, identity) -> bool:
        result = self.run(
            ["podman", "container", "exists", identity.machine_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def create_cmd(
        self,
        identity,
        cwd: Path,
        backend_args: Sequence[str] = (),
        gui: bool = False,
        kvm: bool = False,
    ) -> list[str]:
        forwarded = list(backend_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        options = self.gui_args() if gui else []
        if kvm:
            options.extend(self.kvm_args())
        entrypoint = (
            [
                "xwayland-satellite",
                GUI_X_DISPLAY,
                "-nolisten",
                "tcp",
                "-glamor",
                "none",
            ]
            if gui
            else [
                "/bin/sh",
                "-lc",
                'trap "exit 0" TERM INT; while :; do sleep 3600 & wait $! || :; done',
            ]
        )
        return [
            "podman",
            "create",
            "--name",
            identity.machine_name,
            "--hostname",
            identity.machine_name,
            "--workdir",
            str(cwd),
            *self.mount_args(cwd),
            *options,
            *forwarded,
            self.image_tag,
            *entrypoint,
        ]

    def gui_args(self) -> list[str]:
        display = Path(os.environ.get("WAYLAND_DISPLAY") or "wayland-0")
        if not display.is_absolute():
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime_dir or not Path(runtime_dir).is_absolute():
                raise self.error_type(
                    "--gui requires a Wayland session: set XDG_RUNTIME_DIR "
                    "to an absolute path or WAYLAND_DISPLAY to an absolute socket path"
                )
            display = Path(runtime_dir) / display
        if not display.is_socket():
            raise self.error_type(
                f"Wayland socket does not exist or is not a socket: {display}"
            )
        return [
            "--init",
            "--label",
            f"{GUI_LABEL}=wayland",
            "--tmpfs",
            f"{GUI_RUNTIME_DIR}:rw,mode=0700",
            "--volume",
            f"{display}:{GUI_RUNTIME_DIR / GUI_WAYLAND_DISPLAY}:ro",
            "--env",
            f"XDG_RUNTIME_DIR={GUI_RUNTIME_DIR}",
            "--env",
            f"WAYLAND_DISPLAY={GUI_WAYLAND_DISPLAY}",
            "--env",
            f"DISPLAY={GUI_X_DISPLAY}",
        ]

    def gui_enabled(self, identity) -> bool:
        result = self.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                '{{ index .Config.Labels "' + GUI_LABEL + '" }}',
                identity.machine_name,
            ],
            capture_output=True,
            text=True,
        )
        return (result.stdout or "").strip() == "wayland"

    def require_gui(self, identity) -> None:
        if not self.gui_enabled(identity):
            raise self.error_type(
                f"{identity.machine_name} was created without --gui; "
                "stop and recreate it with create --gui "
                "(stopping removes the container's writable layer)"
            )

    def kvm_args(self) -> list[str]:
        try:
            is_device = KVM_DEVICE.is_char_device()
        except OSError as exc:
            raise self.error_type(f"cannot inspect {KVM_DEVICE}: {exc}") from exc
        if not is_device:
            raise self.error_type(
                f"--kvm requires the host character device {KVM_DEVICE}; "
                "check that KVM is enabled on the host"
            )
        if not os.access(KVM_DEVICE, os.R_OK | os.W_OK):
            raise self.error_type(
                f"--kvm requires read/write access to {KVM_DEVICE}; "
                "check the device permissions and your host kvm group membership"
            )
        return [
            "--device",
            f"{KVM_DEVICE}:{KVM_DEVICE}:rw",
            "--group-add",
            "keep-groups",
            "--label",
            f"{KVM_LABEL}=enabled",
        ]

    def kvm_enabled(self, identity) -> bool:
        result = self.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                '{{ index .Config.Labels "' + KVM_LABEL + '" }}',
                identity.machine_name,
            ],
            capture_output=True,
            text=True,
        )
        return (result.stdout or "").strip() == "enabled"

    def require_kvm(self, identity) -> None:
        if not self.kvm_enabled(identity):
            raise self.error_type(
                f"{identity.machine_name} was created without --kvm; "
                "stop and recreate it with create --kvm "
                "(stopping removes the container's writable layer)"
            )

    def exec_cmd(self, identity, cwd: Path, extra_args: Sequence[str]) -> list[str]:
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        command = ["podman", "exec", "--workdir", str(cwd)]
        if not forwarded or self.should_allocate_tty():
            command.append("-it")
        command.append(identity.machine_name)
        if not forwarded:
            command.extend(
                [
                    "/bin/sh",
                    "-lc",
                    f"cd {shlex.quote(str(cwd))} && exec ${{SHELL:-/bin/bash}} -l",
                ]
            )
            return command
        remote_command = " ".join(shlex.quote(arg) for arg in forwarded)
        command.extend(
            ["/bin/sh", "-lc", f"cd {shlex.quote(str(cwd))} && exec {remote_command}"]
        )
        return command

    def mount_args(self, cwd: Path) -> list[str]:
        args = ["--volume", f"{cwd}:{cwd}"]
        for relative in HOST_BIND_MOUNTS:
            source = self.paths.home / relative
            if source.exists():
                args.extend(["--volume", f"{source}:{Path('/root') / relative}"])
        return args

    @property
    def image_tag(self) -> str:
        return "localhost/agentic-sandbox:base"

    def build_cmd(self) -> list[str]:
        return [
            "podman",
            "build",
            "--no-cache",
            "--tag",
            self.image_tag,
            "--file",
            str(self.image_dir / "Containerfile"),
            str(self.image_dir),
        ]

    def ensure_image_dir(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def ensure_workspace(self, force: bool = False) -> None:
        for source in sorted(self.template_dir.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(self.template_dir)
            if relative.suffix == ".in":
                relative = relative.with_suffix("")
                content = self.render_template(source)
            else:
                content = source.read_text(encoding="utf-8")
            target = self.image_dir / relative
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

    def render_template(self, source: Path) -> str:
        template = source.read_text(encoding="utf-8")
        return template.replace("@PACKAGES@", " \\\n        ".join(PODMAN_PACKAGES))

    def sync_host_mirrorlist(self, force: bool = False) -> None:
        content = ""
        if HOST_PACMAN_MIRRORLIST.exists():
            content = HOST_PACMAN_MIRRORLIST.read_text(encoding="utf-8")
        target = self.image_dir / "host-mirrorlist"
        if (
            force
            or not target.exists()
            or target.read_text(encoding="utf-8") != content
        ):
            target.write_text(content, encoding="utf-8")

    def ensure_image_built(self, gui: bool = False) -> None:
        if self.build_marker.exists():
            if not gui:
                return
            try:
                marker = json.loads(self.build_marker.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                marker = {}
            if (
                isinstance(marker, dict)
                and marker.get("gui_version") == GUI_IMAGE_VERSION
            ):
                return
        self.run(self.build_cmd())
        self.write_build_marker()

    def wait_for_container(
        self,
        identity,
        timeout_seconds: float = DEFAULT_BOOT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_BOOT_POLL_INTERVAL_SECONDS,
    ) -> None:
        gui = self.gui_enabled(identity)
        probe = ["timeout", "2", "xdpyinfo"] if gui else ["true"]
        deadline = time.monotonic() + timeout_seconds
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to start")
        try:
            while True:
                if not self.is_running(identity):
                    raise self.error_type(
                        f"{identity.unit_name} is not active; "
                        f"check podman logs {identity.machine_name}"
                    )
                result = self.run(
                    ["podman", "exec", identity.machine_name, *probe],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return
                if time.monotonic() >= deadline:
                    target = "GUI display" if gui else "container"
                    raise self.error_type(
                        f"timed out waiting for {identity.machine_name} {target}; "
                        f"check podman logs {identity.machine_name}"
                    )
                self.sleeper(poll_interval_seconds)
        finally:
            spinner.finish()

    def remove_container(self, identity, force: bool) -> None:
        command = ["podman", "rm"]
        if force:
            command.extend(["-f", "-t", "0"])
        command.append(identity.machine_name)
        self.run(command, check=False, capture_output=True, text=True)

    def write_build_marker(self) -> None:
        payload = {
            "image_dir": str(self.image_dir),
            "gui_version": GUI_IMAGE_VERSION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.build_marker.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def should_allocate_tty(self) -> bool:
        return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

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

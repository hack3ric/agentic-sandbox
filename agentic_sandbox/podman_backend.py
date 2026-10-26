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
    PODMAN_GUI_PACKAGES,
    PODMAN_PACKAGES,
)

from .backend import Backend
from .spinner import DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS, Spinner

DEFAULT_BOOT_TIMEOUT_SECONDS = 30.0
DEFAULT_BOOT_POLL_INTERVAL_SECONDS = 1.0
ARCH_LINUX_IMAGE = "docker.io/archlinux:latest"
BASE_IMAGE_VERSION = 1
GUI_LABEL = "io.agentic-sandbox.gui"
GUI_IMAGE_VERSION = 2
GUI_RUNTIME_DIR = Path("/run/agentic-sandbox-gui")
GUI_WAYLAND_DISPLAY = "wayland-0"
GUI_X_DISPLAY = ":0"
KVM_DEVICE = Path("/dev/kvm")
KVM_LABEL = "io.agentic-sandbox.kvm"
GRAPHICS_LABEL = "io.agentic-sandbox.graphics"
DRI_DEVICE_DIR = Path("/dev/dri")
DRM_SYSFS_DIR = Path("/sys/class/drm")
NVIDIA_VENDOR_ID = 0x10DE


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
        self.gui_build_marker = self.image_dir / ".gui-image-built.json"
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
        graphics: bool = False,
    ) -> None:
        known = self.is_known(identity)
        if gui and known:
            self.require_gui(identity)
        if kvm and known:
            self.require_kvm(identity)
        if graphics and known:
            self.require_graphics(identity)
        if not known:
            command = self.create_cmd(
                identity, cwd, backend_args, gui=gui, kvm=kvm, graphics=graphics
            )
            self.ensure_image_dir()
            self.ensure_workspace()
            self.ensure_image_built(gui=gui, graphics=graphics)
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
        self.build_image()

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
        graphics: bool = False,
    ) -> list[str]:
        forwarded = list(backend_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        options = self.gui_args() if gui else []
        if kvm:
            options.extend(self.kvm_args())
        if graphics:
            options.extend(self.graphics_args())
        if kvm or graphics:
            options.extend(["--group-add", "keep-groups"])
        entrypoint = (
            [
                "xwayland-satellite",
                GUI_X_DISPLAY,
                "-nolisten",
                "tcp",
            ]
            if gui
            else [
                "/bin/sh",
                "-lc",
                'trap "exit 0" TERM INT; while :; do sleep 3600 & wait $! || :; done',
            ]
        )
        if gui and not graphics:
            entrypoint.append("-shm")
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
            self.gui_image_tag if gui or graphics else self.image_tag,
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

    def graphics_devices(self) -> list[Path]:
        devices = []
        for device in sorted(DRI_DEVICE_DIR.glob("renderD*")):
            if self.uses_nvidia_driver(device):
                continue
            try:
                is_device = device.is_char_device()
            except OSError as exc:
                raise self.error_type(f"cannot inspect {device}: {exc}") from exc
            if not is_device:
                raise self.error_type(
                    f"--graphics requires a host character device at {device}"
                )
            if not os.access(device, os.R_OK | os.W_OK):
                raise self.error_type(
                    f"--graphics requires read/write access to {device}; "
                    "check the device permissions and your host render/video "
                    "group membership"
                )
            devices.append(device)
        if not devices:
            raise self.error_type(
                "--graphics requires a Mesa-compatible GPU render device in "
                f"{DRI_DEVICE_DIR}; "
                "check that a supported graphics driver is loaded on the host "
                "(Nouveau is supported; NVIDIA's own drivers, including nvidia-open, "
                "are not supported)"
            )
        return devices

    def uses_nvidia_driver(self, device: Path) -> bool:
        sysfs_device = DRM_SYSFS_DIR / device.name / "device"
        try:
            try:
                vendor = int((sysfs_device / "vendor").read_text(encoding="utf-8"), 16)
            except FileNotFoundError:
                vendor = None
            try:
                driver = (sysfs_device / "driver").resolve(strict=True).name
            except FileNotFoundError:
                driver = None
        except (OSError, ValueError) as exc:
            raise self.error_type(
                f"cannot identify GPU render device {device} through {sysfs_device}: {exc}"
            ) from exc
        if vendor is None and driver is None:
            raise self.error_type(
                f"cannot identify GPU render device {device}; "
                f"check that vendor or driver information is available in {sysfs_device}"
            )
        return driver == "nvidia" or (vendor == NVIDIA_VENDOR_ID and driver != "nouveau")

    def graphics_args(self) -> list[str]:
        args = []
        for device in self.graphics_devices():
            args.extend(["--device", f"{device}:{device}:rw"])
        args.extend(["--label", f"{GRAPHICS_LABEL}=enabled"])
        return args

    def graphics_enabled(self, identity) -> bool:
        result = self.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                '{{ index .Config.Labels "' + GRAPHICS_LABEL + '" }}',
                identity.machine_name,
            ],
            capture_output=True,
            text=True,
        )
        return (result.stdout or "").strip() == "enabled"

    def require_graphics(self, identity) -> None:
        if not self.graphics_enabled(identity):
            raise self.error_type(
                f"{identity.machine_name} was created without --graphics; "
                "stop and recreate it with create --graphics "
                "(stopping removes the container's writable layer)"
            )

    def exec_cmd(self, identity, cwd: Path, extra_args: Sequence[str]) -> list[str]:
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        command = ["podman", "exec", "--workdir", str(cwd)]
        term = os.environ.get("TERM")
        if term is not None:
            command.extend(["--env", f"TERM={term}"])
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

    @property
    def gui_image_tag(self) -> str:
        return "localhost/agentic-sandbox:gui"

    def build_cmd(self, gui: bool = False) -> list[str]:
        return [
            "podman",
            "build",
            "--no-cache",
            *(["--pull=never"] if gui else []),
            "--tag",
            self.gui_image_tag if gui else self.image_tag,
            "--file",
            str(self.image_dir / ("Containerfile.gui" if gui else "Containerfile")),
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
        template = template.replace("@BASE_IMAGE@", self.image_tag)
        template = template.replace("@GUI_PACKAGES@", " ".join(PODMAN_GUI_PACKAGES))
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

    def ensure_image_built(self, gui: bool = False, graphics: bool = False) -> None:
        if not self.image_is_built():
            self.build_image()
        if (gui or graphics) and not self.image_is_built(gui=True):
            self.build_image(gui=True)

    def image_is_built(self, gui: bool = False) -> bool:
        marker_path = self.gui_build_marker if gui else self.build_marker
        version = GUI_IMAGE_VERSION if gui else BASE_IMAGE_VERSION
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        return isinstance(marker, dict) and marker.get("image_version") == version

    def build_image(self, gui: bool = False) -> None:
        marker_path = self.gui_build_marker if gui else self.build_marker
        marker_path.unlink(missing_ok=True)
        if not gui:
            self.gui_build_marker.unlink(missing_ok=True)
        self.run(self.build_cmd(gui=gui))
        self.write_build_marker(gui=gui)

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

    def write_build_marker(self, gui: bool = False) -> None:
        marker_path = self.gui_build_marker if gui else self.build_marker
        payload = {
            "image_dir": str(self.image_dir),
            "image_version": GUI_IMAGE_VERSION if gui else BASE_IMAGE_VERSION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        marker_path.write_text(
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

from pathlib import Path

APP_NAME = "agentic-sandbox"
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_POLL_INTERVAL_SECONDS = 1.0

MAX_MACHINE_NAME_LENGTH = 64

DEFAULT_PACKAGES = [
    "archlinux-keyring",
    "base",
    "linux",
    "linux-headers",
    "openssh",
    "git",
    "nodejs",
    "npm",
    "rust",
    "rust-analyzer",
    "rustfmt",
    "python",
    "python-pip",
    "pyright",
    "base-devel",
    "clang",
    "cmake",
    "ninja",
    "typst",
    "tinymist",
    "ripgrep",
    "openai-codex",
    "opencode",
]

PODMAN_PACKAGES = [
    package
    for package in DEFAULT_PACKAGES
    if package not in {"base", "linux", "linux-headers", "rustfmt"}
]

HOST_BIND_MOUNTS = (
    Path(".local/share/opencode"),
    Path(".local/state/opencode"),
    Path(".config/opencode"),
    Path(".codex"),
    Path(".claude"),
)

HOST_PACMAN_MIRRORLIST = Path("/etc/pacman.d/mirrorlist")

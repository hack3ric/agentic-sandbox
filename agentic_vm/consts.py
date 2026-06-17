from pathlib import Path

APP_NAME = "agentic-vm"
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_POLL_INTERVAL_SECONDS = 1.0

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

HOST_BIND_MOUNTS = (
    Path(".local/share/opencode"),
    Path(".local/state/opencode"),
    Path(".config/opencode"),
    Path(".codex"),
    Path(".claude"),
)

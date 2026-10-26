from __future__ import annotations

import json
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirectoryMount:
    source: Path
    target: Path

    def __str__(self) -> str:
        return f"{self.source}:{self.target}"


def parse_mounts(
    specs: Sequence[str], cwd: Path, reserved_targets: Sequence[Path]
) -> list[DirectoryMount]:
    mounts: list[DirectoryMount] = []
    targets = list(reserved_targets)
    for spec in specs:
        parts = spec.split(":")
        if len(parts) > 2 or not all(parts):
            raise ValueError(f"invalid --mount {spec!r}: expected HOST[:GUEST]")
        if any(ord(char) < 32 or ord(char) == 127 for char in spec):
            raise ValueError("--mount paths cannot contain control characters")
        try:
            source = Path(parts[0]).expanduser()
            if not source.is_absolute():
                source = cwd / source
            source = source.resolve(strict=True)
            if not source.is_dir():
                raise ValueError("source is not a directory")
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"invalid --mount source {parts[0]!r}: {exc}") from exc
        if ":" in str(source) or any(
            ord(char) < 32 or ord(char) == 127 for char in str(source)
        ):
            raise ValueError(
                "--mount source cannot contain colons or control characters"
            )
        target_text = parts[1] if len(parts) == 2 else str(source)
        if not target_text.startswith("/"):
            raise ValueError(f"--mount guest path must be absolute: {target_text!r}")
        # Guest paths must not resolve symlinks on the host.
        target = Path(posixpath.normpath("/" + target_text.lstrip("/")))
        mount = DirectoryMount(source, target)
        if mount in mounts:
            continue
        for existing in targets:
            if target.is_relative_to(existing) or existing.is_relative_to(target):
                raise ValueError(
                    f"--mount destination {target} conflicts with mount at {existing}"
                )
        mounts.append(mount)
        targets.append(target)
    return mounts


def recorded_mounts(state_file: Path) -> list[str]:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    mounts = state.get("mounts", []) if isinstance(state, dict) else []
    if not isinstance(mounts, list) or not all(isinstance(m, str) for m in mounts):
        return []
    return mounts

from pathlib import Path

from agentic_sandbox.main import Paths


def make_paths(root: Path) -> Paths:
    repo_root = root / "repo"
    template_dir = repo_root / "mkosi"
    podman_template_dir = repo_root / "podman"
    home = root / "home"
    data_dir = root / "data"
    state_dir = root / "state"
    image_dir = data_dir / "base-image"
    podman_image_dir = data_dir / "podman-image"
    for directory in (
        template_dir, podman_template_dir, home, state_dir, image_dir, podman_image_dir
    ):
        directory.mkdir(parents=True)
    return Paths(
        repo_root=repo_root,
        template_dir=template_dir,
        home=home,
        data_dir=data_dir,
        state_dir=state_dir,
        image_dir=image_dir,
        build_marker=image_dir / ".image-built.json",
        podman_template_dir=podman_template_dir,
        podman_image_dir=podman_image_dir,
        podman_build_marker=podman_image_dir / ".image-built.json",
    )

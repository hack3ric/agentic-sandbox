# Repository Guidelines

## Project Structure & Module Organization
`agentic_vm/main.py` contains the CLI, VM lifecycle orchestration, state tracking, and mkosi/systemd integration. `agentic_vm/spinner.py` contains terminal spinner support used while waiting for boot and shutdown. `agentic-sandbox` is the thin shell wrapper that execs the Python entrypoint. Mkosi templates currently live under `mkosi/`, with `mkosi/mkosi.conf.in` rendered into the shared image workspace at runtime.

## Build, Test, and Development Commands
Run commands from the repository root.

- `./agentic-sandbox --help`: inspect the CLI surface quickly.
- `./agentic-sandbox create --wait`: start a VM and wait until it is reachable.
- `./agentic-sandbox run -- uname -a`: create-if-needed, wait for boot, then run a command over SSH.
- `./agentic-sandbox ssh -- uname -a`: connect to an already running VM for the current directory.
- `./agentic-sandbox stop --force`: stop the transient user unit without waiting for an in-guest shutdown.
- `./agentic-sandbox rebuild`: rebuild the shared mkosi image; it refuses to run while managed VMs are active.
- `python3 -m compileall agentic_vm`: quick syntax check after Python changes.

## Coding Style & Naming Conventions
Use Python 3 with 4-space indentation and standard library features where possible. Follow the existing style: small methods, explicit constants, and dataclasses for structured state. Use `snake_case` for functions and variables, `UPPER_SNAKE_CASE` for module-level constants, and descriptive test names like `test_stop_force_skips_graceful_shutdown`. Keep shell wrapper changes minimal and ASCII-only unless the file already requires otherwise.

## Testing Guidelines
Tests should use `unittest` and live under a top-level `tests/` package when added. Cover CLI and lifecycle changes, especially subprocess ordering, state-file cleanup, retries, and timeout behavior. Prefer focused unit tests with a local `runner()` stub that records calls and returns mock `CompletedProcess` objects. If you add tests, run them with `python3 -m unittest discover -s tests`.

## Commit & Pull Request Guidelines
Recent commits use short, imperative subjects such as `graceful stop` and `subcommand changes`. Keep commit titles concise and behavior-focused. PRs should explain the user-visible change, mention any new flags or timeout behavior, and note how it was verified. Include command output summaries when changing CLI semantics; screenshots are not relevant for this repository.

## Security & Configuration Tips
Do not commit generated runtime state or anything from `~/.local/share/agentic-sandbox` or `~/.local/state/agentic-sandbox`, including rendered mkosi workspaces, SSH credentials, or build markers. Treat mkosi template changes carefully: they affect every created VM image and every project that reuses the shared base image.

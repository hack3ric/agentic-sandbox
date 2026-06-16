# Repository Guidelines

## Project Structure & Module Organization
`agentic_vm.py` contains the main CLI and VM lifecycle logic. `agentic-vm` is the thin shell wrapper that execs the Python entrypoint. Tests live in `tests/test_agentic_vm.py` and cover command behavior with mocked subprocess calls. Mkosi templates are stored under `mkosi/`, including the root filesystem repartition override at `mkosi/mkosi.repart/10-root.conf`.

## Build, Test, and Development Commands
Run commands from the repository root.

- `python3 -m unittest tests/test_agentic_vm.py`: run the full test suite.
- `python3 agentic_vm.py --help`: inspect the CLI surface quickly.
- `python3 agentic_vm.py create --wait`: start a VM and wait until it is reachable.
- `python3 agentic_vm.py run -- uname -a`: create-if-needed, wait for boot, then run a command over SSH.
- `python3 agentic_vm.py rebuild`: rebuild the shared mkosi image; it refuses to run while managed VMs are active.

## Coding Style & Naming Conventions
Use Python 3 with 4-space indentation and standard library features where possible. Follow the existing style: small methods, explicit constants, and dataclasses for structured state. Use `snake_case` for functions and variables, `UPPER_SNAKE_CASE` for module-level constants, and descriptive test names like `test_stop_force_skips_graceful_shutdown`. Keep shell wrapper changes minimal and ASCII-only unless the file already requires otherwise.

## Testing Guidelines
Tests use `unittest`. Add or update tests for any CLI or lifecycle change, especially around subprocess ordering, retries, and timeout behavior. Prefer focused unit tests with a local `runner()` stub that records calls and returns mock completed-process objects. Run `python3 -m unittest tests/test_agentic_vm.py` before opening a PR.

## Commit & Pull Request Guidelines
Recent commits use short, imperative subjects such as `graceful stop` and `subcommand changes`. Keep commit titles concise and behavior-focused. PRs should explain the user-visible change, mention any new flags or timeout behavior, and note how it was verified. Include command output summaries when changing CLI semantics; screenshots are not relevant for this repository.

## Security & Configuration Tips
Do not commit generated runtime state or anything from `~/.local/share/agentic-vm` or `~/.local/state/agentic-vm`. Treat mkosi template changes carefully: they affect every created VM image.

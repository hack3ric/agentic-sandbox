# agentic-sandbox

Small Python wrapper around `mkosi` or `podman` for sandboxing LLM agents.

## Commands

Run from any project directory:

```bash
./agentic-sandbox create
./agentic-sandbox --backend podman create --wait
./agentic-sandbox run
./agentic-sandbox --backend podman run -- uname -a
./agentic-sandbox stop
./agentic-sandbox rebuild
```

`mkosi` remains the default backend. `podman` is an alternate rootless host backend; commands inside the container run as container `root`.

Deps:
- general: fakeroot
- for `mkosi` backend: mkosi, `$(mkosi dependencies)`, qemu, socat
- for `podman` backend: podman

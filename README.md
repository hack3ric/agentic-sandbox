# agentic-vm

Small Python wrapper around `mkosi` or `podman` for isolating LLM agents.

## Commands

Run from any project directory:

```bash
./agentic-vm create
./agentic-vm --backend podman create --wait
./agentic-vm run
./agentic-vm --backend podman run -- uname -a
./agentic-vm stop
./agentic-vm rebuild
```

`mkosi` remains the default backend. `podman` is an alternate rootless host backend; commands inside the container run as container `root`.

Deps for `mkosi`: mkosi, `$(mkosi dependencies)`, qemu, socat

Deps for `podman`: podman

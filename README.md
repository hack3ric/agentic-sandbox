# agentic-sandbox

Small Python wrapper around `mkosi` or `podman` for sandboxing LLM agents.

It's sloppy now and only supports Arch Linux, but hopefully it will be better.

Deps:
- general: fakeroot
- for `mkosi` backend: mkosi, `$(mkosi dependencies)`, qemu, socat
- for `podman` backend: podman

## Usage

Run from any project directory:

```console
$ ./agentic-sandbox create
$ ./agentic-sandbox --backend podman create --wait
$ ./agentic-sandbox run -- uname -a
$ ./agentic-sandbox --backend podman run -- uname -a
$ ./agentic-sandbox exec
$ ./agentic-sandbox stop
$ ./agentic-sandbox rebuild
```

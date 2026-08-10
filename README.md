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
$ ./agentic-sandbox create -- --qemu-smp 4
$ ./agentic-sandbox --backend podman create --wait
$ ./agentic-sandbox --backend podman create -- --cpus 2
$ ./agentic-sandbox run -- uname -a
$ ./agentic-sandbox --backend podman run -- uname -a
$ ./agentic-sandbox exec
$ ./agentic-sandbox stop
$ ./agentic-sandbox rebuild
```

Arguments after `--` on `create` are passed to the selected backend's create
command. For mkosi they are added to `mkosi vm`; for podman they are added to
`podman create` before the image name.

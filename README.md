# agentic-sandbox

Small Python wrapper around `mkosi` or `podman` for sandboxing LLM agents.

It's sloppy now and only supports Arch Linux, but hopefully it will be better.

Deps:
- general: fakeroot
- for `mkosi` backend: mkosi, `$(mkosi dependencies)`, qemu, socat
- for `podman` backend: podman

## Usage

Select a backend with the required `--backend` (or `-b`) option before the
subcommand. Run from any project directory:

```console
$ ./agentic-sandbox --backend mkosi create
$ ./agentic-sandbox --backend mkosi create -- --qemu-smp 4
$ ./agentic-sandbox --backend podman create --wait
$ ./agentic-sandbox --backend podman create -- --cpus 2
$ ./agentic-sandbox --backend mkosi run -- uname -a
$ ./agentic-sandbox --backend podman run -- uname -a
$ ./agentic-sandbox --backend mkosi exec
$ ./agentic-sandbox --backend mkosi stop
$ ./agentic-sandbox --backend mkosi rebuild
```

The root wrappers select the backend for you: `agentic-container` runs
`agentic-sandbox --backend podman`, and `agentic-vm` runs
`agentic-sandbox --backend mkosi`. Both forward all arguments:

```console
$ ./agentic-container create --wait
$ ./agentic-container run -- uname -a
$ ./agentic-vm create -- --qemu-smp 4
$ ./agentic-vm run -- uname -a
```

Use `--quiet` (or `-q`) on `create` or `run` to suppress agentic-sandbox's
status messages on stdout. The sandbox command and backend tools still print
their output, and errors remain visible on stderr:

```console
$ ./agentic-container create --quiet --wait
$ ./agentic-vm run -q -- uname -a
```

Arguments after `--` on `create` are passed to the selected backend's create
command. For mkosi they are added to `mkosi vm`; for podman they are added to
`podman create` before the image name.

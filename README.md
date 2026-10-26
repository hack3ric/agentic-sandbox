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
$ ./agentic-sandbox --backend podman create
$ ./agentic-sandbox --backend podman create -- --cpus 2
$ ./agentic-sandbox --backend mkosi run -- uname -a
$ ./agentic-sandbox --backend podman run -- uname -a
$ ./agentic-sandbox --backend mkosi exec
$ ./agentic-sandbox --backend mkosi stop
$ ./agentic-sandbox --backend mkosi rebuild
```

`create` starts the sandbox and waits until it is reachable by default for
both backends. Use `create --no-wait` to start it in the background and return
without waiting for readiness. Explicit `--wait` remains supported.

The root wrappers select the backend for you: `agentic-container` runs
`agentic-sandbox --backend podman`, and `agentic-vm` runs
`agentic-sandbox --backend mkosi`. Both forward all arguments:

```console
$ ./agentic-container create
$ ./agentic-container run -- uname -a
$ ./agentic-vm create -- --qemu-smp 4
$ ./agentic-vm run -- uname -a
```

Arguments after `--` on `create` are passed to the selected backend's create
command. For mkosi they are added to `mkosi vm`; for podman they are added to
`podman create` before the image name.

## GUI applications (Podman)

From a Wayland desktop session, create a container with `--gui`:

```console
$ ./agentic-container create --gui
$ ./agentic-container exec -- your-gui-app
```

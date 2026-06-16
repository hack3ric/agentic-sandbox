# agentic-vm

Small Python wrapper around `mkosi` and transient user services.

The mkosi template lives in [`mkosi/`](./mkosi) in this repo and is synced into the shared workspace under `~/.local/share/agentic-vm/base-image`.

## Commands

Run from any project directory:

```bash
./agentic-vm start
./agentic-vm ssh
./agentic-vm stop
./agentic-vm rebuild
```

`start` creates a shared Arch Linux image once under `~/.local/share/agentic-vm/base-image`, then starts a VM for the current exact working directory through `systemd-run --user`.

The current working directory is bind-mounted into the guest at the same absolute path via `mkosi --runtime-tree`.

The VM disk is expanded at boot time with `mkosi --runtime-size 32G`, which increases usable space inside ephemeral VMs without increasing the stored size of the built base image.

The shared disk image defaults to a btrfs root filesystem via the repo-managed [`mkosi/mkosi.repart/10-root.conf`](./mkosi/mkosi.repart/10-root.conf) override.

`ssh` enters the VM associated with the current directory via `mkosi ssh`.

`stop` stops the transient user unit for the current directory.

`rebuild` forces a rebuild of the shared image and refuses to run while any managed VM is active.

## Notes

- The implementation targets `mkosi 26`, which uses `mkosi vm --vmm=qemu` semantics rather than a dedicated `mkosi qemu` verb.
- VM identity is derived from the exact absolute `$PWD`, so sibling directories get separate VMs.
- Runtime state is stored in `~/.local/state/agentic-vm`.

dosfstools
mtools
virtiofsd

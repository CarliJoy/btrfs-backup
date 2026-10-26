# btrfs-backup

Backup chain for an ext4 source with btrfs targets:

```
SOURCE (ext4, e.g. encrypted NVMe)
   -> LOCAL  (btrfs SSD)   rsync into a clone of the last snapshot, then a read-only snapshot
   -> REMOTE (btrfs HDD)   btrfs send/receive (incremental)
```

Every successful backup is a read-only snapshot `backup_<YYYYMMDD_HHMMSS>` in
`<local-root>/.snapshots`, with a `backup.log` in its root. `<local-root>/current` always
points to the newest snapshot. Failed or killed runs stay behind as
`.backup_inprogress_<time>` (with their log) and never block later runs.

## Commands

| Command        | Purpose                                                              |
|----------------|----------------------------------------------------------------------|
| `init-local`   | Prepare the local backup disk (creates `.snapshots`)                 |
| `init-remote`  | Check that the remote disk is a mounted btrfs filesystem             |
| `local`        | rsync SOURCE into a new snapshot on the local disk                   |
| `remote`       | Send the newest local snapshot to the remote disk (incremental)      |
| `delete-local` | Delete a local snapshot, refusing if it was not sent to the remote   |
| `install`      | Install the script to `/usr/local/sbin/btrfs-backup` via `sudo install` |

## Development

```bash
uv sync
uv run btrfs-backup --help
uv run prek install            # install the git pre-commit hook
uv run prek run --all-files    # run all hooks once
```

[prek](https://github.com/j178/prek) runs the hooks from `.pre-commit-config.yaml`: whitespace
and file hygiene checks, `ruff check --fix` (rules and isort settings in `pyproject.toml`,
including docstring checks), `ruff format`, `uv lock` consistency and `mypy --strict`.
Each hook can also be run directly: `uv run ruff check`, `uv run ruff format`, `uv run mypy`.

## Install system-wide

The installed script runs with the **system Python** (not uv), so it works without network
access or a user environment, for example when restoring from a live USB.

```bash
sudo apt install python3-click rsync btrfs-progs
uv run btrfs-backup install          # runs: sudo install -D -m 755 ... /usr/local/sbin/btrfs-backup
sudo btrfs-backup local --local-root /media/kound/ssd-backup --dry-run
```

`install` copies the single self-contained file `src/btrfs_backup/cli.py` and replaces its
shebang with `/usr/bin/python3` (change with `--python`, destination with `--dest`).
Run it again after every change to update the installed copy.

## Requirements

- `/etc/btrfs-backup.filter`: rsync filter rules, must exist (single source of truth;
  `local --filter-file` selects another file).
- Root for everything except `install`.
- `btrfs-progs`, `rsync`; for per-user flatpak lists `runuser` (util-linux).

## Not done yet

Tests: planned with testcontainers, installing the script into a fresh Docker container and
running it there.

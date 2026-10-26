# btrfs-backup

Snapshot-based backups of a Linux system with an ext4 root onto btrfs disks: checksummed
storage that reveals bitrot, transparent compression, and a long history that stays small.

## Use case

A workstation with an (encrypted) ext4 NVMe SSD is backed up to two disks you control, next
to an offsite backup that runs on its own schedule. The setup has these goals:

- **Detect bitrot.** btrfs checksums all data and metadata, and `btrfs scrub` verifies them.
  Both backup disks are btrfs, so silent corruption is found on each of them separately
  instead of being copied along unnoticed.
- **Keep a long history cheaply.** Every backup is a read-only snapshot. Unchanged data is
  shared between snapshots, changed files are updated block by block (`rsync --inplace`),
  and compression is applied on top, so many snapshots cost little space.
- **Restore without special tools.** A snapshot is a normal directory tree. Getting a file
  back is a `cp` from `<local-root>/current` or from an older snapshot.
- **Use two disks for different jobs.** A fast internal SSD takes the regular backup. An
  external HDD receives the same snapshots later, whenever it is connected, with
  `btrfs send/receive`: only changed data is transferred, and the HDD can be given
  stronger compression and a longer history than the SSD.
- **Never trust a half-finished backup.** A backup is built in a temporary subvolume and
  only becomes a snapshot if rsync completed. A failed or killed run is kept for inspection
  together with its log and never replaces or blocks a good snapshot.
- **Reinstall instead of restoring everything.** What can be recreated (packages, container
  storage, flatpaks, downloaded AI models, virtualenvs, caches) is left out through a
  filter file, and every backup contains lists and scripts to reinstall it. That keeps
  backups small and allows either a full restore or a clean reinstall of the OS followed by
  restoring only `/home` and the configuration.

The first stage uses rsync because the source is ext4, so there is nothing to `btrfs send`.
rsync copies into btrfs, and from there btrfs takes over: snapshots, send/receive, scrub.

This does not replace an offsite backup. Both disks are usually in the same place as the
computer, so fire, theft or a power surge can take all three.

## How it works

```
SOURCE (ext4, e.g. encrypted NVMe)
   -> LOCAL  (btrfs SSD)   rsync into a clone of the last snapshot, then a read-only snapshot
   -> REMOTE (btrfs HDD)   btrfs send/receive (incremental)
```

Every successful backup is a read-only snapshot `backup_<YYYYMMDD_HHMMSS>` in
`<local-root>/.snapshots`, with a `backup.log` in its root. `<local-root>/current` always
points to the newest snapshot. Failed or killed runs stay behind as
`.backup_inprogress_<time>` (with their log) and never block later runs.

Nothing is deleted automatically. `delete-local` refuses to remove a snapshot that has not
been sent to the remote disk yet.

## Commands

| Command        | Purpose                                                              |
|----------------|----------------------------------------------------------------------|
| `init-local`   | Prepare the local backup disk (creates `.snapshots`)                 |
| `init-remote`  | Check that the remote disk is a mounted btrfs filesystem             |
| `local`        | rsync SOURCE into a new snapshot on the local disk                   |
| `remote`       | Send all snapshots missing on the remote disk, oldest first (incremental) |
| `delete-local` | Delete a local snapshot, refusing if it was not sent to the remote   |
| `install`      | Install the script to `/usr/local/sbin/btrfs-backup` via `sudo install` |

### How `remote` decides what to send

`remote` sends every local snapshot that is newer than the one recorded in
`<local-root>/.last_sent_snapshot`, oldest first. Each snapshot is sent incrementally
(`btrfs send -p`) against the newest older snapshot that exists on both disks, or in full if
there is none. The marker is updated after every snapshot, so an interrupted run continues
where it stopped, and a partially received subvolume is removed again. Snapshots that are
already complete on the remote disk are skipped, so deleting the marker file makes `remote`
check all snapshots again.

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

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

Every successful backup is a read-only snapshot `backup_<YYYYMMDD_HHMMSS>Z` in
`<local-root>/.snapshots`, with a `backup.log` in its root.
The time in the name and the times in the log are UTC, which the trailing `Z` marks.
Snapshots from older versions (local time, no `Z`) keep their names and still sort correctly. `<local-root>/current` always
points to the newest snapshot. Failed or killed runs stay behind as
`.backup_inprogress_<time>` (with their log) and never block later runs.

Nothing is deleted automatically. `delete-local` refuses to remove a snapshot that has not
been sent to the remote disk yet.

A lock in `/run/lock` keeps the same job from running twice at the same time.
`delete-local` refuses to run while a `local` or a `remote` run is active.

If rsync could not copy some files (exit code 23, for example an unreadable file), the
snapshot is still created, the log ends with `OK WITH ERRORS`, and a warning is given.
Any other rsync error fails the backup.

## Commands

| Command         | Purpose                                                              |
|-----------------|----------------------------------------------------------------------|
| `init-local`    | Prepare the local backup disk (creates `.snapshots`)                 |
| `init-remote`   | Check that the remote target lies on a mounted btrfs filesystem      |
| `local`         | rsync SOURCE into a new snapshot on the local disk                   |
| `remote`        | Send all snapshots missing on the remote disk, oldest first (incremental) |
| `delete-local`  | Delete a local snapshot, refusing if it was not sent to the remote   |
| `install`       | Install the script to `/usr/local/sbin/btrfs-backup` via `sudo install` |
| `install-timer` | Run the local backup once a day with a systemd timer                 |
| `install-remote-trigger` | Send to the encrypted external disk when it is plugged in    |

### Default paths and the primary user

The primary user is the user with UID 1000, normally the main user of the computer.
`--local-root` defaults to `/media/<USER>/ssd-backup` and `--remote-root` to
`/media/<USER>/8tb/Backups/from-ssd`, where `<USER>` is the primary user.
Both can be changed with the options.
The remote root may be a folder on the backup disk, it does not have to be the mountpoint.
It only has to lie on a mounted btrfs filesystem, never on the root filesystem.

`local --primary-user` selects another user.
The per-user exports are done for this user only: the flatpak list and remotes
(`flatpak-user.txt`, `flatpak-remotes-user.txt`) and the `uv tool` installs
(`uv-tools-restore.sh`).
System-wide lists are always saved.

### Lists and restore scripts

Every backup contains these files in `/root/backup-lists`, written before the copy starts:
`manual-packages.txt` (apt), `snaps.txt`, the flatpak lists, `ollama-restore.sh`,
`huggingface-restore.sh` and `uv-tools-restore.sh`.
The scripts only call the tools again, for example `uv tool install --with click ruff`.
For `uv tool` the `--with` packages are kept, extras of the tool itself are not.
A list or script is removed again when its tool is gone or has nothing to list.

### Notifications

`local --notify` and `remote --notify` show the result on the desktop of the primary user:
a short notification with the duration on success, one that stays in the list for
warnings, and a critical one that stays on screen until dismissed for failures.
The units written by `install-timer` and `install-remote-trigger` use `--notify`.
The script runs as root, so it starts `notify-send` inside the user's session with
`systemd-run --machine=<USER>@.host --user`.
If the user is not logged in, for example when a backup fails before the login, the
notification is skipped, and the result is only in the journal and in `backup.log`.

### Running it automatically

```bash
sudo btrfs-backup install-timer                          # every day at 04:00
sudo btrfs-backup install-timer --on-calendar '*-*-* 12:00:00'
```

This writes `btrfs-backup-local.service` and `.timer` to `/etc/systemd/system` and enables the
timer.
Only the local backup runs automatically, because the remote disk is usually not connected.
The timer uses `Persistent=true`: if the computer was off at the scheduled time, the backup
starts when the timer comes up at boot, before other programs are running.
Rebooting several times a day does not start more backups.
The service waits for the backup disk to be mounted and runs with low CPU and idle I/O
priority.
Run `btrfs-backup install` first, because the service calls the installed script.
Follow a run with `journalctl -u btrfs-backup-local.service -f`, and switch the timer off with
`sudo systemctl disable --now btrfs-backup-local.timer`.

### How `remote` decides what to send

`remote` sends every local snapshot that is newer than the one recorded in
`<local-root>/.last_sent_snapshot`, oldest first. Each snapshot is sent incrementally
(`btrfs send -p`) against the newest older snapshot that exists on both disks, or in full if
there is none. The marker is updated after every snapshot, so an interrupted run continues
where it stopped, and a partially received subvolume is removed again. Snapshots that are
already complete on the remote disk are skipped, so deleting the marker file makes `remote`
check all snapshots again.

### Sending automatically when the external disk is plugged in

The external disk is a LUKS partition with btrfs inside.
`install-remote-trigger` takes the UUID of the LUKS partition, as plain UUID or as
`/dev/disk/by-uuid/<UUID>` path.
Find it with `sudo blkid -t TYPE=crypto_LUKS`.
It is not the UUID of the btrfs filesystem inside.

The unlock has to work without a prompt, so create a key file on the (encrypted) root disk
and add it to the LUKS partition.
Keep the passphrase slot, so you can still open the disk on another computer:

```bash
sudo dd if=/dev/urandom of=/root/.8tb.key bs=64 count=1
sudo chmod 0400 /root/.8tb.key
sudo cryptsetup luksAddKey /dev/disk/by-uuid/<LUKS-UUID> /root/.8tb.key
```

Then add one line each to `/etc/crypttab` and `/etc/fstab`.
`noauto` keeps the boot from waiting for the disk, and `nofail` keeps a missing disk from
being an error:

```
# /etc/crypttab
8tb  UUID=<LUKS-UUID>  /root/.8tb.key  luks,noauto,nofail

# /etc/fstab
/dev/mapper/8tb  /media/<USER>/8tb  btrfs  noauto,nofail,noatime,compress=zstd:6  0  0
```

```bash
sudo btrfs-backup install-remote-trigger <LUKS-UUID>
```

The command checks both files first: the crypttab entry exists and has a key file that
exists and is not readable by others, `noauto` or `nofail` is set in both files, the fstab
entry is btrfs, and `--remote-root` lies below its mount point.
If something is wrong it lists all problems and installs nothing.
The disk has to be connected, so that the command can verify that the UUID belongs to a
LUKS partition.

It then writes `btrfs-backup-remote.service`, started by systemd when the disk appears (no
udev rule needed), and a drop-in for the local service that starts it after every successful
local backup, but only if the disk is plugged in.
The service unlocks and mounts the disk and runs `remote`.
The disk stays mounted afterwards: unmount it yourself when the notification says it is done.
It waits while a local backup is running, but never starts one.
Test it with `sudo systemctl start btrfs-backup-remote.service`.

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
sudo btrfs-backup local --dry-run
```

`install` copies the single self-contained file `src/btrfs_backup/cli.py` and replaces its
shebang with `/usr/bin/python3` (change with `--python`, destination with `--dest`).
Run it again after every change to update the installed copy.

## Requirements

- `/etc/btrfs-backup.filter`: rsync filter rules, must exist (single source of truth;
  `local --filter-file` selects another file).
- Root for everything except `install`.
- `btrfs-progs`, `rsync`; `runuser` (util-linux) and `uv` for the per-user lists;
  `notify-send` (package `libnotify-bin`) for `--notify`;
  `systemd` for `install-timer` and `install-remote-trigger`.

## Not done yet

Tests: planned with testcontainers, installing the script into a fresh Docker container and
running it there.

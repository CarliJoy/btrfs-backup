#!/usr/bin/env python3
"""btrfs-backup: rsync and btrfs snapshots, run as root (except for ``install``).

This file is intentionally self-contained (standard library + click only): the
`install` subcommand copies exactly this file to /usr/local/sbin, where it runs
with the system Python (apt install python3-click).

Backup chain:  SOURCE (ext4, e.g. encrypted NVMe)
                 -> LOCAL  (btrfs, e.g. internal SSD)        [rsync + read-only snapshots]
                 -> REMOTE (btrfs, e.g. external USB HDD)    [btrfs send/receive]

Subcommands:
    init-local     Prepare the local backup disk (creates the .snapshots directory).
    init-remote    Check that the remote backup disk is a mounted btrfs filesystem.
    local          Back up SOURCE into a new read-only snapshot on the local disk
                   (and point <local-root>/current at it).
    remote         Send all local snapshots missing on the remote disk, oldest first (incremental).
    delete-local   Safely delete a local snapshot (refuses if not yet sent to the remote).
    install        Install this script system-wide with 'sudo install' (run as a normal user).
    install-timer  Run the local backup once a day (systemd timer, caught up at boot).
    install-remote-trigger
                   Send to the encrypted external disk when it is plugged in (systemd).

The rsync filter rules live only in /etc/btrfs-backup.filter (single source of
truth). The file must exist, otherwise `local` refuses to run.

Every backup contains a backup.log in its root (what was created/changed/deleted,
timestamps, duration). Failed or killed runs are kept as .backup_inprogress_<time>.
Snapshot names and log times are in UTC, marked by a trailing Z.

With --notify, `local` and `remote` report their result to the desktop of the primary
user (notify-send, started through systemd-run in that user's session).

Requires: btrfs-progs, rsync, click  (apt install btrfs-progs rsync python3-click)
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any, NamedTuple, NoReturn

import click


__version__ = "0.1.0"

INSTALL_DEST = Path("/usr/local/sbin/btrfs-backup")
SYSTEM_PYTHON = "/usr/bin/python3"

# systemd units written by install-timer (only the local backup runs automatically: the
# remote disk is usually not connected).
SYSTEMD_DIR = Path("/etc/systemd/system")
UNIT_NAME = "btrfs-backup-local"
DEFAULT_ON_CALENDAR = "*-*-* 04:00:00"  # every day at 04:00

# install-remote-trigger: the external disk is a LUKS partition (crypttab) with btrfs inside
# (fstab). The tuples below are field positions in those two files.
CRYPTTAB = Path("/etc/crypttab")
FSTAB = Path("/etc/fstab")
REMOTE_UNIT_NAME = "btrfs-backup-remote"
EXAMPLE_MAPPER_NAME = "8tb"
LUKS_TYPE = "crypto_LUKS"
BY_UUID_DIR = Path("/dev/disk/by-uuid")
MAPPER_NAME = re.compile(r"[A-Za-z0-9_-]+")
GROUP_OTHER_BITS = 0o077
CRYPTTAB_NAME, CRYPTTAB_DEVICE, CRYPTTAB_KEY, CRYPTTAB_OPTIONS = 0, 1, 2, 3
FSTAB_DEVICE, FSTAB_MOUNT_POINT, FSTAB_TYPE, FSTAB_OPTIONS = 0, 1, 2, 3

SNAP_PREFIX = "backup_"
# Snapshot names and log lines use UTC, marked by a trailing Z. Names without the Z come
# from older versions (local time) and still sort correctly next to the new ones.
SNAP_DATE_FMT = "%Y%m%d_%H%M%SZ"
SNAP_DIR_NAME = ".snapshots"
MARKER_FILE = ".last_sent_snapshot"
INPROGRESS_PREFIX = ".backup_inprogress_"
CURRENT_LINK_NAME = "current"
LOG_FILE_NAME = "backup.log"  # lives in the root of every backup (and snapshot)
LOG_TIME_FMT = "%Y-%m-%d %H:%M:%SZ"
# rsync always starts its own log lines with "YYYY/MM/DD HH:MM:SS [pid] " (not configurable,
# --log-file-format only changes what follows), so they are rewritten after the run to
# "YYYY-MM-DD HH:MM:SSZ [pid] " (rsync runs with TZ=UTC).
RSYNC_LOG_DATE = re.compile(rb"^(\d{4})/(\d{2})/(\d{2}) (\d{2}:\d{2}:\d{2}) ")

LogFn = Callable[[str], None]

DEFAULT_FILTER_FILE = Path("/etc/btrfs-backup.filter")
LISTS_DIR = Path("/root/backup-lists")
FLATPAK_LIST_FILES = (
    "flatpak-system.txt",
    "flatpak-remotes-system.txt",
    "flatpak-user.txt",
    "flatpak-remotes-user.txt",
)

# The primary user is the one with this UID (the first normal user, UID 1000 on Ubuntu).
# Its per-user exports (flatpak, uv tools) are saved and the default disk paths use its name.
PRIMARY_UID = 1000
MEDIA_DIR = Path("/media")
LOCAL_ROOT_SUBPATH = "ssd-backup"  # default: /media/<USER>/ssd-backup
REMOTE_ROOT_SUBPATH = "8tb/Backups/from-ssd"  # default: /media/<USER>/8tb/Backups/from-ssd

# rsync exit codes that still count as a successful backup:
# 0 = ok, 24 = "some source files vanished" (normal when copying a live system)
RSYNC_VANISHED_CODE = 24
RSYNC_OK_CODES = (0, RSYNC_VANISHED_CODE)
# 23 = "some files could not be transferred": the snapshot is kept, with a warning.
RSYNC_PARTIAL_CODE = 23

# One lock file per kind of job, so that the same job never runs twice at the same time.
LOCK_DIR = Path("/run/lock")
NOTIFY_APP_NAME = "btrfs-backup"

# Ollama manifests live in manifests/<registry>/<namespace>/<model>/<tag>.
MIN_MANIFEST_PARTS = 3  # registry, model, tag (the namespace can be missing)
LIBRARY_MODEL_PARTS = 2  # "library" and the model name


class Notice(NamedTuple):
    """How a desktop notification looks: urgency, icon, category and whether it is kept."""

    urgency: str
    icon: str
    category: str
    transient: bool


class Outcome(NamedTuple):
    """Result of a finished job: one line for the user, plus warnings if there are any."""

    message: str
    warnings: tuple[str, ...]


class RsyncResult(NamedTuple):
    """How the rsync run ended: usable or not, a short description, and warnings."""

    success: bool
    detail: str
    warnings: tuple[str, ...]


# Success: a normal banner that is not kept in the notification list. "low" would not
# show a banner at all on GNOME. Warnings stay in the list, errors stay until dismissed.
NOTICE_OK = Notice("normal", "kfloppy", "transfer.complete", True)
NOTICE_WARNING = Notice("normal", "dialog-warning", "transfer.complete", False)
NOTICE_ERROR = Notice("critical", "dialog-error", "transfer.error", False)


# ---- helpers ----------------------------------------------------------------


def require_root() -> None:
    """Abort with an error message unless the script runs as root.

    Raises:
        click.ClickException: If the effective user id is not 0.
    """
    if os.geteuid() != 0:
        raise click.ClickException("This command must be run as root (use sudo).")


def utc_now() -> datetime:
    """Return the current time in UTC (used for snapshot names and log lines)."""
    return datetime.now(timezone.utc)


def format_duration(seconds: float) -> str:
    """Format a duration for people, e.g. ``45 s``, ``3 min 12 s`` or ``1 h 05 min``."""
    hours, rest = divmod(round(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def raise_terminated(signum: int, _frame: FrameType | None) -> NoReturn:
    """Turn SIGTERM into an exception, so that logs are finished and partial work is removed."""
    raise SystemExit(128 + signum)


def send_notice(user: str | None, notice: Notice, summary: str, body: str) -> None:
    """Show a desktop notification to ``user`` with notify-send; never fails.

    Root has no access to the user's session, so the command runs inside the user's
    systemd instance (``systemd-run --machine=<user>@.host --user``). If the user is not
    logged in, the notification is skipped with a note.

    Args:
        user: Who to notify, or None to send nothing.
        notice: Urgency, icon and category.
        summary: The title line.
        body: The text below it.
    """
    if user is None:
        return
    hints: tuple[str, ...] = ()
    if notice.transient:
        hints = ("--hint=int:transient:1",)
    cmd = (
        "systemd-run",
        f"--machine={user}@.host",
        "--user",
        "--quiet",
        "--collect",
        "notify-send",
        f"--app-name={NOTIFY_APP_NAME}",
        f"--urgency={notice.urgency}",
        f"--icon={notice.icon}",
        f"--category={notice.category}",
        *hints,
        summary,
        body,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as error:
        click.echo(f"Note: could not notify {user}: {error}", err=True)
        return
    if result.returncode != 0:
        click.echo(f"Note: could not notify {user}: {result.stderr.strip()}", err=True)


@contextlib.contextmanager
def exclusive_lock(name: str) -> Iterator[None]:
    """Hold a lock so that the same kind of job cannot run twice at the same time.

    Args:
        name: ``local`` or ``remote``; the lock file is ``/run/lock/btrfs-backup-<name>.lock``.

    Raises:
        click.ClickException: If another process holds the lock.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with (LOCK_DIR / f"btrfs-backup-{name}.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise click.ClickException(
                f"'btrfs-backup {name}' is running right now, try again when it has finished."
            ) from None
        yield


def run_job(
    lock_name: str, title: str, notify_user: str | None, job: Callable[[], Outcome]
) -> None:
    """Run a backup job under its lock and report the result.

    Args:
        lock_name: Name for ``exclusive_lock``.
        title: Start of the notification title, e.g. ``Local backup``.
        notify_user: User who gets a desktop notification, or None for no notifications.
        job: Does the work and returns its outcome; errors are raised.
    """
    begin = time.monotonic()
    try:
        with exclusive_lock(lock_name):
            outcome = job()
    except click.ClickException as error:
        send_notice(notify_user, NOTICE_ERROR, f"{title} failed", error.message)
        raise
    except Exception as error:
        send_notice(notify_user, NOTICE_ERROR, f"{title} failed", f"Unexpected error: {error!r}")
        raise
    took = f"Took {format_duration(time.monotonic() - begin)}."
    click.echo(outcome.message)
    if outcome.warnings:
        body = "\n".join((outcome.message, took, *outcome.warnings))
        send_notice(notify_user, NOTICE_WARNING, f"{title} finished with warnings", body)
    else:
        send_notice(notify_user, NOTICE_OK, f"{title} finished", f"{outcome.message}\n{took}")


def start_log(log_path: Path, first_message: str) -> LogFn:
    """Overwrite the log with a first line and return a function that logs to file and terminal."""

    def log(message: str) -> None:
        """Print a timestamped line (UTC) and append it to the log file."""
        line = utc_now().strftime(LOG_TIME_FMT) + " " + message
        click.echo(line)
        with log_path.open("a") as handle:
            handle.write(line + "\n")

    log_path.write_text("")
    log(first_message)
    return log


def run(
    cmd: Sequence[str | Path], check: bool = True, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Print a command and run it.

    Args:
        cmd: Program and arguments; paths are converted to strings.
        check: Fail on a non-zero exit code.
        **kwargs: Passed through to ``subprocess.run``.

    Returns:
        The completed process.

    Raises:
        click.ClickException: If the program is missing, or fails while ``check`` is set.
    """
    args = tuple(str(c) for c in cmd)
    click.echo("$ " + " ".join(args))
    try:
        return subprocess.run(args, check=check, **kwargs)
    except FileNotFoundError:
        raise click.ClickException(f"Command not found: {args[0]}") from None
    except subprocess.CalledProcessError as error:
        raise click.ClickException(
            f"Command failed with exit code {error.returncode}: {' '.join(args)}"
        ) from None


def run_pipe(send_cmd: Sequence[str], receive_cmd: Sequence[str]) -> None:
    """Run ``send_cmd | receive_cmd`` and fail if either side fails.

    Used for ``btrfs send | btrfs receive``. Both exit codes are checked, whereas a plain
    shell pipe only reports the last one. When one side dies the other one usually fails
    too, so the error names both exit codes instead of blaming one side.

    Args:
        send_cmd: Command whose stdout is piped into the receiver.
        receive_cmd: Command reading the stream from its stdin.

    Raises:
        click.ClickException: If the send or the receive process exits non-zero.
    """
    click.echo("$ " + " ".join(send_cmd) + " | " + " ".join(receive_cmd))
    send = subprocess.Popen(send_cmd, stdout=subprocess.PIPE)
    try:
        receive = subprocess.Popen(receive_cmd, stdin=send.stdout)
    except OSError as error:
        send.kill()
        send.wait()
        raise click.ClickException(f"Could not start {receive_cmd[0]}: {error}") from None
    if send.stdout is not None:
        send.stdout.close()
    receive.communicate()
    send.wait()
    if send.returncode != 0 or receive.returncode != 0:
        raise click.ClickException(
            f"Sending failed (btrfs send exit code {send.returncode}, "
            f"btrfs receive exit code {receive.returncode})."
        )


def ensure_mounted_btrfs(path: Path) -> None:
    """Refuse to continue unless `path` lies on a mounted btrfs filesystem.

    `path` may be the mountpoint or a directory below it (e.g. a folder on the backup
    disk). This protects against writing a whole backup into the root filesystem
    because the backup disk was not mounted.
    """
    if not path.is_dir():
        raise click.ClickException(f"{path} does not exist.")
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE,TARGET", "--target", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    fstype, _, mountpoint = result.stdout.strip().partition(" ")
    if mountpoint.strip() == "/":
        raise click.ClickException(f"{path} is on the root filesystem. Mount the disk first.")
    if fstype != "btrfs":
        raise click.ClickException(f"{path} is not on a btrfs mount (found: {fstype!r}).")


def list_snapshots(snap_dir: Path) -> list[Path]:
    """Return the ``backup_*`` snapshot directories in ``snap_dir``, oldest first.

    The names contain a sortable timestamp. Returns an empty list if the directory is missing.
    """
    if not snap_dir.exists():
        return []
    return sorted(p for p in snap_dir.iterdir() if p.is_dir() and p.name.startswith(SNAP_PREFIX))


def read_marker(local_root: Path) -> str | None:
    """Return the name of the last snapshot confirmed sent to the remote, or None if unknown."""
    marker = local_root / MARKER_FILE
    if marker.exists():
        return marker.read_text().strip()
    return None


def write_marker(local_root: Path, snapshot_name: str) -> None:
    """Record ``snapshot_name`` as the last snapshot successfully sent to the remote."""
    (local_root / MARKER_FILE).write_text(snapshot_name + "\n")


def update_current_link(local_root: Path) -> None:
    """Point <local_root>/current at the newest snapshot (remove the link if none is left).

    The link is relative and replaced atomically, so it is never missing or dangling
    in between.
    """
    link = local_root / CURRENT_LINK_NAME
    if os.path.lexists(link) and not link.is_symlink():
        raise click.ClickException(f"{link} exists and is not a symlink, refusing to replace it.")

    snapshots = list_snapshots(local_root / SNAP_DIR_NAME)
    if not snapshots:
        if link.is_symlink():
            link.unlink()
        return

    newest = snapshots[-1]
    tmp_link = local_root / (CURRENT_LINK_NAME + ".tmp")
    if os.path.lexists(tmp_link):
        tmp_link.unlink()
    tmp_link.symlink_to(Path(SNAP_DIR_NAME) / newest.name)
    tmp_link.replace(link)
    click.echo(f"{link} -> {SNAP_DIR_NAME}/{newest.name}")


def is_complete_subvolume(subvol: Path) -> bool:
    """Tell whether ``subvol`` is a finished, read-only subvolume.

    ``btrfs receive`` creates the subvolume writable and only marks it read-only once the
    whole stream has arrived, so a read-only subvolume is a complete one. This also holds
    for snapshots that were created directly on that disk, for example by replaying old
    backups, which have no "Received UUID".

    Args:
        subvol: Path of the subvolume to check; it does not need to exist.

    Returns:
        True if ``btrfs subvolume show`` reports the ``readonly`` flag.
    """
    result = subprocess.run(
        ["btrfs", "subvolume", "show", str(subvol)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Flags:"):
            return "readonly" in stripped.split(":", 1)[1]
    return False


def primary_user() -> str:
    """Return the name of the user with UID ``PRIMARY_UID``."""
    try:
        return pwd.getpwuid(PRIMARY_UID).pw_name
    except KeyError:
        raise click.ClickException(
            f"No user with UID {PRIMARY_UID} found, pass the paths and --primary-user."
        ) from None


def user_home(user: str) -> str:
    """Return the home directory of ``user``."""
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        raise click.ClickException(f"Unknown user: {user!r}") from None


def default_local_root() -> Path:
    """Default local backup disk: ``/media/<primary user>/ssd-backup``."""
    return MEDIA_DIR / primary_user() / LOCAL_ROOT_SUBPATH


def default_remote_root() -> Path:
    """Default remote backup location: ``/media/<primary user>/8tb/Backups/from-ssd``."""
    return MEDIA_DIR / primary_user() / REMOTE_ROOT_SUBPATH


def remove_stale(path: Path, log: LogFn) -> None:
    """Remove a list or restore script of an earlier run that no longer applies."""
    if path.exists():
        path.unlink()
        log(f"Removed outdated {path}")


def save_package_lists(user: str, log: LogFn) -> None:
    """Write lists of installed packages/flatpaks into LISTS_DIR (part of the backup).

    Args:
        user: The primary user, whose per-user flatpaks and uv tools are exported.
        log: Logging function from ``start_log``.
    """
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, list[str]]] = []

    if shutil.which("apt-mark"):
        jobs.append(("manual-packages.txt", ["apt-mark", "showmanual"]))
    else:
        remove_stale(LISTS_DIR / "manual-packages.txt", log)
    if shutil.which("snap"):
        jobs.append(("snaps.txt", ["snap", "list"]))
    else:
        remove_stale(LISTS_DIR / "snaps.txt", log)

    if shutil.which("flatpak"):
        jobs.append(
            (
                "flatpak-system.txt",
                ["flatpak", "list", "--system", "--app", "--columns=origin,application"],
            )
        )
        jobs.append(
            (
                "flatpak-remotes-system.txt",
                ["flatpak", "remotes", "--system", "--columns=name,url"],
            )
        )
        as_user = ["runuser", "-u", user, "--", "env", f"HOME={user_home(user)}"]
        jobs.append(
            (
                "flatpak-user.txt",
                [*as_user, "flatpak", "list", "--user", "--app", "--columns=origin,application"],
            )
        )
        jobs.append(
            (
                "flatpak-remotes-user.txt",
                [*as_user, "flatpak", "remotes", "--user", "--columns=name,url"],
            )
        )
    else:
        log("flatpak not installed, skipping flatpak lists.")
        for name in FLATPAK_LIST_FILES:
            remove_stale(LISTS_DIR / name, log)

    for filename, cmd in jobs:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            log(f"Warning: '{' '.join(cmd)}' failed: {result.stderr.strip()}")
        else:
            (LISTS_DIR / filename).write_text(result.stdout)
            log(f"Saved {LISTS_DIR / filename}")


def find_ollama_manifest_dirs(extra_dirs: Iterable[Path]) -> list[Path]:
    """Return all existing Ollama 'manifests' directories (service install, user homes, extras)."""
    candidates = [Path(d) for d in extra_dirs]
    env_dir = os.environ.get("OLLAMA_MODELS")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates += [
        Path("/usr/share/ollama/.ollama/models"),
        Path("/var/lib/ollama/models"),
        Path("/var/lib/ollama/.ollama/models"),
        Path("/root/.ollama/models"),
    ]
    for home in sorted(Path("/home").glob("*")):
        candidates.append(home / ".ollama" / "models")

    found: list[Path] = []
    for candidate in candidates:
        manifests = candidate / "manifests"
        if manifests.is_dir() and manifests not in found:
            found.append(manifests)
    return found


def ollama_refs(manifest_dirs: Iterable[Path]) -> list[str]:
    """Turn manifest files into pullable references like 'llama3.2:3b' or 'hf.co/user/repo:tag'."""
    refs: set[str] = set()
    for manifests in manifest_dirs:
        for path in manifests.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(manifests).parts
            if len(parts) < MIN_MANIFEST_PARTS:
                continue
            registry = parts[0]
            tag = parts[-1]
            middle = parts[1:-1]
            if registry == "registry.ollama.ai":
                if len(middle) == LIBRARY_MODEL_PARTS and middle[0] == "library":
                    name = middle[1]
                else:
                    name = "/".join(middle)
            else:
                name = registry + "/" + "/".join(middle)
            refs.add(name + ":" + tag)
    return sorted(refs)


def huggingface_repos() -> list[str]:
    """Model repos found in the default Hugging Face hub cache of root and all /home users."""
    homes = [Path("/root")]
    homes += sorted(p for p in Path("/home").glob("*") if p.is_dir())
    repos: set[str] = set()
    for home in homes:
        hub = home / ".cache" / "huggingface" / "hub"
        if not hub.is_dir():
            continue
        for entry in hub.glob("models--*"):
            if entry.is_dir():
                repos.add(entry.name[len("models--") :].replace("--", "/"))
    return sorted(repos)


def normalize_rsync_log_dates(log_path: Path) -> None:
    """Rewrite rsync's ``YYYY/MM/DD HH:MM:SS`` line prefix to ``YYYY-MM-DD HH:MM:SSZ``."""
    staged = log_path.with_name(log_path.name + ".tmp")
    with log_path.open("rb") as source, staged.open("wb") as target:
        for line in source:
            target.write(RSYNC_LOG_DATE.sub(rb"\1-\2-\3 \4Z ", line, count=1))
    staged.replace(log_path)


def write_restore_script(path: Path, comment: str, body_lines: Iterable[str], log: LogFn) -> None:
    """Write an executable ``/bin/sh`` script and log its path.

    Args:
        path: Where to write the script.
        comment: Text of the comment line below the shebang.
        body_lines: The commands, one per line.
        log: Logging function from ``start_log``.
    """
    content = "#!/bin/sh\n# " + comment + "\n" + "\n".join(body_lines) + "\n"
    path.write_text(content)
    path.chmod(0o755)
    log(f"Saved {path}")


def save_model_restore_scripts(extra_ollama_dirs: Iterable[Path], log: LogFn) -> None:
    """Write shell scripts that re-download the locally stored AI models."""
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    refs = ollama_refs(find_ollama_manifest_dirs(extra_ollama_dirs))
    if refs:
        lines = ["set -u"]
        lines.extend("ollama pull " + shlex.quote(ref) for ref in refs)
        write_restore_script(
            LISTS_DIR / "ollama-restore.sh",
            f"Generated by btrfs-backup on {stamp}: re-download Ollama models.",
            lines,
            log,
        )
    else:
        log("No Ollama models found, skipping ollama-restore.sh.")
        remove_stale(LISTS_DIR / "ollama-restore.sh", log)

    repos = huggingface_repos()
    if repos:
        lines = [
            "HF=$(command -v hf || command -v huggingface-cli) || "
            '{ echo "hf / huggingface-cli not found" >&2; exit 1; }'
        ]
        lines.extend('"$HF" download ' + shlex.quote(repo) for repo in repos)
        write_restore_script(
            LISTS_DIR / "huggingface-restore.sh",
            f"Generated by btrfs-backup on {stamp}: re-download Hugging Face models.",
            lines,
            log,
        )
    else:
        log("No Hugging Face models found, skipping huggingface-restore.sh.")
        remove_stale(LISTS_DIR / "huggingface-restore.sh", log)


def uv_tool_install_lines(listing: str) -> list[str]:
    """Turn the output of ``uv tool list --show-with`` into ``uv tool install`` commands.

    A listing line looks like ``ruff v0.17.2 [with: rich>=13, click]``. Indented lines and
    lines starting with ``-`` are the executables of a tool and are skipped.
    """
    marker = "[with: "
    commands: list[str] = []
    for line in listing.splitlines():
        if not line.strip() or line.startswith(("-", " ")):
            continue
        args = ["uv", "tool", "install"]
        if marker in line:
            packages = line.split(marker, 1)[1].removesuffix("]")
            for package in packages.split(", "):
                args += ["--with", package]
        args.append(line.split()[0])
        commands.append(shlex.join(args))
    return commands


def save_uv_tools_script(user: str, log: LogFn) -> None:
    """Write ``uv-tools-restore.sh``, which reinstalls the ``uv tool`` installs of ``user``."""
    home = user_home(user)
    uv = shutil.which("uv", path=f"{home}/.local/bin:{os.environ.get('PATH', os.defpath)}")
    if uv is None:
        log("uv not found, skipping uv-tools-restore.sh.")
        remove_stale(LISTS_DIR / "uv-tools-restore.sh", log)
        return
    cmd = ["runuser", "-u", user, "--", "env", f"HOME={home}", uv, "tool", "list", "--show-with"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        log(f"Warning: '{' '.join(cmd)}' failed: {result.stderr.strip()}")
        return
    commands = uv_tool_install_lines(result.stdout)
    if not commands:
        log("No uv tools found, skipping uv-tools-restore.sh.")
        remove_stale(LISTS_DIR / "uv-tools-restore.sh", log)
        return
    stamp = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    write_restore_script(
        LISTS_DIR / "uv-tools-restore.sh",
        f"Generated by btrfs-backup on {stamp}: reinstall uv tools. Run as {user}.",
        commands,
        log,
    )


def build_rsync_command(
    source: Path,
    target: Path,
    filter_file: Path,
    *,
    one_file_system: bool,
    dry_run: bool,
    inplace: bool,
    log_file: Path | None,
) -> list[str]:
    """Build the rsync command line for one backup run.

    The destination's ``backup.log`` is protected from ``--delete-excluded`` by two filter
    rules that come before the user's filter file (rsync uses the first matching rule).

    Args:
        source: Directory to back up.
        target: Destination directory (the in-progress subvolume).
        filter_file: rsync filter rules file, merged after the log protection rules.
        one_file_system: Do not cross filesystem boundaries (``-x``).
        dry_run: Only show what would change, together with statistics.
        inplace: Update changed files block by block so btrfs keeps sharing unchanged blocks.
        log_file: Let rsync log every changed item and the statistics to this file.

    Returns:
        The command as an argument list.
    """
    source_arg = str(source).rstrip("/") + "/"
    target_arg = str(target).rstrip("/") + "/"
    cmd = [
        "rsync",
        "-aAXH",
        "--numeric-ids",
        "--delete",
        "--delete-excluded",
        # The log lives in the destination root: protect it from --delete(-excluded).
        # These two rules must come before the user's filter file (first match wins).
        f"--filter=P /{LOG_FILE_NAME}",
        f"--filter=- /{LOG_FILE_NAME}",
        f"--filter=merge {filter_file}",
    ]
    if one_file_system:
        cmd.append("-x")
    if inplace:
        # Update changed files block by block instead of writing a complete new copy,
        # so btrfs keeps sharing the unchanged blocks with older snapshots.
        cmd += ["--inplace", "--no-whole-file"]
    if dry_run:
        cmd += ["--dry-run", "--stats"]
    elif sys.stdout.isatty():
        cmd.append("--info=progress2")
    if log_file is not None:
        # One timestamped line per created/changed/deleted item, plus the statistics.
        # The timestamp prefix itself is fixed by rsync (see normalize_rsync_log_dates).
        cmd += [f"--log-file={log_file}", "--log-file-format=%i %n%L", "--stats"]
    cmd += [source_arg, target_arg]
    return cmd


# ---- shared options -----------------------------------------------------------


def local_root_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """Add the ``--local-root`` option (default: the primary user's disk) to a click command."""
    return click.option(
        "--local-root",
        default=default_local_root,
        show_default=f"{MEDIA_DIR}/<USER>/{LOCAL_ROOT_SUBPATH}",
        type=click.Path(path_type=Path),
        help="Mountpoint of the local btrfs backup disk. <USER> is the user with UID 1000.",
    )(f)


def remote_root_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """Add the ``--remote-root`` option (default: the primary user's disk) to a click command."""
    return click.option(
        "--remote-root",
        default=default_remote_root,
        show_default=f"{MEDIA_DIR}/<USER>/{REMOTE_ROOT_SUBPATH}",
        type=click.Path(path_type=Path),
        help="Directory on the remote btrfs disk that receives the snapshots. "
        "<USER> is the user with UID 1000.",
    )(f)


@click.group()
@click.version_option(__version__, prog_name="btrfs-backup")
def cli() -> None:
    """Snapshot backups: SOURCE -> local (btrfs) -> remote (btrfs). Run as root."""
    signal.signal(signal.SIGTERM, raise_terminated)


# ---- install ------------------------------------------------------------------


@cli.command("install")
@click.option(
    "--dest",
    default=INSTALL_DEST,
    show_default=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Where to install the script.",
)
@click.option(
    "--python",
    "python_path",
    default=SYSTEM_PYTHON,
    show_default=True,
    help="Interpreter written into the shebang line (must be able to import click).",
)
def install_command(dest: Path, python_path: str) -> None:
    """Install this script system-wide with 'sudo install'. Run as a normal user."""
    source = Path(__file__).resolve()
    if dest.exists() and source == dest.resolve():
        raise click.ClickException(
            f"This already is the installed copy ({dest}). To update it, run "
            "'uv run btrfs-backup install' from the project checkout."
        )

    check = subprocess.run(
        [python_path, "-I", "-c", "import click"], capture_output=True, check=False
    )
    if check.returncode != 0:
        click.echo(
            f"Warning: {python_path} cannot import click. "
            "On Ubuntu: sudo apt install python3-click",
            err=True,
        )

    # Replace the shebang so the installed script uses the system Python, not whatever
    # python3 happens to be first in PATH (venv, pyenv, uv).
    lines = source.read_text().splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines[0] = f"#!{python_path}\n"
    else:
        lines.insert(0, f"#!{python_path}\n")

    if os.geteuid() == 0:
        privilege_prefix: list[str] = []
    else:
        privilege_prefix = ["sudo"]

    with tempfile.TemporaryDirectory() as staging_dir:
        staged = Path(staging_dir) / dest.name
        staged.write_text("".join(lines))
        run(
            [
                *privilege_prefix,
                "install",
                "-D",
                "-m",
                "755",
                "-o",
                "root",
                "-g",
                "root",
                staged,
                dest,
            ]
        )
    click.echo(f"Installed {dest}")


# ---- install-timer ------------------------------------------------------------


def require_installed_script() -> None:
    """Abort unless the script is installed, because the systemd units call that copy."""
    if not INSTALL_DEST.exists():
        raise click.ClickException(f"{INSTALL_DEST} not found. Run 'btrfs-backup install' first.")


def require_no_spaces(*paths: Path) -> None:
    """Abort if a path contains whitespace, which the generated unit files cannot hold."""
    for path in paths:
        if any(char.isspace() for char in str(path)):
            raise click.ClickException(f"Spaces in {path} are not supported in the unit file.")


def check_calendar(value: str) -> None:
    """Let systemd validate a calendar expression such as ``*-*-* 04:00:00``.

    Raises:
        click.ClickException: If ``systemd-analyze`` does not accept the value.
    """
    result = subprocess.run(
        ["systemd-analyze", "calendar", value], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"--on-calendar: {value!r} is not a systemd calendar expression."
        )


def render_service_unit(exe: Path, local_root: Path) -> str:
    """Return the text of the systemd service that runs one local backup.

    The backup runs at low CPU and idle I/O priority so the computer stays usable, and
    without a time limit because the first run can take hours. ``--notify`` shows the
    result as a desktop notification.

    Args:
        exe: The installed ``btrfs-backup`` script.
        local_root: The local backup disk, passed explicitly to the ``local`` command.

    Returns:
        The content of the ``.service`` file.
    """
    lines = (
        "[Unit]",
        "Description=btrfs-backup: local snapshot backup",
        f"RequiresMountsFor={local_root}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exe} local --local-root {local_root} --notify",
        "TimeoutStartSec=infinity",
        "Nice=10",
        "IOSchedulingClass=idle",
        "",
    )
    return "\n".join(lines)


def render_timer_unit(on_calendar: str) -> str:
    """Return the text of the systemd timer: once a day, caught up at boot if it was missed.

    ``Persistent=true`` makes systemd remember the last run. If the computer was off at the
    scheduled time, the backup starts as soon as the timer starts at boot. However often
    the computer is rebooted, there is at most one run per scheduled time.

    Args:
        on_calendar: A systemd calendar expression, e.g. ``*-*-* 04:00:00``.

    Returns:
        The content of the ``.timer`` file.
    """
    lines = (
        "[Unit]",
        "Description=btrfs-backup: run the local backup once a day",
        "",
        "[Timer]",
        f"OnCalendar={on_calendar}",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    )
    return "\n".join(lines)


@cli.command("install-timer")
@local_root_option
@click.option(
    "--on-calendar",
    default=DEFAULT_ON_CALENDAR,
    show_default=True,
    help="When to run, as a systemd calendar expression (see 'man systemd.time').",
)
def install_timer(local_root: Path, on_calendar: str) -> None:
    """Run the local backup once a day with a systemd timer (needs root).

    If the computer is off at the scheduled time, the backup starts right after the next
    boot, when no other programs are running yet. Rebooting several times a day does not
    start more backups. Only the local backup runs automatically. Run 'install' first, so
    the unit can call the installed script. Running this command again updates the timer.
    """
    require_root()
    require_installed_script()
    require_no_spaces(local_root)
    check_calendar(on_calendar)

    service_path = SYSTEMD_DIR / (UNIT_NAME + ".service")
    timer_path = SYSTEMD_DIR / (UNIT_NAME + ".timer")
    service_path.write_text(render_service_unit(INSTALL_DEST, local_root))
    timer_path.write_text(render_timer_unit(on_calendar))
    click.echo(f"Wrote {service_path} and {timer_path}")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", timer_path.name])
    # restart (not just start) so that changed values apply when the timer already runs
    run(["systemctl", "restart", timer_path.name])
    run(["systemctl", "list-timers", "--no-pager", timer_path.name])
    click.echo(f"Follow the log with: journalctl -u {service_path.name} -f")


# ---- install-remote-trigger ---------------------------------------------------


def field(row: Sequence[str], index: int, default: str) -> str:
    """Return ``row[index]``, or ``default`` if the row is shorter."""
    if index < len(row):
        return row[index]
    return default


def read_table(path: Path) -> tuple[tuple[str, ...], ...]:
    """Read /etc/fstab or /etc/crypttab as rows of fields, without comments and empty lines.

    A missing file gives no rows.
    """
    if not path.exists():
        return ()
    lines = (line.split("#", 1)[0].split() for line in path.read_text().splitlines())
    return tuple(tuple(fields) for fields in lines if fields)


def parse_luks_uuid(value: str) -> str:
    """Return the UUID in lowercase from ``<uuid>``, ``UUID=<uuid>`` or a by-uuid path."""
    candidate = Path(value.strip().removeprefix("UUID=")).name
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        raise click.ClickException(
            f"{value!r} is not a UUID or a /dev/disk/by-uuid/<UUID> path."
        ) from None


def refers_to_uuid(device: str, luks_uuid: str) -> bool:
    """Tell whether a crypttab device field points at the partition with ``luks_uuid``."""
    return device.lower() in (f"uuid={luks_uuid}", f"/dev/disk/by-uuid/{luks_uuid}")


def find_crypttab_entry(rows: Sequence[Sequence[str]], luks_uuid: str) -> Sequence[str] | None:
    """Return the crypttab row of the partition with ``luks_uuid``, or None."""
    for row in rows:
        if len(row) > CRYPTTAB_DEVICE and refers_to_uuid(row[CRYPTTAB_DEVICE], luks_uuid):
            return row
    return None


def find_fstab_entry(rows: Sequence[Sequence[str]], mapper_name: str) -> Sequence[str] | None:
    """Return the fstab row that mounts ``/dev/mapper/<mapper_name>``, or None."""
    devices = (f"/dev/mapper/{mapper_name}", f"/dev/disk/by-id/dm-name-{mapper_name}")
    for row in rows:
        if len(row) > FSTAB_TYPE and row[FSTAB_DEVICE] in devices:
            return row
    return None


def blkid_type(device: Path) -> str:
    """Return the content type of ``device`` as reported by blkid (empty if unknown)."""
    result = subprocess.run(
        ["blkid", "-s", "TYPE", "-o", "value", str(device)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def disk_problems(luks_uuid: str) -> tuple[str, ...]:
    """Check that the disk is connected and that the UUID belongs to a LUKS partition."""
    device = BY_UUID_DIR / luks_uuid
    if not device.exists():
        return (
            f"{device} does not exist. Connect the external disk, so that the UUID can be "
            "verified.",
        )
    found = blkid_type(device)
    if found == LUKS_TYPE:
        return ()
    return (
        f"{device} contains {found!r}, not {LUKS_TYPE}. Use the UUID of the encrypted "
        "partition, not the UUID of the filesystem inside it.",
    )


def crypttab_problems(entry: Sequence[str]) -> tuple[str, ...]:
    """Check a crypttab row for an unattended unlock that does not block the boot."""
    name = entry[CRYPTTAB_NAME]
    key = field(entry, CRYPTTAB_KEY, "none")
    options = field(entry, CRYPTTAB_OPTIONS, "").split(",")
    problems: tuple[str, ...] = ()
    if MAPPER_NAME.fullmatch(name) is None:
        problems += (f"crypttab: name {name!r} may only contain letters, digits, - and _.",)
    if key in ("none", "-"):
        problems += (
            f"crypttab: {name} has no key file, so it would ask for a passphrase. "
            "Create one (see README) and put its path in the third field.",
        )
    elif not Path(key).is_file():
        problems += (f"crypttab: the key file {key} does not exist.",)
    elif Path(key).stat().st_mode & GROUP_OTHER_BITS:
        problems += (
            f"crypttab: the key file {key} is accessible by others, run: chmod 0400 {key}",
        )
    if "noauto" not in options and "nofail" not in options:
        problems += (
            f"crypttab: add the options noauto,nofail to {name}, "
            "otherwise the boot waits for the disk when it is not plugged in.",
        )
    return problems


def fstab_problems(entry: Sequence[str], remote_root: Path) -> tuple[str, ...]:
    """Check an fstab row: btrfs, not mounted at boot, and the remote root lies below it."""
    mount_point = Path(entry[FSTAB_MOUNT_POINT])
    options = field(entry, FSTAB_OPTIONS, "defaults").split(",")
    problems: tuple[str, ...] = ()
    if entry[FSTAB_TYPE] != "btrfs":
        problems += (
            f"fstab: the type of {entry[FSTAB_DEVICE]} is {entry[FSTAB_TYPE]}, not btrfs.",
        )
    if "noauto" not in options:
        problems += (
            f"fstab: add the option noauto (and nofail) to {mount_point}, "
            "because the disk is not unlocked yet at boot.",
        )
    if not remote_root.is_relative_to(mount_point):
        problems += (
            f"fstab: --remote-root {remote_root} does not lie below the mount point {mount_point}.",
        )
    return problems


def fail_with(problems: Sequence[str]) -> NoReturn:
    """Abort with all problems listed, one per line."""
    raise click.ClickException("Nothing was installed:\n  - " + "\n  - ".join(problems))


def systemd_escape(path: str, suffix: str) -> str:
    """Return the unit name systemd uses for ``path``, e.g. ``dev-sda1.device``."""
    result = subprocess.run(
        ["systemd-escape", "-p", f"--suffix={suffix}", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(f"systemd-escape failed for {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def render_remote_service(
    *,
    exe: Path,
    local_root: Path,
    remote_root: Path,
    luks_uuid: str,
    mapper_name: str,
    mount_point: Path,
    device_unit: str,
) -> str:
    """Return the text of the service that unlocks, mounts and sends to the external disk.

    It is started by the device unit when the disk appears. ``After=`` the local backup only
    delays it while a local backup is running, it never starts one. The disk stays unlocked
    and mounted afterwards: unmounting is left to the user.

    Args:
        exe: The installed ``btrfs-backup`` script.
        local_root: The local backup disk.
        remote_root: Directory on the external disk that receives the snapshots.
        luks_uuid: UUID of the LUKS partition.
        mapper_name: Name of the crypttab entry (``/dev/mapper/<name>``).
        mount_point: Where fstab mounts the unlocked disk.
        device_unit: systemd device unit of the LUKS partition.

    Returns:
        The content of the ``.service`` file.
    """
    crypt_unit = f"systemd-cryptsetup@{mapper_name}.service"
    lines = (
        "[Unit]",
        "Description=btrfs-backup: send snapshots to the external disk",
        f"ConditionPathExists=/dev/disk/by-uuid/{luks_uuid}",
        f"BindsTo={device_unit}",
        f"After={device_unit}",
        f"Requires={crypt_unit}",
        f"After={crypt_unit}",
        f"RequiresMountsFor={mount_point} {local_root}",
        f"After={UNIT_NAME}.service",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exe} remote --local-root {local_root} --remote-root {remote_root} --notify",
        "TimeoutStartSec=infinity",
        "Nice=10",
        "IOSchedulingClass=idle",
        "",
        "[Install]",
        f"WantedBy={device_unit}",
        "",
    )
    return "\n".join(lines)


def render_local_dropin(luks_uuid: str) -> str:
    """Return the drop-in for the local service that starts the remote service afterwards.

    ``ExecStartPost`` of a oneshot service only runs after the backup succeeded. The remote
    service is only started if the disk is plugged in: otherwise systemd would wait for the
    missing device (90 seconds by default) and then report a failed unit. ``--no-block``
    lets the local service finish first, and the leading ``-`` keeps a failing check or
    start from turning a good backup into a failed one.

    Args:
        luks_uuid: UUID of the LUKS partition of the external disk.

    Returns:
        The content of the drop-in file.
    """
    check = (
        f"test -e /dev/disk/by-uuid/{luks_uuid}"
        f" && /usr/bin/systemctl --no-block start {REMOTE_UNIT_NAME}.service"
    )
    return f"[Service]\nExecStartPost=-/bin/sh -c '{check}'\n"


@cli.command("install-remote-trigger")
@click.argument("disk")
@local_root_option
@remote_root_option
def install_remote_trigger(disk: str, local_root: Path, remote_root: Path) -> None:
    """Send the snapshots when the encrypted external disk is plugged in (needs root).

    DISK is the UUID of the LUKS partition (the UUID used in /etc/crypttab), given as plain
    UUID or as /dev/disk/by-uuid/<UUID> path. The disk has to be connected, so that the
    UUID can be verified.

    /etc/crypttab and /etc/fstab are checked first, and nothing is installed if they are
    not set up for an unattended unlock and mount. The service then unlocks and mounts the
    disk and runs 'remote'. The disk stays mounted afterwards, unmount it yourself. The
    service also runs after every successful local backup if the disk is plugged in, and
    waits while a local backup is running. Run 'install' first, so the unit can call the
    installed script.
    """
    require_root()
    require_installed_script()
    luks_uuid = parse_luks_uuid(disk)

    crypttab_entry = find_crypttab_entry(read_table(CRYPTTAB), luks_uuid)
    if crypttab_entry is None:
        fail_with(
            (
                f"crypttab: no entry for UUID {luks_uuid} in {CRYPTTAB}. Add for example:\n"
                f"      {EXAMPLE_MAPPER_NAME}  UUID={luks_uuid}  /root/.{EXAMPLE_MAPPER_NAME}.key"
                "  luks,noauto,nofail",
            )
        )
    mapper_name = crypttab_entry[CRYPTTAB_NAME]
    problems = (*disk_problems(luks_uuid), *crypttab_problems(crypttab_entry))

    fstab_entry = find_fstab_entry(read_table(FSTAB), mapper_name)
    if fstab_entry is None:
        fail_with(
            (
                *problems,
                f"fstab: no entry for /dev/mapper/{mapper_name} in {FSTAB}. Add for example:\n"
                f"      /dev/mapper/{mapper_name}  <mount point>  btrfs"
                "  noauto,nofail,noatime,compress=zstd:6  0  0",
            )
        )
    problems = (*problems, *fstab_problems(fstab_entry, remote_root))
    if problems:
        fail_with(problems)

    mount_point = Path(fstab_entry[FSTAB_MOUNT_POINT])
    require_no_spaces(local_root, remote_root, mount_point)
    service_text = render_remote_service(
        exe=INSTALL_DEST,
        local_root=local_root,
        remote_root=remote_root,
        luks_uuid=luks_uuid,
        mapper_name=mapper_name,
        mount_point=mount_point,
        device_unit=systemd_escape(f"/dev/disk/by-uuid/{luks_uuid}", "device"),
    )
    service_path = SYSTEMD_DIR / (REMOTE_UNIT_NAME + ".service")
    service_path.write_text(service_text)

    # After a successful local backup, start the remote service if the disk is plugged in.
    # A drop-in survives re-running install-timer.
    dropin_dir = SYSTEMD_DIR / (UNIT_NAME + ".service.d")
    dropin_dir.mkdir(exist_ok=True)
    dropin_path = dropin_dir / "remote.conf"
    dropin_path.write_text(render_local_dropin(luks_uuid))
    click.echo(f"Wrote {service_path} and {dropin_path}")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", service_path.name])
    click.echo(f"Test it with: sudo systemctl start {service_path.name}")
    click.echo(f"Follow the log with: journalctl -u {service_path.name} -f")


# ---- init-local ---------------------------------------------------------------


@cli.command("init-local")
@local_root_option
def init_local(local_root: Path) -> None:
    """Prepare the local backup disk (creates the snapshot directory)."""
    require_root()
    ensure_mounted_btrfs(local_root)

    snap_dir = local_root / SNAP_DIR_NAME
    if snap_dir.exists():
        click.echo(f"{snap_dir} already exists.")
    else:
        snap_dir.mkdir()
        click.echo(f"Created {snap_dir}")


# ---- init-remote --------------------------------------------------------------


@cli.command("init-remote")
@remote_root_option
def init_remote(remote_root: Path) -> None:
    """Check that the remote backup target is a mounted btrfs filesystem."""
    require_root()
    ensure_mounted_btrfs(remote_root)
    click.echo(f"Remote target {remote_root} is ready to receive snapshots.")


def find_leftovers(local_root: Path) -> tuple[Path, ...]:
    """Return incomplete ``.backup_inprogress_*`` subvolumes of earlier runs and warn about them."""
    leftovers = tuple(sorted(local_root.glob(INPROGRESS_PREFIX + "*")))
    for leftover in leftovers:
        click.echo(
            f"Warning: incomplete backup from an earlier run: {leftover} "
            f"(delete with 'btrfs subvolume delete {leftover}')",
            err=True,
        )
    return leftovers


def check_name_order(snapshots: Sequence[Path], new_name: str) -> None:
    """Refuse a snapshot name that would not sort after the newest existing snapshot.

    Everything relies on the names sorting by age. That can only break if the clock was set
    back, or right after the switch from local time to UTC names.

    Raises:
        click.ClickException: If ``new_name`` does not sort after the newest snapshot.
    """
    if snapshots and new_name <= snapshots[-1].name:
        raise click.ClickException(
            f"The new snapshot {new_name} would not sort after the newest one "
            f"({snapshots[-1].name}). Names are in UTC (trailing Z): if older snapshots are "
            "named in local time or the clock was set back, run again once the UTC time has "
            "passed the time in the newest name."
        )


def dry_run_backup(
    source: Path,
    snapshots: Sequence[Path],
    filter_file: Path,
    *,
    one_file_system: bool,
    inplace: bool,
) -> None:
    """Show what rsync would change compared to the newest snapshot; create nothing.

    Args:
        source: Directory to back up.
        snapshots: Existing snapshots, oldest first. Without any, everything counts as new.
        filter_file: rsync filter rules file.
        one_file_system: Do not cross filesystem boundaries.
        inplace: Mirror the real run's in-place update mode.

    Raises:
        click.ClickException: If rsync fails.
    """
    with tempfile.TemporaryDirectory() as empty_dir:
        compare_to = Path(empty_dir)
        if snapshots:
            compare_to = snapshots[-1]
        command = build_rsync_command(
            source,
            compare_to,
            filter_file,
            one_file_system=one_file_system,
            dry_run=True,
            inplace=inplace,
            log_file=None,
        )
        result = run(command, check=False)
    if result.returncode not in (*RSYNC_OK_CODES, RSYNC_PARTIAL_CODE):
        raise click.ClickException(f"rsync dry run failed with exit code {result.returncode}")


def commit_snapshot(tmp: Path, snap_path: Path, local_root: Path) -> None:
    """Freeze a finished working subvolume as a read-only snapshot and point ``current`` at it.

    Args:
        tmp: The working subvolume that rsync completed successfully.
        snap_path: Destination of the read-only snapshot.
        local_root: The local backup root, where the ``current`` link lives.
    """
    run(["btrfs", "subvolume", "snapshot", "-r", tmp, snap_path])
    run(["btrfs", "subvolume", "delete", tmp])
    click.echo(f"Created snapshot: {snap_path}")
    update_current_link(local_root)


def create_in_progress_subvolume(snapshots: Sequence[Path], tmp: Path) -> str:
    """Create the writable working subvolume for a new backup.

    It starts as a clone of the newest snapshot, so unchanged data stays shared via
    copy-on-write, or as an empty subvolume on the very first run.

    Args:
        snapshots: Existing snapshots, oldest first.
        tmp: Path of the working subvolume to create.

    Returns:
        Text for the first log line, saying what the backup started from.
    """
    if snapshots:
        run(["btrfs", "subvolume", "snapshot", snapshots[-1], tmp])
        return f"started from old {snapshots[-1].name}"
    run(["btrfs", "subvolume", "create", tmp])
    return "started from scratch (no previous snapshot)"


def copy_source(rsync_cmd: Sequence[str], log: LogFn) -> RsyncResult:
    """Run rsync and judge its exit code.

    rsync runs with ``TZ=UTC`` so that its log lines are in UTC like all other times.
    Exit code 23 (some files could not be copied, for example unreadable ones) still gives
    a snapshot, because a backup without a few files is better than none, but it is
    reported as a warning.

    Args:
        rsync_cmd: The command from ``build_rsync_command``.
        log: Logging function from ``start_log``.

    Returns:
        Whether the copy is usable, with a description and warnings.
    """
    log("$ " + " ".join(rsync_cmd))
    code = subprocess.run(rsync_cmd, check=False, env={**os.environ, "TZ": "UTC"}).returncode
    if code in RSYNC_OK_CODES:
        if code == RSYNC_VANISHED_CODE:
            log("Note: some files vanished during the copy (normal on a live system).")
        return RsyncResult(True, f"rsync exit code {code}", ())
    if code == RSYNC_PARTIAL_CODE:
        warning = f"rsync could not copy some files (exit code {code}), see {LOG_FILE_NAME}."
        log("Warning: " + warning)
        return RsyncResult(True, f"rsync exit code {code}, some files were not copied", (warning,))
    return RsyncResult(False, f"rsync failed with exit code {code}", ())


def finish_log(log: LogFn, log_path: Path, started: str, result: RsyncResult, begin: float) -> None:
    """Convert rsync's log dates and write the final line with the outcome and the duration."""
    try:
        normalize_rsync_log_dates(log_path)
    except OSError as error:
        log(f"Warning: could not convert rsync's log dates: {error}")
    elapsed = str(timedelta(seconds=round(time.monotonic() - begin)))
    if not result.success:
        outcome = "FAILED"
    elif result.warnings:
        outcome = "OK WITH ERRORS"
    else:
        outcome = "OK"
    log(f"Backup {started} {outcome} ({result.detail}), took {elapsed}")


def perform_local_backup(
    *,
    local_root: Path,
    source: Path,
    filter_file: Path,
    user: str,
    ollama_dirs: Sequence[Path],
    one_file_system: bool,
    inplace: bool,
    dry_run: bool,
) -> Outcome:
    """Do the work of the ``local`` command, see there.

    Returns:
        The created snapshot (or the dry run) as a message, with warnings about files that
        could not be copied and about incomplete backups of earlier runs.
    """
    ensure_mounted_btrfs(local_root)

    started = utc_now().strftime(SNAP_DATE_FMT)
    snap_dir = local_root / SNAP_DIR_NAME
    tmp = local_root / (INPROGRESS_PREFIX + started)
    snap_path = snap_dir / (SNAP_PREFIX + started)

    if not snap_dir.exists():
        raise click.ClickException(f"{snap_dir} does not exist. Run init-local first.")
    if tmp.exists() or snap_path.exists():
        raise click.ClickException(
            f"{tmp.name} or {snap_path.name} already exists, retry in a second."
        )

    leftovers = find_leftovers(local_root)
    snapshots = list_snapshots(snap_dir)

    if dry_run:
        dry_run_backup(
            source, snapshots, filter_file, one_file_system=one_file_system, inplace=inplace
        )
        return Outcome("Dry run only, nothing was changed.", ())

    check_name_order(snapshots, snap_path.name)
    origin = create_in_progress_subvolume(snapshots, tmp)

    # First thing in the new backup: replace the log inherited from the old snapshot.
    log_path = tmp / LOG_FILE_NAME
    log = start_log(log_path, f"Backup {started} {origin}")
    begin = time.monotonic()

    result = RsyncResult(False, "aborted by an error or an interrupt", ())
    try:
        # Lists are written before the copy so they end up inside this snapshot.
        save_package_lists(user, log)
        save_model_restore_scripts(ollama_dirs, log)
        save_uv_tools_script(user, log)

        rsync_cmd = build_rsync_command(
            source,
            tmp,
            filter_file,
            one_file_system=one_file_system,
            dry_run=False,
            inplace=inplace,
            log_file=log_path,
        )
        result = copy_source(rsync_cmd, log)
    finally:
        finish_log(log, log_path, started, result, begin)

    if not result.success:
        raise click.ClickException(
            f"Backup failed ({result.detail}). The incomplete backup was kept at {tmp}, "
            f"see {log_path}. Previous snapshots are untouched; delete the leftover with "
            "'btrfs subvolume delete' when you no longer need it."
        )

    commit_snapshot(tmp, snap_path, local_root)

    warnings = result.warnings
    if leftovers:
        warnings += (
            f"{len(leftovers)} incomplete backup(s) of earlier runs are kept in {local_root}. "
            "Delete them with 'btrfs subvolume delete' when you no longer need them.",
        )
    return Outcome(f"Snapshot {snap_path.name} created.", warnings)


# ---- local --------------------------------------------------------------------


@cli.command("local")
@local_root_option
@click.option(
    "--source",
    default="/",
    show_default=True,
    type=click.Path(path_type=Path, exists=True),
    help="Directory to back up.",
)
@click.option(
    "--filter-file",
    default=DEFAULT_FILTER_FILE,
    show_default=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="rsync filter rules (includes/excludes). Must exist.",
)
@click.option(
    "--primary-user",
    "primary_user_name",
    default=primary_user,
    show_default="the user with UID 1000",
    help="The main user of this computer. The per-user exports are done for this user only: "
    "its flatpak list and remotes (flatpak-user.txt, flatpak-remotes-user.txt) and its "
    "uv tools (uv-tools-restore.sh). System-wide lists are always saved. "
    "This user also gets the notifications of --notify.",
)
@click.option(
    "--ollama-dir",
    multiple=True,
    type=click.Path(path_type=Path),
    help="Extra Ollama models directory (the one containing 'manifests'). "
    "Default locations, $OLLAMA_MODELS and ~/.ollama are found automatically.",
)
@click.option(
    "--one-file-system/--cross-filesystems",
    default=True,
    show_default=True,
    help="Do not descend into other filesystems (rsync -x).",
)
@click.option(
    "--inplace/--no-inplace",
    default=True,
    show_default=True,
    help="Update changed files in place, block by block (saves space on btrfs).",
)
@click.option("--dry-run", is_flag=True, help="Show what rsync would change, create nothing.")
@click.option(
    "--notify",
    is_flag=True,
    help="Show a desktop notification to the primary user when the backup finished or failed.",
)
def local_backup(
    *,
    local_root: Path,
    source: Path,
    filter_file: Path,
    primary_user_name: str,
    ollama_dir: tuple[Path, ...],
    one_file_system: bool,
    inplace: bool,
    dry_run: bool,
    notify: bool,
) -> None:
    """Back up SOURCE into a new read-only snapshot on the local disk.

    rsync copies into a disposable clone of the last good snapshot, and a read-only
    snapshot is only committed if rsync succeeds. A failed or killed run never leaves a
    partial snapshot behind: its timestamped .backup_inprogress_<time> subvolume is kept
    for inspection (with backup.log) and never blocks later runs. Delete it manually
    with 'btrfs subvolume delete'.

    Snapshot names and log times are in UTC, marked by a trailing Z. If rsync could not
    copy some files (exit code 23), the snapshot is still created and a warning is given.
    """
    require_root()
    notify_user = None
    if notify and not dry_run:
        notify_user = primary_user_name
    job = partial(
        perform_local_backup,
        local_root=local_root,
        source=source,
        filter_file=filter_file,
        user=primary_user_name,
        ollama_dirs=ollama_dir,
        one_file_system=one_file_system,
        inplace=inplace,
        dry_run=dry_run,
    )
    run_job("local", "Local backup", notify_user, job)


# ---- remote -------------------------------------------------------------------


def find_remote_parent(snapshots: Sequence[Path], snapshot: Path, remote_root: Path) -> Path | None:
    """Find the parent for an incremental send of ``snapshot``.

    Args:
        snapshots: All local snapshots, oldest first.
        snapshot: The snapshot that is about to be sent.
        remote_root: Directory on the remote disk that receives the snapshots.

    Returns:
        The newest local snapshot older than ``snapshot`` that also exists, complete, on the
        remote disk, or None if there is none (the send then has to be a full one).
    """
    older = tuple(s for s in snapshots if s.name < snapshot.name)
    for candidate in reversed(older):
        if is_complete_subvolume(remote_root / candidate.name):
            return candidate
    return None


def send_snapshot(snapshot: Path, parent: Path | None, remote_root: Path) -> None:
    """Send one snapshot with ``btrfs send | btrfs receive`` and verify the result.

    A partially received subvolume is removed again if anything fails.

    Args:
        snapshot: The local read-only snapshot to send.
        parent: Snapshot that exists on both disks, for an incremental send; None for a full one.
        remote_root: Directory on the remote disk that receives the snapshot.

    Raises:
        click.ClickException: If sending, receiving or the final check fails.
    """
    target = remote_root / snapshot.name
    send_cmd: tuple[str, ...]
    if parent is None:
        send_cmd = ("btrfs", "send", str(snapshot))
    else:
        send_cmd = ("btrfs", "send", "-p", str(parent), str(snapshot))

    success = False
    try:
        run_pipe(send_cmd, ("btrfs", "receive", str(remote_root)))
        if not is_complete_subvolume(target):
            raise click.ClickException(f"{target} is not complete after receive.")
        success = True
    finally:
        if not success and target.exists():
            click.echo("Removing the partially received subvolume on the remote.", err=True)
            subprocess.run(["btrfs", "subvolume", "delete", str(target)], check=False)


def perform_remote_backup(local_root: Path, remote_root: Path) -> Outcome:
    """Do the work of the ``remote`` command, see there.

    Returns:
        What was sent, as a message.
    """
    ensure_mounted_btrfs(local_root)
    ensure_mounted_btrfs(remote_root)

    snapshots = list_snapshots(local_root / SNAP_DIR_NAME)
    if not snapshots:
        raise click.ClickException("No local snapshots found. Run 'local' first.")

    last_sent = read_marker(local_root)
    pending = snapshots
    if last_sent is not None:
        pending = [s for s in snapshots if s.name > last_sent]
    if not pending:
        return Outcome(f"Nothing to send, {snapshots[-1].name} is already on the remote.", ())

    click.echo(f"Sending {len(pending)} snapshot(s): " + ", ".join(s.name for s in pending))
    sent = 0
    for snapshot in pending:
        target = remote_root / snapshot.name
        if target.exists():
            if not is_complete_subvolume(target):
                raise click.ClickException(
                    f"{target} exists on the remote but looks incomplete. "
                    f"Check it, then remove it with 'btrfs subvolume delete {target}' and retry."
                )
            click.echo(f"{snapshot.name} is already complete on the remote, skipping.")
        else:
            parent = find_remote_parent(snapshots, snapshot, remote_root)
            if parent is None:
                click.echo(f"No common parent for {snapshot.name}, performing a full send.")
            send_snapshot(snapshot, parent, remote_root)
            click.echo(f"Sent {snapshot.name} to {remote_root}")
            sent += 1
        write_marker(local_root, snapshot.name)
    return Outcome(f"Sent {sent} snapshot(s) to {remote_root}, newest: {pending[-1].name}.", ())


@cli.command("remote")
@local_root_option
@remote_root_option
@click.option(
    "--notify",
    is_flag=True,
    help="Show a desktop notification to the user with UID 1000 when it finished or failed.",
)
def remote_backup(local_root: Path, remote_root: Path, notify: bool) -> None:
    """Send all local snapshots that are missing on the remote disk.

    Snapshots newer than the last confirmed one are sent oldest first, each incrementally
    against the newest older snapshot that exists on both disks (a full send if there is
    none). The progress is recorded after every snapshot, so an interrupted run continues
    where it stopped. Delete .last_sent_snapshot to check all snapshots again; those that
    are already on the remote are skipped.
    """
    require_root()
    notify_user = None
    if notify:
        notify_user = primary_user()
    run_job(
        "remote",
        "Remote backup",
        notify_user,
        partial(perform_remote_backup, local_root, remote_root),
    )


# ---- delete-local -------------------------------------------------------------


def perform_delete(local_root: Path, snapshot_name: str, force: bool) -> None:
    """Do the work of the ``delete-local`` command, see there."""
    ensure_mounted_btrfs(local_root)

    snap_dir = local_root / SNAP_DIR_NAME
    names = tuple(s.name for s in list_snapshots(snap_dir))

    if snapshot_name not in names:
        raise click.ClickException(f"{snapshot_name} is not a known backup snapshot.")

    # The names sort by age, so everything up to the marker has been sent. This also holds
    # when the snapshot named in the marker was deleted in the meantime.
    last_sent = read_marker(local_root)
    already_sent = last_sent is not None and snapshot_name <= last_sent

    if not already_sent and not force:
        raise click.ClickException(
            f"Refusing to delete {snapshot_name}: it has not been confirmed sent to the "
            f"remote (last confirmed send: {last_sent!r}). Run 'remote' first, or pass --force."
        )

    if snapshot_name == last_sent:
        click.confirm(
            f"{snapshot_name} is the newest snapshot that is known to be on the remote. "
            "Without it the next 'remote' run has to send against an older common snapshot, "
            "or send everything again if there is none. Continue?",
            abort=True,
        )

    run(["btrfs", "subvolume", "delete", snap_dir / snapshot_name])
    click.echo(f"Deleted {snap_dir / snapshot_name}")
    update_current_link(local_root)


@cli.command("delete-local")
@local_root_option
@click.argument("snapshot_name")
@click.option("--force", is_flag=True, help="Delete even if not confirmed sent to the remote.")
def delete_local(local_root: Path, snapshot_name: str, force: bool) -> None:
    """Safely delete a local snapshot, refusing if it hasn't been sent to the remote yet.

    Nothing is deleted while a 'local' or a 'remote' run is in progress.
    """
    require_root()
    with exclusive_lock("local"), exclusive_lock("remote"):
        perform_delete(local_root, snapshot_name, force)


if __name__ == "__main__":
    cli()

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

The rsync filter rules live only in /etc/btrfs-backup.filter (single source of
truth). The file must exist, otherwise `local` refuses to run.

Every backup contains a backup.log in its root (what was created/changed/deleted,
timestamps, duration). Failed or killed runs are kept as .backup_inprogress_<time>.

Requires: btrfs-progs, rsync, click  (apt install btrfs-progs rsync python3-click)
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import click


__version__ = "0.1.0"

INSTALL_DEST = Path("/usr/local/sbin/btrfs-backup")
SYSTEM_PYTHON = "/usr/bin/python3"

SNAP_PREFIX = "backup_"
SNAP_DATE_FMT = "%Y%m%d_%H%M%S"
SNAP_DIR_NAME = ".snapshots"
MARKER_FILE = ".last_sent_snapshot"
INPROGRESS_PREFIX = ".backup_inprogress_"
CURRENT_LINK_NAME = "current"
LOG_FILE_NAME = "backup.log"  # lives in the root of every backup (and snapshot)
LOG_TIME_FMT = "%Y-%m-%d %H:%M:%S"
# rsync always starts its own log lines with "YYYY/MM/DD HH:MM:SS [pid] " (not configurable,
# --log-file-format only changes what follows), so those dates are rewritten after the run.
RSYNC_LOG_DATE = re.compile(rb"^(\d{4})/(\d{2})/(\d{2}) ")

LogFn = Callable[[str], None]

DEFAULT_FILTER_FILE = Path("/etc/btrfs-backup.filter")
LISTS_DIR = Path("/root/backup-lists")

# rsync exit codes that still count as a successful backup:
# 0 = ok, 24 = "some source files vanished" (normal when copying a live system)
RSYNC_VANISHED_CODE = 24
RSYNC_OK_CODES = (0, RSYNC_VANISHED_CODE)

# Ollama manifests live in manifests/<registry>/<namespace>/<model>/<tag>.
MIN_MANIFEST_PARTS = 3  # registry, model, tag (the namespace can be missing)
LIBRARY_MODEL_PARTS = 2  # "library" and the model name


# ---- helpers ----------------------------------------------------------------


def require_root() -> None:
    """Abort with an error message unless the script runs as root.

    Raises:
        click.ClickException: If the effective user id is not 0.
    """
    if os.geteuid() != 0:
        raise click.ClickException("This command must be run as root (use sudo).")


def start_log(log_path: Path, first_message: str) -> LogFn:
    """Overwrite the log with a first line and return a function that logs to file and terminal."""

    def log(message: str) -> None:
        """Print a timestamped line and append it to the log file."""
        line = datetime.now().strftime(LOG_TIME_FMT) + " " + message
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
        check: Raise ``CalledProcessError`` on a non-zero exit code.
        **kwargs: Passed through to ``subprocess.run``.

    Returns:
        The completed process.
    """
    args = [str(c) for c in cmd]
    click.echo("$ " + " ".join(args))
    return subprocess.run(args, check=check, **kwargs)


def run_pipe(send_cmd: Sequence[str], receive_cmd: Sequence[str]) -> None:
    """Run ``send_cmd | receive_cmd`` and fail if either side fails.

    Used for ``btrfs send | btrfs receive``. Both exit codes are checked, whereas a plain
    shell pipe only reports the last one.

    Args:
        send_cmd: Command whose stdout is piped into the receiver.
        receive_cmd: Command reading the stream from its stdin.

    Raises:
        click.ClickException: If the send or the receive process exits non-zero.
    """
    click.echo("$ " + " ".join(send_cmd) + " | " + " ".join(receive_cmd))
    send = subprocess.Popen(send_cmd, stdout=subprocess.PIPE)
    receive = subprocess.Popen(receive_cmd, stdin=send.stdout)
    if send.stdout is not None:
        send.stdout.close()
    receive.communicate()
    send.wait()
    if send.returncode != 0:
        raise click.ClickException("btrfs send failed")
    if receive.returncode != 0:
        raise click.ClickException("btrfs receive failed")


def ensure_mounted_btrfs(path: Path) -> None:
    """Refuse to continue unless `path` is a mountpoint of a btrfs filesystem.

    This protects against writing a whole backup into the root filesystem
    because the backup disk was not mounted.
    """
    if not path.is_dir():
        raise click.ClickException(f"{path} does not exist.")
    if not os.path.ismount(path):
        raise click.ClickException(f"{path} is not a mountpoint. Mount the disk first.")
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    fstype = result.stdout.strip()
    if fstype != "btrfs":
        raise click.ClickException(f"{path} is not a btrfs mount (found: {fstype!r}).")


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


def save_package_lists(flatpak_user: str, log: LogFn) -> None:
    """Write lists of installed packages/flatpaks into LISTS_DIR (part of the backup)."""
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, list[str]]] = []

    if shutil.which("apt-mark"):
        jobs.append(("manual-packages.txt", ["apt-mark", "showmanual"]))
    if shutil.which("snap"):
        jobs.append(("snaps.txt", ["snap", "list"]))

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
        if flatpak_user:
            try:
                home = pwd.getpwnam(flatpak_user).pw_dir
            except KeyError:
                raise click.ClickException(f"Unknown user for flatpaks: {flatpak_user!r}") from None
            as_user = ["runuser", "-u", flatpak_user, "--", "env", f"HOME={home}"]
            jobs.append(
                (
                    "flatpak-user.txt",
                    [
                        *as_user,
                        "flatpak",
                        "list",
                        "--user",
                        "--app",
                        "--columns=origin,application",
                    ],
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
    """Rewrite the leading YYYY/MM/DD of rsync's log lines to YYYY-MM-DD (binary safe)."""
    staged = log_path.with_name(log_path.name + ".tmp")
    with log_path.open("rb") as source, staged.open("wb") as target:
        for line in source:
            target.write(RSYNC_LOG_DATE.sub(rb"\1-\2-\3 ", line, count=1))
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
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    refs = ollama_refs(find_ollama_manifest_dirs(extra_ollama_dirs))
    if refs:
        lines = ["set -u"]
        lines.extend("ollama pull " + shlex.quote(ref) for ref in refs)
        write_restore_script(
            LISTS_DIR / "ollama-restore.sh",
            f"Generated by btrfs_backup.py on {stamp}: re-download Ollama models.",
            lines,
            log,
        )
    else:
        log("No Ollama models found, skipping ollama-restore.sh.")

    repos = huggingface_repos()
    if repos:
        lines = [
            "HF=$(command -v hf || command -v huggingface-cli) || "
            '{ echo "hf / huggingface-cli not found" >&2; exit 1; }'
        ]
        lines.extend('"$HF" download ' + shlex.quote(repo) for repo in repos)
        write_restore_script(
            LISTS_DIR / "huggingface-restore.sh",
            f"Generated by btrfs_backup.py on {stamp}: re-download Hugging Face models.",
            lines,
            log,
        )
    else:
        log("No Hugging Face models found, skipping huggingface-restore.sh.")


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
    """Add the required ``--local-root`` option to a click command."""
    return click.option(
        "--local-root",
        required=True,
        type=click.Path(path_type=Path),
        help="Mountpoint of the local btrfs backup disk (e.g. /media/kound/ssd-backup).",
    )(f)


def remote_root_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """Add the required ``--remote-root`` option to a click command."""
    return click.option(
        "--remote-root",
        required=True,
        type=click.Path(path_type=Path),
        help="Mountpoint of the remote btrfs backup disk (e.g. /media/kound/hdd-backup).",
    )(f)


@click.group()
@click.version_option(__version__, prog_name="btrfs-backup")
def cli() -> None:
    """Snapshot backups: SOURCE -> local (btrfs) -> remote (btrfs). Run as root."""


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


def warn_about_leftovers(local_root: Path) -> None:
    """Warn about incomplete ``.backup_inprogress_*`` subvolumes left by earlier runs."""
    for leftover in sorted(local_root.glob(INPROGRESS_PREFIX + "*")):
        click.echo(
            f"Warning: incomplete backup from an earlier run: {leftover} "
            f"(delete with 'btrfs subvolume delete {leftover}')",
            err=True,
        )


def dry_run_backup(
    source: Path,
    snapshots: list[Path],
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
    if result.returncode not in RSYNC_OK_CODES:
        raise click.ClickException(f"rsync dry run failed with exit code {result.returncode}")
    click.echo("Dry run only, nothing was changed.")


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


def create_in_progress_subvolume(snapshots: list[Path], tmp: Path) -> str:
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
    "--flatpak-user",
    default="kound",
    show_default=True,
    help="User whose per-user flatpak list is saved (empty string to skip).",
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
def local_backup(
    *,
    local_root: Path,
    source: Path,
    filter_file: Path,
    flatpak_user: str,
    ollama_dir: tuple[Path, ...],
    one_file_system: bool,
    inplace: bool,
    dry_run: bool,
) -> None:
    """Back up SOURCE into a new read-only snapshot on the local disk.

    rsync copies into a disposable clone of the last good snapshot, and a read-only
    snapshot is only committed if rsync succeeds. A failed or killed run never leaves a
    partial snapshot behind: its timestamped .backup_inprogress_<time> subvolume is kept
    for inspection (with backup.log) and never blocks later runs. Delete it manually
    with 'btrfs subvolume delete'.
    """
    require_root()
    ensure_mounted_btrfs(local_root)

    started = datetime.now().strftime(SNAP_DATE_FMT)
    snap_dir = local_root / SNAP_DIR_NAME
    tmp = local_root / (INPROGRESS_PREFIX + started)
    snap_path = snap_dir / (SNAP_PREFIX + started)

    if not snap_dir.exists():
        raise click.ClickException(f"{snap_dir} does not exist. Run init-local first.")
    if tmp.exists() or snap_path.exists():
        raise click.ClickException(
            f"{tmp.name} or {snap_path.name} already exists, retry in a second."
        )

    warn_about_leftovers(local_root)
    snapshots = list_snapshots(snap_dir)

    if dry_run:
        dry_run_backup(
            source, snapshots, filter_file, one_file_system=one_file_system, inplace=inplace
        )
        return

    origin = create_in_progress_subvolume(snapshots, tmp)

    # First thing in the new backup: replace the log inherited from the old snapshot.
    log_path = tmp / LOG_FILE_NAME
    log = start_log(log_path, f"Backup {started} {origin}")
    begin = time.monotonic()

    success = False
    detail = "aborted by an error or an interrupt"
    try:
        # Lists are written before the copy so they end up inside this snapshot.
        save_package_lists(flatpak_user, log)
        save_model_restore_scripts(ollama_dir, log)

        rsync_cmd = build_rsync_command(
            source,
            tmp,
            filter_file,
            one_file_system=one_file_system,
            dry_run=False,
            inplace=inplace,
            log_file=log_path,
        )
        log("$ " + " ".join(rsync_cmd))
        rsync_code = subprocess.run(rsync_cmd, check=False).returncode
        if rsync_code in RSYNC_OK_CODES:
            success = True
            detail = f"rsync exit code {rsync_code}"
            if rsync_code == RSYNC_VANISHED_CODE:
                log("Note: some files vanished during the copy (normal on a live system).")
        else:
            detail = f"rsync failed with exit code {rsync_code}"
    finally:
        try:
            normalize_rsync_log_dates(log_path)
        except OSError as error:
            log(f"Warning: could not convert rsync's log dates: {error}")
        elapsed = str(timedelta(seconds=round(time.monotonic() - begin)))
        if success:
            outcome = "OK"
        else:
            outcome = "FAILED"
        log(f"Backup {started} {outcome} ({detail}), took {elapsed}")

    if not success:
        raise click.ClickException(
            f"Backup failed ({detail}). The incomplete backup was kept at {tmp}, "
            f"see {log_path}. Previous snapshots are untouched; delete the leftover with "
            "'btrfs subvolume delete' when you no longer need it."
        )

    commit_snapshot(tmp, snap_path, local_root)


# ---- remote -------------------------------------------------------------------


def find_remote_parent(snapshots: list[Path], snapshot: Path, remote_root: Path) -> Path | None:
    """Find the parent for an incremental send of ``snapshot``.

    Args:
        snapshots: All local snapshots, oldest first.
        snapshot: The snapshot that is about to be sent.
        remote_root: Directory on the remote disk that receives the snapshots.

    Returns:
        The newest local snapshot older than ``snapshot`` that also exists, complete, on the
        remote disk, or None if there is none (the send then has to be a full one).
    """
    older = [s for s in snapshots if s.name < snapshot.name]
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
    if parent is None:
        send_cmd = ["btrfs", "send", str(snapshot)]
    else:
        send_cmd = ["btrfs", "send", "-p", str(parent), str(snapshot)]

    success = False
    try:
        run_pipe(send_cmd, ["btrfs", "receive", str(remote_root)])
        if not is_complete_subvolume(target):
            raise click.ClickException(f"{target} is not complete after receive.")
        success = True
    finally:
        if not success and target.exists():
            click.echo("Removing the partially received subvolume on the remote.", err=True)
            subprocess.run(["btrfs", "subvolume", "delete", str(target)], check=False)


@cli.command("remote")
@local_root_option
@remote_root_option
def remote_backup(local_root: Path, remote_root: Path) -> None:
    """Send all local snapshots that are missing on the remote disk.

    Snapshots newer than the last confirmed one are sent oldest first, each incrementally
    against the newest older snapshot that exists on both disks (a full send if there is
    none). The progress is recorded after every snapshot, so an interrupted run continues
    where it stopped. Delete .last_sent_snapshot to check all snapshots again; those that
    are already on the remote are skipped.
    """
    require_root()
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
        click.echo(f"Nothing to send, {snapshots[-1].name} is already on the remote.")
        return

    click.echo(f"Sending {len(pending)} snapshot(s): " + ", ".join(s.name for s in pending))
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
        write_marker(local_root, snapshot.name)


# ---- delete-local -------------------------------------------------------------


@cli.command("delete-local")
@local_root_option
@click.argument("snapshot_name")
@click.option("--force", is_flag=True, help="Delete even if not confirmed sent to the remote.")
def delete_local(local_root: Path, snapshot_name: str, force: bool) -> None:
    """Safely delete a local snapshot, refusing if it hasn't been sent to the remote yet."""
    require_root()
    ensure_mounted_btrfs(local_root)

    snap_dir = local_root / SNAP_DIR_NAME
    names = [s.name for s in list_snapshots(snap_dir)]

    if snapshot_name not in names:
        raise click.ClickException(f"{snapshot_name} is not a known backup snapshot.")

    last_sent = read_marker(local_root)
    already_sent = False
    if last_sent is not None and last_sent in names:
        already_sent = names.index(snapshot_name) <= names.index(last_sent)

    if not already_sent and not force:
        raise click.ClickException(
            f"Refusing to delete {snapshot_name}: it has not been confirmed sent to the "
            f"remote (last confirmed send: {last_sent!r}). Run 'remote' first, or pass --force."
        )

    if snapshot_name == last_sent:
        click.confirm(
            f"{snapshot_name} is the parent for the next incremental send. Deleting it "
            "means the next 'remote' run falls back to a full send. Continue?",
            abort=True,
        )

    run(["btrfs", "subvolume", "delete", snap_dir / snapshot_name])
    click.echo(f"Deleted {snap_dir / snapshot_name}")
    update_current_link(local_root)


if __name__ == "__main__":
    cli()

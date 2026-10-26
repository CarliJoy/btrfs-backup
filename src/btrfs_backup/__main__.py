"""Allow running the tool with ``python -m btrfs_backup``."""

from __future__ import annotations

from btrfs_backup.cli import cli


if __name__ == "__main__":
    cli()

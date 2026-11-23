"""Estimate readily reclaimable page cache for cgroup v2 admission decisions."""

from pathlib import Path


def reclaimable_file_bytes(group: Path) -> int:
    try:
        stats = dict(line.split() for line in (group / "memory.stat").read_text().splitlines())
        # tmpfs/shmem and unflushed writes cannot be treated as immediately free.
        inactive = min(int(stats["inactive_file"]), int(stats["file"]) - int(stats["shmem"]))
        clean = max(0, inactive - int(stats["file_dirty"]) - int(stats["file_writeback"]))
    except (OSError, ValueError, KeyError):
        return 0
    # Leave half the clean inactive cache in place. Re-reading Lake artifacts
    # can be expensive, and memory.stat counters do not represent an atomic snapshot.
    return clean // 2

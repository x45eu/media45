"""Read-only filesystem inventory; never modify, move or open media files.

Only run in a dedicated scanner container with explicitly mounted read-only roots.
No symlink traversal, hash extraction or metadata calls. Results require admin review.
"""
import os
import re
import stat
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from catalog.models import ScannedFile

ROOTS = {
    "movies": (Path("/media/movies"), "movie"),
    "series": (Path("/media/series"), "series"),
}
VIDEO = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"})
SEASON_EPISODE = re.compile(r"(?i)(?:[. _-]*s\d{1,2}e\d{1,3}).*$")
YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")


def guess_title(path, root):
    """Extract a review hint, not a verified identification."""
    raw = path.parts[0] if len(path.parts) > 1 else path.stem
    raw = SEASON_EPISODE.sub("", raw)
    raw = YEAR_SUFFIX.sub("", raw)
    return re.sub(r"[._]+", " ", raw).strip()[:300] or "Unbekannt"


class Command(BaseCommand):
    help = "Inventarisiert eingehängte Medienverzeichnisse nur lesend."

    def add_arguments(self, parser):
        parser.add_argument("--root", choices=(*ROOTS, "all"), default="all")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=100000)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000000:
            raise CommandError("--limit muss zwischen 1 und 1000000 liegen.")
        selected = ROOTS if options["root"] == "all" else {options["root"]: ROOTS[options["root"]]}
        count = 0
        new = 0
        now = timezone.now()
        for root_name, (root_path, kind) in selected.items():
            if not root_path.is_dir():
                raise CommandError(f"Medienverzeichnis nicht eingehängt: {root_path}")
            root_real = root_path.resolve(strict=True)
            for dirpath, dirs, files in os.walk(root_real, followlinks=False):
                # Hidden folders and symlinked directories are deliberately excluded.
                dirs[:] = [
                    name for name in dirs
                    if not name.startswith(".")
                    and not (Path(dirpath) / name).is_symlink()
                ]
                for filename in files:
                    if filename.startswith("."):
                        continue
                    file_path = Path(dirpath) / filename
                    if file_path.suffix.lower() not in VIDEO:
                        continue
                    try:
                        info = file_path.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    # No file content is opened. Paths are always relative inside the scanner container.
                    relative = file_path.relative_to(root_real)
                    if any(part in (".", "..") for part in relative.parts):
                        continue
                    count += 1
                    if count > limit:
                        raise CommandError(f"Scan-Limit {limit} erreicht; getrennte kleinere Scans verwenden.")
                    if options["dry_run"]:
                        continue
                    _, created = ScannedFile.objects.update_or_create(
                        root=root_name,
                        relative_path=relative.as_posix(),
                        defaults={
                            "media_kind": kind,
                            "suggested_title": guess_title(relative, root_name),
                            "file_size": info.st_size,
                            "modified_ns": info.st_mtime_ns,
                            "last_seen_at": now,
                        },
                    )
                    new += int(created)
        self.stdout.write(self.style.SUCCESS(
            f"Scan abgeschlossen: {count} Dateien erkannt, {new} neue Einträge."
            + (" (Dry-Run: nichts gespeichert)" if options["dry_run"] else "")
        ))

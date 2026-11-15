"""Pure, conservative filename heuristics. Not a verified identity matcher."""
import hashlib
import re
from pathlib import PurePosixPath

from catalog.models import Media

EPISODE = re.compile(r"(?i)(?:[. _-]*s\d{1,2}e\d{1,3}(?:[ -]*e\d{1,3})?).*$")
TRAILING_YEAR = re.compile(r"(?i)[ ._-]*\((?:19|20)\d{2}\)\s*$")
VIDEO_TAG = re.compile(r"(?i)(?:[ ._-]+(?:1080p|720p|2160p|4k|bluray|brrip|web[- .]?dl|webrip|dvdrip|x264|x265|h264|h265|hevc|remux|hdr|proper|repack))+(?:[ ._-].*)?$")


def tidy(name):
    name = TRAILING_YEAR.sub("", name)
    name = re.sub(r"[._]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()[:300] or "Unbekannt"


def group_info(root, relative_path):
    """Return (identity hash, kind, suggested title).

    Hash is scoped to *root* by the DB constraint. No matching across roots:
    similarly named items may be entirely different works.
    """
    path = PurePosixPath(relative_path)
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts:
        raise ValueError("Ungültiger relativer Medienpfad")
    if root == "movies":
        if len(parts) > 1:
            key = parts[0]
            display = parts[0]
        else:
            display = VIDEO_TAG.sub("", path.stem)
            key = display
        kind = Media.Kind.MOVIE
        title = tidy(display)
    elif root == "series":
        if len(parts) > 1:
            key = parts[0]
            display = parts[0]
        else:
            display = EPISODE.sub("", path.stem)
            key = display
        kind = Media.Kind.SERIES
        title = tidy(display)
    else:
        raise ValueError(f"Unbekannte Bibliothek: {root}")
    identity = hashlib.sha256(key.casefold().encode("utf-8")).hexdigest()
    return identity, kind, title[:300]

"""Persisting user-uploaded documents into the agent's workspace.

Two hazards handled here, both because the filename comes from the browser and
is fully attacker-controlled:

* **Traversal.** An upload named ``../../.env`` must land in the workspace, not
  above it. Only the final path component is ever used.
* **Collision.** Banks reuse fixed export names, so February's download is very
  often called exactly what January's was. Overwriting would destroy the earlier
  statement and silently change the contents of a path that a previously written
  analysis already cites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["destination_for", "save_uploads"]


def destination_for(directory: Path, filename: str) -> Path | None:
    """Resolve an untrusted upload name to a safe, non-colliding destination.

    Args:
        directory: The workspace directory to write into.
        filename: The browser-supplied name.

    Returns:
        A path inside ``directory``, suffixed ``-2``, ``-3``... if needed, or
        None if the name has no usable final component.
    """
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        return None

    destination = directory / safe_name
    if not destination.exists():
        return destination

    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_uploads(files: list[Any], directory: Path) -> tuple[list[str], list[str]]:
    """Write uploaded files into ``directory``, reporting saves and skips.

    A file fails to land two ways -- a name with no usable final component, or
    a write that errors -- and dropping either quietly is data loss the user
    never hears about: they attached a file, the agent is never told about it,
    and nothing on screen says why. So the skip is returned rather than
    swallowed, leaving the caller free to say so.

    The write error is caught here rather than raised for the second half of
    that same reason. A raise abandons the batch mid-loop, so the files already
    on disk are never named to the caller and the agent is never told they
    exist; in the Streamlit app it also escapes the turn's own try/except, which
    is the only place an exception is redacted and escaped before it renders.

    Args:
        files: Objects exposing ``.name`` and ``.getvalue()`` (Streamlit's
            ``UploadedFile``).
        directory: Destination directory, created by the caller.

    Returns:
        ``(saved, skipped)``. ``saved`` holds the filenames actually written,
        which may differ from the uploaded names when a collision was renamed;
        ``skipped`` holds the uploaded names that could not be written at all,
        reported as uploaded because no file exists under any other name.
    """
    saved: list[str] = []
    skipped: list[str] = []
    for item in files:
        destination = destination_for(directory, item.name)
        if destination is None:
            skipped.append(item.name)
            continue
        try:
            destination.write_bytes(item.getvalue())
        except OSError:
            skipped.append(item.name)
            continue
        saved.append(destination.name)
    return saved, skipped

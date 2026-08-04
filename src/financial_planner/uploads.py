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


def save_uploads(files: list[Any], directory: Path) -> list[str]:
    """Write uploaded files into ``directory``, returning the names used.

    Args:
        files: Objects exposing ``.name`` and ``.getvalue()`` (Streamlit's
            ``UploadedFile``).
        directory: Destination directory, created by the caller.

    Returns:
        The filenames actually written, which may differ from the uploaded
        names when a collision was renamed.
    """
    saved: list[str] = []
    for item in files:
        destination = destination_for(directory, item.name)
        if destination is None:
            continue
        destination.write_bytes(item.getvalue())
        saved.append(destination.name)
    return saved

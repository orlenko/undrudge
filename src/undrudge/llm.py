"""LLM invocation seam.

Resolves the configured ``[llm].command`` to a concrete argv. Two modes:

- ``"@bundled"`` (default): use the wrapper script shipped with this package.
  The wrapper runs Claude under ``nono`` if available, else exec's bare
  ``claude``. Means undrudge ships everything it needs to call Claude in a
  reasonable sandbox without external repos.
- Anything else: treated as a literal command, resolved via ``shutil.which``.
  Useful for users who already have their own wrapper (e.g.
  ``/path/to/your-claude-wrapper.sh``) or want to point at bare ``claude``.
"""

from __future__ import annotations

import os
import shutil
from importlib.resources import as_file, files
from pathlib import Path

BUNDLED_SENTINEL = "@bundled"


def bundled_wrapper_path() -> Path:
    """Filesystem path to the bundled ``claude-sandboxed.sh`` wrapper.

    Resolved via ``importlib.resources``; works whether undrudge is
    installed normally or run from source. The path is a real on-disk
    file (the wrapper is a plain script, not zipped).
    """
    ref = files("undrudge").joinpath("scripts/claude-sandboxed.sh")
    with as_file(ref) as p:  # context manager safe for any backend
        # When `as_file` returns a path under a temp dir for zipped installs,
        # we'd need to keep the context open. Hatch ships flat files, so the
        # path is stable on disk. Returning a stable Path is fine.
        return Path(p)


def resolve_command(command: str) -> Path:
    """Resolve the configured command string to an absolute, executable path.

    Raises ``FileNotFoundError`` if neither the bundled wrapper nor the
    configured binary can be found. This is the only failure mode for
    Phase 3's ``analyze``; doctor surfaces it ahead of time.
    """
    if command == BUNDLED_SENTINEL:
        path = bundled_wrapper_path()
        if not path.exists():
            raise FileNotFoundError(f"bundled wrapper missing: {path}")
        if not os.access(path, os.X_OK):
            # Wheels can lose +x; restore it.
            path.chmod(0o755)
        return path

    if "/" in command:
        p = Path(command).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"configured llm.command not found: {p}")
        return p

    found = shutil.which(command)
    if found is None:
        raise FileNotFoundError(f"configured llm.command not on PATH: {command}")
    return Path(found)

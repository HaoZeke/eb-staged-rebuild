"""Path checks that do not follow symlinks."""

import os
import stat


def abspath(path):
    return os.path.abspath(os.path.normpath(os.fspath(path)))


def is_within(child, parent):
    child = abspath(child)
    parent = abspath(parent)
    if child == parent:
        return True
    prefix = parent.rstrip(os.sep) + os.sep
    return child.startswith(prefix)


def lstat_kind(path):
    """``missing``, ``symlink``, ``dir``, ``file``, or ``other``."""
    if not os.path.lexists(path):
        return "missing"
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def is_real_dir(path):
    return lstat_kind(path) == "dir"


def assert_real_components(path, stop_at=None):
    """Return a refusal reason if any existing component of ``path`` is a symlink.

    ``stop_at`` is an ancestor that is not itself checked (used when the caller
    already accepted the software root). ``None`` checks every existing component.
    """
    path = abspath(path)
    stop = abspath(stop_at) if stop_at else None
    current = path
    while True:
        if stop is not None and current == stop:
            break
        kind = lstat_kind(current)
        if kind == "symlink":
            return f"{current} is a symlink"
        if current == os.path.dirname(current):
            break
        parent = os.path.dirname(current)
        if not os.path.lexists(current):
            current = parent
            continue
        current = parent
    return None

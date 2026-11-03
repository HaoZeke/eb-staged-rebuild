"""Same-directory writes. The destination name is replaced only after the bytes are durable."""

import os
import stat
import tempfile

from eb_staged_rebuild.errors import Refused


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_mode(path):
    """Mode for a new file: the existing file's mode, otherwise 0644 masked by umask."""
    if os.path.lexists(path) and not os.path.islink(path):
        return stat.S_IMODE(os.lstat(path).st_mode)
    umask = os.umask(0)
    os.umask(umask)
    return 0o644 & ~umask


def atomic_write(path, data, faults, step, mode=None):
    if faults is None:
        from eb_staged_rebuild.faults import FaultInjector

        faults = FaultInjector()
    """Write ``data`` to ``path`` by renaming a complete temporary file in the same directory.

    Fault points, each raised before the action:

    * ``<step>.write`` before the temporary file is created
    * ``<step>.fsync`` after the bytes are written and before ``fsync``
    * ``<step>.rename`` after ``fsync`` and before ``os.replace``

    A crash at any of those points leaves ``path`` as it was. ``os.replace`` of a
    file in the same directory is the publish step; it does not unlink the
    destination first.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(path):
        raise Refused(f"refusing to replace a symlink: {path}")
    if mode is None:
        mode = file_mode(path)
    faults.reach(f"{step}.write")
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.new-", dir=parent)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        faults.reach(f"{step}.fsync")
        os.fsync(fd)
    finally:
        os.close(fd)

    faults.reach(f"{step}.rename")
    os.replace(tmp, path)
    fsync_dir(parent)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def sha256_bytes(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()

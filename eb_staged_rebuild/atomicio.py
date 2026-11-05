"""Same-directory writes. The destination name is replaced only after the bytes are durable."""

import errno
import os
import stat

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


def _acl_missing(error):
    return error.errno in {
        errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA),
        errno.ENOTSUP, errno.EOPNOTSUPP,
    }


def _metadata(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"metadata source is not a regular file: {path}")
        try:
            acl = os.getxattr(fd, "system.posix_acl_access")
        except OSError as error:
            if not _acl_missing(error):
                raise
            acl = None
        return info, acl
    finally:
        os.close(fd)


def _restore_metadata(fd, metadata, mode):
    if metadata is not None:
        info, acl = metadata
        os.fchown(fd, info.st_uid, info.st_gid)
        if acl is not None:
            os.setxattr(fd, "system.posix_acl_access", acl)
        else:
            try:
                os.removexattr(fd, "system.posix_acl_access")
            except OSError as error:
                if not _acl_missing(error):
                    raise
    os.fchmod(fd, mode)


def atomic_write(path, data, faults, step, mode=None, metadata_from=None):
    """Replace a destination only after its bytes and access metadata are durable.

    Existing files retain their ownership, mode and POSIX access ACL, including
    the absence of an ACL. ``metadata_from`` selects a prepared permission
    policy instead. New files without a metadata source inherit the directory's
    default ACL. Metadata errors leave the destination unchanged.

    Fault points ``<step>.write``, ``<step>.fsync`` and ``<step>.rename``
    precede those operations. The destination is never unlinked first.
    """
    if faults is None:
        from eb_staged_rebuild.faults import FaultInjector

        faults = FaultInjector()
    if isinstance(data, str):
        data = data.encode("utf-8")
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(path):
        raise Refused(f"refusing to replace a symlink: {path}")
    metadata_path = metadata_from
    if metadata_path is None and os.path.lexists(path):
        metadata_path = path
    try:
        metadata = _metadata(metadata_path) if metadata_path is not None else None
    except OSError as error:
        raise Refused(f"cannot read access metadata for {metadata_path}: {error}") from error
    if mode is None:
        mode = stat.S_IMODE(metadata[0].st_mode) if metadata is not None else file_mode(path)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.new")
    try:
        stale = os.lstat(tmp)
    except FileNotFoundError:
        stale = None
    if stale is not None:
        if not stat.S_ISREG(stale.st_mode) or stale.st_uid != os.geteuid() or stale.st_nlink != 1:
            raise Refused(f"temporary file must be an owned regular file with one link: {tmp}")
        os.unlink(tmp)

    faults.reach(f"{step}.write")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        try:
            _restore_metadata(fd, metadata, mode)
        except OSError as error:
            raise Refused(f"cannot preserve access metadata for {path}: {error}") from error
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

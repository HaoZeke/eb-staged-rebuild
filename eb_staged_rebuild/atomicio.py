"""Same-directory writes. The destination name is replaced only after the bytes are durable."""

import errno
import os
import stat
from collections import namedtuple

from eb_staged_rebuild.errors import Refused

# Most native first. A filesystem may expose one ACL through several names
# (a native NFSv4 model, the generic NFSv4 model, a POSIX projection); only
# the most native listed name is written and every listed name is verified.
ACL_ATTRIBUTES = (
    "system.gpfs_nfs4_acl",
    "system.nfs4_acl",
    "system.posix_acl_access",
)

_NO_DATA = {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}
_NOT_PROVIDED = _NO_DATA | {errno.ENOTSUP, errno.EOPNOTSUPP}
# NFSv4-family names only. EINVAL on an unlisted probe is suppressed solely
# when the same inspection also lists a POSIX access ACL that is read below.
_NFS4_FAMILY = frozenset({"system.gpfs_nfs4_acl", "system.nfs4_acl"})
_POSIX_ACCESS = "system.posix_acl_access"

Metadata = namedtuple("Metadata", "info acls")


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


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_ctime_ns)


def _listed_acls(fd):
    names = set()
    for name in os.listxattr(fd):
        if name in ACL_ATTRIBUTES:
            names.add(name)
        elif name.startswith("system."):
            raise Refused(f"unsupported access-control attribute {name}")
    return names


def _read_all_acls(fd):
    listed = _listed_acls(fd)
    acls = {}
    for name in ACL_ATTRIBUTES:
        try:
            acls[name] = os.getxattr(fd, name)
        except OSError as error:
            if name in listed:
                if error.errno in _NO_DATA:
                    raise Refused(f"{name} was listed but is no longer readable") from error
                raise
            if error.errno in _NOT_PROVIDED:
                continue
            # Narrow compatibility: unlisted NFSv4-family EINVAL only when
            # this inspection lists POSIX access (read below; missing POSIX
            # bytes fail closed). Never suppress EINVAL for POSIX itself,
            # listed NFSv4 names, or denied/other errors.
            if (
                error.errno == errno.EINVAL
                and name in _NFS4_FAMILY
                and _POSIX_ACCESS in listed
            ):
                continue
            raise
    if _POSIX_ACCESS in listed and _POSIX_ACCESS not in acls:
        raise Refused(f"{_POSIX_ACCESS} was listed but is no longer readable")
    return acls


def read_acls(fd):
    """Read listed and virtual ACLs through one regular-file descriptor.

    GPFS can expose system.nfs4_acl through getxattr without listing it.
    Every known ACL name is probed; denied inspection and metadata changes
    refuse replacement. Two complete passes protect the snapshot within
    one ctime tick.
    """
    before = os.fstat(fd)
    acls = _read_all_acls(fd)
    if _read_all_acls(fd) != acls or _identity(os.fstat(fd)) != _identity(before):
        raise Refused("access metadata changed during inspection")
    return acls


def read_metadata(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"metadata source is not a regular file: {path}")
        acls = read_acls(fd)
        if _identity(os.fstat(fd)) != _identity(info):
            raise Refused("access metadata changed during inspection")
        return Metadata(info, acls)
    finally:
        os.close(fd)


def _same_metadata(left, right):
    return _identity(left.info) == _identity(right.info) and left.acls == right.acls


def _restore_metadata(fd, metadata, mode):
    """Copy ownership, mode and ACL bytes, then read them back from ``fd``.

    Ownership and mode come first: chown can clear set-id bits and chmod can
    rewrite an NFSv4 ACL or a POSIX mask, so neither may follow the ACL.
    """
    if metadata is None:
        os.fchmod(fd, mode)
        return
    os.fchown(fd, metadata.info.st_uid, metadata.info.st_gid)
    os.fchmod(fd, mode)
    primary = next((name for name in ACL_ATTRIBUTES if name in metadata.acls), None)
    # A mode-derived virtual ACL can already match the required policy.
    # Writing it can materialize an additional native ACL on GPFS.
    if primary is not None and read_acls(fd) != metadata.acls:
        os.setxattr(fd, primary, metadata.acls[primary])
    # Applying the required ACL can replace inherited virtual views.
    for _ in ACL_ATTRIBUTES:
        present = read_acls(fd)
        extra = [name for name in ACL_ATTRIBUTES if name in present and name not in metadata.acls]
        if not extra:
            break
        # Removing a native ACL can also remove the views derived from it.
        os.removexattr(fd, extra[0])
    restored = os.fstat(fd)
    if (restored.st_uid, restored.st_gid, stat.S_IMODE(restored.st_mode)) != (
        metadata.info.st_uid, metadata.info.st_gid, mode
    ):
        raise Refused(
            "ownership or mode did not round-trip: expected "
            f"{metadata.info.st_uid}:{metadata.info.st_gid} {mode:o}, found "
            f"{restored.st_uid}:{restored.st_gid} {stat.S_IMODE(restored.st_mode):o}"
        )
    if read_acls(fd) != metadata.acls:
        raise Refused("access-control attributes did not round-trip")


def _discard(tmp, created):
    try:
        current = os.lstat(tmp)
        if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            os.unlink(tmp)
    except OSError:
        pass


def atomic_write(path, data, faults, step, mode=None, metadata_from=None):
    """Replace a destination only after its bytes and access metadata are durable.

    Existing files retain their ownership, mode and listed ACL attributes
    (POSIX access, generic NFSv4 and GPFS native NFSv4), including the
    absence of an ACL. ``metadata_from`` selects a prepared permission
    policy instead. New files without a metadata source inherit the
    directory's default ACL. Metadata that cannot be inspected, restored
    byte for byte, or that changes before the rename leaves the destination
    unchanged.

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
        metadata = read_metadata(metadata_path) if metadata_path is not None else None
    except (OSError, Refused) as error:
        raise Refused(f"cannot read access metadata for {metadata_path}: {error}") from error
    if mode is None:
        mode = stat.S_IMODE(metadata.info.st_mode) if metadata is not None else file_mode(path)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.new")
    try:
        stale = os.lstat(tmp)
    except FileNotFoundError:
        stale = None
    if stale is not None:
        # A privileged writer hands its temporary file to the metadata owner
        # before a crash can interrupt it.
        owners = {os.geteuid()} | ({metadata.info.st_uid} if metadata is not None else set())
        if not stat.S_ISREG(stale.st_mode) or stale.st_uid not in owners or stale.st_nlink != 1:
            raise Refused(f"temporary file must be an owned regular file with one link: {tmp}")
        os.unlink(tmp)

    faults.reach(f"{step}.write")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    created = os.fstat(fd)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            try:
                _restore_metadata(fd, metadata, mode)
            except (OSError, Refused) as error:
                raise Refused(f"cannot preserve access metadata for {path}: {error}") from error
            faults.reach(f"{step}.fsync")
            os.fsync(fd)
        finally:
            os.close(fd)
        if metadata is not None:
            try:
                current = read_metadata(metadata_path)
            except (OSError, Refused) as error:
                raise Refused(f"cannot recheck access metadata for {metadata_path}: {error}") from error
            if not _same_metadata(current, metadata):
                raise Refused(f"access metadata of {metadata_path} changed before {path} was replaced")
        faults.reach(f"{step}.rename")
        os.replace(tmp, path)
    except Exception:
        _discard(tmp, created)
        raise
    fsync_dir(parent)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def sha256_bytes(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()

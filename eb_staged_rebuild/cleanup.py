"""Remove an owned generation tree without following links or changing live data."""

import os
import stat

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.paths import abspath, is_within


def _open_owned_directory(parent, name, device):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if info.st_dev != device:
        raise Refused(f"cleanup refuses a different filesystem: {name}")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise Refused(f"cleanup requires an owned real directory: {name}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except PermissionError:
        os.chmod(name, stat.S_IMODE(info.st_mode) | 0o700,
                 dir_fd=parent, follow_symlinks=False)
        fd = os.open(name, flags, dir_fd=parent)
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino, current.st_uid) != (info.st_dev, info.st_ino, info.st_uid):
        os.close(fd)
        raise Refused(f"cleanup directory changed: {name}")
    os.fchmod(fd, stat.S_IMODE(current.st_mode) | 0o700)
    return fd


def _empty(fd, device):
    with os.scandir(fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _open_owned_directory(fd, name, device)
            try:
                if os.fstat(child).st_dev != device:
                    raise Refused(f"cleanup refuses a different filesystem: {name}")
                _empty(child, device)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def remove_generation_tree(path, boundary):
    path, boundary = abspath(path), abspath(boundary)
    if path == boundary or not is_within(path, boundary):
        raise Refused(f"cleanup path is outside its generation boundary: {path}")
    parent = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        name = os.path.basename(path)
        fd = _open_owned_directory(parent, name, os.fstat(parent).st_dev)
        try:
            _empty(fd, os.fstat(fd).st_dev)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)

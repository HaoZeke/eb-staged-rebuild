"""A before/after content record of a live install directory."""

import hashlib
import os

from eb_staged_rebuild.paths import lstat_kind

HASH_BLOCK_BYTES = 1024 * 1024


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(HASH_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def snapshot(path):
    path = os.fspath(path)
    kind = lstat_kind(path)
    if kind == "missing":
        return {"exists": False}
    if kind == "symlink":
        return {"exists": True, "symlink": True, "target": os.readlink(path)}
    if kind != "dir":
        return {"exists": True, "symlink": False, "kind": kind}
    st = os.lstat(path)
    rows = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        kept = []
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                lst = os.lstat(child)
                rows.append(
                    [
                        os.path.relpath(child, path),
                        "symlink",
                        lst.st_ino,
                        lst.st_size,
                        os.readlink(child),
                    ]
                )
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(dirpath, name)
            lst = os.lstat(child)
            kind_name = "symlink" if os.path.islink(child) else "file"
            detail = os.readlink(child) if kind_name == "symlink" else _sha256(child)
            rows.append(
                [
                    os.path.relpath(child, path),
                    kind_name,
                    lst.st_ino,
                    lst.st_size,
                    detail,
                ]
            )
    rows.sort()
    return {
        "exists": True,
        "symlink": False,
        "kind": "dir",
        "inode": st.st_ino,
        "device": st.st_dev,
        "entries": rows,
    }


def same_snapshot(before, after):
    return before == after


def empty_real_dir(path):
    if lstat_kind(path) != "dir":
        return False
    return not os.listdir(path)

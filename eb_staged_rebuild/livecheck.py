"""A before/after record of a live install directory.

The record is names, inode numbers, and sizes. It notices a directory being
removed or recreated. It does not hash file contents.
"""

import os

from eb_staged_rebuild.paths import lstat_kind


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
                rows.append([os.path.relpath(child, path), "symlink", lst.st_ino, lst.st_size])
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(dirpath, name)
            lst = os.lstat(child)
            kind_name = "symlink" if os.path.islink(child) else "file"
            rows.append([os.path.relpath(child, path), kind_name, lst.st_ino, lst.st_size])
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

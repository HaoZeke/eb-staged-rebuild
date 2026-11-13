"""The store file of the test artifact's ACL models (zig/aclmodel.zig).

One "MAJOR:MINOR:INO NAME HEX" line per emulated attribute. A test sets attributes
before a run, changes them while the run is paused, and reads them after it,
through these helpers.
"""

import os

GPFS = "system.gpfs_nfs4_acl"
NFS4 = "system.nfs4_acl"
POSIX = "system.posix_acl_access"

MODELS = {
    "gpfs-native": (GPFS, {}),
    "gpfs-views": (GPFS, {NFS4: b"generic:", POSIX: b"posix-projection:"}),
    "nfs4-generic": (NFS4, {}),
    "posix-access": (POSIX, {}),
}


def _key(path):
    info = os.stat(path)
    return f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}:{info.st_ino}"


def load(store):
    attrs = {}
    try:
        with open(store) as handle:
            for line in handle:
                parts = line.split()
                if len(parts) == 3:
                    attrs.setdefault(parts[0], {})[parts[1]] = bytes.fromhex(parts[2])
    except FileNotFoundError:
        pass
    return attrs


def save(store, attrs):
    tmp = store + ".tmp"
    with open(tmp, "w") as handle:
        for key, names in attrs.items():
            for name, value in names.items():
                handle.write(f"{key} {name} {value.hex()}\n")
    os.replace(tmp, store)


def touch(path):
    """An attribute change updates ctime, as on a real file system."""
    os.chmod(path, os.stat(path).st_mode & 0o7777)


def set_native(store, model, path, value):
    native, views = MODELS[model]
    attrs = load(store)
    entry = attrs.setdefault(_key(path), {})
    entry[native] = value
    for view, prefix in views.items():
        entry[view] = prefix + value
    save(store, attrs)
    touch(path)


def clear(store, path):
    """A new file has no attributes, whatever an earlier inode had."""
    attrs = load(store)
    attrs.pop(_key(path), None)
    save(store, attrs)


def get(store, path):
    return dict(load(store).get(_key(path), {}))

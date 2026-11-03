"""Enter a user/mount-namespace sandbox and make the live trees read-only."""

import argparse
import ctypes
import errno
import os
import sys
import tempfile


# Linux UAPI include/uapi/linux/mount.h.
MS_RDONLY = 1
MS_REMOUNT = 1 << 5
MS_BIND = 1 << 12
MS_REC = 1 << 14
MS_PRIVATE = 1 << 18
# GNU timeout and container runtimes conventionally reserve 125 for setup errors.
SANDBOX_SETUP_EXIT = 125


def _mount(source, target, flags):
    libc = ctypes.CDLL(None, use_errno=True)
    mount = libc.mount
    mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    mount.restype = ctypes.c_int
    ctypes.set_errno(0)
    source_bytes = os.fsencode(source) if source is not None else None
    if mount(source_bytes, os.fsencode(target), None, flags, None) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, f"mount {target}: {os.strerror(error)}")


def _make_mounts_private():
    _mount(None, "/", MS_REC | MS_PRIVATE)


def _protect(path):
    path = os.path.abspath(path)
    if not os.path.isdir(path) or os.path.islink(path):
        raise RuntimeError(f"read-only sandbox path is not a real directory: {path}")
    _mount(path, path, MS_BIND | MS_REC)
    _mount(path, path, MS_BIND | MS_REMOUNT | MS_RDONLY)


def _probe():
    # The mount disappears with this private namespace. Trying to remove its
    # mountpoint before namespace exit would correctly fail with EBUSY.
    root = tempfile.mkdtemp()
    _protect(root)
    try:
        with open(os.path.join(root, "must-fail"), "w", encoding="utf-8"):
            pass
    except OSError as error:
        if error.errno == errno.EROFS:
            return 0
        raise
    raise RuntimeError("read-only bind mount remained writable")


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="append", default=[])
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        _make_mounts_private()
        if args.probe:
            return _probe()
        for path in args.read_only:
            _protect(path)
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise RuntimeError("no sandbox command was supplied")
        os.execvpe(command[0], command, os.environ)
    except (OSError, RuntimeError) as error:
        print(f"eb-staged-rebuild user namespace: {error}", file=sys.stderr)
        return SANDBOX_SETUP_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Descriptor-level emulation of filesystem ACL models for fault tests.

Only xattr calls on open file descriptors are accepted; a path argument fails
the test. Attributes are stored per inode, so they follow ``os.replace``.
"""

from contextlib import ExitStack
import errno
import os
import stat
from unittest.mock import patch

GPFS = "system.gpfs_nfs4_acl"
NFS4 = "system.nfs4_acl"
POSIX = "system.posix_acl_access"

_REAL = {name: getattr(os, name) for name in
         ("listxattr", "getxattr", "setxattr", "removexattr", "fchmod", "open", "fstat")}

def fd_path(fd):
    return os.readlink(f"/proc/self/fd/{fd}")

class AclModel:
    """One filesystem's ACL interface.

    ``supported`` are the names getxattr/setxattr accept. ``views`` maps the
    native name to derived names and how each is derived from the native
    bytes. Writing a derived name directly replaces the native ACL with a
    translated one, which a correct publisher never does. ``chmod_rewrites``
    makes chmod regenerate the native ACL, as NFSv4 servers and GPFS do.
    """

    def __init__(self, label, native, supported, views=None, chmod_rewrites=True):
        self.label = label
        self.native = native
        self.supported = set(supported)
        self.views = dict(views or {})
        self.chmod_rewrites = chmod_rewrites
        self.store = {}
        self.calls = []
        self.hooks = {}
        self.inherit = {}

    def _key(self, fd):
        if not isinstance(fd, int):
            raise AssertionError(f"xattr access by path, not descriptor: {fd!r}")
        info = _REAL["fstat"](fd)
        return info.st_dev, info.st_ino

    def _attrs(self, fd):
        return self.store.setdefault(self._key(fd), {})

    def _hook(self, op, fd, name=None):
        self.calls.append((op, fd_path(fd), name))
        hook = self.hooks.get(op)
        if hook is not None:
            return hook(fd, name)
        return None

    def _write_native(self, attrs, value):
        attrs[self.native] = value
        for view, derive in self.views.items():
            attrs[view] = derive(value)

    @staticmethod
    def _touch(fd):
        """An attribute change updates ctime, as on a real filesystem."""
        _REAL["fchmod"](fd, stat.S_IMODE(_REAL["fstat"](fd).st_mode))

    def set_path(self, path, name, value):
        fd = _REAL["open"](path, os.O_RDONLY)
        try:
            attrs = self._attrs(fd)
            if name == self.native:
                self._write_native(attrs, value)
            else:
                attrs[name] = value
            self._touch(fd)
        finally:
            os.close(fd)

    def get_path(self, path):
        fd = _REAL["open"](path, os.O_RDONLY)
        try:
            return dict(self._attrs(fd))
        finally:
            os.close(fd)

    def listxattr(self, fd):
        hooked = self._hook("listxattr", fd)
        if hooked is not None:
            return hooked
        return list(self._attrs(fd))

    def getxattr(self, fd, name):
        hooked = self._hook("getxattr", fd, name)
        if hooked is not None:
            return hooked
        attrs = self._attrs(fd)
        if name in attrs:
            return attrs[name]
        if name not in self.supported:
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))

    def setxattr(self, fd, name, value, *args):
        self._hook("setxattr", fd, name)
        if name not in self.supported:
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        attrs = self._attrs(fd)
        if name == self.native:
            self._write_native(attrs, bytes(value))
        else:
            self._write_native(attrs, b"translated:" + bytes(value))
        self._touch(fd)

    def removexattr(self, fd, name):
        self._hook("removexattr", fd, name)
        attrs = self._attrs(fd)
        if name not in self.supported:
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))
        if name not in attrs:
            raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))
        if name == self.native:
            for view in self.views:
                attrs.pop(view, None)
        del attrs[name]
        self._touch(fd)

    def fchmod(self, fd, mode):
        self._hook("fchmod", fd)
        _REAL["fchmod"](fd, mode)
        attrs = self._attrs(fd)
        if self.chmod_rewrites and self.native in attrs:
            self._write_native(attrs, b"chmod-regenerated:%o" % mode)

    def open(self, path, flags, *args, **kwargs):
        fd = _REAL["open"](path, flags, *args, **kwargs)
        if flags & os.O_CREAT and flags & os.O_EXCL:
            attrs = {}
            self.store[self._key(fd)] = attrs
            if self.native in self.inherit:
                self._write_native(attrs, self.inherit[self.native])
        return fd

    def installed(self):
        stack = ExitStack()
        for name in ("listxattr", "getxattr", "setxattr", "removexattr", "fchmod", "open"):
            stack.enter_context(patch.object(os, name, getattr(self, name)))
        return stack

    def ops(self, op):
        return [(path, name) for kind, path, name in self.calls if kind == op]

def models():
    """One emulation per ACL model the publisher recognises."""
    return [
        AclModel("gpfs-native", GPFS, {GPFS}),
        AclModel("gpfs-native-with-generic-and-posix-views", GPFS, {GPFS, NFS4, POSIX},
                 views={NFS4: lambda value: b"generic:" + value,
                        POSIX: lambda value: b"posix-projection:" + value}),
        AclModel("nfs4-generic", NFS4, {NFS4}),
        AclModel("posix-access", POSIX, {POSIX}, chmod_rewrites=False),
    ]

OPAQUE = b"\x00\x01native-acl\xff\xfe\x00" + bytes(range(256))

def raise_errno(code):
    def hook(fd, name=None):
        raise OSError(code, os.strerror(code))

    return hook

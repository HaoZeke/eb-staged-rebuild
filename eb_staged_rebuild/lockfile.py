"""Serialize a state root with a local gate and a POSIX record lock.

The flock gate excludes threads and processes sharing a kernel. The distinct
POSIX sidecar excludes nodes when the filesystem provides coherent record
locks. Open the sidecar only while holding the gate: closing any descriptor
for its inode drops this process's record locks. Other code in this process
must not open that sidecar. Release and close it before releasing the gate.

Never unlink either lock file while the state root is in use. Every node must
use this protocol; flock-only holders on other nodes do not participate.
The filesystem controls recovery after node failure. Fork children must close
inherited descriptors without unlocking the parent's open file description.
"""

import errno
import fcntl
import os
import stat
import time

from eb_staged_rebuild.errors import Refused

LOCK_RETRY_INTERVAL_SECONDS = 0.02


def _take(lock_fn, fd, deadline):
    if deadline is None:
        lock_fn(fd, fcntl.LOCK_EX)
        return
    while True:
        try:
            lock_fn(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError from exc
            time.sleep(min(LOCK_RETRY_INTERVAL_SECONDS, remaining))


class FileLock:
    """One instance per acquirer; do not share an instance between threads."""

    def __init__(self, state_root):
        self.state_root = os.fspath(state_root)
        self.path = os.path.join(self.state_root, ".lock")
        self.posix_path = self.path + ".posix"
        self.fd = None
        self.posix_fd = None
        self._owner = None

    def acquire(self, timeout=None):
        if self.fd is not None:
            raise RuntimeError(f"lock at {self.path} is already held by this object")
        os.makedirs(self.state_root, exist_ok=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        gate = posix = None
        try:
            gate = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            _take(fcntl.flock, gate, deadline)
            posix = os.open(
                self.posix_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644
            )
            info = os.fstat(posix)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused(f"record lock requires a private regular file: {self.posix_path}")
            _take(fcntl.lockf, posix, deadline)
            os.ftruncate(gate, 0)
            os.write(gate, f"{os.getpid()}\n".encode())
        except BaseException as exc:
            self._drop(gate, posix, unlock=False)
            if isinstance(exc, TimeoutError):
                raise Refused(
                    f"another eb-staged-rebuild holds the lock at {self.path}"
                ) from exc
            raise
        self.fd, self.posix_fd, self._owner = gate, posix, os.getpid()

    def release(self):
        gate, posix = self.fd, self.posix_fd
        if gate is None:
            return
        inherited = self._owner != os.getpid()
        self.fd = self.posix_fd = self._owner = None
        self._drop(gate, posix, unlock=not inherited)

    @staticmethod
    def _drop(gate, posix, unlock):
        try:
            if posix is not None:
                try:
                    if unlock:
                        fcntl.lockf(posix, fcntl.LOCK_UN)
                finally:
                    os.close(posix)
        finally:
            if gate is not None:
                try:
                    if unlock:
                        fcntl.flock(gate, fcntl.LOCK_UN)
                finally:
                    os.close(gate)

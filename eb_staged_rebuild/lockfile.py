"""One advisory flock for a state root.

``flock`` is held on an open file descriptor and is released when that
descriptor is closed, including when the process is killed. A lock file that
still exists after a crash is not a held lock. The pid written into the file
is a hint for operators and is not checked.

Cross-node behaviour is not established by this tool. On a local Linux
filesystem the kernel serialises ``flock`` holders on one machine. On NFS,
locks are advisory and depend on the protocol and the lock manager. GPFS
documents POSIX ``fcntl`` locking, which is not the same call, and this
repository has not measured two clients locking one GPFS file. ``open-004``
(flock from two build nodes at once) stays open. Two runs on one node are
serialised. Two nodes are not something this lock can promise.
"""

import errno
import fcntl
import os
import time

from eb_staged_rebuild.errors import Refused


class FileLock:
    def __init__(self, state_root):
        self.state_root = os.fspath(state_root)
        self.path = os.path.join(self.state_root, ".lock")
        self.fd = None

    def acquire(self, timeout=None):
        os.makedirs(self.state_root, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if timeout is None:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        os.close(self.fd)
                        self.fd = None
                        raise Refused(
                            f"another eb-staged-rebuild holds the lock at {self.path}"
                        ) from exc
                    time.sleep(0.02)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()}\n".encode())

    def release(self):
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

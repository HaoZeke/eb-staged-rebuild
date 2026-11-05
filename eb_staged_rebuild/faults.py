import os
import time


class InjectedCrash(BaseException):
    """Test-only crash. A BaseException so ``except Exception`` does not swallow it."""

    def __init__(self, step):
        super().__init__(step)
        self.step = step


class FaultInjector:
    """Record each named step and, when asked, raise before that step runs."""

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.reached = []

    def reach(self, step):
        self.reached.append(step)
        if self.fail_at is not None and step == self.fail_at:
            raise InjectedCrash(step)
        # A real SIGKILL cannot be injected in-process. Tests set this to
        # pause after the previous step has happened and before this one.
        token = os.environ.get("EB_STAGED_REBUILD_PAUSE_AT")
        if token and token == step:
            ready = os.environ.get("EB_STAGED_REBUILD_PAUSE_READY")
            if ready:
                fd = os.open(ready, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
                os.close(fd)
            time.sleep(float(os.environ.get("EB_STAGED_REBUILD_PAUSE_SECONDS", "30")))

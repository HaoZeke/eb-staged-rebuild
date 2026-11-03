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

import errno
import os
import tempfile
import unittest

from eb_staged_rebuild.faults import FaultInjector, InjectedCrash
from eb_staged_rebuild.swap import (
    FALLBACK_HOLD_SUFFIX,
    recover_interrupted_swap,
    swap_directories,
)


def _payload(path):
    with open(os.path.join(path, "PAYLOAD"), encoding="utf-8") as handle:
        return handle.read()


def _pair(root):
    live = os.path.join(root, "live")
    staged = os.path.join(root, ".live.staged")
    os.makedirs(live)
    os.makedirs(staged)
    with open(os.path.join(live, "PAYLOAD"), "w", encoding="utf-8") as handle:
        handle.write("old\n")
    with open(os.path.join(staged, "PAYLOAD"), "w", encoding="utf-8") as handle:
        handle.write("new\n")
    return live, staged


def _portable_exchange(first, second):
    temporary = second + ".test-exchange"
    os.rename(first, temporary)
    os.rename(second, first)
    os.rename(temporary, second)


class SwapTests(unittest.TestCase):
    def assert_original_pair(self, live, staged):
        self.assertEqual(_payload(live), "old\n")
        self.assertEqual(_payload(staged), "new\n")
        self.assertFalse(os.path.lexists(staged + FALLBACK_HOLD_SUFFIX))

    def test_exchange_swaps_the_complete_directories(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)
            result = swap_directories(
                live,
                staged,
                exchange_call=_portable_exchange,
            )
            self.assertEqual(result.method, "renameat2-exchange")
            self.assertEqual(_payload(live), "new\n")
            self.assertEqual(_payload(staged), "old\n")

    def test_exchange_failure_after_success_restores_live(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)
            with self.assertRaises(InjectedCrash):
                swap_directories(
                    live,
                    staged,
                    faults=FaultInjector("swap.exchange.after"),
                    exchange_call=_portable_exchange,
                )
            self.assert_original_pair(live, staged)

    def test_non_fallback_exchange_error_leaves_live_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)

            def denied(_first, _second):
                raise OSError(errno.EPERM, "denied")

            with self.assertRaises(OSError):
                swap_directories(live, staged, exchange_call=denied)
            self.assert_original_pair(live, staged)

    def test_unsupported_exchange_uses_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)

            def unsupported(_first, _second):
                raise OSError(errno.EOPNOTSUPP, "unsupported")

            result = swap_directories(live, staged, exchange_call=unsupported)
            self.assertEqual(result.method, "rename-fallback")
            self.assertEqual(_payload(live), "new\n")
            self.assertEqual(_payload(staged), "old\n")

    def test_every_injected_fallback_failure_restores_live(self):
        steps = (
            "swap.fallback.live-to-hold",
            "swap.fallback.staged-to-live",
            "swap.fallback.hold-to-staged",
        )
        for step in steps:
            with self.subTest(step=step):
                with tempfile.TemporaryDirectory() as root:
                    live, staged = _pair(root)
                    with self.assertRaises(InjectedCrash):
                        swap_directories(
                            live,
                            staged,
                            allow_exchange=False,
                            faults=FaultInjector(step),
                        )
                    self.assert_original_pair(live, staged)

    def test_every_rename_error_in_fallback_restores_live(self):
        for failing_call in (1, 2, 3):
            with self.subTest(failing_call=failing_call):
                with tempfile.TemporaryDirectory() as root:
                    live, staged = _pair(root)
                    calls = 0

                    def rename(first, second):
                        nonlocal calls
                        calls += 1
                        if calls == failing_call:
                            raise OSError(errno.EIO, "injected rename error")
                        os.rename(first, second)

                    with self.assertRaises(OSError):
                        swap_directories(
                            live,
                            staged,
                            allow_exchange=False,
                            rename=rename,
                        )
                    self.assert_original_pair(live, staged)

    def test_recover_after_first_fallback_rename(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)
            hold = staged + FALLBACK_HOLD_SUFFIX
            os.rename(live, hold)
            recover_interrupted_swap(live, staged)
            self.assert_original_pair(live, staged)

    def test_recover_after_second_fallback_rename(self):
        with tempfile.TemporaryDirectory() as root:
            live, staged = _pair(root)
            hold = staged + FALLBACK_HOLD_SUFFIX
            os.rename(live, hold)
            os.rename(staged, live)
            recover_interrupted_swap(live, staged)
            self.assert_original_pair(live, staged)


if __name__ == "__main__":
    unittest.main()

"""tests/test_easybuild_integration.py against the program named by TOOL_BIN.

The same real-EasyBuild rebuilds, with the publication done by
``TOOL_BIN run`` instead of the Python engine. An exit status of 2 is the
tool's refusal and is raised as ``Refused`` so the original assertions
apply unchanged. Skipped unless EB_STAGED_REBUILD_INTEGRATION=1 and
TOOL_BIN are set.

    EB_STAGED_REBUILD_INTEGRATION=1 TOOL_BIN=zig-out/bin/eb-staged-rebuild \\
        python3 -m unittest -v tests.test_easybuild_integration_cli
"""

import os
import subprocess
import unittest
from types import SimpleNamespace

from eb_staged_rebuild.errors import Refused
from tests import test_easybuild_integration as base


@unittest.skipUnless(os.environ.get("TOOL_BIN"), "set TOOL_BIN to the program to test")
class CliEasyBuildIntegrationTests(base.EasyBuildIntegrationTests):
    def _rebuild(self, prefix, ec, src, backend):
        done = subprocess.run(
            [
                os.environ["TOOL_BIN"], "run",
                "--software", os.path.join(prefix, "software"),
                "--modules", os.path.join(prefix, "modules"),
                "--state-root", os.path.join(prefix, ".eb-staged-rebuild"),
                "--backend", backend,
                "--eb", "eb",
                "--module", "stagedemo/1",
                "--", ec, f"--sourcepath={src}", "--module-syntax=Lua",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        message = (done.stdout + done.stderr).strip()
        if done.returncode == 2:
            raise Refused(message)
        return SimpleNamespace(returncode=done.returncode, message=message)


if __name__ == "__main__":
    unittest.main()

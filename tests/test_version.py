"""The version the CLI prints is the version the package is built as."""
import pathlib
import re
import unittest

import eb_staged_rebuild

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Version(unittest.TestCase):
    def test_module_version_matches_pyproject(self):
        text = (ROOT / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
        self.assertEqual(eb_staged_rebuild.__version__, declared)


if __name__ == "__main__":
    unittest.main()

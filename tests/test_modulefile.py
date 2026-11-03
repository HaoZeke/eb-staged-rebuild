import unittest

from eb_staged_rebuild.modulefile import parse_root, replace_path, rewrite_root
from eb_staged_rebuild.errors import Refused


SAMPLE = """\
help([==[
Description
]==])

local root = "/opt/software/stagedemo/1"

conflict("stagedemo")

prepend_path("PATH", root)
setenv("EBROOTSTAGEDEMO", root)
setenv("EBVERSIONSTAGEDEMO", "1")
"""


class ModuleFileTests(unittest.TestCase):
    def test_parse_lua_and_tcl(self):
        self.assertEqual(parse_root(SAMPLE)[1], "/opt/software/stagedemo/1")
        self.assertEqual(parse_root('set root /opt/software/stagedemo/1\n')[0], "tcl")

    def test_rewrite_does_not_eat_a_longer_prefix(self):
        text = SAMPLE.replace(
            'setenv("EBROOTSTAGEDEMO", root)',
            'setenv("EBROOTSTAGEDEMO", "/opt/software/stagedemo/1")',
        )
        text += 'setenv("OTHER", "/opt/software/stagedemo/10")\n'
        updated = rewrite_root(text, "/gens/run/software/stagedemo/1")
        self.assertIn('local root = "/gens/run/software/stagedemo/1"', updated)
        self.assertIn('setenv("EBROOTSTAGEDEMO", "/gens/run/software/stagedemo/1")', updated)
        self.assertIn('"/opt/software/stagedemo/10"', updated)
        self.assertNotIn('"/opt/software/stagedemo/1"', updated)
        self.assertEqual(parse_root(updated)[1], "/gens/run/software/stagedemo/1")

    def test_new_path_that_contains_the_old_path_is_not_doubled(self):
        updated = replace_path(
            'local root = "/opt/software/demo/1"\n',
            "/opt/software/demo/1",
            "/opt/software/demo/1/gen",
        )
        self.assertEqual(updated, 'local root = "/opt/software/demo/1/gen"\n')

    def test_refuses_a_file_with_no_root(self):
        with self.assertRaises(Refused):
            rewrite_root("help([[no root]])\n", "/gens/demo/1")


if __name__ == "__main__":
    unittest.main()

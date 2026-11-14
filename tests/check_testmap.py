"""Fail when a test that imports the Python code has no row in TESTMAP.md.

Port plan D4: every test in a module that imports ``eb_staged_rebuild`` maps
to what covers it for the Zig build. The test names come from unittest's own
loader, so inherited and reassigned test methods count. A row is
``module.Class.test``, or ``Class.test`` under a ``tests/module.py`` heading.
A module that is only a replacement (cited in a replacement column, with no
section of its own) needs no rows. A row that names no such test, or whose
replacement is "not yet", fails the check.

    python3 tests/check_testmap.py
"""

import ast
import importlib
import inspect
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADING = re.compile(r"^#{2,3} +`?tests/(test_\w+)\.py`?")
ROW = re.compile(r"^\| *([A-Za-z_][\w.]*) *\| *(.*?) *\|$")
CITED = re.compile(r"tests/(test_\w+)\.py")


def read_map(path):
    rows, not_yet, sections, cited = set(), [], set(), set()
    module = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith("#"):
                found = HEADING.match(line)
                module = found.group(1) if found else None
                if module:
                    sections.add(module)
                continue
            found = ROW.match(line)
            if not found or found.group(1) == "test":
                continue
            name, replacement = found.groups()
            cited.update(CITED.findall(replacement))
            parts = name.split(".")
            if len(parts) == 2 and module:
                name = f"{module}.{name}"
            elif len(parts) != 3:
                continue
            rows.add(name)
            if replacement.strip().lower().startswith("not yet"):
                not_yet.append(name)
    return rows, not_yet, sections, cited


def imports_python_code(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "eb_staged_rebuild":
            return True
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "eb_staged_rebuild" for a in node.names):
            return True
    return False


def required(sections, cited):
    loader = unittest.TestLoader()
    names = set()
    for entry in sorted(os.listdir(os.path.join(REPO, "tests"))):
        if not (entry.startswith("test_") and entry.endswith(".py")):
            continue
        stem = entry[:-3]
        if not imports_python_code(os.path.join(REPO, "tests", entry)):
            continue
        if stem in cited and stem not in sections:
            continue
        module = importlib.import_module(f"tests.{stem}")
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, unittest.TestCase):
                continue
            for method in loader.getTestCaseNames(cls):
                names.add(f"{stem}.{cls.__name__}.{method}")
    return names


def main():
    sys.path.insert(0, REPO)
    rows, not_yet, sections, cited = read_map(os.path.join(REPO, "TESTMAP.md"))
    needed = required(sections, cited)
    missing = sorted(needed - rows)
    stale = sorted(rows - needed)
    for name in missing:
        print(f"no TESTMAP.md row: {name}")
    for name in stale:
        print(f"row names no such test: {name}")
    for name in not_yet:
        print(f"no replacement yet: {name}")
    print(f"{len(rows)} rows, {len(missing)} missing, {len(stale)} stale, {len(not_yet)} not yet")
    return 1 if missing or stale or not_yet else 0


if __name__ == "__main__":
    sys.exit(main())

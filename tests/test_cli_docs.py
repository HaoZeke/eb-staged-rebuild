"""Diff argparse options in cli.py against README and the runbook.

Run as a script: missing names are printed, and a clean tree prints nothing.
"""

import os
import re
import unittest

from eb_staged_rebuild.cli import _parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", os.path.join("docs", "runbook.md"))


def argparse_names():
    """Long options, subcommands, and choice values exposed by the parser."""
    names = []
    parser = _parser()
    for action in parser._actions:
        if action.option_strings:
            names.extend(opt for opt in action.option_strings if opt.startswith("--"))
        sub = getattr(action, "choices", None)
        if not isinstance(sub, dict):
            continue
        for command, subparser in sub.items():
            names.append(command)
            for sub_action in subparser._actions:
                names.extend(opt for opt in sub_action.option_strings if opt.startswith("--"))
                if sub_action.choices:
                    names.extend(str(choice) for choice in sub_action.choices)
    with open(os.path.join(ROOT, "eb_staged_rebuild", "cli.py"), encoding="utf-8") as handle:
        source = handle.read()
    names.extend(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
    return names


def missing_from_docs():
    text = []
    for relative in DOCS:
        with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            text.append(handle.read())
    combined = "\n".join(text)
    missing = []
    seen = set()
    for name in argparse_names():
        if name in seen or name in combined:
            seen.add(name)
            continue
        seen.add(name)
        missing.append(name)
    return missing


def main():
    missing = missing_from_docs()
    for name in missing:
        print(name)
    return 1 if missing else 0


class CliDocTests(unittest.TestCase):
    def test_readme_and_runbook_name_every_cli_option(self):
        self.assertEqual(missing_from_docs(), [])


if __name__ == "__main__":
    raise SystemExit(main())

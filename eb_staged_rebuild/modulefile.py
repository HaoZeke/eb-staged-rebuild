"""Read and rewrite the install-directory assignment in an EasyBuild module file.

EasyBuild 5.4 writes Lua as ``local root = "<installdir>"`` and Tcl as
``set root <installdir>`` (no quotes). Both expressions are matched the way
``easybuild/tools/module_generator.py`` matches them.
"""

import os
import re

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.paths import abspath

LUA_ROOT = re.compile(r'(?m)^local root\s*=\s*"((?:\\.|[^"\\])*)"\s*$')
TCL_ROOT = re.compile(r'(?m)^set root\s+(?:"([^"]*)"|(\S+))\s*$')
LOAD_STMT = re.compile(r'(?m)^\s*(?:load|depends_on)\("([^"]+)"\)')


def parse_root(text):
    """Return ``("lua"|"tcl", path)`` or ``(None, None)``."""
    lua = LUA_ROOT.search(text)
    tcl = TCL_ROOT.search(text)
    if lua and tcl:
        if lua.start() < tcl.start():
            return "lua", lua.group(1)
        return "tcl", tcl.group(1) or tcl.group(2)
    if lua:
        return "lua", lua.group(1)
    if tcl:
        return "tcl", tcl.group(1) or tcl.group(2)
    return None, None


def loads(text):
    return LOAD_STMT.findall(text)


def replace_path(text, old, new):
    """Replace ``old`` as a path token with ``new``.

    Existing occurrences of ``new`` are left in place, so a new path that
    contains the old path as a prefix is not rewritten twice.
    """
    if old == new:
        return text
    if not old:
        raise Refused("refusing to rewrite an empty root")
    if "\n" in new or '"' in new or "\x00" in new:
        raise Refused("refusing to write a root that contains a quote, newline, or NUL")
    token = "\x00KEEP\x00"
    protected = text.replace(new, token)
    pattern = re.compile(re.escape(old) + r"(?![A-Za-z0-9._+\-])")
    replaced, count = pattern.subn(lambda _match: new, protected)
    if count < 1:
        raise Refused(f"module file does not contain the install path {old}")
    return replaced.replace(token, new)


def rewrite_root(text, new_root):
    """Point the module file at ``new_root`` and return the new text."""
    kind, old = parse_root(text)
    if kind is None or not old:
        raise Refused("module file has no root assignment")
    new_root = abspath(new_root)
    updated = replace_path(text, old, new_root)
    kind2, got = parse_root(updated)
    if kind2 != kind or abspath(got) != new_root:
        raise Refused(
            f"rewritten module file root is {got!r}, expected {new_root}"
        )
    return updated


def module_identity(modules_root, module_path):
    """``name/version`` from the last two path components of a module file."""
    relative = os.path.relpath(module_path, modules_root)
    parts = relative.split(os.sep)
    if len(parts) < 2:
        return os.path.splitext(parts[-1])[0]
    stem = os.path.splitext(parts[-1])[0]
    return os.path.join(parts[-2], stem)


def generation_complete(path):
    """A generation is complete when it is a real directory with install metadata.

    Accepted markers, any one of them:

    * ``PAYLOAD`` (used by the fault-injection tests)
    * ``easybuild/*.eb`` (EasyBuild copies the easyconfig in at the end of a build)
    * ``.eb-staged-rebuild-complete`` (written by this tool before it publishes)
    """
    from eb_staged_rebuild.paths import is_real_dir, lstat_kind

    if not is_real_dir(path):
        return False
    payload = os.path.join(path, "PAYLOAD")
    if lstat_kind(payload) == "file":
        return True
    stamp = os.path.join(path, ".eb-staged-rebuild-complete")
    if lstat_kind(stamp) == "file":
        return True
    meta = os.path.join(path, "easybuild")
    if is_real_dir(meta):
        for name in os.listdir(meta):
            if name.endswith(".eb") and lstat_kind(os.path.join(meta, name)) == "file":
                return True
    return False

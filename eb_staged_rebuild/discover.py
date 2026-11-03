"""Find module files EasyBuild wrote and decide which install directory they name."""

import os

from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.modulefile import loads, module_identity, parse_root, rewrite_root
from eb_staged_rebuild.paths import abspath, is_real_dir, is_within, lstat_kind


def walk_module_tree(modules_root):
    """Return ``(files, links)``.

    ``files`` is a list of regular ``.lua`` and ``.tcl`` files. ``links`` is a
    list of ``(relative posix path, link text)``. Symlinked directories are not
    descended into.
    """
    modules_root = os.fspath(modules_root)
    files = []
    links = []
    if lstat_kind(modules_root) != "dir":
        raise Refused(f"module tree is not a real directory: {modules_root}")
    for dirpath, dirnames, filenames in os.walk(modules_root, followlinks=False):
        kept = []
        for name in list(dirnames):
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                rel = os.path.relpath(child, modules_root).replace(os.sep, "/")
                links.append((rel, os.readlink(child)))
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            child = os.path.join(dirpath, name)
            rel = os.path.relpath(child, modules_root).replace(os.sep, "/")
            if os.path.islink(child):
                links.append((rel, os.readlink(child)))
                continue
            if os.path.isfile(child) and name.endswith((".lua", ".tcl")):
                files.append(child)
    files.sort()
    links.sort()
    return files, links


def generation_for(text, stage_software, live_software):
    """Return ``(generation_dir, module_text)``.

    Install-path builds already name the stage directory. Bubblewrap builds
    name the live directory, because that is the path inside the bind mount;
    the text is rewritten to the staged directory, which is where the files are.
    """
    kind, root = parse_root(text)
    if kind is None or not root:
        raise Refused("module file has no root assignment")
    root_abs = abspath(root)
    stage_abs = abspath(stage_software)
    live_abs = abspath(live_software)
    if is_within(root_abs, stage_abs):
        if not is_real_dir(root_abs):
            raise Refused(f"generation {root_abs} is not a real directory")
        return root_abs, text if root == root_abs else rewrite_root(text, root_abs)
    if is_within(root_abs, live_abs):
        relative = os.path.relpath(root_abs, live_abs)
        generation = os.path.join(stage_abs, relative)
        if not is_real_dir(generation):
            raise Refused(
                f"staged generation {generation} is not a real directory "
                f"(module root was {root_abs})"
            )
        return generation, rewrite_root(text, generation)
    raise Refused(
        f"module root {root_abs} is outside both the stage software tree "
        f"{stage_abs} and the live software tree {live_abs}"
    )


def order_module_files(files, modules_root):
    """Publish dependencies first. A cycle falls back to path order for the rest."""
    texts = {}
    for path in files:
        with open(path, encoding="utf-8") as handle:
            texts[path] = handle.read()
    identity = {path: module_identity(modules_root, path).replace(os.sep, "/") for path in files}
    by_identity = {}
    for path, name in identity.items():
        by_identity.setdefault(name, path)
    dependencies = {path: [] for path in files}
    for path, text in texts.items():
        for name in loads(text):
            earlier = by_identity.get(name)
            if earlier is not None and earlier != path:
                dependencies[path].append(earlier)
    incoming = {path: 0 for path in files}
    children = {path: [] for path in files}
    for path, deps in dependencies.items():
        for dep in deps:
            incoming[path] += 1
            children[dep].append(path)
    ready = sorted((path for path in files if incoming[path] == 0), key=lambda item: item)
    ordered = []
    seen = set()
    while ready:
        path = ready.pop(0)
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
        for child in children[path]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
        ready.sort()
    ordered.extend(path for path in files if path not in seen)
    return ordered, texts


def map_link_target(target, stage_modules, live_modules):
    """Point a staged class symlink at the same relative path in the live tree."""
    if not os.path.isabs(target):
        return target
    target_abs = abspath(target)
    stage_abs = abspath(stage_modules)
    if is_within(target_abs, stage_abs):
        relative = os.path.relpath(target_abs, stage_abs)
        return os.path.join(abspath(live_modules), relative)
    return target_abs

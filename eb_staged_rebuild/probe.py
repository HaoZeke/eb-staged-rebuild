"""Capability probes. Results describe this machine and this directory, nothing else."""

import ctypes
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile

# Linux UAPI include/uapi/linux/fs.h and include/uapi/linux/fcntl.h.
RENAME_EXCHANGE = 1 << 1
AT_FDCWD = -100
VERSION_PROBE_TIMEOUT_SECONDS = 30
CAPABILITY_PROBE_TIMEOUT_SECONDS = 60
USER_NAMESPACE_FLAGS = (
    "--user",
    "--map-root-user",
    "--mount",
    "--fork",
    "--kill-child",
)

# Keys read from easybuild/tools/bwrap.py at these tags. A version that is not
# in this table is reported as unknown; the JSON written by a build is what a
# later step trusts, and a missing stage path is not guessed.
BWRAP_STAGE_KEYS_READ_FROM_SOURCE = {
    (5, 3, 0): False,
    (5, 3, 1): False,
    (5, 4, 0): True,
    (5, 4, 2): True,
}
LEGACY_BWRAP_LAYOUT = {(5, 3, 0), (5, 3, 1)}


def _run(cmd, timeout, env=None):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return exc


def probe_bwrap():
    path = shutil.which("bwrap")
    if not path:
        return {
            "installed": False,
            "usable": False,
            "path": None,
            "version": None,
            "reason": "bwrap is not installed",
        }
    version = _run([path, "--version"], VERSION_PROBE_TIMEOUT_SECONDS)
    version_text = ""
    if isinstance(version, subprocess.CompletedProcess) and version.returncode == 0:
        version_text = (version.stdout or version.stderr or "").strip()
    probe = _run(
        [path, "--dev-bind", "/", "/", "true"],
        CAPABILITY_PROBE_TIMEOUT_SECONDS,
    )
    if isinstance(probe, subprocess.CompletedProcess) and probe.returncode == 0:
        return {
            "installed": True,
            "usable": True,
            "path": path,
            "version": version_text or None,
            "reason": None,
        }
    if isinstance(probe, subprocess.CompletedProcess):
        err = (probe.stderr or probe.stdout or "").strip()
        reason = f"bwrap exited {probe.returncode}"
        if err:
            reason = f"{reason}: {err}"
    else:
        reason = f"bwrap probe failed: {probe}"
    return {
        "installed": True,
        "usable": False,
        "path": path,
        "version": version_text or None,
        "reason": reason,
    }


def probe_user_namespaces():
    path = "/proc/sys/user/max_user_namespaces"
    clone_path = "/proc/sys/kernel/unprivileged_userns_clone"
    out = {"max_user_namespaces": None, "unprivileged_userns_clone": None, "readable": False}
    try:
        with open(path, encoding="utf-8") as handle:
            out["max_user_namespaces"] = int(handle.read().strip())
            out["readable"] = True
    except (OSError, ValueError) as exc:
        out["error"] = f"{path}: {exc}"
    try:
        with open(clone_path, encoding="utf-8") as handle:
            out["unprivileged_userns_clone"] = int(handle.read().strip())
    except (OSError, ValueError):
        out["unprivileged_userns_clone"] = None
    return out


def probe_userns_sandbox():
    """Test a user and mount namespace with a verified read-only bind mount."""
    path = shutil.which("unshare")
    result = {
        "installed": bool(path),
        "usable": False,
        "path": path,
        "reason": None,
    }
    if not path:
        result["reason"] = "unshare is not installed"
        return result
    command = [
        path,
        *USER_NAMESPACE_FLAGS,
        sys.executable,
        "-m",
        "eb_staged_rebuild.userns_sandbox",
        "--probe",
    ]
    probe = _run(command, CAPABILITY_PROBE_TIMEOUT_SECONDS, env=os.environ.copy())
    if isinstance(probe, subprocess.CompletedProcess) and probe.returncode == 0:
        result["usable"] = True
        return result
    if isinstance(probe, subprocess.CompletedProcess):
        detail = (probe.stderr or probe.stdout or "").strip()
        result["reason"] = f"user-namespace probe exited {probe.returncode}"
        if detail:
            result["reason"] += f": {detail}"
    else:
        result["reason"] = f"user-namespace probe failed: {probe}"
    return result


def filesystem(path):
    """Longest mount in ``/proc/mounts`` covering ``path``. This host only."""
    target = os.path.realpath(path if os.path.isdir(path) else os.path.dirname(path) or ".")
    best = None
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return {"error": str(exc), "path": target}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        mount = mount.replace("\\040", " ")
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            if best is None or len(mount) > len(best["mount"]):
                best = {"device": device, "mount": mount, "fstype": fstype, "path": target}
    return best or {"path": target, "fstype": None}


def probe_rename_exchange(directory):
    """Try ``renameat2(RENAME_EXCHANGE)`` on two temporary directories.

    The result applies to the filesystem that holds ``directory``. It is not a
    measurement of any other filesystem.
    """
    directory = os.fspath(directory)
    os.makedirs(directory, exist_ok=True)
    info = filesystem(directory)
    info.update({"ok": False, "errno": None, "error": None, "flag": RENAME_EXCHANGE})
    parent = tempfile.mkdtemp(prefix=".rename-exchange-", dir=directory)
    left = os.path.join(parent, "left")
    right = os.path.join(parent, "right")
    try:
        os.mkdir(left)
        os.mkdir(right)
        with open(os.path.join(left, "mark"), "w", encoding="utf-8") as handle:
            handle.write("L")
        with open(os.path.join(right, "mark"), "w", encoding="utf-8") as handle:
            handle.write("R")
        libc = ctypes.CDLL(None, use_errno=True)
        if not hasattr(libc, "renameat2"):
            info["errno"] = errno.ENOSYS
            info["error"] = "renameat2 is not in libc"
            return info
        func = libc.renameat2
        func.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        func.restype = ctypes.c_int
        rc = func(AT_FDCWD, left.encode(), AT_FDCWD, right.encode(), RENAME_EXCHANGE)
        if rc == 0:
            with open(os.path.join(left, "mark"), encoding="utf-8") as handle:
                swapped = handle.read() == "R"
            info["ok"] = bool(swapped)
            if not swapped:
                info["error"] = "renameat2 returned 0 but the directories did not exchange"
            return info
        err = ctypes.get_errno()
        info["errno"] = err
        info["error"] = os.strerror(err) if err else f"renameat2 returned {rc}"
        return info
    except OSError as exc:
        info["errno"] = exc.errno
        info["error"] = str(exc)
        return info
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def parse_framework_version(text):
    import re

    match = re.search(r"framework:\s*(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        match = re.search(r"This is EasyBuild\s+(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _import_bwrap_keys(eb_path):
    python = os.path.join(os.path.dirname(os.path.realpath(eb_path)), "python3")
    if not os.path.isfile(python):
        python = os.path.join(os.path.dirname(os.path.realpath(eb_path)), "python")
    if not os.path.isfile(python):
        return None
    code = (
        "import json\n"
        "import easybuild.tools.bwrap as b\n"
        "print(json.dumps(sorted(b._bwrap_info)))\n"
    )
    proc = _run([python, "-c", code], CAPABILITY_PROBE_TIMEOUT_SECONDS)
    if not isinstance(proc, subprocess.CompletedProcess) or proc.returncode != 0:
        return None
    try:
        keys = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(keys, list) and all(isinstance(item, str) for item in keys):
        return keys
    return None


def probe_easybuild(eb="eb"):
    path = shutil.which(eb) if os.sep not in eb else (eb if os.path.isfile(eb) else None)
    result = {
        "eb": eb,
        "path": path,
        "found": bool(path),
        "version_text": None,
        "framework_version": None,
        "bwrap_info_keys": None,
        "keys_source": None,
        "stage_path_keys": None,
        "legacy_layout_versions": ["5.3.0", "5.3.1"],
        "reason": None,
    }
    if not path:
        result["reason"] = f"{eb} was not found on PATH"
        return result
    proc = _run([path, "--version"], CAPABILITY_PROBE_TIMEOUT_SECONDS)
    if not isinstance(proc, subprocess.CompletedProcess):
        result["reason"] = str(proc)
        return result
    text = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    result["version_text"] = text.strip() or None
    if proc.returncode != 0:
        result["reason"] = f"eb --version exited {proc.returncode}"
        return result
    version = parse_framework_version(text)
    result["framework_version"] = list(version) if version else None
    imported = _import_bwrap_keys(path)
    if imported is not None:
        result["bwrap_info_keys"] = imported
        result["keys_source"] = "imported"
        result["stage_path_keys"] = "bwrap_installpath_software" in imported
        return result
    if version in BWRAP_STAGE_KEYS_READ_FROM_SOURCE:
        present = BWRAP_STAGE_KEYS_READ_FROM_SOURCE[version]
        result["keys_source"] = "source-table"
        result["stage_path_keys"] = present
        result["bwrap_info_keys"] = None
        return result
    result["keys_source"] = "unknown"
    result["stage_path_keys"] = None
    result["reason"] = "this EasyBuild version was not read and its bwrap keys could not be imported"
    return result


def probe_all(directory, eb="eb"):
    directory = os.fspath(directory)
    return {
        "bwrap": probe_bwrap(),
        "user_namespaces": probe_user_namespaces(),
        "userns_sandbox": probe_userns_sandbox(),
        "rename_exchange": probe_rename_exchange(directory),
        "easybuild": probe_easybuild(eb),
        "filesystem": filesystem(directory),
        "flock": {
            "method": "flock",
            "scope": "one kernel, the host that runs this process",
            "cross_node": "unmeasured",
        },
    }


def stage_paths_from_bwrap_info(info, framework_version, bwrap_installpath):
    """Return ``(software, modules, how)`` or raise Refused.

    ``bwrap_installpath_software`` is used when EasyBuild wrote it. EasyBuild
    5.3.0 and 5.3.1 do not write it; their ``prepare_bwrap`` joins
    ``<bwrap_installpath>/software`` and ``<bwrap_installpath>/modules``. That
    layout is used only for those two versions. Any other version without the
    key is refused, and the stage is left unpublished.
    """
    from eb_staged_rebuild.errors import Refused

    if not isinstance(info, dict):
        raise Refused("bwrap_info.json is not an object")
    command = info.get("bwrap_cmd") or []
    if "--overlay" in command:
        raise Refused(
            "bwrap used overlayfs for this install; the stage directory is an upper "
            "layer, not a complete generation, so it was not published"
        )
    software = info.get("bwrap_installpath_software") or ""
    modules = info.get("bwrap_installpath_modules") or ""
    if software and modules:
        return software, modules, "bwrap_info"
    version = tuple(framework_version) if framework_version else None
    if version in LEGACY_BWRAP_LAYOUT:
        root = info.get("bwrap_installpath") or bwrap_installpath
        if not root:
            raise Refused("EasyBuild 5.3.x bwrap_info.json has no bwrap_installpath")
        return (
            os.path.join(root, "software"),
            os.path.join(root, "modules"),
            "easybuild-5.3-layout",
        )
    present = sorted(info)
    raise Refused(
        "bwrap_info.json has no bwrap_installpath_software "
        f"(keys: {', '.join(present) or 'none'}). "
        "EasyBuild 5.3.0 and 5.3.1 omit it; this version was not identified as either, "
        "so the stage was kept and not published"
    )

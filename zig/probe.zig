//! Capability probes. Results describe this machine and this directory,
//! nothing else.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");

const sys = common.sys;
const fsx = common.fsx;
const proc = common.proc;
const json = common.json;
const pytext = common.pytext;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

const version_timeout_ms = 30_000;
const capability_timeout_ms = 60_000;
const rename_exchange: u32 = 1 << 1;

/// The hidden subcommand the user-namespace sandbox runs this program as.
pub const sandbox_command = "__userns-sandbox";
/// Conventional exit for a sandbox that could not be set up.
pub const sandbox_setup_exit: u8 = 125;

pub const Env = struct {
    envp: proc.Envp,

    pub fn get(e: Env, name: []const u8) ?[]const u8 {
        var i: usize = 0;
        while (e.envp[i]) |p| : (i += 1) {
            const entry = std.mem.span(p);
            if (entry.len > name.len and entry[name.len] == '=' and std.mem.startsWith(u8, entry, name)) return entry[name.len + 1 ..];
        }
        return null;
    }
};

const D = std.ArrayList(Py.Entry);

fn put(a: Allocator, d: *D, k: []const u8, v: Py) void {
    for (d.items) |*e| if (std.mem.eql(u8, e.key, k)) {
        e.value = v;
        return;
    };
    d.append(a, .{ .key = k, .value = v }) catch oom();
}

/// shutil.which: PATH, or os.defpath when PATH is unset.
fn which(a: Allocator, env: Env, name: []const u8) ?[]const u8 {
    const path = env.get("PATH") orelse "/bin:/usr/bin";
    return proc.which(a, name, path) catch null;
}

const Ran = union(enum) {
    done: struct { code: i64, stdout: []const u8, stderr: []const u8 },
    failed: []const u8,
};

/// subprocess.run(text=True, timeout=...): the result, or the exception text.
fn run(a: Allocator, env: Env, argv: []const []const u8, timeout_ms: u32) Ran {
    const exe = if (argv[0].len > 0 and argv[0][0] == '/') argv[0] else (which(a, env, argv[0]) orelse
        return .{ .failed = std.fmt.allocPrint(a, "[Errno 2] No such file or directory: '{s}'", .{argv[0]}) catch "not found" });
    const r = proc.run(a, exe, argv, env.envp, .{
        .stdout_limit = 16 << 20,
        .stderr_limit = 1 << 20,
        .timeout_ms = timeout_ms,
        .grace_ms = 0,
        .new_session = false,
    }) catch |err| return .{ .failed = switch (err) {
        error.TimedOut => std.fmt.allocPrint(a, "Command '{s}' timed out after {d} seconds", .{ listRepr(a, argv), timeout_ms / 1000 }) catch "timed out",
        else => std.fmt.allocPrint(a, "{t}", .{err}) catch "failed",
    } };
    const code: i64 = switch (r.term) {
        .exited => |c| c,
        .signaled => |s| -@as(i64, s),
    };
    return .{ .done = .{
        .code = code,
        .stdout = pytext.decodeReplace(a, r.stdout) catch r.stdout,
        .stderr = pytext.decodeReplace(a, r.stderr) catch r.stderr,
    } };
}

fn listRepr(a: Allocator, argv: []const []const u8) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    out.append(a, '[') catch oom();
    for (argv, 0..) |s, i| {
        if (i > 0) out.appendSlice(a, ", ") catch oom();
        out.append(a, '\'') catch oom();
        out.appendSlice(a, s) catch oom();
        out.append(a, '\'') catch oom();
    }
    out.append(a, ']') catch oom();
    return out.items;
}

fn orNone(s: []const u8) Py {
    return if (s.len > 0) Py.s(s) else .null;
}

fn probeBwrap(a: Allocator, env: Env) Py {
    var d: D = .empty;
    const path = which(a, env, "bwrap") orelse {
        put(a, &d, "installed", .{ .bool = false });
        put(a, &d, "usable", .{ .bool = false });
        put(a, &d, "path", .null);
        put(a, &d, "version", .null);
        put(a, &d, "reason", Py.s("bwrap is not installed"));
        return .{ .dict = d.items };
    };
    var version_text: []const u8 = "";
    switch (run(a, env, &.{ path, "--version" }, version_timeout_ms)) {
        .done => |r| if (r.code == 0) {
            version_text = pytext.pyStrip(if (r.stdout.len > 0) r.stdout else r.stderr);
        },
        .failed => {},
    }
    put(a, &d, "installed", .{ .bool = true });
    put(a, &d, "path", Py.s(path));
    put(a, &d, "version", orNone(version_text));
    switch (run(a, env, &.{ path, "--dev-bind", "/", "/", "true" }, capability_timeout_ms)) {
        .done => |r| {
            if (r.code == 0) {
                put(a, &d, "usable", .{ .bool = true });
                put(a, &d, "reason", .null);
                return .{ .dict = d.items };
            }
            const err = pytext.pyStrip(if (r.stderr.len > 0) r.stderr else r.stdout);
            var reason = std.fmt.allocPrint(a, "bwrap exited {d}", .{r.code}) catch "";
            if (err.len > 0) reason = std.fmt.allocPrint(a, "{s}: {s}", .{ reason, err }) catch reason;
            put(a, &d, "usable", .{ .bool = false });
            put(a, &d, "reason", Py.s(reason));
        },
        .failed => |why| {
            put(a, &d, "usable", .{ .bool = false });
            put(a, &d, "reason", Py.s(std.fmt.allocPrint(a, "bwrap probe failed: {s}", .{why}) catch why));
        },
    }
    return .{ .dict = d.items };
}

fn readInt(a: Allocator, path: []const u8) union(enum) { ok: i64, err: []const u8 } {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| return .{ .err = osText(a, err, path) };
    defer sys.close(f.fd);
    const data = fsx.readAll(a, f.fd, 4096) catch |err| return .{ .err = osText(a, err, path) };
    const s = pytext.pyStrip(data);
    const n = std.fmt.parseInt(i64, s, 10) catch return .{ .err = std.fmt.allocPrint(a, "invalid literal for int() with base 10: '{s}'", .{s}) catch "invalid literal" };
    return .{ .ok = n };
}

fn osText(a: Allocator, err: anyerror, path: []const u8) []const u8 {
    const t: struct { n: u32, s: []const u8 } = switch (err) {
        error.NotFound => .{ .n = 2, .s = "No such file or directory" },
        error.AccessDenied => .{ .n = 13, .s = "Permission denied" },
        else => return std.fmt.allocPrint(a, "{t}: '{s}'", .{ err, path }) catch path,
    };
    return std.fmt.allocPrint(a, "[Errno {d}] {s}: '{s}'", .{ t.n, t.s, path }) catch path;
}

fn probeUserNamespaces(a: Allocator) Py {
    var d: D = .empty;
    const path = "/proc/sys/user/max_user_namespaces";
    put(a, &d, "max_user_namespaces", .null);
    put(a, &d, "unprivileged_userns_clone", .null);
    put(a, &d, "readable", .{ .bool = false });
    switch (readInt(a, path)) {
        .ok => |n| {
            put(a, &d, "max_user_namespaces", .{ .int = n });
            put(a, &d, "readable", .{ .bool = true });
        },
        .err => |e| put(a, &d, "error", Py.s(std.fmt.allocPrint(a, "{s}: {s}", .{ path, e }) catch e)),
    }
    switch (readInt(a, "/proc/sys/kernel/unprivileged_userns_clone")) {
        .ok => |n| put(a, &d, "unprivileged_userns_clone", .{ .int = n }),
        .err => {},
    }
    return .{ .dict = d.items };
}

fn selfExe(a: Allocator) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, "/proc/self/exe", &buf, buf.len }) catch return "/proc/self/exe";
    return a.dupe(u8, buf[0..n]) catch "/proc/self/exe";
}

fn probeUsernsSandbox(a: Allocator, env: Env) Py {
    var d: D = .empty;
    const path = which(a, env, "unshare");
    put(a, &d, "installed", .{ .bool = path != null });
    put(a, &d, "usable", .{ .bool = false });
    put(a, &d, "path", if (path) |p| Py.s(p) else .null);
    put(a, &d, "reason", .null);
    const unshare = path orelse {
        put(a, &d, "reason", Py.s("unshare is not installed"));
        return .{ .dict = d.items };
    };
    const argv = [_][]const u8{ unshare, "--user", "--map-root-user", "--mount", "--fork", "--kill-child", selfExe(a), sandbox_command, "--probe" };
    switch (run(a, env, &argv, capability_timeout_ms)) {
        .done => |r| {
            if (r.code == 0) {
                put(a, &d, "usable", .{ .bool = true });
                return .{ .dict = d.items };
            }
            const detail = pytext.pyStrip(if (r.stderr.len > 0) r.stderr else r.stdout);
            var reason = std.fmt.allocPrint(a, "user-namespace probe exited {d}", .{r.code}) catch "";
            if (detail.len > 0) reason = std.fmt.allocPrint(a, "{s}: {s}", .{ reason, detail }) catch reason;
            put(a, &d, "reason", Py.s(reason));
        },
        .failed => |why| put(a, &d, "reason", Py.s(std.fmt.allocPrint(a, "user-namespace probe failed: {s}", .{why}) catch why)),
    }
    return .{ .dict = d.items };
}

fn isDir(path: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, path, true) catch return false;
    return st.isDir();
}

/// filesystem(): the longest /proc/mounts entry covering the path.
fn filesystem(a: Allocator, path: []const u8) Py {
    const base = if (isDir(path)) path else blk: {
        const dn = std.fs.path.dirname(path) orelse "";
        break :blk if (dn.len > 0) dn else ".";
    };
    var rb: [sys.path_max]u8 = undefined;
    const target = a.dupe(u8, fsx.realpath(fsx.cwd, base, &rb) catch base) catch base;
    var d: D = .empty;
    const f = fsx.openRegular(fsx.cwd, "/proc/mounts", true) catch |err| {
        put(a, &d, "error", Py.s(osText(a, err, "/proc/mounts")));
        put(a, &d, "path", Py.s(target));
        return .{ .dict = d.items };
    };
    defer sys.close(f.fd);
    const data = fsx.readAll(a, f.fd, 16 << 20) catch "";
    var best_mount: ?[]const u8 = null;
    var best: D = .empty;
    var it = std.mem.splitScalar(u8, data, '\n');
    while (it.next()) |l| {
        const parts = pytext.pySplit(a, l) catch continue;
        if (parts.len < 3) continue;
        const mount = std.mem.replaceOwned(u8, a, parts[1], "\\040", " ") catch continue;
        const trimmed = std.mem.trimEnd(u8, mount, "/");
        const covers = std.mem.eql(u8, target, mount) or (std.mem.startsWith(u8, target, trimmed) and target.len > trimmed.len and target[trimmed.len] == '/');
        if (!covers) continue;
        if (best_mount == null or mount.len > best_mount.?.len) {
            best_mount = mount;
            best = .empty;
            put(a, &best, "device", Py.s(parts[0]));
            put(a, &best, "mount", Py.s(mount));
            put(a, &best, "fstype", Py.s(parts[2]));
            put(a, &best, "path", Py.s(target));
        }
    }
    if (best_mount != null) return .{ .dict = best.items };
    put(a, &d, "path", Py.s(target));
    put(a, &d, "fstype", .null);
    return .{ .dict = d.items };
}

fn strerror(e: linux.E) []const u8 {
    return switch (e) {
        .INVAL => "Invalid argument",
        .NOSYS => "Function not implemented",
        .PERM => "Operation not permitted",
        .ACCES => "Permission denied",
        .XDEV => "Invalid cross-device link",
        .OPNOTSUPP => "Operation not supported",
        else => @tagName(e),
    };
}

fn mkdtemp(a: Allocator, dir: []const u8, prefix: []const u8) ![]const u8 {
    const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_";
    var tries: usize = 0;
    while (tries < 100) : (tries += 1) {
        var rnd: [8]u8 = undefined;
        _ = linux.getrandom(&rnd, rnd.len, 0);
        for (&rnd) |*b| b.* = alphabet[b.* % alphabet.len];
        const p = try std.fmt.allocPrint(a, "{s}/{s}{s}", .{ dir, prefix, rnd });
        var zb: [sys.path_max:0]u8 = undefined;
        switch (linux.errno(linux.mkdirat(linux.AT.FDCWD, (try sys.z(&zb, p)).ptr, 0o700))) {
            .SUCCESS => return p,
            .EXIST => continue,
            .ACCES, .PERM => return error.AccessDenied,
            .NOENT => return error.NotFound,
            else => return error.Unexpected,
        }
    }
    return error.Exists;
}

fn mkdir(a: Allocator, p: []const u8) !void {
    _ = a;
    var zb: [sys.path_max:0]u8 = undefined;
    _ = try sys.call(linux.mkdirat, .{ linux.AT.FDCWD, (try sys.z(&zb, p)).ptr, @as(linux.mode_t, 0o777) });
}

fn writeFile(p: []const u8, data: []const u8) !void {
    const fd = try sys.openat(fsx.cwd, p, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o666);
    defer sys.close(fd);
    try sys.writeAll(fd, data);
}

/// os.makedirs(path, exist_ok=True).
fn makedirs(a: Allocator, p: []const u8) !void {
    if (isDir(p)) return;
    if (std.fs.path.dirname(p)) |parent| if (parent.len > 0 and !isDir(parent)) try makedirs(a, parent);
    mkdir(a, p) catch |err| switch (err) {
        error.Exists => if (!isDir(p)) return err,
        else => return err,
    };
}

fn probeRenameExchange(a: Allocator, directory: []const u8) !Py {
    try makedirs(a, directory);
    const fs = filesystem(a, directory);
    var d: D = .empty;
    for (fs.dict) |e| put(a, &d, e.key, e.value);
    put(a, &d, "ok", .{ .bool = false });
    put(a, &d, "errno", .null);
    put(a, &d, "error", .null);
    put(a, &d, "flag", .{ .int = rename_exchange });
    const parent = try mkdtemp(a, directory, ".rename-exchange-");
    defer fsx.deleteTree(a, fsx.cwd, parent) catch {};
    const left = try std.fs.path.join(a, &.{ parent, "left" });
    const right = try std.fs.path.join(a, &.{ parent, "right" });
    attempt: {
        mkdir(a, left) catch |err| {
            osFail(a, &d, err, left);
            break :attempt;
        };
        mkdir(a, right) catch |err| {
            osFail(a, &d, err, right);
            break :attempt;
        };
        const lm = std.fs.path.join(a, &.{ left, "mark" }) catch break :attempt;
        writeFile(lm, "L") catch |err| {
            osFail(a, &d, err, lm);
            break :attempt;
        };
        writeFile(std.fs.path.join(a, &.{ right, "mark" }) catch break :attempt, "R") catch |err| {
            osFail(a, &d, err, right);
            break :attempt;
        };
        var zl: [sys.path_max:0]u8 = undefined;
        var zr: [sys.path_max:0]u8 = undefined;
        const rc = linux.renameat2(linux.AT.FDCWD, (sys.z(&zl, left) catch break :attempt).ptr, linux.AT.FDCWD, (sys.z(&zr, right) catch break :attempt).ptr, .{ .EXCHANGE = true });
        const e = linux.errno(rc);
        if (e == .SUCCESS) {
            const f = fsx.openRegular(fsx.cwd, lm, true) catch break :attempt;
            defer sys.close(f.fd);
            const got = fsx.readAll(a, f.fd, 16) catch "";
            const swapped = std.mem.eql(u8, got, "R");
            put(a, &d, "ok", .{ .bool = swapped });
            if (!swapped) put(a, &d, "error", Py.s("renameat2 returned 0 but the directories did not exchange"));
        } else {
            put(a, &d, "errno", .{ .int = @backingInt(e) });
            put(a, &d, "error", Py.s(strerror(e)));
        }
    }
    return .{ .dict = d.items };
}

fn osFail(a: Allocator, d: *D, err: anyerror, path: []const u8) void {
    const n: i64 = switch (err) {
        error.NotFound => 2,
        error.AccessDenied => 13,
        error.Exists => 17,
        error.NoSpace => 28,
        error.ReadOnly => 30,
        else => 5,
    };
    put(a, d, "errno", .{ .int = n });
    put(a, d, "error", Py.s(osText(a, err, path)));
}

const keys_from_source = [_]struct { v: [3]i64, present: bool }{
    .{ .v = .{ 5, 3, 0 }, .present = false },
    .{ .v = .{ 5, 3, 1 }, .present = false },
    .{ .v = .{ 5, 4, 0 }, .present = true },
    .{ .v = .{ 5, 4, 1 }, .present = true },
    .{ .v = .{ 5, 4, 2 }, .present = true },
};

/// parse_framework_version: "framework: X.Y.Z", else "This is EasyBuild X.Y.Z".
fn parseFrameworkVersion(t: []const u8) ?[3]i64 {
    for ([_][]const u8{ "framework:", "This is EasyBuild" }) |lead| {
        var from: usize = 0;
        while (std.mem.indexOfPos(u8, t, from, lead)) |at| : (from = at + 1) {
            var i = at + lead.len;
            const ws = i;
            while (i < t.len and pytext.pyWhitespace(t[i])) i += 1;
            if (std.mem.eql(u8, lead, "This is EasyBuild") and i == ws) continue;
            var v: [3]i64 = undefined;
            var ok = true;
            for (0..3) |k| {
                const s = i;
                while (i < t.len and std.ascii.isDigit(t[i])) i += 1;
                if (i == s) {
                    ok = false;
                    break;
                }
                v[k] = std.fmt.parseInt(i64, t[s..i], 10) catch {
                    ok = false;
                    break;
                };
                if (k < 2) {
                    if (i >= t.len or t[i] != '.') {
                        ok = false;
                        break;
                    }
                    i += 1;
                }
            }
            if (ok) return v;
        }
    }
    return null;
}

fn importBwrapKeys(a: Allocator, env: Env, eb_path: []const u8) ?[]const Py {
    var rb: [sys.path_max]u8 = undefined;
    const real = fsx.realpath(fsx.cwd, eb_path, &rb) catch return null;
    const dir = std.fs.path.dirname(real) orelse return null;
    var python = std.fs.path.join(a, &.{ dir, "python3" }) catch return null;
    if (!isFileFollow(python)) python = std.fs.path.join(a, &.{ dir, "python" }) catch return null;
    if (!isFileFollow(python)) return null;
    const code = "import json\nimport easybuild.tools.bwrap as b\nprint(json.dumps(sorted(b._bwrap_info)))\n";
    const r = switch (run(a, env, &.{ python, "-c", code }, capability_timeout_ms)) {
        .done => |x| x,
        .failed => return null,
    };
    if (r.code != 0) return null;
    const v = json.parse(a, pytext.pyStrip(r.stdout)) catch return null;
    if (v != .array) return null;
    const out = a.alloc(Py, v.array.len) catch return null;
    for (v.array, 0..) |item, i| {
        if (item != .string) return null;
        out[i] = Py.s(json.decodeCache(a, item.string) catch return null);
    }
    return out;
}

fn isFileFollow(p: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, p, true) catch return false;
    return st.isReg();
}

fn probeEasybuild(a: Allocator, env: Env, eb: []const u8) Py {
    var d: D = .empty;
    const path: ?[]const u8 = if (std.mem.indexOfScalar(u8, eb, '/') == null) which(a, env, eb) else (if (isFileFollow(eb)) eb else null);
    put(a, &d, "eb", Py.s(eb));
    put(a, &d, "path", if (path) |p| Py.s(p) else .null);
    put(a, &d, "found", .{ .bool = path != null });
    put(a, &d, "version_text", .null);
    put(a, &d, "framework_version", .null);
    put(a, &d, "bwrap_info_keys", .null);
    put(a, &d, "keys_source", .null);
    put(a, &d, "stage_path_keys", .null);
    put(a, &d, "legacy_layout_versions", .{ .list = a.dupe(Py, &.{ Py.s("5.3.0"), Py.s("5.3.1") }) catch &.{} });
    put(a, &d, "reason", .null);
    const p = path orelse {
        put(a, &d, "reason", Py.s(std.fmt.allocPrint(a, "{s} was not found on PATH", .{eb}) catch eb));
        return .{ .dict = d.items };
    };
    const exe = if (p[0] == '/') p else blk: {
        var rb: [sys.path_max]u8 = undefined;
        const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch ".";
        break :blk std.fs.path.join(a, &.{ cwd, p }) catch p;
    };
    const r = switch (run(a, env, &.{ exe, "--version" }, capability_timeout_ms)) {
        .done => |x| x,
        .failed => |why| {
            put(a, &d, "reason", Py.s(why));
            return .{ .dict = d.items };
        },
    };
    var parts: std.ArrayList([]const u8) = .empty;
    if (r.stdout.len > 0) parts.append(a, r.stdout) catch oom();
    if (r.stderr.len > 0) parts.append(a, r.stderr) catch oom();
    const t = std.mem.join(a, "\n", parts.items) catch "";
    put(a, &d, "version_text", orNone(pytext.pyStrip(t)));
    if (r.code != 0) {
        put(a, &d, "reason", Py.s(std.fmt.allocPrint(a, "eb --version exited {d}", .{r.code}) catch ""));
        return .{ .dict = d.items };
    }
    const version = parseFrameworkVersion(t);
    if (version) |v| put(a, &d, "framework_version", .{ .list = a.dupe(Py, &.{ .{ .int = v[0] }, .{ .int = v[1] }, .{ .int = v[2] } }) catch &.{} });
    if (importBwrapKeys(a, env, exe)) |keys| {
        put(a, &d, "bwrap_info_keys", .{ .list = keys });
        put(a, &d, "keys_source", Py.s("imported"));
        var present = false;
        for (keys) |k| if (std.mem.eql(u8, k.str, "bwrap_installpath_software")) {
            present = true;
        };
        put(a, &d, "stage_path_keys", .{ .bool = present });
        return .{ .dict = d.items };
    }
    if (version) |v| for (keys_from_source) |row| if (std.mem.eql(i64, &row.v, &v)) {
        put(a, &d, "keys_source", Py.s("source-table"));
        put(a, &d, "stage_path_keys", .{ .bool = row.present });
        put(a, &d, "bwrap_info_keys", .null);
        return .{ .dict = d.items };
    };
    put(a, &d, "keys_source", Py.s("unknown"));
    put(a, &d, "stage_path_keys", .null);
    put(a, &d, "reason", Py.s("this EasyBuild version was not read and its bwrap keys could not be imported"));
    return .{ .dict = d.items };
}

pub fn probeAll(a: Allocator, env: Env, directory: []const u8, eb: []const u8) !Py {
    var d: D = .empty;
    put(a, &d, "bwrap", probeBwrap(a, env));
    put(a, &d, "user_namespaces", probeUserNamespaces(a));
    put(a, &d, "userns_sandbox", probeUsernsSandbox(a, env));
    put(a, &d, "rename_exchange", try probeRenameExchange(a, directory));
    put(a, &d, "easybuild", probeEasybuild(a, env, eb));
    put(a, &d, "filesystem", filesystem(a, directory));
    var fl: D = .empty;
    put(a, &fl, "method", Py.s("flock"));
    put(a, &fl, "scope", Py.s("one kernel, the host that runs this process"));
    put(a, &fl, "cross_node", Py.s("unmeasured"));
    put(a, &d, "flock", .{ .dict = fl.items });
    return .{ .dict = d.items };
}

fn get(v: Py, k: []const u8) ?Py {
    if (v != .dict) return null;
    for (v.dict) |e| if (std.mem.eql(u8, e.key, k)) return e.value;
    return null;
}

fn truthy(v: ?Py) bool {
    const x = v orelse return false;
    return switch (x) {
        .null => false,
        .bool => |b| b,
        .int => |n| n != 0,
        .float => |f| f != 0,
        .str => |s| s.len > 0,
        .list => |l| l.len > 0,
        .dict => |l| l.len > 0,
    };
}

fn strOf(a: Allocator, v: ?Py) []const u8 {
    const x = v orelse return "None";
    return switch (x) {
        .null => "None",
        .bool => |b| if (b) "True" else "False",
        .int => |n| std.fmt.allocPrint(a, "{d}", .{n}) catch "",
        .str => |s| s,
        else => json.dumps(a, x, 0, false) catch "",
    };
}

/// _print_probe: the human-readable report.
pub fn printText(a: Allocator, r: Py) void {
    const bwrap = get(r, "bwrap").?;
    const ns = get(r, "user_namespaces").?;
    const userns = get(r, "userns_sandbox").?;
    const ex = get(r, "rename_exchange").?;
    const eb = get(r, "easybuild").?;
    const fs = get(r, "filesystem") orelse Py{ .dict = &.{} };
    var o: std.ArrayList(u8) = .empty;
    o.print(a, "bwrap: {s}\n", .{if (truthy(get(bwrap, "usable"))) "usable" else "not usable"}) catch oom();
    if (truthy(get(bwrap, "version"))) o.print(a, "  version: {s}\n", .{strOf(a, get(bwrap, "version"))}) catch oom();
    if (truthy(get(bwrap, "reason"))) o.print(a, "  reason: {s}\n", .{strOf(a, get(bwrap, "reason"))}) catch oom();
    o.print(a, "user.max_user_namespaces: {s}\n", .{strOf(a, get(ns, "max_user_namespaces"))}) catch oom();
    if (get(ns, "unprivileged_userns_clone")) |v| if (v != .null) o.print(a, "kernel.unprivileged_userns_clone: {s}\n", .{strOf(a, v)}) catch oom();
    o.print(a, "user-namespace sandbox: {s}\n", .{if (truthy(get(userns, "usable"))) "usable" else "not usable"}) catch oom();
    if (truthy(get(userns, "reason"))) o.print(a, "  reason: {s}\n", .{strOf(a, get(userns, "reason"))}) catch oom();
    const fstype = if (truthy(get(ex, "fstype"))) get(ex, "fstype") else get(fs, "fstype");
    o.print(a, "RENAME_EXCHANGE: {s}", .{if (truthy(get(ex, "ok"))) "yes" else "no"}) catch oom();
    if (truthy(fstype)) o.print(a, " on {s}", .{strOf(a, fstype)}) catch oom();
    o.append(a, '\n') catch oom();
    if (truthy(get(ex, "error"))) o.print(a, "  {s}\n", .{strOf(a, get(ex, "error"))}) catch oom();
    if (truthy(get(eb, "found"))) {
        var rendered: []const u8 = "unknown";
        if (get(eb, "framework_version")) |v| if (truthy(v)) {
            var parts: std.ArrayList([]const u8) = .empty;
            for (v.list) |x| parts.append(a, strOf(a, x)) catch oom();
            rendered = std.mem.join(a, ".", parts.items) catch "unknown";
        };
        o.print(a, "EasyBuild framework: {s}\n", .{rendered}) catch oom();
        o.print(a, "  bwrap stage-path keys: {s} ({s})\n", .{ strOf(a, get(eb, "stage_path_keys")), strOf(a, get(eb, "keys_source")) }) catch oom();
    } else {
        o.print(a, "EasyBuild: {s}\n", .{strOf(a, get(eb, "reason"))}) catch oom();
    }
    const mount = if (truthy(get(fs, "mount"))) strOf(a, get(fs, "mount")) else "";
    const fl = std.fmt.allocPrint(a, "filesystem: {s} {s}", .{ strOf(a, get(fs, "fstype")), mount }) catch "";
    o.appendSlice(a, std.mem.trimEnd(u8, fl, " \t\n\r\x0b\x0c")) catch oom();
    o.append(a, '\n') catch oom();
    o.appendSlice(a, "lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured (open-004)\n") catch oom();
    cli.put(1, o.items);
}

// ---- the sandbox, run inside unshare --user --mount ------------------------

const MS_RDONLY = 1;
const MS_REMOUNT = 1 << 5;
const MS_BIND = 1 << 12;
const MS_REC = 1 << 14;
const MS_PRIVATE = 1 << 18;

fn mountRaw(source: ?[*:0]const u8, target: [*:0]const u8, flags: u32) linux.E {
    return linux.errno(linux.mount(source, target, null, flags, 0));
}

/// The sandbox setup the Python tool's userns_sandbox module does; only
/// --probe is in this build.
pub fn sandboxMain(a: Allocator, env: Env, args: []const []const u8) u8 {
    const prefix = "eb-staged-rebuild user namespace: ";
    var read_only: std.ArrayList([]const u8) = .empty;
    var probe_only = false;
    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        if (std.mem.eql(u8, args[i], "--probe")) {
            probe_only = true;
        } else if (std.mem.eql(u8, args[i], "--read-only") and i + 1 < args.len) {
            i += 1;
            read_only.append(a, args[i]) catch oom();
        } else if (std.mem.startsWith(u8, args[i], "--read-only=")) {
            read_only.append(a, args[i]["--read-only=".len..]) catch oom();
        } else break;
    }
    var command = args[i..];
    if (command.len > 0 and std.mem.eql(u8, command[0], "--")) command = command[1..];
    const e = mountRaw(null, "/", MS_REC | MS_PRIVATE);
    if (e != .SUCCESS) {
        cli.err(prefix ++ "mount /: {s}\n", .{strerror(e)});
        return sandbox_setup_exit;
    }
    if (probe_only) {
        // tempfile.gettempdir(): TMPDIR, TEMP, TMP, then /tmp.
        const tmp = blk: {
            for ([_][]const u8{ "TMPDIR", "TEMP", "TMP" }) |k| if (env.get(k)) |v| if (v.len > 0 and isDir(v)) break :blk v;
            break :blk "/tmp";
        };
        const root = mkdtemp(a, tmp, "tmp") catch |err| {
            cli.err(prefix ++ "{t}\n", .{err});
            return sandbox_setup_exit;
        };
        if (protect(a, root)) |msg| {
            cli.err(prefix ++ "{s}\n", .{msg});
            return sandbox_setup_exit;
        }
        const probe_file = std.fs.path.join(a, &.{ root, "must-fail" }) catch return sandbox_setup_exit;
        if (sys.openat(fsx.cwd, probe_file, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o666)) |fd| {
            sys.close(fd);
            cli.err(prefix ++ "read-only bind mount remained writable\n", .{});
            return sandbox_setup_exit;
        } else |err| switch (err) {
            error.ReadOnly => return 0,
            else => {
                cli.err(prefix ++ "{t}\n", .{err});
                return sandbox_setup_exit;
            },
        }
    }
    for (read_only.items) |p| if (protect(a, p)) |msg| {
        cli.err(prefix ++ "{s}\n", .{msg});
        return sandbox_setup_exit;
    };
    if (command.len == 0) {
        cli.err(prefix ++ "no sandbox command was supplied\n", .{});
        return sandbox_setup_exit;
    }
    // os.execvpe(command[0], command, os.environ): a PATH search for a name.
    const exe = if (std.mem.indexOfScalar(u8, command[0], '/') != null) command[0] else (which(a, env, command[0]) orelse {
        cli.err(prefix ++ "[Errno 2] No such file or directory\n", .{});
        return sandbox_setup_exit;
    });
    const zargv = a.allocSentinel(?[*:0]const u8, command.len, null) catch return sandbox_setup_exit;
    for (command, 0..) |x, k| zargv[k] = (a.dupeSentinel(u8, x, 0) catch return sandbox_setup_exit).ptr;
    const zexe = a.dupeSentinel(u8, exe, 0) catch return sandbox_setup_exit;
    const rc = linux.execve(zexe.ptr, zargv.ptr, env.envp);
    cli.err(prefix ++ "exec {s}: {s}\n", .{ command[0], strerror(linux.errno(rc)) });
    return sandbox_setup_exit;
}

/// _protect: bind the directory onto itself, then remount it read-only.
/// Null on success, else the reason.
fn protect(a: Allocator, path_in: []const u8) ?[]const u8 {
    var rb: [sys.path_max]u8 = undefined;
    const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch "/";
    const path = if (path_in.len > 0 and path_in[0] == '/') path_in else (std.fs.path.join(a, &.{ cwd, path_in }) catch path_in);
    const st = fsx.statAt(fsx.cwd, path, false) catch return std.fmt.allocPrint(a, "read-only sandbox path is not a real directory: {s}", .{path}) catch "bad path";
    if (!st.isDir()) return std.fmt.allocPrint(a, "read-only sandbox path is not a real directory: {s}", .{path}) catch "bad path";
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, path) catch return "path too long";
    var e = mountRaw(z.ptr, z.ptr, MS_BIND | MS_REC);
    if (e == .SUCCESS) e = mountRaw(z.ptr, z.ptr, MS_BIND | MS_REMOUNT | MS_RDONLY);
    if (e != .SUCCESS) return std.fmt.allocPrint(a, "mount {s}: {s}", .{ path, strerror(e) }) catch "mount failed";
    return null;
}

test "parseFrameworkVersion" {
    try std.testing.expectEqual([3]i64{ 5, 4, 1 }, parseFrameworkVersion("This is EasyBuild 5.4.1 (framework: 5.4.1, easyblocks: 5.4.1)").?);
    try std.testing.expectEqual([3]i64{ 5, 3, 0 }, parseFrameworkVersion("This is EasyBuild 5.3.0").?);
    try std.testing.expect(parseFrameworkVersion("eb 5") == null);
}

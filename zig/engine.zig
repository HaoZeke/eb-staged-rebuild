//! run: build a complete staged install, then swap it with the live
//! directory. This build carries the checks and the dry run; the build, the
//! swap and resume follow.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const probe = @import("probe.zig");
const pyfmt = @import("pyfmt.zig");
const run_mod = @import("run.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

fn refuse(comptime f: []const u8, args: anytype) noreturn {
    main_mod.refuse(f, args);
}

fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

pub const Spec = struct {
    software: []const u8,
    modules: []const u8,
    state_root: []const u8,
    eb_args: []const []const u8,
    backend: []const u8 = "auto",
    grace_hours: f64 = admin.default_grace_hours,
    dry_run: bool = false,
    module_rels: []const []const u8 = &.{},
    eb_executable: []const u8 = "eb",
    run_id: ?[]const u8 = null,
    skip_live_check: bool = false,
    lock_timeout: ?f64 = null,
    prepare_executable: ?[]const u8 = null,
    ledger_config: ?[]const u8 = null,
};

// ---- eb arguments, as builders.py shapes them -----------------------------

const drop_flags = [_][]const u8{ "--bwrap", "--disable-bwrap", "--remove-ghost-install-dirs" };
const drop_with_value = [_][]const u8{ "--installpath", "--installpath-software", "--installpath-modules", "--prefix", "--bwrap-installpath", "--bwrap-options", "--subdir-software", "--subdir-modules" };

fn inList(list: []const []const u8, s: []const u8) bool {
    for (list) |x| if (std.mem.eql(u8, x, s)) return true;
    return false;
}

/// filter_eb_args: drop install-path and bubblewrap options.
pub fn filterEbArgs(a: Allocator, args: []const []const u8) []const []const u8 {
    var kept: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    while (i < args.len) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "--")) {
            i += 1;
            continue;
        }
        const eq = std.mem.indexOfScalar(u8, arg, '=');
        const name = arg[0 .. eq orelse arg.len];
        if (inList(&drop_flags, name)) {
            i += 1;
            continue;
        }
        if (inList(&drop_with_value, name)) {
            i += if (eq == null) 2 else 1;
            continue;
        }
        kept.append(a, arg) catch {};
        i += 1;
    }
    return kept.items;
}

/// ensure_rebuild: add --rebuild unless --rebuild, --force or -f is there.
pub fn ensureRebuild(a: Allocator, args: []const []const u8) []const []const u8 {
    for (args) |arg| {
        const name = arg[0 .. std.mem.indexOfScalar(u8, arg, '=') orelse arg.len];
        if (std.mem.eql(u8, name, "--rebuild") or std.mem.eql(u8, name, "--force") or std.mem.eql(u8, name, "-f")) return args;
    }
    var out = a.dupe([]const u8, args) catch return args;
    out = a.realloc(out, out.len + 1) catch return args;
    out[out.len - 1] = "--rebuild";
    return out;
}

fn join(a: Allocator, parts: []const []const u8) []const u8 {
    return std.mem.join(a, " ", parts) catch "";
}

fn which(a: Allocator, env: probe.Env, name: []const u8) ?[]const u8 {
    return common.proc.which(a, name, env.get("PATH") orelse "/bin:/usr/bin") catch null;
}

fn selfExe(a: Allocator) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, "/proc/self/exe", &buf, buf.len }) catch return "/proc/self/exe";
    return a.dupe(u8, buf[0..n]) catch "/proc/self/exe";
}

/// The build command each backend runs, as the dry run prints it.
fn buildCommand(a: Allocator, env: probe.Env, spec: Spec, probes: Py, generation_root: []const u8) []const u8 {
    const filtered = ensureRebuild(a, filterEbArgs(a, spec.eb_args));
    var cmd: std.ArrayList([]const u8) = .empty;
    if (std.mem.eql(u8, spec.backend, "bwrap")) {
        cmd.appendSlice(a, &.{ spec.eb_executable, "--experimental", "--bwrap", std.fmt.allocPrint(a, "--bwrap-installpath={s}", .{generation_root}) catch "", "--bwrap-options=--die-with-parent" }) catch {};
        cmd.appendSlice(a, filtered) catch {};
        return join(a, cmd.items);
    }
    const software = std.fs.path.join(a, &.{ generation_root, "software" }) catch "";
    const modules = std.fs.path.join(a, &.{ generation_root, "modules" }) catch "";
    var inner: std.ArrayList([]const u8) = .empty;
    inner.appendSlice(a, &.{ spec.eb_executable, std.fmt.allocPrint(a, "--installpath-software={s}", .{software}) catch "", std.fmt.allocPrint(a, "--installpath-modules={s}", .{modules}) catch "" }) catch {};
    inner.appendSlice(a, filtered) catch {};
    if (!std.mem.eql(u8, spec.backend, "userns")) return join(a, inner.items);
    const report = pyfmt.get(probes, "userns_sandbox");
    const unshare = blk: {
        if (report) |r| if (pyfmt.get(r, "path")) |p| if (p == .str and p.str.len > 0) break :blk p.str;
        break :blk which(a, env, "unshare") orelse "unshare";
    };
    cmd.appendSlice(a, &.{ unshare, "--user", "--map-root-user", "--mount", "--fork", "--kill-child", selfExe(a), probe.sandbox_command, "--read-only", spec.software, "--read-only", spec.modules, "--" }) catch {};
    cmd.appendSlice(a, inner.items) catch {};
    return join(a, cmd.items);
}

// ---- checks -------------------------------------------------------------

/// assert_real_components: the first existing component that is a symlink.
fn symlinkComponent(a: Allocator, path: []const u8) ?[]const u8 {
    var current = admin.abspath(a, path);
    while (true) {
        if (std.mem.eql(u8, main_mod.lstatKind(current), "symlink")) return current;
        const parent = std.fs.path.dirname(current) orelse return null;
        if (std.mem.eql(u8, parent, current)) return null;
        current = parent;
    }
}

pub fn checkRoots(a: Allocator, spec: Spec) void {
    if (spec.prepare_executable) |p| {
        if (p.len == 0) refuse("publication preparation executable must not be empty", .{});
        if (p[0] != '/') refuse("publication preparation executable must be an absolute path", .{});
    }
    const sk = main_mod.lstatKind(spec.software);
    if (!std.mem.eql(u8, sk, "dir")) {
        if (std.mem.eql(u8, sk, "symlink")) refuse("software path is a symlink ({s}); Name/version must stay a real directory", .{spec.software});
        refuse("software path is not a real directory: {s} ({s})", .{ spec.software, sk });
    }
    const mk = main_mod.lstatKind(spec.modules);
    if (!std.mem.eql(u8, mk, "dir")) {
        if (std.mem.eql(u8, mk, "symlink")) refuse("modules path is a symlink: {s}", .{spec.modules});
        refuse("modules path is not a real directory: {s} ({s})", .{ spec.modules, mk });
    }
    if (symlinkComponent(a, spec.state_root)) |s| refuse("state root is unsafe: {s} is a symlink", .{s});
    const known = [_][]const u8{ "auto", "installpath", "bwrap", "userns" };
    if (!inList(&known, spec.backend)) refuse("unknown backend {s}", .{spec.backend});
}

fn usable(probes: Py, key: []const u8) bool {
    const r = pyfmt.get(probes, key) orelse return false;
    return pyfmt.truthy(pyfmt.get(r, "usable"));
}

fn strOr(v: ?Py, fallback: []const u8) []const u8 {
    if (pyfmt.truthy(v) and v.? == .str) return v.?.str;
    return fallback;
}

// ---- run ----------------------------------------------------------------

pub fn run(a: Allocator, env: probe.Env, spec_in: Spec) u8 {
    var spec = spec_in;
    checkRoots(a, spec);
    const probes = probe.probeAll(a, env, spec.software, spec.eb_executable) catch |err| refuse("{s}", .{main_mod.osError(a, err, spec.software)});
    if (std.mem.eql(u8, spec.backend, "auto")) {
        spec.backend = if (usable(probes, "bwrap")) "bwrap" else if (usable(probes, "userns_sandbox")) "userns" else "installpath";
        if (std.mem.eql(u8, spec.backend, "installpath"))
            log("neither bubblewrap nor the user-namespace sandbox is usable; using the unsandboxed install-path fallback", .{});
    }
    if (spec.dry_run) return dryRun(a, env, spec, probes);
    var ctx: admin.Ctx = .{ .a = a, .env = env, .pause = .{ .env = env } };
    var r: run_mod.R = .{ .a = a, .env = env, .ctx = &ctx, .spec = spec };
    const res = run_mod.run(&r, probes) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{r.why}),
    };
    return res.code;
}

fn dryRun(a: Allocator, env: probe.Env, spec: Spec, probes: Py) u8 {
    const bwrap = pyfmt.get(probes, "bwrap") orelse Py{ .dict = &.{} };
    const easybuild = pyfmt.get(probes, "easybuild") orelse Py{ .dict = &.{} };
    const exchange = pyfmt.get(probes, "rename_exchange") orelse Py{ .dict = &.{} };
    const ns = pyfmt.get(probes, "user_namespaces") orelse Py{ .dict = &.{} };
    var maxns: std.ArrayList(u8) = .empty;
    pyfmt.str(&maxns, a, pyfmt.get(ns, "max_user_namespaces")) catch {};
    var lines: std.ArrayList([]const u8) = .empty;
    const fmt = struct {
        fn f(al: Allocator, comptime s: []const u8, args: anytype) []const u8 {
            return std.fmt.allocPrint(al, s, args) catch "";
        }
    }.f;
    lines.append(a, fmt(a, "backend: {s}", .{spec.backend})) catch {};
    lines.append(a, fmt(a, "software: {s}", .{admin.abspath(a, spec.software)})) catch {};
    lines.append(a, fmt(a, "modules: {s}", .{admin.abspath(a, spec.modules)})) catch {};
    lines.append(a, fmt(a, "state root: {s}", .{admin.abspath(a, spec.state_root)})) catch {};
    lines.append(a, fmt(a, "bwrap usable: {s} ({s})", .{
        if (usable(probes, "bwrap")) "True" else "False",
        strOr(pyfmt.get(bwrap, "reason"), strOr(pyfmt.get(bwrap, "version"), "present")),
    })) catch {};
    lines.append(a, fmt(a, "user.max_user_namespaces: {s}", .{maxns.items})) catch {};
    lines.append(a, fmt(a, "renameat2 RENAME_EXCHANGE on {s}: {s}", .{
        strOr(pyfmt.get(exchange, "fstype"), "this filesystem"),
        if (pyfmt.truthy(pyfmt.get(exchange, "ok"))) "yes" else "no",
    })) catch {};
    lines.append(a, fmt(a, "easybuild: {s}", .{strOr(pyfmt.get(easybuild, "version_text"), strOr(pyfmt.get(easybuild, "reason"), "not found"))})) catch {};
    lines.append(a, "lock: local flock lock plus POSIX record lock; cross-node coherence unmeasured (open-004)") catch {};
    lines.append(a, "publish: renameat2(RENAME_EXCHANGE), with a restoring ordinary-rename fallback") catch {};
    var problems: std.ArrayList([]const u8) = .empty;
    if (std.mem.eql(u8, spec.backend, "bwrap") and !usable(probes, "bwrap"))
        problems.append(a, strOr(pyfmt.get(bwrap, "reason"), "bwrap is not usable")) catch {};
    for (spec.module_rels) |rel| {
        const path = std.fs.path.join(a, &.{ spec.software, rel }) catch continue;
        if (std.mem.eql(u8, main_mod.lstatKind(path), "symlink")) problems.append(a, fmt(a, "{s} is a symlink", .{path})) catch {};
    }
    const gen = std.fs.path.join(a, &.{ spec.state_root, "generations", "<run-id>" }) catch "";
    lines.append(a, fmt(a, "command: {s}", .{buildCommand(a, env, spec, probes, gen)})) catch {};
    if (problems.items.len > 0) {
        log("dry-run would refuse: {s}", .{std.mem.join(a, "; ", problems.items) catch ""});
        for (lines.items) |l| log("{s}", .{l});
        return 2;
    }
    log("dry-run: no build, no module file written", .{});
    for (lines.items) |l| log("{s}", .{l});
    return 0;
}

test "filterEbArgs and ensureRebuild" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const kept = filterEbArgs(a, &.{ "--", "x.eb", "--prefix", "/p", "--installpath=/i", "--bwrap", "--robot" });
    try std.testing.expectEqual(@as(usize, 2), kept.len);
    try std.testing.expectEqualStrings("--robot", kept[1]);
    const r = ensureRebuild(a, kept);
    try std.testing.expectEqualStrings("--rebuild", r[2]);
    try std.testing.expectEqual(@as(usize, 2), ensureRebuild(a, &.{ "x.eb", "-f" }).len);
}

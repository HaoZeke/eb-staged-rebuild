//! run, resume and rollback of a run journal, without a ledger: build into
//! a generation under the state root, check the live tree did not change,
//! plan each module, optionally run a publication-preparation program, then
//! exchange each staged tree with its live directory and write the module
//! files. Every step is journaled before it happens; a failure while
//! publishing puts the old live trees back.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const engine = @import("engine.zig");
const probe = @import("probe.zig");
const pyfmt = @import("pyfmt.zig");
const swap = @import("swap.zig");
const livecheck = @import("livecheck.zig");
const discover = @import("discover.zig");
const modulefile = @import("modulefile.zig");
const builders = @import("builders.zig");
const publish_mod = @import("publish.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const state = common.state;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

/// Refused and NeedsIntervention are the Python tool's own errors; Os is an
/// OSError it does not catch on the way up, which the command line reports
/// the same way (exit 2) but which leaves the journal as it was.
pub const E = error{ Refused, NeedsIntervention, Os, OutOfMemory };

fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

pub const Result = struct { code: u8, message: []const u8 = "" };

/// One run in progress: its journal, kept as the Python tool keeps it.
pub const R = struct {
    a: Allocator,
    env: probe.Env,
    ctx: *admin.Ctx,
    spec: engine.Spec,
    journal: Py = .{ .dict = &.{} },
    path: []const u8 = "",
    why: []const u8 = "",

    fn faults(r: *R) atomic.Faults {
        return r.ctx.faults();
    }

    fn fail(r: *R, comptime which_err: E, comptime f: []const u8, args: anytype) E {
        r.why = std.fmt.allocPrint(r.a, f, args) catch "out of memory";
        return which_err;
    }

    fn refuse(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.Refused, f, args);
    }

    fn osFail(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.Os, f, args);
    }

    fn intervene(r: *R, comptime f: []const u8, args: anytype) E {
        return r.fail(error.NeedsIntervention, f, args);
    }

    pub fn get(r: *R, k: []const u8) ?Py {
        return pyfmt.get(r.journal, k);
    }

    pub fn set(r: *R, k: []const u8, v: Py) void {
        r.journal = dictSet(r.a, r.journal, k, v);
    }

    fn str(r: *R, k: []const u8) []const u8 {
        const v = r.get(k) orelse return "";
        return if (v == .str) v.str else "";
    }

    fn list(r: *R, k: []const u8) []Py {
        const v = r.get(k) orelse return &.{};
        if (v != .list) return &.{};
        return @constCast(v.list);
    }

    fn setItem(r: *R, k: []const u8, i: usize, field: []const u8, v: Py) void {
        const items = r.a.dupe(Py, r.list(k)) catch return;
        items[i] = dictSet(r.a, items[i], field, v);
        r.set(k, .{ .list = items });
    }

    fn save(r: *R, step: []const u8) E!void {
        try saveJournal(r, r.path, step, r.faults());
    }

    fn saveSilently(r: *R, step: []const u8) E!void {
        try saveJournal(r, r.path, step, .{});
    }
};

fn dictSet(a: Allocator, d: Py, key: []const u8, value: Py) Py {
    var out: std.ArrayList(Py.Entry) = .empty;
    var found = false;
    if (d == .dict) for (d.dict) |e| {
        if (std.mem.eql(u8, e.key, key)) {
            out.append(a, .{ .key = e.key, .value = value }) catch {};
            found = true;
        } else out.append(a, e) catch {};
    };
    if (!found) out.append(a, .{ .key = key, .value = value }) catch {};
    return .{ .dict = out.items };
}

fn s(v: ?Py) []const u8 {
    const x = v orelse return "";
    return if (x == .str) x.str else "";
}

fn now() f64 {
    return admin.nowSeconds();
}

fn kind(p: []const u8) []const u8 {
    return main_mod.lstatKind(p);
}

fn isKind(p: []const u8, k: []const u8) bool {
    return std.mem.eql(u8, kind(p), k);
}

fn readBytes(r: *R, p: []const u8) E![]const u8 {
    const f = fsx.openRegular(fsx.cwd, p, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, p)});
    defer sys.close(f.fd);
    return fsx.readAll(r.a, f.fd, 1 << 30) catch |err| r.refuse("{s}", .{main_mod.osError(r.a, err, p)});
}

fn sha(a: Allocator, data: []const u8) []const u8 {
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(data, &out, .{});
    const hex = std.fmt.bytesToHex(out, .lower);
    return a.dupe(u8, &hex) catch "";
}

/// Reach `step`; a step a test made fail is refused with the text of
/// Python's OSError(EIO, "injected failure at STEP").
fn hit(r: *R, faults: atomic.Faults, step: []const u8) E!void {
    if (faults.fails(step)) return r.osFail("[Errno 5] injected failure at {s}", .{step});
}

fn write(r: *R, path: []const u8, data: []const u8, step: []const u8, metadata_from: ?[]const u8, faults: atomic.Faults) E!void {
    var why: atomic.Why = .{};
    // atomic_write raises Refused for what it refuses and OSError for what
    // the system refused; a failure a test injected carries OSError's text.
    atomic.atomicWrite(r.a, path, data, .{ .faults = faults, .step = step, .metadata_from = metadata_from }, &why) catch |err| {
        if (err == error.Refused and !std.mem.startsWith(u8, why.msg, "[Errno ")) return r.refuse("{s}", .{why.msg});
        return r.osFail("{s}", .{if (err == error.Refused) why.msg else main_mod.osError(r.a, err, path)});
    };
}

fn saveJournal(r: *R, path: []const u8, step: []const u8, faults: atomic.Faults) E!void {
    if (r.get("ledger_policy")) |lp| if (pyfmt.truthy(lp)) {
        r.set("schema", .{ .int = 4 });
        r.set("publication", Py.s("directory-swap"));
    } else {
        r.set("schema", .{ .int = 2 });
    } else r.set("schema", .{ .int = 2 });
    const p = r.get("publication");
    if (p == null or p.? == .null) r.set("publication", Py.s("directory-swap"));
    const body = json.dumps(r.a, r.journal, 2, true) catch return error.OutOfMemory;
    const raw = std.fmt.allocPrint(r.a, "{s}\n", .{body}) catch return error.OutOfMemory;
    try write(r, path, raw, step, null, faults);
}

fn joinCmd(a: Allocator, cmd: []const []const u8) []const u8 {
    return std.mem.join(a, " ", cmd) catch "";
}

fn pyList(a: Allocator, items: []const []const u8) Py {
    const out = a.alloc(Py, items.len) catch return .{ .list = &.{} };
    for (items, 0..) |x, i| out[i] = Py.s(x);
    return .{ .list = out };
}

fn nowStampRunId(a: Allocator) []const u8 {
    const t = now();
    const secs: u64 = @intFromFloat(@floor(t));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    return std.fmt.allocPrint(a, "{d:0>4}{d:0>2}{d:0>2}-{d:0>2}{d:0>2}{d:0>2}", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

fn runDir(a: Allocator, state_root: []const u8, run_id: []const u8) []const u8 {
    return std.fs.path.join(a, &.{ state_root, "runs", run_id }) catch "";
}

// ---- the environment eb runs with -------------------------------------------

/// child_env: the install-path and ghost-removal variables replaced, and the
/// live module tree first on MODULEPATH.
fn childEnv(a: Allocator, env: probe.Env, software: []const u8, modules: []const u8, live_modules: []const u8) common.proc.Envp {
    var b = common.env.Builder.fromEnv(a, common.env.Env.fromEnvp(env.envp)) catch return env.envp;
    for ([_][]const u8{ "EASYBUILD_INSTALLPATH", "EASYBUILD_PREFIX", "EASYBUILD_REMOVE_GHOST_INSTALL_DIRS", "EASYBUILD_INSTALLPATH_SOFTWARE", "EASYBUILD_INSTALLPATH_MODULES" }) |k| b.unset(k);
    b.set("EASYBUILD_INSTALLPATH_SOFTWARE", software) catch {};
    b.set("EASYBUILD_INSTALLPATH_MODULES", modules) catch {};
    const mp = b.get("MODULEPATH") orelse "";
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, mp, ':');
    var present = false;
    while (it.next()) |x| if (x.len > 0) {
        parts.append(a, x) catch {};
        if (std.mem.eql(u8, x, live_modules)) present = true;
    };
    if (!present) {
        const joined = if (parts.items.len > 0) std.fmt.allocPrint(a, "{s}:{s}", .{ live_modules, std.mem.join(a, ":", parts.items) catch "" }) catch live_modules else live_modules;
        b.set("MODULEPATH", joined) catch {};
    }
    return b.envp() catch env.envp;
}

fn resolveExe(a: Allocator, env: probe.Env, name: []const u8) ?[]const u8 {
    if (std.mem.indexOfScalar(u8, name, '/') != null) return admin.abspath(a, name);
    return common.proc.which(a, name, env.get("PATH") orelse "/bin:/usr/bin") catch null;
}

// ---- learning the module list ----------------------------------------------

/// software_rels_from_dry_run: ^\s*\*\s+\[[ xFR-]\]\s+.*\(module:\s*(.+)\)\s*$
fn relsFromDryRun(a: Allocator, text: []const u8) []const []const u8 {
    var rels: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, text, '\n');
    while (it.next()) |raw| {
        const line = std.mem.trimEnd(u8, raw, "\r");
        var i: usize = 0;
        while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
        if (i >= line.len or line[i] != '*') continue;
        i += 1;
        const ws = i;
        while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
        if (i == ws or i + 3 > line.len or line[i] != '[' or std.mem.indexOfScalar(u8, " xFR-", line[i + 1]) == null or line[i + 2] != ']') continue;
        i += 3;
        const ws2 = i;
        while (i < line.len and (line[i] == ' ' or line[i] == '\t')) i += 1;
        if (i == ws2) continue;
        // .*\(module:\s*(.+)\)\s*$ : the last "(module:" whose tail closes.
        const trimmed = std.mem.trimEnd(u8, line, " \t");
        if (trimmed.len == 0 or trimmed[trimmed.len - 1] != ')') continue;
        const at = std.mem.lastIndexOf(u8, trimmed[i..], "(module:") orelse continue;
        var field = trimmed[i + at + "(module:".len .. trimmed.len - 1];
        field = std.mem.trimStart(u8, field, " \t");
        if (field.len == 0) continue;
        field = std.mem.trim(u8, field, " \t");
        if (std.mem.indexOf(u8, field, " | ")) |bar| {
            const sub = std.mem.trim(u8, field[0..bar], " \t");
            const short = std.mem.trim(u8, field[bar + 3 ..], " \t");
            rels.append(a, std.fs.path.join(a, &.{ sub, short }) catch continue) catch {};
        } else rels.append(a, field) catch {};
    }
    return rels.items;
}

fn learnRels(r: *R) E![]const []const u8 {
    const a = r.a;
    var rels: std.ArrayList([]const u8) = .empty;
    for (r.spec.module_rels) |rel| if (!contains(rels.items, rel)) try rels.append(a, rel);
    var cmd: std.ArrayList([]const u8) = .empty;
    try cmd.appendSlice(a, &.{ r.spec.eb_executable, "--dry-run" });
    try cmd.appendSlice(a, engine.filterEbArgs(a, r.spec.eb_args));
    log("module preflight: {s}", .{joinCmd(a, cmd.items)});
    const envp = childEnv(a, r.env, r.spec.software, r.spec.modules, r.spec.modules);
    var found: []const []const u8 = &.{};
    var code: u32 = 0;
    var tail: []const u8 = "";
    if (resolveExe(a, r.env, r.spec.eb_executable)) |exe| {
        if (common.proc.run(a, exe, cmd.items, envp, .{ .stdout_limit = 256 << 20, .stderr_limit = 64 << 20, .timeout_ms = null, .new_session = false })) |res| {
            code = res.term.code();
            const both = try std.fmt.allocPrint(a, "{s}\n{s}", .{ res.stdout, res.stderr });
            found = relsFromDryRun(a, both);
            tail = std.mem.trim(u8, if (res.stderr.len > 0) res.stderr else res.stdout, " \t\n\r");
        } else |err| return r.refuse("{s}", .{main_mod.osError(a, err, r.spec.eb_executable)});
    } else return r.refuse("[Errno 2] No such file or directory: '{s}'", .{r.spec.eb_executable});
    if (code != 0 and std.mem.eql(u8, r.spec.backend, "bwrap") and found.len == 0 and rels.items.len == 0)
        return r.refuse("bwrap backend did not get a module list from eb --dry-run, so it will not mount on an unknown path (eb exited {d}). {s}", .{ code, tail });
    for (found) |rel| if (!contains(rels.items, rel)) try rels.append(a, rel);
    if (rels.items.len == 0) return r.refuse("run needs --module Name/version or an eb --dry-run module line; refusing to build for an unknown live path", .{});
    return rels.items;
}

fn contains(list: []const []const u8, x: []const u8) bool {
    for (list) |y| if (std.mem.eql(u8, x, y)) return true;
    return false;
}

fn symlinkComponent(a: Allocator, path: []const u8) ?[]const u8 {
    var current = admin.abspath(a, path);
    while (true) {
        if (isKind(current, "symlink")) return current;
        const parent = std.fs.path.dirname(current) orelse return null;
        if (std.mem.eql(u8, parent, current)) return null;
        current = parent;
    }
}

fn checkInstallDir(r: *R, rel: []const u8) E!void {
    const a = r.a;
    const path = admin.abspath(a, try std.fs.path.join(a, &.{ r.spec.software, rel }));
    if (std.mem.eql(u8, path, admin.abspath(a, r.spec.software)) or !admin.isWithin(a, path, r.spec.software))
        return r.refuse("module path escapes the software root: {s}", .{rel});
    if (isKind(path, "symlink"))
        return r.refuse("{s} is a symlink. Name/version must stay a real directory: bubblewrap 0.12 and later refuse to mount on a symlink, and bubblewrap 0.6.3 follows one. This tool depends on neither.", .{path});
    if (symlinkComponent(a, path)) |c| return r.refuse("{s}: {s} is a symlink. Name/version must stay a real directory", .{ rel, c });
    const k = kind(path);
    if (!std.mem.eql(u8, k, "dir")) return r.refuse("{s} is a {s}, not an existing rebuild directory", .{ path, k });
}

// ---- build ------------------------------------------------------------

fn removeGenerationModules(r: *R, generation_root: []const u8) E!void {
    const modules = try std.fs.path.join(r.a, &.{ admin.abspath(r.a, generation_root), "modules" });
    if (isKind(modules, "symlink")) return r.refuse("refusing to clear a symlinked module stage: {s}", .{modules});
    // remove_generation_tree: owned directories on one filesystem, each
    // opened 0700 when a sealed build left it without write or search.
    if (isKind(modules, "dir")) admin.removeTree(r.ctx, modules, admin.abspath(r.a, generation_root));
}

fn build(r: *R) E!u8 {
    const a = r.a;
    r.set("state", Py.s("building"));
    try r.save("journal.building");
    const gen = r.str("generation_root");
    atomic.makedirs(gen) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, gen)});
    try removeGenerationModules(r, gen);
    try hit(r, r.faults(), "build.start");
    const filtered = engine.ensureRebuild(a, engine.filterEbArgs(a, r.spec.eb_args));
    var cmd: std.ArrayList([]const u8) = .empty;
    var envp: common.proc.Envp = undefined;
    const software = try std.fs.path.join(a, &.{ gen, "software" });
    const modules = try std.fs.path.join(a, &.{ gen, "modules" });
    if (std.mem.eql(u8, r.spec.backend, "bwrap")) {
        try cmd.appendSlice(a, &.{ r.spec.eb_executable, "--experimental", "--bwrap", try std.fmt.allocPrint(a, "--bwrap-installpath={s}", .{gen}), "--bwrap-options=--die-with-parent" });
        try cmd.appendSlice(a, filtered);
        envp = childEnv(a, r.env, r.spec.software, r.spec.modules, r.spec.modules);
    } else {
        atomic.makedirs(software) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, software)});
        atomic.makedirs(modules) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, modules)});
        var inner: std.ArrayList([]const u8) = .empty;
        try inner.appendSlice(a, &.{ r.spec.eb_executable, try std.fmt.allocPrint(a, "--installpath-software={s}", .{software}), try std.fmt.allocPrint(a, "--installpath-modules={s}", .{modules}) });
        try inner.appendSlice(a, filtered);
        envp = childEnv(a, r.env, software, modules, r.spec.modules);
        if (std.mem.eql(u8, r.spec.backend, "userns")) {
            const probes = r.get("probes") orelse Py{ .dict = &.{} };
            const unshare = blk: {
                if (pyfmt.get(probes, "userns_sandbox")) |u| if (pyfmt.get(u, "path")) |p| if (p == .str and p.str.len > 0) break :blk p.str;
                break :blk resolveExe(a, r.env, "unshare") orelse "unshare";
            };
            try cmd.appendSlice(a, &.{ unshare, "--user", "--map-root-user", "--mount", "--fork", "--kill-child", selfExe(a), probe.sandbox_command, "--read-only", r.spec.software, "--read-only", r.spec.modules, "--" });
            try cmd.appendSlice(a, inner.items);
        } else try cmd.appendSlice(a, inner.items);
    }
    r.set("eb_command", pyList(a, cmd.items));
    log("running {s}", .{joinCmd(a, cmd.items)});
    const exe = resolveExe(a, r.env, cmd.items[0]) orelse return r.refuse("[Errno 2] No such file or directory: '{s}'", .{cmd.items[0]});
    const rc = builders.runCommand(a, exe, cmd.items, envp, r.faults(), "build.invoke") catch |err| switch (err) {
        error.Refused => return r.refuse("external commands require /proc to confirm their process group stopped", .{}),
        error.CannotStart => return r.refuse("could not start {s}", .{cmd.items[0]}),
        error.ExecFailed => if (builders.injected_at) |st|
            return r.refuse("[Errno 5] injected failure at {s}", .{st})
        else
            return r.refuse("[Errno {d}] {s}: '{s}'", .{ @backingInt(builders.exec_errno), builders.strerror(builders.exec_errno), cmd.items[0] }),
    };
    try hit(r, r.faults(), "build.finish");
    r.set("eb_returncode", .{ .int = rc });
    return rc;
}

fn selfExe(a: Allocator) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, "/proc/self/exe", &buf, buf.len }) catch return "/proc/self/exe";
    return a.dupe(u8, buf[0..n]) catch "/proc/self/exe";
}

fn enforceManifest(r: *R) E!void {
    if (r.spec.skip_live_check) return;
    const a = r.a;
    var changed: std.ArrayList([]const u8) = .empty;
    const manifests = r.get("manifests") orelse return;
    if (manifests != .dict) return;
    for (manifests.dict) |e| {
        const path = try std.fs.path.join(a, &.{ r.spec.software, e.key });
        if (std.mem.eql(u8, r.spec.backend, "bwrap")) {
            const ex = pyfmt.get(e.value, "exists");
            if (ex != null and ex.? == .bool and !ex.?.bool and livecheck.emptyRealDir(a, path))
                sys.unlinkat(fsx.cwd, path, linux.AT.REMOVEDIR) catch {};
        }
        const after = livecheck.snapshot(a, path) catch return r.refuse("cannot snapshot {s}", .{path});
        if (!livecheck.same(a, e.value, after)) try changed.append(a, e.key);
    }
    if (changed.items.len > 0) return r.refuse("live software directory changed during the build; not published: {s}", .{std.mem.join(a, ", ", changed.items) catch ""});
}

// ---- plan ---------------------------------------------------------------

fn stageRoots(r: *R, gen: []const u8) E!struct { software: []const u8, modules: []const u8, how: []const u8 } {
    const a = r.a;
    if (!std.mem.eql(u8, r.spec.backend, "bwrap"))
        return .{ .software = try std.fs.path.join(a, &.{ gen, "software" }), .modules = try std.fs.path.join(a, &.{ gen, "modules" }), .how = "installpath" };
    const info_path = try std.fs.path.join(a, &.{ gen, "bwrap_info.json" });
    if (!isKind(info_path, "file")) return r.refuse("bwrap backend finished without {s}; nothing was published", .{info_path});
    const raw = try readBytes(r, info_path);
    const parsed = json.parse(a, raw) catch return r.refuse("cannot read {s}: not valid JSON", .{info_path});
    const info = json.toPy(a, parsed) catch return r.refuse("cannot read {s}: not valid JSON", .{info_path});
    if (info != .dict) return r.refuse("bwrap_info.json is not an object", .{});
    const command = pyfmt.get(info, "bwrap_cmd");
    if (command) |c| if (c == .list) for (c.list) |x| if (x == .str and std.mem.eql(u8, x.str, "--overlay"))
        return r.refuse("bwrap used overlayfs for this install; the stage directory is an upper layer, not a complete generation, so it was not published", .{});
    var software = s(pyfmt.get(info, "bwrap_installpath_software"));
    var modules = s(pyfmt.get(info, "bwrap_installpath_modules"));
    var how: []const u8 = "bwrap_info";
    if (software.len == 0 or modules.len == 0) {
        const probes = r.get("probes") orelse Py{ .dict = &.{} };
        const eb = pyfmt.get(probes, "easybuild") orelse Py{ .dict = &.{} };
        const v = pyfmt.get(eb, "framework_version");
        var legacy = false;
        if (v) |vv| if (vv == .list and vv.list.len == 3) {
            const x = vv.list;
            legacy = x[0] == .int and x[1] == .int and x[2] == .int and x[0].int == 5 and x[1].int == 3 and (x[2].int == 0 or x[2].int == 1);
        };
        if (!legacy) {
            var keys: std.ArrayList([]const u8) = .empty;
            for (info.dict) |e| try keys.append(a, e.key);
            std.mem.sort([]const u8, keys.items, {}, struct {
                fn lt(_: void, x: []const u8, y: []const u8) bool {
                    return json.pyTextLess(x, y);
                }
            }.lt);
            const shown = if (keys.items.len > 0) try std.mem.join(a, ", ", keys.items) else "none";
            return r.refuse("bwrap_info.json has no bwrap_installpath_software (keys: {s}). EasyBuild 5.3.0 and 5.3.1 omit it; this version was not identified as either, so the stage was kept and not published", .{shown});
        }
        const root = if (s(pyfmt.get(info, "bwrap_installpath")).len > 0) s(pyfmt.get(info, "bwrap_installpath")) else gen;
        software = try std.fs.path.join(a, &.{ root, "software" });
        modules = try std.fs.path.join(a, &.{ root, "modules" });
        how = "easybuild-5.3-layout";
    }
    for ([_][2][]const u8{ .{ "software", software }, .{ "modules", modules } }) |p| {
        if (!admin.isWithin(a, p[1], gen)) return r.refuse("bwrap stage {s} path escapes this run: {s}", .{ p[0], p[1] });
    }
    return .{ .software = software, .modules = modules, .how = how };
}

fn withWritableDir(r: *R, dir: []const u8) E!?u32 {
    const st = fsx.statAt(fsx.cwd, dir, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, dir)});
    const mode = st.perm();
    if (mode & 0o200 != 0) return null;
    chmod(dir, mode | 0o300);
    return mode;
}

fn chmod(p: []const u8, mode: u32) void {
    var zb: [sys.path_max:0]u8 = undefined;
    const z = sys.z(&zb, p) catch return;
    _ = linux.fchmodat(linux.AT.FDCWD, z.ptr, @intCast(mode));
}

fn plan(r: *R) E!void {
    const a = r.a;
    const gen = r.str("generation_root");
    const roots = try stageRoots(r, gen);
    r.set("stage_software", Py.s(admin.abspath(a, roots.software)));
    r.set("stage_modules", Py.s(admin.abspath(a, roots.modules)));
    r.set("stage_how", Py.s(roots.how));
    if (!isKind(roots.software, "dir")) return r.refuse("stage software tree is not a real directory: {s}", .{roots.software});
    var why: []const u8 = "";
    const tree = discover.walkModuleTree(a, roots.modules, &why) catch |err| return if (err == error.Refused) r.refuse("{s}", .{why}) else error.OutOfMemory;
    if (tree.files.len == 0) return r.refuse("no module files under {s}", .{roots.modules});
    const ordered = discover.orderModuleFiles(a, tree.files, roots.modules) catch return r.refuse("cannot read the staged module files", .{});
    const Draft = struct { rel: []const u8, staged: []const u8, live: []const u8, retired: []const u8, text: []const u8, live_module: []const u8, path: []const u8 };
    var drafted: std.ArrayList(Draft) = .empty;
    const run_id = r.str("run_id");
    for (ordered.order) |path| {
        const rel = modulefile.relpath(a, path, roots.modules);
        const inst = discover.swapInstallFor(a, ordered.texts.get(path).?, roots.software, r.spec.software, &why) catch |err| return if (err == error.Refused) r.refuse("{s}", .{why}) else error.OutOfMemory;
        if (!generationComplete(a, inst.staged)) return r.refuse("{s} is not a complete staged installation", .{inst.staged});
        if (isKind(inst.staged, "symlink")) return r.refuse("staged installation {s} is a symlink", .{inst.staged});
        if (isKind(inst.live, "symlink")) return r.refuse("{s} is a symlink. Name/version must stay a real directory", .{inst.live});
        if (!isKind(inst.live, "dir")) return r.refuse("staged rebuild target must already be a real directory: {s}", .{inst.live});
        const retired = try std.fs.path.join(a, &.{ std.fs.path.dirname(inst.live) orelse "/", try std.fmt.allocPrint(a, ".{s}.{s}", .{ std.fs.path.basename(inst.live), run_id }) });
        if (!isKind(retired, "missing")) return r.refuse("retired path already exists: {s}", .{retired});
        const live_module = try std.fs.path.join(a, &.{ r.spec.modules, rel });
        if (!admin.isWithin(a, live_module, r.spec.modules)) return r.refuse("module path escapes the module root: {s}", .{rel});
        if (isKind(live_module, "symlink")) return r.refuse("refusing to replace module symlink {s}. The module file that is the pointer must be a regular file", .{live_module});
        if (symlinkComponent(a, live_module)) |c| return r.refuse("{s} is a symlink", .{c});
        const lk = kind(live_module);
        if (!std.mem.eql(u8, lk, "missing") and !std.mem.eql(u8, lk, "file")) return r.refuse("{s} is not a regular file", .{live_module});
        try drafted.append(a, .{ .rel = rel, .staged = inst.staged, .live = inst.live, .retired = retired, .text = inst.text, .live_module = live_module, .path = path });
    }
    const rdir = runDir(a, r.spec.state_root, run_id);
    var plan_items: std.ArrayList(Py) = .empty;
    for (drafted.items) |d| {
        const prepared = try std.fs.path.join(a, &.{ rdir, "prepared", d.rel });
        const metadata_source = if (isKind(d.live_module, "file")) d.live_module else d.path;
        try write(r, prepared, d.text, try std.fmt.allocPrint(a, "prepare:{s}", .{d.rel}), metadata_source, r.faults());
        const stamp = Py{ .dict = &.{
            .{ .key = "run_id", .value = Py.s(run_id) },
            .{ .key = "staged", .value = Py.s(d.staged) },
            .{ .key = "live", .value = Py.s(d.live) },
            .{ .key = "module", .value = Py.s(d.rel) },
            .{ .key = "sha256", .value = Py.s(sha(a, d.text)) },
        } };
        const stamp_text = try std.fmt.allocPrint(a, "{s}\n", .{try json.dumps(a, stamp, 2, true)});
        const sealed = try withWritableDir(r, d.staged);
        const res = write(r, try std.fs.path.join(a, &.{ d.staged, ".eb-staged-rebuild-complete" }), stamp_text, try std.fmt.allocPrint(a, "stamp:{s}", .{d.rel}), null, r.faults());
        if (sealed) |m| chmod(d.staged, m);
        try res;
        const existed = isKind(d.live_module, "file");
        var old_hash: Py = .null;
        var previous_root: Py = .null;
        if (existed) {
            const old = try readBytes(r, d.live_module);
            old_hash = Py.s(sha(a, old));
            if (!std.unicode.utf8ValidateSlice(old)) return r.refuse("{s} is not UTF-8 text", .{d.live_module});
            if (modulefile.parseRoot(old)) |pr| if (pr.path.len > 0) {
                previous_root = Py.s(pr.path);
                if (!std.mem.eql(u8, admin.abspath(a, pr.path), admin.abspath(a, d.live)))
                    return r.refuse("live module {s} points at {s}, not its stable install path {s}", .{ d.live_module, pr.path, d.live });
            };
        }
        try plan_items.append(a, .{ .dict = try a.dupe(Py.Entry, &.{
            .{ .key = "rel", .value = Py.s(d.rel) },
            .{ .key = "live_module", .value = Py.s(d.live_module) },
            .{ .key = "prepared", .value = Py.s(prepared) },
            .{ .key = "staged", .value = Py.s(d.staged) },
            .{ .key = "live_software", .value = Py.s(d.live) },
            .{ .key = "retired", .value = Py.s(d.retired) },
            .{ .key = "backup", .value = Py.s(try std.fs.path.join(a, &.{ rdir, "backups", d.rel })) },
            .{ .key = "new_hash", .value = Py.s(sha(a, d.text)) },
            .{ .key = "old_hash", .value = old_hash },
            .{ .key = "previous_root", .value = previous_root },
            .{ .key = "existed", .value = .{ .bool = existed } },
            .{ .key = "status", .value = Py.s("pending") },
            .{ .key = "swap_method", .value = .null },
            .{ .key = "published_at", .value = .null },
        }) });
    }
    var link_items: std.ArrayList(Py) = .empty;
    for (tree.links) |l| {
        try link_items.append(a, .{ .dict = try a.dupe(Py.Entry, &.{
            .{ .key = "rel", .value = Py.s(l.rel) },
            .{ .key = "live_path", .value = Py.s(try std.fs.path.join(a, &.{ r.spec.modules, l.rel })) },
            .{ .key = "target", .value = Py.s(discover.mapLinkTarget(a, l.target, roots.modules, r.spec.modules)) },
            .{ .key = "status", .value = Py.s("pending") },
            .{ .key = "created", .value = .{ .bool = false } },
        }) });
    }
    r.set("modules_plan", .{ .list = plan_items.items });
    r.set("links_plan", .{ .list = link_items.items });
}

fn generationComplete(a: Allocator, path: []const u8) bool {
    if (!isKind(path, "dir")) return false;
    for ([_][]const u8{ "PAYLOAD", ".eb-staged-rebuild-complete" }) |n| if (isKind(std.fs.path.join(a, &.{ path, n }) catch return false, "file")) return true;
    return installCompleteMeta(a, path);
}

fn installCompleteMeta(a: Allocator, path: []const u8) bool {
    const meta = std.fs.path.join(a, &.{ path, "easybuild" }) catch return false;
    if (!isKind(meta, "dir")) return false;
    const fd = fsx.openDir(fsx.cwd, meta, false) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    while (it.next() catch null) |e| {
        if (std.mem.endsWith(u8, e.name, ".eb") and isKind(std.fs.path.join(a, &.{ meta, e.name }) catch continue, "file")) return true;
    }
    return false;
}

/// install_complete: PAYLOAD, or a readable easybuild/*.eb; the tool's own
/// stamp is not build evidence.
fn installComplete(a: Allocator, path: []const u8) bool {
    if (!isKind(path, "dir")) return false;
    if (isKind(std.fs.path.join(a, &.{ path, "PAYLOAD" }) catch return false, "file")) return true;
    return installCompleteMeta(a, path);
}

// ---- publish --------------------------------------------------------------

fn treeRunId(a: Allocator, path: []const u8) ?[]const u8 {
    const stamp = std.fs.path.join(a, &.{ path, ".eb-staged-rebuild-complete" }) catch return null;
    if (!isKind(stamp, "file")) return null;
    const f = fsx.openRegular(fsx.cwd, stamp, true) catch return null;
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 1 << 20) catch return null;
    const v = json.toPy(a, json.parse(a, raw) catch return null) catch return null;
    const id = pyfmt.get(v, "run_id") orelse return null;
    return if (id == .str) id.str else null;
}

fn allowExchange(r: *R) bool {
    const probes = r.get("probes") orelse return false;
    const ex = pyfmt.get(probes, "rename_exchange") orelse return false;
    return pyfmt.truthy(pyfmt.get(ex, "ok"));
}

fn swapPair(r: *R, live: []const u8, retired: []const u8, faults: atomic.Faults, step: []const u8) E![]const u8 {
    var why: swap.Why = .{};
    const m = swap.swapDirectories(r.a, live, retired, allowExchange(r), faults, step, &why) catch |err| return switch (err) {
        error.Refused => r.refuse("{s}", .{why.msg}),
        error.NeedsIntervention => r.intervene("{s}", .{why.msg}),
        error.Failed => r.refuse("{s}", .{why.msg}),
    };
    return @tagName(m);
}

fn recoverPair(r: *R, live: []const u8, retired: []const u8) E!void {
    var why: swap.Why = .{};
    swap.recoverInterrupted(r.a, live, retired, &why) catch |err| return switch (err) {
        error.Refused, error.Failed => r.refuse("{s}", .{why.msg}),
        error.NeedsIntervention => r.intervene("{s}", .{why.msg}),
    };
}

fn sameAccess(a: Allocator, left: []const u8, right: []const u8) bool {
    var why: atomic.Why = .{};
    const x = atomic.readMetadata(a, left, &why) catch return false;
    const y = atomic.readMetadata(a, right, &why) catch return false;
    return x.info.uid == y.info.uid and x.info.gid == y.info.gid and x.info.perm() == y.info.perm() and x.acls.eql(y.acls);
}

fn fileHash(r: *R, p: []const u8) E![]const u8 {
    return sha(r.a, try readBytes(r, p));
}

fn prepareBackup(r: *R, m: Py) E!void {
    const a = r.a;
    const live = s(pyfmt.get(m, "live_module"));
    const rel = s(pyfmt.get(m, "rel"));
    const k = kind(live);
    if (std.mem.eql(u8, k, "symlink")) return r.refuse("refusing to replace module symlink {s}", .{live});
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        if (!std.mem.eql(u8, k, "file")) return r.intervene("{s} disappeared before publish", .{live});
        const current = try readBytes(r, live);
        const current_hash = sha(a, current);
        const backup = s(pyfmt.get(m, "backup"));
        if (std.mem.eql(u8, current_hash, s(pyfmt.get(m, "new_hash"))) and isKind(backup, "file")) {
            if (!std.mem.eql(u8, try fileHash(r, backup), s(pyfmt.get(m, "old_hash")))) return r.intervene("backup for {s} does not match the previous module file", .{rel});
            return;
        }
        if (!std.mem.eql(u8, current_hash, s(pyfmt.get(m, "old_hash")))) return r.intervene("{s} changed after the build", .{live});
        if (isKind(backup, "missing")) {
            try write(r, backup, current, try std.fmt.allocPrint(a, "publish.backup:{s}", .{rel}), live, r.faults());
        } else if (!isKind(backup, "file") or !std.mem.eql(u8, try fileHash(r, backup), s(pyfmt.get(m, "old_hash")))) {
            return r.intervene("backup for {s} does not match the previous module file", .{rel});
        }
    } else if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        return;
    } else if (!std.mem.eql(u8, k, "missing")) {
        return r.intervene("new module path appeared before publish: {s}", .{live});
    }
}

fn moveSealed(r: *R, source: []const u8, destination: []const u8) E!void {
    const st = fsx.statAt(fsx.cwd, source, true) catch |err| return r.refuse("{s}", .{main_mod.osError(r.a, err, source)});
    const mode = st.perm();
    if (mode & 0o200 == 0) chmod(source, mode | 0o300);
    sys.renameat(fsx.cwd, source, fsx.cwd, destination) catch |err| {
        if (mode & 0o200 == 0) chmod(source, mode);
        if (err == error.CrossDevice) return r.refuse("the stage and live software tree are on different filesystems; place --state-root beside the software tree", .{});
        return r.refuse("{s}", .{main_mod.osError(r.a, err, source)});
    };
    if (mode & 0o200 == 0) chmod(destination, mode);
}

fn prepareSwapSource(r: *R, m: Py, run_id: []const u8) E!void {
    const staged = s(pyfmt.get(m, "staged"));
    const retired = s(pyfmt.get(m, "retired"));
    const sk = kind(staged);
    const rk = kind(retired);
    if (std.mem.eql(u8, sk, "dir") and std.mem.eql(u8, rk, "missing")) {
        try hit(r, r.faults(), try std.fmt.allocPrint(r.a, "stage.move:{s}", .{s(pyfmt.get(m, "rel"))}));
        try moveSealed(r, staged, retired);
        atomic.fsyncDir(std.fs.path.dirname(retired) orelse "/") catch {};
    } else if (!(std.mem.eql(u8, sk, "missing") and std.mem.eql(u8, rk, "dir"))) {
        return r.intervene("cannot prepare {s}: staged={s}, retired={s}", .{ s(pyfmt.get(m, "rel")), sk, rk });
    }
    // After an exchange the journal did not record, the run's tree is live
    // and the retired name holds the old tree; the baseline step swaps them
    // back.
    const tid = treeRunId(r.a, retired);
    const lid = treeRunId(r.a, s(pyfmt.get(m, "live_software")));
    const retired_ours = tid != null and std.mem.eql(u8, tid.?, run_id);
    const live_ours = lid != null and std.mem.eql(u8, lid.?, run_id);
    if (!retired_ours and !live_ours) return r.intervene("prepared tree does not belong to run {s}: {s}", .{ run_id, retired });
}

fn writeNewModule(r: *R, m: Py, faults: atomic.Faults) E!void {
    const a = r.a;
    const rel = s(pyfmt.get(m, "rel"));
    const prepared = try readBytes(r, s(pyfmt.get(m, "prepared")));
    if (!std.mem.eql(u8, sha(a, prepared), s(pyfmt.get(m, "new_hash")))) return r.intervene("prepared module bytes for {s} changed", .{rel});
    const live = s(pyfmt.get(m, "live_module"));
    const k = kind(live);
    if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        if (sameAccess(a, live, s(pyfmt.get(m, "prepared")))) return;
    }
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        if (!std.mem.eql(u8, k, "file") or !std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "old_hash"))))
            return r.intervene("{s} is not the previous module file", .{live});
    } else if (!std.mem.eql(u8, k, "missing")) {
        return r.intervene("{s} appeared before it could be published", .{live});
    }
    try write(r, live, prepared, try std.fmt.allocPrint(a, "publish.file:{s}", .{rel}), s(pyfmt.get(m, "prepared")), faults);
}

fn restoreOldModule(r: *R, m: Py, faults: atomic.Faults) E!void {
    const a = r.a;
    const rel = s(pyfmt.get(m, "rel"));
    const live = s(pyfmt.get(m, "live_module"));
    const k = kind(live);
    if (pyfmt.truthy(pyfmt.get(m, "existed"))) {
        const backup = s(pyfmt.get(m, "backup"));
        if (!isKind(backup, "file")) return r.intervene("no backup to restore for {s}", .{rel});
        const previous = try readBytes(r, backup);
        if (!std.mem.eql(u8, sha(a, previous), s(pyfmt.get(m, "old_hash")))) return r.intervene("backup for {s} changed", .{rel});
        if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "old_hash")))) {
            // The backup carries the previous access policy; the same bytes
            // under a published policy still have to go back.
            if (sameAccess(a, live, backup)) return;
        }
        if (!std.mem.eql(u8, k, "file") or !std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash"))))
            return r.intervene("{s} is neither the old nor new module file", .{live});
        try write(r, live, previous, try std.fmt.allocPrint(a, "restore.file:{s}", .{rel}), backup, faults);
        return;
    }
    if (std.mem.eql(u8, k, "missing")) return;
    if (std.mem.eql(u8, k, "file") and std.mem.eql(u8, try fileHash(r, live), s(pyfmt.get(m, "new_hash")))) {
        try hit(r, faults, try std.fmt.allocPrint(a, "restore.remove:{s}", .{rel}));
        sys.unlinkat(fsx.cwd, live, 0) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, live)});
        atomic.fsyncDir(std.fs.path.dirname(live) orelse "/") catch {};
        return;
    }
    return r.intervene("{s} is not the module file this run created", .{live});
}

fn readlinkText(a: Allocator, p: []const u8) []const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = sys.z(&zb, p) catch return "";
    const n = sys.call(linux.readlinkat, .{ linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch return "";
    return a.dupe(u8, buf[0..n]) catch "";
}

fn createLinks(r: *R, faults: atomic.Faults) E!void {
    const a = r.a;
    for (r.list("links_plan"), 0..) |l, i| {
        const path = s(pyfmt.get(l, "live_path"));
        const target = s(pyfmt.get(l, "target"));
        const k = kind(path);
        if (std.mem.eql(u8, k, "symlink") and std.mem.eql(u8, readlinkText(a, path), target)) {
            r.setItem("links_plan", i, "status", Py.s("present"));
            continue;
        }
        if (!std.mem.eql(u8, k, "missing")) {
            r.setItem("links_plan", i, "status", Py.s("kept"));
            continue;
        }
        const parent = std.fs.path.dirname(path) orelse "/";
        if (symlinkComponent(a, parent)) |c| return r.refuse("{s} is a symlink", .{c});
        atomic.makedirs(parent) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, parent)});
        try hit(r, faults, try std.fmt.allocPrint(a, "links.create:{s}", .{s(pyfmt.get(l, "rel"))}));
        var zt: [sys.path_max:0]u8 = undefined;
        var zp: [sys.path_max:0]u8 = undefined;
        const zt_ = sys.z(&zt, target) catch return r.refuse("bad link target", .{});
        const zp_ = sys.z(&zp, path) catch return r.refuse("bad link path", .{});
        if (linux.errno(linux.symlinkat(zt_.ptr, linux.AT.FDCWD, zp_.ptr)) != .SUCCESS) return r.refuse("cannot create {s}", .{path});
        r.setItem("links_plan", i, "created", .{ .bool = true });
        r.setItem("links_plan", i, "status", Py.s("created"));
    }
}

fn removeCreatedLinks(r: *R) void {
    const links = r.list("links_plan");
    var i = links.len;
    while (i > 0) {
        i -= 1;
        const l = r.list("links_plan")[i];
        if (!pyfmt.truthy(pyfmt.get(l, "created"))) continue;
        const path = s(pyfmt.get(l, "live_path"));
        if (isKind(path, "symlink") and std.mem.eql(u8, readlinkText(r.a, path), s(pyfmt.get(l, "target")))) {
            sys.unlinkat(fsx.cwd, path, 0) catch {};
            atomic.fsyncDir(std.fs.path.dirname(path) orelse "/") catch {};
        }
        r.setItem("links_plan", i, "created", .{ .bool = false });
        r.setItem("links_plan", i, "status", Py.s("pending"));
    }
}

fn normalizeBaseline(r: *R) E!void {
    const run_id = r.str("run_id");
    const mods = r.list("modules_plan");
    var i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        const lr = treeRunId(r.a, live);
        const rr = treeRunId(r.a, retired);
        const live_is = lr != null and std.mem.eql(u8, lr.?, run_id);
        const retired_is = rr != null and std.mem.eql(u8, rr.?, run_id);
        if (live_is and !retired_is) {
            _ = try swapPair(r, live, retired, .{}, try std.fmt.allocPrint(r.a, "recover:{s}", .{s(pyfmt.get(m, "rel"))}));
        } else if (!(!live_is and retired_is)) {
            return r.intervene("cannot identify old and staged trees for {s}: live run={s}, retired run={s}", .{ s(pyfmt.get(m, "rel")), reprOpt(r.a, lr), reprOpt(r.a, rr) });
        }
        try restoreOldModule(r, m, .{});
        r.setItem("modules_plan", i, "status", Py.s("pending"));
        r.setItem("modules_plan", i, "swap_method", .null);
        r.setItem("modules_plan", i, "published_at", .null);
    }
    removeCreatedLinks(r);
}

fn reprOpt(a: Allocator, v: ?[]const u8) []const u8 {
    const x = v orelse return "None";
    var out: std.ArrayList(u8) = .empty;
    pyfmt.reprStr(&out, a, x) catch return x;
    return out.items;
}

fn validatePublicationSet(r: *R) E!void {
    for (r.list("modules_plan")) |m| {
        const rel = s(pyfmt.get(m, "rel"));
        const prepared = s(pyfmt.get(m, "prepared"));
        const f = fsx.openRegular(fsx.cwd, prepared, true) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, main_mod.osError(r.a, err, prepared) });
        defer sys.close(f.fd);
        const bytes = fsx.readAll(r.a, f.fd, 1 << 30) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, main_mod.osError(r.a, err, prepared) });
        if (!std.mem.eql(u8, sha(r.a, bytes), s(pyfmt.get(m, "new_hash")))) return r.intervene("prepared module bytes for {s} changed", .{rel});
        var why: atomic.Why = .{};
        _ = atomic.readMetadata(r.a, prepared, &why) catch |err| return r.refuse("cannot validate publication for {s}: {s}", .{ rel, if (err == error.Refused) why.msg else main_mod.osError(r.a, err, prepared) });
    }
}

fn publish(r: *R) E!void {
    const a = r.a;
    for (r.list("modules_plan")) |m| {
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        if (live.len > 0 and retired.len > 0) try recoverPair(r, live, retired);
    }
    try validatePublicationSet(r);
    r.set("state", Py.s("publishing"));
    try r.save("journal.publishing");
    const run_id = r.str("run_id");
    for (r.list("modules_plan")) |m| {
        try prepareBackup(r, m);
        try prepareSwapSource(r, m, run_id);
    }
    try r.save("journal.prepared");
    publishSteps(r) catch |err| {
        const first = r.why;
        const kind_err = err;
        removeCreatedLinks(r);
        normalizeBaseline(r) catch {
            return r.intervene("publish failed and the old live tree could not be restored: {s}", .{r.why});
        };
        r.set("state", Py.s("built"));
        r.set("message", Py.s("publish interrupted; old live tree restored"));
        r.saveSilently("journal.restored") catch {};
        r.why = first;
        return kind_err;
    };
    _ = a;
}

fn publishSteps(r: *R) E!void {
    const a = r.a;
    try normalizeBaseline(r);
    for (r.list("modules_plan"), 0..) |m, i| {
        const rel = s(pyfmt.get(m, "rel"));
        const method = try swapPair(r, s(pyfmt.get(m, "live_software")), s(pyfmt.get(m, "retired")), r.faults(), try std.fmt.allocPrint(a, "swap:{s}", .{rel}));
        r.setItem("modules_plan", i, "status", Py.s("swapped"));
        r.setItem("modules_plan", i, "swap_method", Py.s(method));
        r.setItem("modules_plan", i, "published_at", .{ .float = now() });
        log("swapped {s} ({s})", .{ rel, method });
        try r.save(try std.fmt.allocPrint(a, "journal.swap:{s}", .{rel}));
    }
    for (r.list("modules_plan"), 0..) |m, i| {
        const rel = s(pyfmt.get(m, "rel"));
        try writeNewModule(r, m, r.faults());
        r.setItem("modules_plan", i, "status", Py.s("published"));
        log("published module {s}", .{rel});
        try r.save(try std.fmt.allocPrint(a, "journal.module:{s}", .{rel}));
    }
    try createLinks(r, r.faults());
    r.set("state", Py.s("complete"));
    r.set("completed_at", .{ .float = now() });
    r.set("message", Py.s("published"));
    try r.save("journal.complete");
}

// ---- preparation and the run as a whole ------------------------------------

fn preparationFailure(r: *R, message: []const u8, code: u8) E!u8 {
    r.set("message", Py.s(message));
    r.set("prepare_returncode", .{ .int = code });
    r.set("updated_at", .{ .float = now() });
    try r.save("journal.prepare_failed");
    log("{s}", .{message});
    return code;
}

fn preparePublication(r: *R) E!u8 {
    const a = r.a;
    const exe = r.spec.prepare_executable orelse return 0;
    if (exe.len == 0 or pyfmt.truthy(r.get("prepare_complete"))) return 0;
    const context_path = try std.fs.path.join(a, &.{ runDir(a, r.spec.state_root, r.str("run_id")), "prepare-context.json" });
    var mods: std.ArrayList(Py) = .empty;
    for (r.list("modules_plan")) |m| {
        var entries: std.ArrayList(Py.Entry) = .empty;
        for ([_][]const u8{ "rel", "staged", "live_software", "retired", "live_module", "prepared" }) |k| try entries.append(a, .{ .key = k, .value = pyfmt.get(m, k) orelse .null });
        try entries.append(a, .{ .key = "generation", .value = pyfmt.get(m, "staged") orelse .null });
        try mods.append(a, .{ .dict = entries.items });
    }
    const context = Py{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "run_id", .value = Py.s(r.str("run_id")) },
        .{ .key = "publication", .value = Py.s("directory-swap") },
        .{ .key = "generation_root", .value = Py.s(r.str("generation_root")) },
        .{ .key = "state_root", .value = Py.s(r.spec.state_root) },
        .{ .key = "live_software", .value = Py.s(r.spec.software) },
        .{ .key = "live_modules", .value = Py.s(r.spec.modules) },
        .{ .key = "stage_software", .value = Py.s(r.str("stage_software")) },
        .{ .key = "stage_modules", .value = Py.s(r.str("stage_modules")) },
        .{ .key = "modules", .value = .{ .list = mods.items } },
    }) };
    try write(r, context_path, try std.fmt.allocPrint(a, "{s}\n", .{try json.dumps(a, context, 2, true)}), "prepare.context", null, r.faults());
    log("preparing publication permissions: {s} {s}", .{ exe, context_path });
    const rc = builders.runCommand(a, exe, &.{ exe, context_path }, r.env.envp, r.faults(), "prepare.invoke") catch |err| {
        if (err == error.Refused) return r.refuse("external commands require /proc to confirm their process group stopped", .{});
        if (builders.injected_at) |st| return preparationFailure(r, try std.fmt.allocPrint(a, "publication preparation failed: [Errno 5] injected failure at {s}", .{st}), 2);
        const e: linux.E = if (err == error.ExecFailed) builders.exec_errno else .NOENT;
        return preparationFailure(r, try std.fmt.allocPrint(a, "publication preparation failed: [Errno {d}] {s}: '{s}'", .{ @backingInt(e), builders.strerror(e), exe }), 2);
    };
    r.set("prepare_returncode", .{ .int = rc });
    if (rc != 0) return preparationFailure(r, try std.fmt.allocPrint(a, "publication preparation exited {d}; the live directory was not swapped", .{rc}), rc);
    for (r.list("modules_plan")) |m| {
        if (!installComplete(a, s(pyfmt.get(m, "staged"))))
            return preparationFailure(r, try std.fmt.allocPrint(a, "publication preparation left {s} incomplete at {s}; the live directory was not swapped", .{ s(pyfmt.get(m, "rel")), s(pyfmt.get(m, "staged")) }), 2);
    }
    validatePublicationSet(r) catch |err| switch (err) {
        error.Refused, error.NeedsIntervention => return preparationFailure(r, r.why, 2),
        else => |e| return e,
    };
    r.set("message", Py.s(""));
    r.set("prepare_complete", .{ .bool = true });
    try r.save("journal.prepared");
    return 0;
}

fn failRun(r: *R, message: []const u8, code: u8) E!Result {
    r.set("state", Py.s("failed"));
    r.set("message", Py.s(message));
    r.set("updated_at", .{ .float = now() });
    try r.save("journal.failed");
    log("{s}", .{message});
    return .{ .code = code, .message = message };
}

/// _publisher: the ledger configuration a journal recorded, checked against
/// the file it came from; null without a ledger.
fn publisher(r: *R) E!?Py {
    const path = r.str("ledger_config");
    if (path.len == 0) return null;
    const policy = r.get("ledger_policy");
    if (policy == null or policy.? == .null) return r.refuse("ledger policy is missing from the journal; inspect the publication before resuming", .{});
    var why: []const u8 = "";
    const now_config = publish_mod.loadConfig(r.a, path, &why) catch |err| return switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => r.refuse("{s}", .{why}),
    };
    if (!publish_mod.sameValue(r.a, now_config, policy.?)) return r.refuse("ledger policy changed; restore the recorded configuration before resuming", .{});
    return policy.?;
}

fn record(r: *R, config: Py, recs_in: []const Py, step: ?[]const u8) E![]const Py {
    const rdir = runDir(r.a, r.str("state_root"), r.str("run_id"));
    var why: []const u8 = "";
    if (recs_in.len > 0) publish_mod.enqueue(r.a, rdir, recs_in, r.faults(), step.?, &why) catch |err| return switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        error.NeedsIntervention => r.intervene("{s}", .{why}),
        error.Refused => r.refuse("{s}", .{why}),
    };
    return publish_mod.deliver(r.a, r.env.envp, rdir, config, r.faults(), &why) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        error.NeedsIntervention => r.intervene("{s}", .{why}),
        error.Refused => r.refuse("{s}", .{why}),
    };
}

fn records(r: *R, config: Py, stage: []const u8, corrects: ?*const publish_mod.Corrects) E![]const Py {
    var why: []const u8 = "";
    return publish_mod.publicationRecords(r.a, config, r.journal, stage, now(), corrects, &why) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => r.refuse("{s}", .{why}),
    };
}

fn required(config: Py) bool {
    return pyfmt.truthy(pyfmt.get(config, "required"));
}

fn recordFinished(r: *R, config: ?Py, message: []const u8) E!Result {
    if (config) |c| {
        const left = try record(r, c, try records(r, c, "finished", null), "outbox.finished");
        if (left.len > 0) {
            const m = try std.fmt.allocPrint(r.a, "{s}; {s}", .{ message, publish_mod.pendingMessage(r.a, left) });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    }
    return .{ .code = 0, .message = message };
}

/// _publish_recorded: swap only after a required ledger acknowledged every
/// started record.
fn publishRecorded(r: *R) E!Result {
    const rc = try preparePublication(r);
    if (rc != 0) return .{ .code = rc, .message = r.str("message") };
    const config = try publisher(r);
    if (config) |c| {
        try validatePublicationSet(r);
        const left = try record(r, c, try records(r, c, "started", null), "outbox.started");
        if (left.len > 0 and required(c)) {
            const m = try std.fmt.allocPrint(r.a, "the live directory was not swapped: {s}", .{publish_mod.pendingMessage(r.a, left)});
            r.set("message", Py.s(m));
            try r.save("journal.ledger_pending");
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    }
    try publish(r);
    const message = try std.fmt.allocPrint(r.a, "run {s} published {d} module(s)", .{ r.str("run_id"), r.list("modules_plan").len });
    log("{s}", .{message});
    return recordFinished(r, config, message);
}

fn runLocked(r: *R, fresh_build: bool) E!Result {
    if (fresh_build) {
        const rc = try build(r);
        if (rc != 0) return failRun(r, try std.fmt.allocPrint(r.a, "eb exited {d}; live modules were not changed", .{rc}), rc);
        enforceManifest(r) catch |err| switch (err) {
            error.Refused => return failRun(r, r.why, 2),
            else => |e| return e,
        };
        plan(r) catch |err| switch (err) {
            error.Refused => return failRun(r, r.why, 2),
            else => |e| return e,
        };
        r.set("state", Py.s("built"));
        r.set("updated_at", .{ .float = now() });
        try r.save("journal.built");
    }
    return publishRecorded(r);
}

fn lockRoot(r: *R) E!state.RootLock {
    const ms: ?u32 = if (r.spec.lock_timeout) |t| @intFromFloat(@min(@max(t, 0) * 1000, 4.0e9)) else null;
    return state.RootLock.acquire(r.a, r.spec.state_root, ms) catch |err| switch (err) {
        error.Busy => r.refuse("another eb-staged-rebuild holds the lock at {s}/.lock", .{r.spec.state_root}),
        error.NotPrivate => r.refuse("record lock requires a private regular file: {s}/.lock.posix", .{r.spec.state_root}),
        else => r.refuse("{s}", .{main_mod.osError(r.a, err, r.spec.state_root)}),
    };
}

/// run without --dry-run.
pub fn run(r: *R, probes: Py) E!Result {
    const a = r.a;
    if (std.mem.eql(u8, r.spec.backend, "bwrap") and !usable(probes, "bwrap")) {
        const b = pyfmt.get(probes, "bwrap") orelse Py{ .dict = &.{} };
        const reason = if (pyfmt.truthy(pyfmt.get(b, "reason"))) s(pyfmt.get(b, "reason")) else "bwrap is not usable";
        return r.refuse("{s}; refusing to rebuild in place", .{reason});
    }
    if (std.mem.eql(u8, r.spec.backend, "userns") and !usable(probes, "userns_sandbox")) {
        const u = pyfmt.get(probes, "userns_sandbox") orelse Py{ .dict = &.{} };
        const reason = if (pyfmt.truthy(pyfmt.get(u, "reason"))) s(pyfmt.get(u, "reason")) else "the user-namespace sandbox is not usable";
        return r.refuse("{s}; refusing to run the userns backend", .{reason});
    }
    var policy: Py = .null;
    if (r.spec.ledger_config) |lc| {
        var why: []const u8 = "";
        policy = publish_mod.loadConfig(a, lc, &why) catch |err| return switch (err) {
            error.OutOfMemory => error.OutOfMemory,
            else => r.refuse("{s}", .{why}),
        };
        r.spec.ledger_config = s(pyfmt.get(policy, "path"));
    }
    const rels = try learnRels(r);
    for (rels) |rel| try checkInstallDir(r, rel);
    var run_id: []const u8 = r.spec.run_id orelse "";
    if (run_id.len == 0) {
        const base = try std.fmt.allocPrint(a, "{s}-{d}", .{ nowStampRunId(a), linux.getpid() });
        run_id = base;
        var n: usize = 0;
        while (fsx.statAt(fsx.cwd, runDir(a, r.spec.state_root, run_id), true)) |_| {
            n += 1;
            run_id = try std.fmt.allocPrint(a, "{s}-{d}", .{ base, n });
        } else |_| {}
    }
    const l = try lockRoot(r);
    defer l.release();
    r.path = try std.fs.path.join(a, &.{ runDir(a, r.spec.state_root, run_id), "journal.json" });
    if (fsx.statAt(fsx.cwd, r.path, true)) |_| return r.refuse("run {s} already exists; resume it or choose another --run-id", .{run_id}) else |_| {}
    atomic.makedirs(runDir(a, r.spec.state_root, run_id)) catch |err| return r.refuse("{s}", .{main_mod.osError(a, err, r.spec.state_root)});
    const t = now();
    var manifests: std.ArrayList(Py.Entry) = .empty;
    if (!r.spec.skip_live_check) for (rels) |rel| {
        const snap = livecheck.snapshot(a, try std.fs.path.join(a, &.{ r.spec.software, rel })) catch return r.refuse("cannot snapshot {s}", .{rel});
        try manifests.append(a, .{ .key = rel, .value = snap });
    };
    const gen = admin.abspath(a, try std.fs.path.join(a, &.{ r.spec.state_root, "generations", run_id }));
    r.journal = .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "schema", .value = .{ .int = 2 } },
        .{ .key = "run_id", .value = Py.s(run_id) },
        .{ .key = "state", .value = Py.s("created") },
        .{ .key = "backend", .value = Py.s(r.spec.backend) },
        .{ .key = "software", .value = Py.s(admin.abspath(a, r.spec.software)) },
        .{ .key = "modules", .value = Py.s(admin.abspath(a, r.spec.modules)) },
        .{ .key = "state_root", .value = Py.s(admin.abspath(a, r.spec.state_root)) },
        .{ .key = "generation_root", .value = Py.s(gen) },
        .{ .key = "eb_args", .value = pyList(a, r.spec.eb_args) },
        .{ .key = "eb_executable", .value = Py.s(r.spec.eb_executable) },
        .{ .key = "grace_hours", .value = .{ .float = r.spec.grace_hours } },
        .{ .key = "skip_live_check", .value = .{ .bool = r.spec.skip_live_check } },
        .{ .key = "prepare_executable", .value = Py.opt(r.spec.prepare_executable) },
        .{ .key = "ledger_config", .value = Py.opt(r.spec.ledger_config) },
        .{ .key = "ledger_policy", .value = policy },
        .{ .key = "prepare_complete", .value = .{ .bool = false } },
        .{ .key = "prepare_returncode", .value = .null },
        .{ .key = "publication", .value = Py.s("directory-swap") },
        .{ .key = "module_rels", .value = pyList(a, rels) },
        .{ .key = "created_at", .value = .{ .float = t } },
        .{ .key = "updated_at", .value = .{ .float = t } },
        .{ .key = "manifests", .value = .{ .dict = manifests.items } },
        .{ .key = "probes", .value = probes },
        .{ .key = "modules_plan", .value = .{ .list = &.{} } },
        .{ .key = "links_plan", .value = .{ .list = &.{} } },
        .{ .key = "message", .value = Py.s("") },
        .{ .key = "eb_returncode", .value = .null },
        .{ .key = "eb_command", .value = .null },
        .{ .key = "retired_at", .value = .null },
        .{ .key = "generation_removed_at", .value = .null },
    }) };
    try r.save("journal.created");
    log("run {s} backend {s} generation {s}", .{ run_id, r.spec.backend, gen });
    return runLocked(r, true);
}

fn usable(probes: Py, key: []const u8) bool {
    const x = pyfmt.get(probes, key) orelse return false;
    return pyfmt.truthy(pyfmt.get(x, "usable"));
}

/// The spec a journal records, for resume and rollback.
fn specFromJournal(a: Allocator, j: Py, lock_timeout: ?f64) engine.Spec {
    var rels: std.ArrayList([]const u8) = .empty;
    if (pyfmt.get(j, "module_rels")) |v| if (v == .list) for (v.list) |x| if (x == .str) rels.append(a, x.str) catch {};
    var args: std.ArrayList([]const u8) = .empty;
    if (pyfmt.get(j, "eb_args")) |v| if (v == .list) for (v.list) |x| if (x == .str) args.append(a, x.str) catch {};
    const grace = pyfmt.get(j, "grace_hours");
    return .{
        .software = s(pyfmt.get(j, "software")),
        .modules = s(pyfmt.get(j, "modules")),
        .state_root = s(pyfmt.get(j, "state_root")),
        .eb_args = args.items,
        .backend = s(pyfmt.get(j, "backend")),
        .grace_hours = if (grace) |g| switch (g) {
            .float => |f| if (f != 0) f else admin.default_grace_hours,
            .int => |n| if (n != 0) @floatFromInt(n) else admin.default_grace_hours,
            else => admin.default_grace_hours,
        } else admin.default_grace_hours,
        .module_rels = rels.items,
        .eb_executable = if (s(pyfmt.get(j, "eb_executable")).len > 0) s(pyfmt.get(j, "eb_executable")) else "eb",
        .run_id = s(pyfmt.get(j, "run_id")),
        .skip_live_check = pyfmt.truthy(pyfmt.get(j, "skip_live_check")),
        .lock_timeout = lock_timeout,
        .prepare_executable = if (pyfmt.get(j, "prepare_executable")) |p| (if (p == .str) p.str else null) else null,
        .ledger_config = if (pyfmt.get(j, "ledger_config")) |p| (if (p == .str and p.str.len > 0) p.str else null) else null,
    };
}

pub fn resumeRun(a: Allocator, env: probe.Env, ctx: *admin.Ctx, state_root: []const u8, run_id: []const u8, lock_timeout: ?f64) E!Result {
    const path = try std.fs.path.join(a, &.{ state_root, "runs", run_id, "journal.json" });
    const st = fsx.statAt(fsx.cwd, path, false) catch {
        main_mod.refuse("no journal at {s}", .{path});
    };
    if (!st.isReg()) main_mod.refuse("no journal at {s}", .{path});
    var j = main_mod.loadJournal(a, path);
    if (std.mem.eql(u8, s(pyfmt.get(j, "kind")), "retire")) main_mod.refuse("run {s} is a retire record; rollback restores the module file", .{run_id});
    var r: R = .{ .a = a, .env = env, .ctx = ctx, .spec = specFromJournal(a, j, lock_timeout), .path = path };
    defer main_mod.last_why = r.why;
    engine.checkRoots(a, r.spec);
    const l = try lockRoot(&r);
    defer l.release();
    j = main_mod.loadJournal(a, path);
    r.journal = j;
    const state_name = r.str("state");
    var published = false;
    for (r.list("modules_plan")) |m| if (std.mem.eql(u8, s(pyfmt.get(m, "status")), "published")) {
        published = true;
    };
    if (std.mem.eql(u8, state_name, "complete")) {
        const m = try std.fmt.allocPrint(a, "run {s} is already complete", .{run_id});
        log("{s}", .{m});
        return recordFinished(&r, try publisher(&r), m);
    }
    if (std.mem.eql(u8, state_name, "rolled_back")) return r.refuse("run {s} was rolled back; start a new run", .{run_id});
    if (std.mem.eql(u8, state_name, "created") or std.mem.eql(u8, state_name, "building") or std.mem.eql(u8, state_name, "failed")) {
        if (published) return r.intervene("run {s} is {s} but a module was already published", .{ run_id, state_name });
        log("resume {s} from {s}: rebuilding the unpublished generation", .{ run_id, state_name });
        return runLocked(&r, true);
    }
    if (std.mem.eql(u8, state_name, "built") or std.mem.eql(u8, state_name, "publishing")) {
        log("resume {s} from {s}: publishing", .{ run_id, state_name });
        return publishRecorded(&r);
    }
    return r.intervene("run {s} is in an unknown state {s}", .{ run_id, state_name });
}

/// rollback of a complete run journal, without a ledger.
pub fn rollbackRun(r: *R) E!Result {
    const a = r.a;
    const run_id = r.str("run_id");
    const config = try publisher(r);
    var finished: publish_mod.Corrects = .empty;
    var started = false;
    if (config) |c| {
        const left = try record(r, c, &.{}, null);
        if (left.len > 0 and required(c)) {
            const m = try std.fmt.allocPrint(a, "run {s} was not rolled back: {s}", .{ run_id, publish_mod.pendingMessage(a, left) });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
        var why: []const u8 = "";
        const data = publish_mod.load(a, runDir(a, r.str("state_root"), run_id), &why) catch |err| return switch (err) {
            error.OutOfMemory => error.OutOfMemory,
            error.NeedsIntervention => r.intervene("{s}", .{why}),
            error.Refused => r.refuse("{s}", .{why}),
        };
        for ((pyfmt.get(data, "events") orelse Py{ .list = &.{} }).list) |ev| {
            const rec = pyfmt.get(ev, "record") orelse continue;
            const stage = s(pyfmt.get(rec, "stage"));
            if (std.mem.eql(u8, stage, "started")) started = true;
            if (std.mem.eql(u8, s(pyfmt.get(ev, "status")), "acknowledged") and !std.mem.eql(u8, s(pyfmt.get(rec, "action")), "correction") and std.mem.eql(u8, stage, "finished")) {
                const mf = s(pyfmt.get(pyfmt.get(rec, "build") orelse Py{ .dict = &.{} }, "module_file"));
                const id = pyfmt.get(pyfmt.get(ev, "receipt") orelse Py{ .dict = &.{} }, "id") orelse .null;
                try finished.put(a, mf, id);
            }
        }
    }
    if (std.mem.eql(u8, r.str("state"), "rolled_back")) {
        var all = true;
        for (r.list("modules_plan")) |m| if (!std.mem.eql(u8, s(pyfmt.get(m, "status")), "restored")) {
            all = false;
        };
        if (all) {
            const m = try std.fmt.allocPrint(a, "run {s} is already rolled back", .{run_id});
            log("{s}", .{m});
            return recordRollback(r, config, &finished, started, m);
        }
    }
    if (!std.mem.eql(u8, r.str("state"), "complete")) return r.refuse("run {s} is {s}; only a complete run can roll back", .{ run_id, r.str("state") });
    if (config) |c| if (finished.count() > 0) {
        const left = try record(r, c, try records(r, c, "started", &finished), "outbox.rollback_started");
        if (left.len > 0 and required(c)) {
            const m = try std.fmt.allocPrint(a, "run {s} was not rolled back: {s}", .{ run_id, publish_mod.pendingMessage(a, left) });
            log("{s}", .{m});
            return .{ .code = publish_mod.pending_exit, .message = m };
        }
    };
    rollbackSteps(r) catch |err| {
        const first = r.why;
        restorePublished(r) catch return r.intervene("rollback failed and the published tree could not be restored: {s}", .{r.why});
        r.set("state", Py.s("complete"));
        r.set("message", Py.s("rollback interrupted; published tree restored"));
        r.saveSilently("journal.rollback-restored") catch {};
        r.why = first;
        return err;
    };
    const m = try std.fmt.allocPrint(a, "run {s} rolled back; replacement kept at its retired path", .{run_id});
    log("{s}", .{m});
    return recordRollback(r, config, &finished, started, m);
}

/// _record_rollback: finish the correction, or abort a publication that
/// never finished.
fn recordRollback(r: *R, config: ?Py, finished: *const publish_mod.Corrects, started: bool, message: []const u8) E!Result {
    const c = config orelse return .{ .code = 0, .message = message };
    var recs: []const Py = &.{};
    if (finished.count() > 0) {
        recs = try records(r, c, "finished", finished);
    } else if (started) {
        recs = try records(r, c, "aborted", null);
    }
    const left = try record(r, c, recs, "outbox.rollback_finished");
    if (left.len > 0) {
        const m = try std.fmt.allocPrint(r.a, "{s}; {s}", .{ message, publish_mod.pendingMessage(r.a, left) });
        log("{s}", .{m});
        return .{ .code = publish_mod.pending_exit, .message = m };
    }
    return .{ .code = 0, .message = message };
}

fn rollbackSteps(r: *R) E!void {
    const a = r.a;
    const mods = r.list("modules_plan");
    var i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const rel = s(pyfmt.get(m, "rel"));
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        // An exchange the journal did not record already put the old tree
        // back: this run's tree is at the retired name.
        const lr = treeRunId(a, live);
        const rr = treeRunId(a, retired);
        const run_id = r.str("run_id");
        const done_already = !(lr != null and std.mem.eql(u8, lr.?, run_id)) and (rr != null and std.mem.eql(u8, rr.?, run_id));
        const method = if (done_already)
            (if (s(pyfmt.get(m, "rollback_method")).len > 0) s(pyfmt.get(m, "rollback_method")) else "recovered")
        else
            try swapPair(r, live, retired, r.faults(), try std.fmt.allocPrint(a, "rollback.swap:{s}", .{rel}));
        r.setItem("modules_plan", i, "status", Py.s("restoring"));
        r.setItem("modules_plan", i, "rollback_method", Py.s(method));
        try r.save(try std.fmt.allocPrint(a, "journal.rollback-swap:{s}", .{rel}));
    }
    i = mods.len;
    while (i > 0) {
        i -= 1;
        const m = r.list("modules_plan")[i];
        const rel = s(pyfmt.get(m, "rel"));
        try restoreOldModule(r, m, r.faults());
        r.setItem("modules_plan", i, "status", Py.s("restored"));
        log("restored {s}", .{rel});
        try r.save(try std.fmt.allocPrint(a, "journal.restore:{s}", .{rel}));
    }
    for (r.list("links_plan"), 0..) |l, k| {
        if (!pyfmt.truthy(pyfmt.get(l, "created"))) continue;
        const path = s(pyfmt.get(l, "live_path"));
        if (isKind(path, "symlink") and std.mem.eql(u8, readlinkText(a, path), s(pyfmt.get(l, "target")))) {
            try hit(r, r.faults(), try std.fmt.allocPrint(a, "rollback.unlink:{s}", .{s(pyfmt.get(l, "rel"))}));
            sys.unlinkat(fsx.cwd, path, 0) catch {};
            atomic.fsyncDir(std.fs.path.dirname(path) orelse "/") catch {};
        }
        r.setItem("links_plan", k, "created", .{ .bool = false });
        r.setItem("links_plan", k, "status", Py.s("removed"));
    }
    r.set("state", Py.s("rolled_back"));
    r.set("rolled_back_at", .{ .float = now() });
    r.set("message", Py.s("rolled back"));
    try r.save("journal.rolled_back");
}

fn restorePublished(r: *R) E!void {
    const run_id = r.str("run_id");
    for (r.list("modules_plan"), 0..) |m, i| {
        const live = s(pyfmt.get(m, "live_software"));
        const retired = s(pyfmt.get(m, "retired"));
        try recoverPair(r, live, retired);
        const lr = treeRunId(r.a, live);
        const rr = treeRunId(r.a, retired);
        const live_is = lr != null and std.mem.eql(u8, lr.?, run_id);
        const retired_is = rr != null and std.mem.eql(u8, rr.?, run_id);
        if (!live_is and retired_is) {
            _ = try swapPair(r, live, retired, .{}, try std.fmt.allocPrint(r.a, "rollback-recover:{s}", .{s(pyfmt.get(m, "rel"))}));
        } else if (!(live_is and !retired_is)) {
            return r.intervene("cannot restore published tree for {s}", .{s(pyfmt.get(m, "rel"))});
        }
        try writeNewModule(r, m, .{});
        r.setItem("modules_plan", i, "status", Py.s("published"));
    }
    try createLinks(r, .{});
}

test "relsFromDryRun" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const rels = relsFromDryRun(a, " * [ ] /x/zlib-1.3.eb (module: zlib/1.3)\n * [x] /x/GCC.eb (module: Core | GCC/13)\nnoise\n * [R] /y.eb (module: a/b) \n");
    try std.testing.expectEqual(@as(usize, 3), rels.len);
    try std.testing.expectEqualStrings("Core/GCC/13", rels[1]);
}

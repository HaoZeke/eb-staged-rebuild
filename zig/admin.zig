//! retire, rollback of a retire journal, and gc: the commands that change a
//! module tree or a state root without building anything.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const state = common.state;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

pub const default_grace_hours: f64 = 120 + 24;
const seconds_per_hour: f64 = 3600;

fn refuse(comptime f: []const u8, args: anytype) noreturn {
    main_mod.refuse(f, args);
}

/// "eb-staged-rebuild: MESSAGE" on stdout, as the Python tool logs progress.
fn log(comptime f: []const u8, args: anytype) void {
    cli.out("eb-staged-rebuild: " ++ f ++ "\n", args);
}

pub fn nowSeconds() f64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.REALTIME, &ts);
    return @as(f64, @floatFromInt(ts.sec)) + @as(f64, @floatFromInt(ts.nsec)) / 1e9;
}

// ---- paths --------------------------------------------------------------

/// os.path.abspath(os.path.normpath(p)).
pub fn abspath(a: Allocator, p: []const u8) []const u8 {
    var full = p;
    if (p.len == 0 or p[0] != '/') {
        var rb: [sys.path_max]u8 = undefined;
        const cwd = fsx.realpath(fsx.cwd, ".", &rb) catch ".";
        full = std.fs.path.join(a, &.{ cwd, p }) catch p;
    }
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, full, '/');
    while (it.next()) |seg| {
        if (seg.len == 0 or std.mem.eql(u8, seg, ".")) continue;
        if (std.mem.eql(u8, seg, "..")) {
            _ = parts.pop();
            continue;
        }
        parts.append(a, seg) catch {};
    }
    const joined = std.mem.join(a, "/", parts.items) catch return full;
    return std.fmt.allocPrint(a, "/{s}", .{joined}) catch full;
}

pub fn isWithin(a: Allocator, child: []const u8, parent: []const u8) bool {
    const c = abspath(a, child);
    const p = abspath(a, parent);
    if (std.mem.eql(u8, c, p)) return true;
    const prefix = std.fmt.allocPrint(a, "{s}/", .{std.mem.trimEnd(u8, p, "/")}) catch return false;
    return std.mem.startsWith(u8, c, prefix);
}

fn dirname(p: []const u8) []const u8 {
    return std.fs.path.dirname(p) orelse "";
}

// ---- journals -------------------------------------------------------------

fn dictSet(a: Allocator, d: Py, key: []const u8, value: Py) Py {
    var out: std.ArrayList(Py.Entry) = .empty;
    var found = false;
    for (d.dict) |e| {
        if (std.mem.eql(u8, e.key, key)) {
            out.append(a, .{ .key = e.key, .value = value }) catch {};
            found = true;
        } else out.append(a, e) catch {};
    }
    if (!found) out.append(a, .{ .key = key, .value = value }) catch {};
    return .{ .dict = out.items };
}

fn str(v: ?Py) ?[]const u8 {
    const x = v orelse return null;
    return if (x == .str) x.str else null;
}

fn eqlStr(v: ?Py, s: []const u8) bool {
    const x = str(v) orelse return false;
    return std.mem.eql(u8, x, s);
}

/// Pause before a step the tests name in EB_STAGED_REBUILD_PAUSE_AT, as
/// the Python fault injector does.
const PauseCtx = struct { env: probe.Env };

fn reachStep(ctx: ?*anyopaque, step: []const u8) void {
    const c: *const PauseCtx = @ptrCast(@alignCast(ctx.?));
    const token = c.env.get("EB_STAGED_REBUILD_PAUSE_AT") orelse return;
    if (!std.mem.eql(u8, token, step)) return;
    if (c.env.get("EB_STAGED_REBUILD_PAUSE_READY")) |ready| if (ready.len > 0) {
        if (sys.openat(fsx.cwd, ready, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o644)) |fd| sys.close(fd) else |_| {}
    };
    const secs = std.fmt.parseFloat(f64, c.env.get("EB_STAGED_REBUILD_PAUSE_SECONDS") orelse "30") catch 30;
    var ts: linux.timespec = .{ .sec = @intFromFloat(@floor(secs)), .nsec = @intFromFloat((secs - @floor(secs)) * 1e9) };
    _ = linux.nanosleep(&ts, null);
}

pub const Ctx = struct {
    a: Allocator,
    env: probe.Env,
    pause: PauseCtx,

    pub fn faults(c: *Ctx) atomic.Faults {
        return .{ .ctx = &c.pause, .reach_fn = reachStep };
    }
};

/// save_journal: the schema and publication fields, the directory-swap
/// check, then json.dumps(indent=2, sort_keys=True) written atomically.
fn saveJournal(c: *Ctx, path: []const u8, data_in: Py, step: []const u8) Py {
    var data = data_in;
    if (eqlStr(pyfmt.get(data, "kind"), "retire")) {
        data = dictSet(c.a, data, "schema", .{ .int = 2 });
    } else if (pyfmt.truthy(pyfmt.get(data, "ledger_policy"))) {
        data = dictSet(c.a, data, "schema", .{ .int = 4 });
        data = dictSet(c.a, data, "publication", Py.s("directory-swap"));
    } else {
        data = dictSet(c.a, data, "schema", .{ .int = 2 });
        const p = pyfmt.get(data, "publication");
        if (p == null or p.? == .null) data = dictSet(c.a, data, "publication", Py.s("directory-swap"));
    }
    main_mod.requireDirectorySwap(c.a, data);
    const body = json.dumps(c.a, data, 2, true) catch refuse("out of memory", .{});
    const raw = std.fmt.allocPrint(c.a, "{s}\n", .{body}) catch refuse("out of memory", .{});
    var why: atomic.Why = .{};
    atomic.atomicWrite(c.a, path, raw, .{ .faults = c.faults(), .step = step }, &why) catch |err|
        if (err == error.Refused) refuse("{s}", .{why.msg}) else refuse("{s}", .{main_mod.osError(c.a, err, path)});
    return data;
}

fn journalPath(a: Allocator, state_root: []const u8, run_id: []const u8) []const u8 {
    return std.fs.path.join(a, &.{ state_root, "runs", run_id, "journal.json" }) catch state_root;
}

fn lock(c: *Ctx, state_root: []const u8, timeout_s: ?f64) state.RootLock {
    const ms: ?u32 = if (timeout_s) |t| @intFromFloat(@min(@max(t, 0) * 1000, 4.0e9)) else null;
    return state.RootLock.acquire(c.a, state_root, ms) catch |err| switch (err) {
        error.Busy => refuse("another eb-staged-rebuild holds the lock at {s}/.lock", .{state_root}),
        error.NotPrivate => refuse("record lock requires a private regular file: {s}/.lock.posix", .{state_root}),
        else => refuse("{s}", .{main_mod.osError(c.a, err, state_root)}),
    };
}

fn sha256Hex(a: Allocator, data: []const u8) []const u8 {
    var out: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(data, &out, .{});
    const hex = std.fmt.bytesToHex(out, .lower);
    return a.dupe(u8, &hex) catch "";
}

fn readFile(c: *Ctx, path: []const u8) []const u8 {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    defer sys.close(f.fd);
    return fsx.readAll(c.a, f.fd, 64 << 20) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
}

// ---- retire -------------------------------------------------------------

fn gmtimeStamp(a: Allocator, t: f64, comptime with_dashes: bool) []const u8 {
    const secs: u64 = @intFromFloat(@max(@floor(t), 0));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    if (with_dashes) return std.fmt.allocPrint(a, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
    return std.fmt.allocPrint(a, "{d:0>4}{d:0>2}{d:0>2}T{d:0>2}{d:0>2}{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

fn lexists(p: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, p, false) catch return false;
    return true;
}

const Plan = struct { module: []const u8, data: []const u8, software_dir: []const u8 };

fn plan(c: *Ctx, state_root: []const u8, module_in: []const u8) Plan {
    const module = abspath(c.a, module_in);
    if (isWithin(c.a, module, state_root)) refuse("{s} is inside the state root", .{module});
    for (main_mod.listJournals(c.a, state_root)) |j| {
        if (!eqlStr(pyfmt.get(j, "kind"), "retire")) continue;
        const st = pyfmt.get(j, "state");
        if (eqlStr(pyfmt.get(j, "module_path"), module) and (eqlStr(st, "retiring") or eqlStr(st, "retired"))) {
            var rid: std.ArrayList(u8) = .empty;
            pyfmt.str(&rid, c.a, pyfmt.get(j, "run_id")) catch {};
            refuse("{s} is already retired ({s})", .{ module, rid.items });
        }
    }
    const kind = main_mod.lstatKind(module);
    if (!std.mem.eql(u8, kind, "file")) refuse("{s} is not a regular file ({s})", .{ module, kind });
    const data = readFile(c, module);
    if (!std.unicode.utf8ValidateSlice(data)) refuse("{s} is not UTF-8 text", .{module});
    const root = modulefile.parseRoot(data) orelse refuse("{s} has no root assignment, so its software directory is unknown", .{module});
    if (root.path.len == 0) refuse("{s} has no root assignment, so its software directory is unknown", .{module});
    return .{ .module = module, .data = data, .software_dir = abspath(c.a, root.path) };
}

fn discardEmpty(a: Allocator, top: []const u8) void {
    // os.walk(top, topdown=False) then rmdir each: children first.
    const fd = fsx.openDir(fsx.cwd, top, false) catch {
        rmdir(top);
        return;
    };
    var names: std.ArrayList([]const u8) = .empty;
    {
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch return;
        while (it.next() catch null) |e| if ((fsx.entryType(fd, e) catch 0) == fsx.DT.DIR) names.append(a, a.dupe(u8, e.name) catch continue) catch {};
    }
    for (names.items) |n| discardEmpty(a, std.fs.path.join(a, &.{ top, n }) catch continue);
    rmdir(top);
}

fn rmdir(p: []const u8) void {
    sys.unlinkat(fsx.cwd, p, linux.AT.REMOVEDIR) catch {};
}

fn statDev(c: *Ctx, p: []const u8) u64 {
    const st = fsx.statAt(fsx.cwd, p, true) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, p)});
    return st.dev;
}

/// Rename `source` onto `destination` once the file's bytes are durable.
fn move(c: *Ctx, source: []const u8, destination: []const u8, step: []const u8) void {
    const f = c.faults();
    f.reach(std.fmt.allocPrint(c.a, "{s}.fsync", .{step}) catch step);
    {
        const fd = sys.openat(fsx.cwd, source, .{}, 0) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, source)});
        defer sys.close(fd);
        sys.fsync(fd) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, source)});
    }
    f.reach(std.fmt.allocPrint(c.a, "{s}.rename", .{step}) catch step);
    sys.renameat(fsx.cwd, source, fsx.cwd, destination) catch |err| switch (err) {
        error.CrossDevice => refuse("{s} and {s} are on different filesystems", .{ source, destination }),
        else => refuse("{s}", .{main_mod.osError(c.a, err, source)}),
    };
    atomic.fsyncDir(dirname(destination)) catch {};
    atomic.fsyncDir(dirname(source)) catch {};
}

pub fn retire(c: *Ctx, state_root_in: []const u8, module_file: []const u8, reason: ?[]const u8, dry_run: bool, lock_timeout: ?f64) u8 {
    const a = c.a;
    const state_root = abspath(a, state_root_in);
    if (dry_run) {
        const p = plan(c, state_root, module_file);
        log("would retire {s} (software directory {s} is left in place)", .{ p.module, p.software_dir });
        return 0;
    }
    const l = lock(c, state_root, lock_timeout);
    defer l.release();
    const p = plan(c, state_root, module_file);
    const when = nowSeconds();
    const stamp = gmtimeStamp(a, when, false);
    var name = stamp;
    var count: usize = 1;
    const retired_root = std.fs.path.join(a, &.{ state_root, "retired" }) catch state_root;
    while (lexists(std.fs.path.join(a, &.{ retired_root, name }) catch "") or
        lexists(std.fs.path.join(a, &.{ state_root, "runs", std.fmt.allocPrint(a, "retire-{s}", .{name}) catch "" }) catch ""))
    {
        count += 1;
        name = std.fmt.allocPrint(a, "{s}-{d}", .{ stamp, count }) catch name;
    }
    const run_id = std.fmt.allocPrint(a, "retire-{s}", .{name}) catch name;
    const destination = std.fs.path.join(a, &.{ retired_root, name, std.mem.trimStart(u8, p.module, "/") }) catch "";
    atomic.makedirs(dirname(destination)) catch |err| refuse("{s}", .{main_mod.osError(a, err, dirname(destination))});
    if (statDev(c, dirname(p.module)) != statDev(c, dirname(destination))) {
        discardEmpty(a, std.fs.path.join(a, &.{ retired_root, name }) catch "");
        refuse("{s} and the state root are on different filesystems; retire needs one atomic rename, so put --state-root on the same filesystem as the module tree", .{p.module});
    }
    const path = journalPath(a, state_root, run_id);
    var journal: Py = .{ .dict = &.{} };
    const fields = [_]Py.Entry{
        .{ .key = "schema", .value = .{ .int = 1 } },
        .{ .key = "kind", .value = Py.s("retire") },
        .{ .key = "run_id", .value = Py.s(run_id) },
        .{ .key = "state", .value = Py.s("retiring") },
        .{ .key = "state_root", .value = Py.s(state_root) },
        .{ .key = "module_path", .value = Py.s(p.module) },
        .{ .key = "module_hash", .value = Py.s(sha256Hex(a, p.data)) },
        .{ .key = "software_dir", .value = Py.s(p.software_dir) },
        .{ .key = "retired_path", .value = Py.s(destination) },
        .{ .key = "reason", .value = Py.s(reason orelse "") },
        .{ .key = "created_at", .value = .{ .float = when } },
        .{ .key = "updated_at", .value = .{ .float = when } },
        .{ .key = "retired_at", .value = .null },
        .{ .key = "rolled_back_at", .value = .null },
        .{ .key = "modules_plan", .value = .{ .list = &.{} } },
        .{ .key = "links_plan", .value = .{ .list = &.{} } },
        .{ .key = "message", .value = Py.s("") },
    };
    for (fields) |f| journal = dictSet(a, journal, f.key, f.value);
    journal = saveJournal(c, path, journal, "journal.retiring");
    move(c, p.module, destination, "retire");
    const t = nowSeconds();
    journal = dictSet(a, journal, "state", Py.s("retired"));
    journal = dictSet(a, journal, "retired_at", .{ .float = t });
    journal = dictSet(a, journal, "updated_at", .{ .float = t });
    journal = dictSet(a, journal, "message", Py.s("module file retired"));
    _ = saveJournal(c, path, journal, "journal.retired");
    log("retired {s} -> {s}; software directory {s} left in place", .{ p.module, destination, p.software_dir });
    return 0;
}

// ---- rollback -------------------------------------------------------------

pub fn rollback(c: *Ctx, state_root: []const u8, run_id: []const u8, lock_timeout: ?f64) u8 {
    const a = c.a;
    const path = journalPath(a, state_root, run_id);
    const l = lock(c, state_root, lock_timeout);
    defer l.release();
    const st = fsx.statAt(fsx.cwd, path, false) catch refuse("no journal at {s}", .{path});
    if (!st.isReg()) refuse("no journal at {s}", .{path});
    var journal = main_mod.loadJournal(a, path);
    if (!eqlStr(pyfmt.get(journal, "kind"), "retire")) {
        const run_mod = @import("run.zig");
        if (str(pyfmt.get(journal, "ledger_config"))) |lc| if (lc.len > 0)
            refuse("rollback of a run with a ledger configuration is not in this build yet; use the Python eb-staged-rebuild", .{});
        var r: run_mod.R = .{ .a = a, .env = c.env, .ctx = c, .spec = .{ .software = "", .modules = "", .state_root = state_root, .eb_args = &.{} }, .journal = journal, .path = path };
        const res = run_mod.rollbackRun(&r) catch |err| switch (err) {
            error.OutOfMemory => refuse("out of memory", .{}),
            else => refuse("{s}", .{r.why}),
        };
        return res.code;
    }
    if (eqlStr(pyfmt.get(journal, "state"), "rolled_back")) {
        log("run {s} is already rolled back", .{run_id});
        return 0;
    }
    const module = str(pyfmt.get(journal, "module_path")) orelse refuse("journal has no module_path", .{});
    const retired = str(pyfmt.get(journal, "retired_path")) orelse refuse("journal has no retired_path", .{});
    const want = str(pyfmt.get(journal, "module_hash")) orelse "";
    const live_kind = main_mod.lstatKind(module);
    const retired_kind = main_mod.lstatKind(retired);
    if (std.mem.eql(u8, retired_kind, "file")) {
        if (!std.mem.eql(u8, sha256Hex(a, readFile(c, retired)), want)) refuse("{s} does not match the module file recorded for {s}", .{ retired, run_id });
        if (!std.mem.eql(u8, live_kind, "missing")) refuse("{s} exists again; refusing to overwrite it with the retired copy", .{module});
        move(c, retired, module, "rollback.retire");
        log("restored {s}", .{module});
    } else if (std.mem.eql(u8, live_kind, "file") and std.mem.eql(u8, sha256Hex(a, readFile(c, module)), want)) {
        // Already back in place.
    } else {
        refuse("neither {s} nor {s} holds the retired module file", .{ module, retired });
    }
    const t = nowSeconds();
    journal = dictSet(a, journal, "state", Py.s("rolled_back"));
    journal = dictSet(a, journal, "rolled_back_at", .{ .float = t });
    journal = dictSet(a, journal, "updated_at", .{ .float = t });
    journal = dictSet(a, journal, "message", Py.s("rolled back"));
    _ = saveJournal(c, path, journal, "journal.rolled_back");
    log("run {s} rolled back; {s} restored", .{ run_id, module });
    return 0;
}

// ---- gc -----------------------------------------------------------------

fn moduleRoot(c: *Ctx, path: []const u8) ?[]const u8 {
    const st = fsx.statAt(fsx.cwd, path, false) catch |err| switch (err) {
        error.NotFound => return null,
        else => refuse("cannot inspect live module {s}: {s}", .{ path, main_mod.osError(c.a, err, path) }),
    };
    if (!st.isReg()) refuse("cannot determine live references from a non-regular module: {s}", .{path});
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("cannot read live references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    defer sys.close(f.fd);
    const data = fsx.readAll(c.a, f.fd, 64 << 20) catch |err| refuse("cannot read live references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    if (!std.unicode.utf8ValidateSlice(data)) refuse("cannot read live references from {s}: not UTF-8 text", .{path});
    const root = modulefile.parseRoot(data) orelse refuse("cannot determine the live software root from {s}", .{path});
    if (root.path.len == 0) refuse("cannot determine the live software root from {s}", .{path});
    return abspath(c.a, root.path);
}

fn resolved(c: *Ctx, path: []const u8) []const u8 {
    var rb: [sys.path_max]u8 = undefined;
    const r = fsx.realpath(fsx.cwd, path, &rb) catch |err| refuse("cannot resolve live software references from {s}: {s}", .{ path, main_mod.osError(c.a, err, path) });
    return c.a.dupe(u8, r) catch path;
}

fn liveRoots(c: *Ctx, journals: []const Py) []const []const u8 {
    var roots: std.ArrayList([]const u8) = .empty;
    for (journals) |j| {
        if (eqlStr(pyfmt.get(j, "kind"), "retire")) {
            if (!eqlStr(pyfmt.get(j, "state"), "retired")) {
                if (moduleRoot(c, str(pyfmt.get(j, "module_path")) orelse "")) |r| {
                    roots.append(c.a, r) catch {};
                    roots.append(c.a, resolved(c, r)) catch {};
                }
            }
            continue;
        }
        const plan_v = pyfmt.get(j, "modules_plan");
        if (!pyfmt.truthy(plan_v)) continue;
        for (plan_v.?.list) |m| {
            const live = str(pyfmt.get(m, "live_module")) orelse continue;
            if (live.len == 0) continue;
            if (moduleRoot(c, live)) |r| {
                roots.append(c.a, r) catch {};
                roots.append(c.a, resolved(c, r)) catch {};
            }
        }
    }
    return roots.items;
}

fn referenced(c: *Ctx, directory: []const u8, roots: []const []const u8) bool {
    const spellings = [_][]const u8{ abspath(c.a, directory), resolved(c, directory) };
    for (roots) |r| for (spellings) |s| if (isWithin(c.a, r, s)) return true;
    return false;
}

fn num(v: ?Py) ?f64 {
    const x = v orelse return null;
    return switch (x) {
        .int => |n| @floatFromInt(n),
        .float => |f| f,
        .bool => |b| if (b) 1 else 0,
        else => null,
    };
}

const Action = struct { path: []const u8, action: []const u8, reason: []const u8 };

fn safeRetiredPath(c: *Ctx, m: Py) []const u8 {
    const live_raw = str(pyfmt.get(m, "live_software")) orelse "";
    const retired_raw = str(pyfmt.get(m, "retired")) orelse "";
    const live = abspath(c.a, live_raw);
    const retired = abspath(c.a, retired_raw);
    if (live.len == 0 or retired.len == 0) refuse("journal has no live/retired software path", .{});
    if (std.mem.eql(u8, live, retired) or !std.mem.eql(u8, dirname(live), dirname(retired))) refuse("refusing retired path that is not a live sibling: {s}", .{retired});
    const prefix = std.fmt.allocPrint(c.a, ".{s}.", .{std.fs.path.basename(live)}) catch "";
    if (!std.mem.startsWith(u8, std.fs.path.basename(retired), prefix)) refuse("refusing unexpected retired path name: {s}", .{retired});
    return retired;
}

/// remove_generation_tree: delete an owned tree on one filesystem without
/// following links, opening a directory 0700 when it refuses entry.
fn removeTree(c: *Ctx, path_in: []const u8, boundary_in: []const u8) void {
    const path = abspath(c.a, path_in);
    const boundary = abspath(c.a, boundary_in);
    if (std.mem.eql(u8, path, boundary) or !isWithin(c.a, path, boundary)) refuse("cleanup path is outside its generation boundary: {s}", .{path});
    const parent = fsx.openDir(fsx.cwd, dirname(path), false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, dirname(path))});
    defer sys.close(parent);
    const dev = (fsx.fstat(parent) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)})).dev;
    const name = std.fs.path.basename(path);
    const fd = openOwned(c, parent, name, dev);
    emptyDir(c, fd, (fsx.fstat(fd) catch refuse("cleanup directory changed: {s}", .{name})).dev);
    sys.close(fd);
    sys.unlinkat(parent, name, linux.AT.REMOVEDIR) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, path)});
    sys.fsync(parent) catch {};
}

fn openOwned(c: *Ctx, parent: sys.fd_t, name: []const u8, dev: u64) sys.fd_t {
    const info = fsx.statAt(parent, name, false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, name)});
    if (info.dev != dev) refuse("cleanup refuses a different filesystem: {s}", .{name});
    if (!info.isDir() or info.uid != linux.geteuid()) refuse("cleanup requires an owned real directory: {s}", .{name});
    const fd = fsx.openDir(parent, name, false) catch |err| switch (err) {
        error.AccessDenied => blk: {
            var zb: [sys.path_max:0]u8 = undefined;
            _ = linux.fchmodat(parent, (sys.z(&zb, name) catch refuse("bad name", .{})).ptr, @intCast(info.perm() | 0o700));
            break :blk fsx.openDir(parent, name, false) catch |e2| refuse("{s}", .{main_mod.osError(c.a, e2, name)});
        },
        else => refuse("{s}", .{main_mod.osError(c.a, err, name)}),
    };
    const cur = fsx.fstat(fd) catch refuse("cleanup directory changed: {s}", .{name});
    if (cur.dev != info.dev or cur.ino != info.ino or cur.uid != info.uid) {
        sys.close(fd);
        refuse("cleanup directory changed: {s}", .{name});
    }
    sys.fchmod(fd, @intCast(cur.perm() | 0o700)) catch {};
    return fd;
}

fn emptyDir(c: *Ctx, fd: sys.fd_t, dev: u64) void {
    var names: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(c.a, fd) catch return;
    while (it.next() catch |err| refuse("{t}", .{err})) |e| names.append(c.a, c.a.dupe(u8, e.name) catch continue) catch {};
    for (names.items) |n| {
        const info = fsx.statAt(fd, n, false) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
        if (info.isDir()) {
            const child = openOwned(c, fd, n, dev);
            emptyDir(c, child, dev);
            sys.close(child);
            sys.unlinkat(fd, n, linux.AT.REMOVEDIR) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
        } else sys.unlinkat(fd, n, 0) catch |err| refuse("{s}", .{main_mod.osError(c.a, err, n)});
    }
}

pub fn gc(c: *Ctx, state_root_in: []const u8, grace_hours: f64, dry_run: bool, include_failed: bool, lock_timeout: ?f64) u8 {
    const a = c.a;
    const now = nowSeconds();
    const l = lock(c, state_root_in, lock_timeout);
    defer l.release();
    const state_root = abspath(a, state_root_in);
    var journals = main_mod.listJournals(a, state_root);
    const roots = liveRoots(c, journals);
    var actions: std.ArrayList(Action) = .empty;
    const grace = grace_hours * seconds_per_hour;
    for (journals, 0..) |j_in, ji| {
        var j = j_in;
        if (eqlStr(pyfmt.get(j, "kind"), "retire")) continue;
        var changed = false;
        const plan_v = pyfmt.get(j, "modules_plan");
        if (!pyfmt.truthy(plan_v)) continue;
        var mods = a.dupe(Py, plan_v.?.list) catch continue;
        for (mods, 0..) |m, mi| {
            if (pyfmt.truthy(pyfmt.get(m, "retired_removed_at"))) continue;
            const retired = safeRetiredPath(c, m);
            if (!std.mem.eql(u8, main_mod.lstatKind(retired), "dir")) continue;
            const st = str(pyfmt.get(j, "state"));
            var when: ?f64 = null;
            var why: []const u8 = "";
            if (st != null and std.mem.eql(u8, st.?, "complete")) {
                when = num(pyfmt.get(m, "published_at"));
                why = "previous live tree";
            } else if (st != null and std.mem.eql(u8, st.?, "rolled_back")) {
                when = num(pyfmt.get(j, "rolled_back_at"));
                why = "rolled-back candidate";
            } else if (include_failed and st != null and std.mem.eql(u8, st.?, "failed") and eqlStr(pyfmt.get(m, "status"), "pending")) {
                when = num(if (pyfmt.truthy(pyfmt.get(j, "updated_at"))) pyfmt.get(j, "updated_at") else pyfmt.get(j, "created_at"));
                why = "failed candidate";
            } else {
                var b: std.ArrayList(u8) = .empty;
                pyfmt.str(&b, a, pyfmt.get(j, "state")) catch {};
                why = std.fmt.allocPrint(a, "state {s} is kept", .{b.items}) catch "";
            }
            const w = when orelse {
                actions.append(a, .{ .path = retired, .action = "keep", .reason = why }) catch {};
                continue;
            };
            if (referenced(c, retired, roots)) {
                actions.append(a, .{ .path = retired, .action = "keep", .reason = "a live module resolves inside it" }) catch {};
                continue;
            }
            if (now - w < grace) {
                actions.append(a, .{ .path = retired, .action = "keep", .reason = std.fmt.allocPrint(a, "within grace ({s})", .{why}) catch why }) catch {};
                continue;
            }
            actions.append(a, .{ .path = retired, .action = "remove", .reason = why }) catch {};
            if (dry_run) continue;
            removeTree(c, retired, dirname(retired));
            mods[mi] = dictSet(a, m, "retired_removed_at", .{ .float = now });
            changed = true;
        }
        if (changed) {
            j = dictSet(a, j, "modules_plan", .{ .list = mods });
            j = dictSet(a, j, "message", Py.s("retired tree removed"));
            const rid = str(pyfmt.get(j, "run_id")) orelse "";
            j = saveJournal(c, journalPath(a, state_root, rid), j, "journal.gc");
            journals = blk: {
                const copy = a.dupe(Py, journals) catch break :blk journals;
                copy[ji] = j;
                break :blk copy;
            };
        }
    }
    // Stable software directories named by retired module files: reported,
    // never deleted.
    var latest: std.StringArrayHashMapUnmanaged(f64) = .empty;
    for (journals) |j| {
        if (!eqlStr(pyfmt.get(j, "kind"), "retire") or !eqlStr(pyfmt.get(j, "state"), "retired")) continue;
        const dir = abspath(a, str(pyfmt.get(j, "software_dir")) orelse continue);
        const w = num(pyfmt.get(j, "retired_at")) orelse continue;
        const r = latest.getOrPut(a, dir) catch continue;
        r.value_ptr.* = if (r.found_existing) @max(r.value_ptr.*, w) else w;
    }
    const dirs = a.dupe([]const u8, latest.keys()) catch &.{};
    std.mem.sort([]const u8, @constCast(dirs), {}, struct {
        fn lt(_: void, x: []const u8, y: []const u8) bool {
            return json.pyTextLess(x, y);
        }
    }.lt);
    for (dirs) |dir| {
        const st = fsx.statAt(fsx.cwd, dir, true) catch continue;
        if (!st.isDir()) continue;
        const ready = latest.get(dir).? + grace;
        if (referenced(c, dir, roots)) {
            actions.append(a, .{ .path = dir, .action = "keep", .reason = "a live module resolves inside it" }) catch {};
        } else if (now < ready) {
            actions.append(a, .{ .path = dir, .action = "keep", .reason = std.fmt.allocPrint(a, "within grace (retired module); ready to remove by hand after {s}", .{gmtimeStamp(a, ready, true)}) catch "" }) catch {};
        } else {
            actions.append(a, .{ .path = dir, .action = "manual", .reason = std.fmt.allocPrint(a, "ready to remove by hand after {s}; gc does not delete the stable software directory", .{gmtimeStamp(a, ready, true)}) catch "" }) catch {};
        }
    }
    for (actions.items) |act| cli.out("{s}: {s} ({s})\n", .{ act.action, act.path, act.reason });
    return 0;
}

//! eb-staged-rebuild: build EasyBuild software in a staged tree, then
//! exchange the complete tree with the stable live directory.
//!
//! This build carries probe, status, resolve, retire, gc, and rollback of a
//! retire journal; run, resume and rollback of a run journal follow in the order the port plan
//! sets, and until then report that they are not in this build.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");
const ledger = @import("ledger.zig");
const admin = @import("admin.zig");
const engine = @import("engine.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

pub const version = "1.0.0";
const prog = "eb-staged-rebuild";

const commands = [_][]const u8{ "probe", "run", "resume", "rollback", "gc", "retire", "status", "resolve" };

const top_usage =
    \\usage: eb-staged-rebuild [-h] [--version]
    \\                         {probe,run,resume,rollback,gc,retire,status,resolve} ...
    \\
;

const top_help = top_usage ++
    \\
    \\Build EasyBuild software in a staged tree, then exchange the complete tree
    \\with the stable live directory.
    \\
    \\positional arguments:
    \\  {probe,run,resume,rollback,gc,retire,status,resolve}
    \\    probe               report bwrap, user namespaces, RENAME_EXCHANGE, and
    \\                        EasyBuild
    \\    run                 build and atomically swap a staged tree
    \\    resume              continue a run after a crash
    \\    rollback            restore the previous module files, or put a retired
    \\                        module file back
    \\    gc                  remove hidden retired siblings after the grace period
    \\    retire              move a module file out of the module tree after its
    \\                        package is published elsewhere
    \\    status              show run journals
    \\    resolve             print the install path a module file names
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --version             show program's version number and exit
    \\
;

const probe_usage = "usage: eb-staged-rebuild probe [-h] [--path PATH] [--eb EB] [--json]\n";
const probe_help = probe_usage ++
    \\
    \\options:
    \\  -h, --help   show this help message and exit
    \\  --path PATH  directory on the filesystem to probe (default: .)
    \\  --eb EB
    \\  --json
    \\
;
const status_usage = "usage: eb-staged-rebuild status [-h] --state-root STATE_ROOT [run_id]\n";
const status_help = status_usage ++
    \\
    \\positional arguments:
    \\  run_id
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\
;
const resolve_usage = "usage: eb-staged-rebuild resolve [-h] module\n";
const resolve_help = resolve_usage ++
    \\
    \\positional arguments:
    \\  module
    \\
    \\options:
    \\  -h, --help  show this help message and exit
    \\
;

/// argparse's error exit: the usage, then "PROG: error: MESSAGE", status 2.
fn usageError(usage: []const u8, who: []const u8, comptime f: []const u8, args: anytype) noreturn {
    cli.put(2, usage);
    cli.err("{s}: error: " ++ f ++ "\n", .{who} ++ args);
    std.process.exit(2);
}

/// "eb-staged-rebuild: MESSAGE" on stderr, status 2: a refusal or an OSError.
pub fn refuse(comptime f: []const u8, args: anytype) noreturn {
    cli.err(prog ++ ": " ++ f ++ "\n", args);
    std.process.exit(2);
}

const Opt = struct {
    name: []const u8,
    takes_value: bool,
    value: ?[]const u8 = null,
    seen: bool = false,
};

/// argparse long options: --name, --name=value, any unique prefix of a name;
/// -h and --help print `help`. Positionals are returned in order; a word
/// starting with '-' that is no option is unrecognized.
fn parseOpts(a: Allocator, args: []const []const u8, opts: []Opt, help: []const u8, usage: []const u8, who: []const u8, extra: *std.ArrayList([]const u8)) []const []const u8 {
    var pos: std.ArrayList([]const u8) = .empty;
    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "--")) {
            for (args[i + 1 ..]) |p| pos.append(a, p) catch oom();
            break;
        }
        if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            cli.put(1, help);
            std.process.exit(0);
        }
        if (arg.len < 2 or arg[0] != '-') {
            pos.append(a, arg) catch oom();
            continue;
        }
        if (!std.mem.startsWith(u8, arg, "--")) {
            extra.append(a, arg) catch oom();
            continue;
        }
        const eq = std.mem.findScalar(u8, arg, '=');
        const name = arg[2 .. eq orelse arg.len];
        var match: ?*Opt = null;
        var count: usize = 0;
        for (opts) |*o| {
            if (std.mem.eql(u8, o.name, name)) {
                match = o;
                count = 1;
                break;
            }
            if (std.mem.startsWith(u8, o.name, name)) {
                match = o;
                count += 1;
            }
        }
        if (count > 1) usageError(usage, who, "ambiguous option: --{s} could match {s}", .{ name, ambiguous(a, opts, name) });
        const o = match orelse {
            extra.append(a, arg) catch oom();
            continue;
        };
        o.seen = true;
        if (!o.takes_value) {
            if (eq != null) usageError(usage, who, "argument --{s}: ignored explicit argument '{s}'", .{ o.name, arg[eq.? + 1 ..] });
            continue;
        }
        if (eq) |e| {
            o.value = arg[e + 1 ..];
        } else {
            if (i + 1 >= args.len or (args[i + 1].len > 0 and args[i + 1][0] == '-'))
                usageError(usage, who, "argument --{s}: expected one argument", .{o.name});
            i += 1;
            o.value = args[i];
        }
    }
    return pos.items;
}

fn ambiguous(a: Allocator, opts: []const Opt, name: []const u8) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    for (opts) |o| if (std.mem.startsWith(u8, o.name, name)) {
        if (out.items.len > 0) out.appendSlice(a, ", ") catch oom();
        out.print(a, "--{s}", .{o.name}) catch oom();
    };
    return out.items;
}

fn unrecognized(a: Allocator, extra: []const []const u8) void {
    if (extra.len == 0) return;
    const joined = std.mem.join(a, " ", extra) catch "";
    usageError(top_usage, prog, "unrecognized arguments: {s}", .{joined});
}

// ---- resolve -----------------------------------------------------------

/// lstat_kind: missing, symlink, dir, file or other.
pub fn lstatKind(path: []const u8) []const u8 {
    const st = fsx.statAt(fsx.cwd, path, false) catch return "missing";
    if (st.isLink()) return "symlink";
    if (st.isDir()) return "dir";
    if (st.isReg()) return "file";
    return "other";
}

/// generation_complete: a real directory with PAYLOAD, the tool's stamp or
/// an easybuild/*.eb file.
fn generationComplete(a: Allocator, path: []const u8) bool {
    if (!std.mem.eql(u8, lstatKind(path), "dir")) return false;
    for ([_][]const u8{ "PAYLOAD", ".eb-staged-rebuild-complete" }) |n| {
        const p = std.fs.path.join(a, &.{ path, n }) catch return false;
        if (std.mem.eql(u8, lstatKind(p), "file")) return true;
    }
    const meta = std.fs.path.join(a, &.{ path, "easybuild" }) catch return false;
    if (!std.mem.eql(u8, lstatKind(meta), "dir")) return false;
    const fd = fsx.openDir(fsx.cwd, meta, false) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    while (it.next() catch return false) |e| {
        if (!std.mem.endsWith(u8, e.name, ".eb")) continue;
        const p = std.fs.path.join(a, &.{ meta, e.name }) catch continue;
        if (std.mem.eql(u8, lstatKind(p), "file")) return true;
    }
    return false;
}

/// Python's str() of an OSError with a filename: "[Errno N] text: 'path'",
/// the path as repr() gives it. An error that is not an OSError is named.
pub fn osError(a: Allocator, err: anyerror, path: []const u8) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    out.appendSlice(a, if (common.sys.asError(err)) |e| common.sys.describe(e) else @errorName(err)) catch oom();
    out.appendSlice(a, ": ") catch oom();
    pyfmt.reprStr(&out, a, path) catch oom();
    return out.items;
}

fn cmdResolve(a: Allocator, args: []const []const u8) u8 {
    var opts = [_]Opt{};
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, resolve_help, resolve_usage, prog ++ " resolve", &extra);
    if (pos.len == 0) usageError(resolve_usage, prog ++ " resolve", "the following arguments are required: module", .{});
    var all_extra = extra;
    for (pos[1..]) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const path = pos[0];
    const kind = lstatKind(path);
    var described: std.ArrayList(Py.Entry) = .empty;
    described.append(a, .{ .key = "path", .value = Py.s(path) }) catch oom();
    described.append(a, .{ .key = "kind", .value = Py.s(kind) }) catch oom();
    var complete = false;
    if (!std.mem.eql(u8, kind, "file")) {
        described.append(a, .{ .key = "complete", .value = .{ .bool = false } }) catch oom();
        described.append(a, .{ .key = "root", .value = .null }) catch oom();
    } else {
        // Opened as Python's open() does: following a link, which lstat
        // above has already ruled out.
        const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("{s}", .{osError(a, err, path)});
        defer sys.close(f.fd);
        const data = fsx.readAll(a, f.fd, 64 << 20) catch |err| refuse("{s}", .{osError(a, err, path)});
        if (!std.unicode.utf8ValidateSlice(data)) refuse("{s} is not UTF-8 text", .{path});
        const root = modulefile.parseRoot(data);
        complete = root != null and root.?.path.len > 0 and generationComplete(a, root.?.path);
        described.append(a, .{ .key = "syntax", .value = if (root) |r| Py.s(@tagName(r.syntax)) else .null }) catch oom();
        described.append(a, .{ .key = "root", .value = if (root) |r| Py.s(r.path) else .null }) catch oom();
        described.append(a, .{ .key = "root_kind", .value = if (root) |r| (if (r.path.len > 0) Py.s(lstatKind(r.path)) else .null) else .null }) catch oom();
        described.append(a, .{ .key = "complete", .value = .{ .bool = complete } }) catch oom();
    }
    const out = json.dumps(a, .{ .dict = described.items }, 2, true) catch return 1;
    cli.put(1, out);
    cli.put(1, "\n");
    return if (std.mem.eql(u8, kind, "file") and complete) 0 else 1;
}

// ---- status ------------------------------------------------------------

const schema = 2;
const ledger_schema = 4;

fn needsIntervention(comptime f: []const u8, args: anytype) noreturn {
    refuse(f, args);
}

/// load_journal with its schema checks.
pub fn loadJournal(a: Allocator, path: []const u8) Py {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| needsIntervention("journal cannot be read: {s}: {s}", .{ path, osError(a, err, path) });
    defer sys.close(f.fd);
    const data = fsx.readAll(a, f.fd, 256 << 20) catch |err| needsIntervention("journal cannot be read: {s}: {s}", .{ path, osError(a, err, path) });
    if (!std.unicode.utf8ValidateSlice(data)) needsIntervention("journal cannot be read: {s}: not UTF-8 text", .{path});
    const parsed = json.parse(a, data) catch needsIntervention("journal cannot be read: {s}: not valid JSON", .{path});
    const j = json.toPy(a, parsed) catch needsIntervention("journal cannot be read: {s}: not valid JSON", .{path});
    if (j != .dict) {
        cli.err("Traceback: AttributeError: journal is not a JSON object: {s}\n", .{path});
        std.process.exit(1);
    }
    const sch = pyfmt.get(j, "schema");
    const sch_int: ?i64 = if (sch) |s| switch (s) {
        .int => |n| n,
        .bool => |b| @intFromBool(b),
        .float => |x| if (x == @floor(x) and @abs(x) < 1e15) @intFromFloat(x) else null,
        else => null,
    } else null;
    if (sch_int == 1 or sch_int == 3) {
        needsIntervention("journal schema {d} is a generation-pointer record{s}; this program publishes by directory swap and will not apply it", .{ sch_int.?, if (sch_int == 3) " and carries that line's ledger policy" else "" });
    }
    var shown: std.ArrayList(u8) = .empty;
    pyfmt.repr(&shown, a, sch orelse .null) catch oom();
    if (sch_int == ledger_schema) {
        const publ = pyfmt.get(j, "publication");
        const is_swap = publ != null and publ.? == .str and std.mem.eql(u8, publ.?.str, "directory-swap");
        const lp = pyfmt.get(j, "ledger_policy");
        if (!is_swap or lp == null or lp.? != .dict) needsIntervention("schema 4 journal is not a frozen directory-swap ledger record", .{});
    } else if (sch_int != schema) {
        needsIntervention("journal schema {s} is not {d} or {d}", .{ shown.items, schema, ledger_schema });
    }
    requireDirectorySwap(a, j);
    return j;
}

pub fn requireDirectorySwap(a: Allocator, j: Py) void {
    if (pyfmt.get(j, "publication")) |p| {
        const ok = p == .null or (p == .str and std.mem.eql(u8, p.str, "directory-swap"));
        if (!ok) {
            var shown: std.ArrayList(u8) = .empty;
            pyfmt.repr(&shown, a, p) catch oom();
            needsIntervention("journal publication {s} is not a directory swap", .{shown.items});
        }
    }
    if (pyfmt.get(j, "kind")) |k| if (k == .str and std.mem.eql(u8, k.str, "retire")) return;
    const plan = pyfmt.get(j, "modules_plan");
    if (!pyfmt.truthy(plan)) return;
    const items = switch (plan.?) {
        .list => |l| l,
        else => typeError("modules_plan is not a list"),
    };
    for (items) |m| {
        if (m != .dict) typeError("a modules_plan entry is not an object");
        if (pyfmt.get(m, "live_software") == null or pyfmt.get(m, "retired") == null)
            needsIntervention("journal does not record a directory swap; a generation-pointer plan is not applied", .{});
    }
}

fn typeError(msg: []const u8) noreturn {
    cli.err("Traceback: TypeError: {s}\n", .{msg});
    std.process.exit(1);
}

fn lessName(_: void, x: []const u8, y: []const u8) bool {
    return json.pyTextLess(x, y);
}

pub fn listJournals(a: Allocator, state_root: []const u8) []const Py {
    var found: std.ArrayList(Py) = .empty;
    const runs = std.fs.path.join(a, &.{ state_root, "runs" }) catch return &.{};
    const fd = fsx.openDir(fsx.cwd, runs, true) catch return &.{};
    defer sys.close(fd);
    var names: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(a, fd) catch return &.{};
    while (it.next() catch |err| refuse("{s}", .{osError(a, err, runs)})) |e| names.append(a, a.dupe(u8, e.name) catch continue) catch oom();
    std.mem.sort([]const u8, names.items, {}, lessName);
    for (names.items) |n| {
        const p = std.fs.path.join(a, &.{ runs, n, "journal.json" }) catch continue;
        const st = fsx.statAt(fsx.cwd, p, false) catch continue;
        if (!st.isReg()) continue;
        found.append(a, loadJournal(a, p)) catch oom();
    }
    return found.items;
}

fn line(a: Allocator, buf: *std.ArrayList(u8)) void {
    buf.append(a, '\n') catch oom();
    cli.put(1, buf.items);
    buf.clearRetainingCapacity();
}

fn cmdStatus(a: Allocator, args: []const []const u8) u8 {
    var opts = [_]Opt{.{ .name = "state-root", .takes_value = true }};
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, status_help, status_usage, prog ++ " status", &extra);
    if (opts[0].value == null) usageError(status_usage, prog ++ " status", "the following arguments are required: --state-root", .{});
    var all_extra = extra;
    if (pos.len > 1) for (pos[1..]) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    var journals = listJournals(a, opts[0].value.?);
    if (pos.len > 0) {
        const want = pos[0];
        var keep: std.ArrayList(Py) = .empty;
        for (journals) |j| if (pyfmt.get(j, "run_id")) |r| if (r == .str and std.mem.eql(u8, r.str, want)) keep.append(a, j) catch oom();
        journals = keep.items;
        if (journals.len == 0) refuse("no journal for {s}", .{want});
    }
    if (journals.len == 0) {
        cli.put(1, "no runs\n");
        return 0;
    }
    var b: std.ArrayList(u8) = .empty;
    for (journals) |j| {
        const kind = pyfmt.get(j, "kind");
        if (kind != null and kind.? == .str and std.mem.eql(u8, kind.?.str, "retire")) {
            pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch oom();
            b.appendSlice(a, "  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "state")) catch oom();
            b.appendSlice(a, "  retire  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "module_path")) catch oom();
            line(a, &b);
            b.appendSlice(a, "  software directory ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "software_dir")) catch oom();
            line(a, &b);
            continue;
        }
        const plan = pyfmt.get(j, "modules_plan");
        const mods: []const Py = if (pyfmt.truthy(plan)) switch (plan.?) {
            .list => |l| l,
            else => typeError("modules_plan is not a list"),
        } else &.{};
        var published: usize = 0;
        for (mods) |m| {
            if (m != .dict) typeError("a modules_plan entry is not an object");
            if (pyfmt.get(m, "status")) |s| if (s == .str and std.mem.eql(u8, s.str, "published")) {
                published += 1;
            };
        }
        pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch oom();
        b.appendSlice(a, "  ") catch oom();
        pyfmt.str(&b, a, pyfmt.get(j, "state")) catch oom();
        b.appendSlice(a, "  backend=") catch oom();
        pyfmt.str(&b, a, pyfmt.get(j, "backend")) catch oom();
        b.print(a, "  modules={d}/{d}", .{ published, mods.len }) catch oom();
        line(a, &b);
        if (pyfmt.truthy(pyfmt.get(j, "message"))) {
            b.appendSlice(a, "  ") catch oom();
            pyfmt.str(&b, a, pyfmt.get(j, "message")) catch oom();
            line(a, &b);
        }
        for (mods) |m| {
            b.appendSlice(a, "  ") catch oom();
            pyfmt.leftPad(&b, a, pyfmt.get(m, "status"), 10) catch typeError("unsupported format string passed to the status value's __format__");
            b.append(a, ' ') catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "rel")) catch oom();
            b.appendSlice(a, "  live=") catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "live_software")) catch oom();
            b.appendSlice(a, " retired=") catch oom();
            pyfmt.str(&b, a, pyfmt.get(m, "retired")) catch oom();
            line(a, &b);
        }
    }
    return 0;
}

const retire_usage =
    \\usage: eb-staged-rebuild retire [-h] --state-root STATE_ROOT
    \\                                --module-file MODULE_FILE [--reason REASON]
    \\                                [--dry-run] [--lock-timeout LOCK_TIMEOUT]
    \\
;
const retire_help = retire_usage ++
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --module-file MODULE_FILE
    \\                        module file to retire
    \\  --reason REASON
    \\  --dry-run
    \\  --lock-timeout LOCK_TIMEOUT
    \\                        seconds to wait for the lock (default: refuse at once)
    \\
;
const rollback_usage =
    \\usage: eb-staged-rebuild rollback [-h] --state-root STATE_ROOT
    \\                                  [--lock-timeout LOCK_TIMEOUT]
    \\                                  run_id
    \\
;
const rollback_help = rollback_usage ++
    \\
    \\positional arguments:
    \\  run_id
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --lock-timeout LOCK_TIMEOUT
    \\
;
const gc_usage =
    \\usage: eb-staged-rebuild gc [-h] --state-root STATE_ROOT
    \\                            [--grace-hours GRACE_HOURS] [--dry-run]
    \\                            [--lock-timeout LOCK_TIMEOUT] [--include-failed]
    \\
;
const gc_help = gc_usage ++
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --grace-hours GRACE_HOURS
    \\  --dry-run
    \\  --lock-timeout LOCK_TIMEOUT
    \\  --include-failed      also remove failed runs that published nothing and are
    \\                        older than the grace period
    \\
;

/// argparse type=float: Python float() of the text.
fn floatArg(usage: []const u8, who: []const u8, name: []const u8, v: []const u8) f64 {
    const t = std.mem.trim(u8, v, " \t\n\r\x0b\x0c");
    return std.fmt.parseFloat(f64, t) catch usageError(usage, who, "argument --{s}: invalid float value: '{s}'", .{ name, v });
}

fn missing(a: Allocator, usage: []const u8, who: []const u8, names: []const []const u8) void {
    if (names.len == 0) return;
    usageError(usage, who, "the following arguments are required: {s}", .{std.mem.join(a, ", ", names) catch ""});
}

fn adminCtx(a: Allocator, env: probe.Env) admin.Ctx {
    return .{ .a = a, .env = env, .pause = .{ .env = env } };
}

fn cmdRetire(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    var opts = [_]Opt{
        .{ .name = "state-root", .takes_value = true },
        .{ .name = "module-file", .takes_value = true },
        .{ .name = "reason", .takes_value = true },
        .{ .name = "dry-run", .takes_value = false },
        .{ .name = "lock-timeout", .takes_value = true },
    };
    const who = prog ++ " retire";
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, retire_help, retire_usage, who, &extra);
    var need: std.ArrayList([]const u8) = .empty;
    if (opts[0].value == null) need.append(a, "--state-root") catch oom();
    if (opts[1].value == null) need.append(a, "--module-file") catch oom();
    missing(a, retire_usage, who, need.items);
    var all_extra = extra;
    for (pos) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const timeout: f64 = if (opts[4].value) |v| floatArg(retire_usage, who, "lock-timeout", v) else 0;
    var ctx = adminCtx(a, env);
    return admin.retire(&ctx, opts[0].value.?, opts[1].value.?, opts[2].value, opts[3].seen, timeout);
}

fn cmdRollback(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    var opts = [_]Opt{
        .{ .name = "state-root", .takes_value = true },
        .{ .name = "lock-timeout", .takes_value = true },
    };
    const who = prog ++ " rollback";
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, rollback_help, rollback_usage, who, &extra);
    var need: std.ArrayList([]const u8) = .empty;
    if (opts[0].value == null) need.append(a, "--state-root") catch oom();
    if (pos.len == 0) need.append(a, "run_id") catch oom();
    missing(a, rollback_usage, who, need.items);
    var all_extra = extra;
    for (pos[1..]) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const timeout: ?f64 = if (opts[1].value) |v| floatArg(rollback_usage, who, "lock-timeout", v) else null;
    var ctx = adminCtx(a, env);
    return admin.rollback(&ctx, opts[0].value.?, pos[0], timeout);
}

fn cmdGc(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    var opts = [_]Opt{
        .{ .name = "state-root", .takes_value = true },
        .{ .name = "grace-hours", .takes_value = true },
        .{ .name = "dry-run", .takes_value = false },
        .{ .name = "lock-timeout", .takes_value = true },
        .{ .name = "include-failed", .takes_value = false },
    };
    const who = prog ++ " gc";
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, gc_help, gc_usage, who, &extra);
    if (opts[0].value == null) missing(a, gc_usage, who, &.{"--state-root"});
    var all_extra = extra;
    for (pos) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const grace: f64 = if (opts[1].value) |v| floatArg(gc_usage, who, "grace-hours", v) else blk: {
        const e = env.get("EB_STAGED_REBUILD_GRACE_HOURS") orelse break :blk admin.default_grace_hours;
        break :blk std.fmt.parseFloat(f64, std.mem.trim(u8, e, " \t\n\r\x0b\x0c")) catch refuse("EB_STAGED_REBUILD_GRACE_HOURS is not a number: '{s}'", .{e});
    };
    const timeout: ?f64 = if (opts[3].value) |v| floatArg(gc_usage, who, "lock-timeout", v) else null;
    var ctx = adminCtx(a, env);
    return admin.gc(&ctx, opts[0].value.?, grace, opts[2].seen, opts[4].seen, timeout);
}

const run_usage = @embedFile("run_usage.txt");
const run_help = @embedFile("run_help.txt");
const backends = [_][]const u8{ "auto", "bwrap", "userns", "installpath" };

/// run's options; the first word that is no option, or "--", starts the
/// easyconfig arguments (argparse REMAINDER).
fn cmdRun(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    const who = prog ++ " run";
    const names = [_][]const u8{ "software", "modules", "state-root", "eb", "backend", "grace-hours", "dry-run", "module", "run-id", "lock-timeout", "skip-live-check", "prepare", "ledger-config" };
    const takes = [_]bool{ true, true, true, true, true, true, false, true, true, true, false, true, true };
    var vals: [names.len]?[]const u8 = @splat(null);
    var seen: [names.len]bool = @splat(false);
    var modules_list: std.ArrayList([]const u8) = .empty;
    var rest: []const []const u8 = &.{};
    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            cli.put(1, run_help);
            return 0;
        }
        if (std.mem.eql(u8, arg, "--") or arg.len == 0 or arg[0] != '-') {
            rest = args[i..];
            break;
        }
        if (!std.mem.startsWith(u8, arg, "--")) usageError(top_usage, prog, "unrecognized arguments: {s}", .{arg});
        const eq = std.mem.findScalar(u8, arg, '=');
        const name = arg[2 .. eq orelse arg.len];
        var hit: ?usize = null;
        var count: usize = 0;
        for (names, 0..) |n, k| {
            if (std.mem.eql(u8, n, name)) {
                hit = k;
                count = 1;
                break;
            }
            if (std.mem.startsWith(u8, n, name)) {
                hit = k;
                count += 1;
            }
        }
        if (count > 1) {
            var amb: std.ArrayList(u8) = .empty;
            for (names) |n| if (std.mem.startsWith(u8, n, name)) {
                if (amb.items.len > 0) amb.appendSlice(a, ", ") catch oom();
                amb.print(a, "--{s}", .{n}) catch oom();
            };
            usageError(run_usage, who, "ambiguous option: --{s} could match {s}", .{ name, amb.items });
        }
        const k = hit orelse usageError(top_usage, prog, "unrecognized arguments: {s}", .{arg});
        seen[k] = true;
        if (!takes[k]) {
            if (eq != null) usageError(run_usage, who, "argument --{s}: ignored explicit argument '{s}'", .{ names[k], arg[eq.? + 1 ..] });
            continue;
        }
        var v: []const u8 = undefined;
        if (eq) |e| {
            v = arg[e + 1 ..];
        } else {
            if (i + 1 >= args.len or (args[i + 1].len > 0 and args[i + 1][0] == '-'))
                usageError(run_usage, who, "argument --{s}: expected one argument", .{names[k]});
            i += 1;
            v = args[i];
        }
        if (std.mem.eql(u8, names[k], "module")) modules_list.append(a, v) catch oom() else vals[k] = v;
    }
    if (vals[4]) |b| if (!engine_inList(&backends, b))
        usageError(run_usage, who, "argument --backend: invalid choice: '{s}' (choose from 'auto', 'bwrap', 'userns', 'installpath')", .{b});
    const grace_cli: ?f64 = if (vals[5]) |v| floatArg(run_usage, who, "grace-hours", v) else null;
    const lock_timeout: ?f64 = if (vals[9]) |v| floatArg(run_usage, who, "lock-timeout", v) else null;

    const software = nonEmpty(vals[0]) orelse nonEmpty(env.get("EASYBUILD_INSTALLPATH_SOFTWARE"));
    const modules = nonEmpty(vals[1]) orelse nonEmpty(env.get("EASYBUILD_INSTALLPATH_MODULES"));
    if (software == null or modules == null)
        refuse("--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required", .{});
    const state_root = nonEmpty(vals[2]) orelse nonEmpty(env.get("EB_STAGED_REBUILD_STATE_ROOT")) orelse
        (std.fs.path.join(a, &.{ std.fs.path.dirname(admin.abspath(a, software.?)) orelse "/", ".eb-staged-rebuild" }) catch "");
    const grace: f64 = grace_cli orelse blk: {
        const e = env.get("EB_STAGED_REBUILD_GRACE_HOURS") orelse break :blk admin.default_grace_hours;
        break :blk std.fmt.parseFloat(f64, std.mem.trim(u8, e, " \t\n\r\x0b\x0c")) catch refuse("EB_STAGED_REBUILD_GRACE_HOURS is not a number: '{s}'", .{e});
    };
    var eb_args = rest;
    if (eb_args.len > 0 and std.mem.eql(u8, eb_args[0], "--")) eb_args = eb_args[1..];
    const has_positional = for (eb_args) |x| {
        if (x.len == 0 or x[0] != '-') break true;
    } else false;
    if (!has_positional) refuse("pass the easyconfig after --, for example: eb-staged-rebuild run --software S --modules M -- file.eb", .{});
    if (vals[11]) |p| if (p.len == 0) refuse("--prepare must name a program; it is empty", .{});
    return engine.run(a, env, .{
        .software = software.?,
        .modules = modules.?,
        .state_root = state_root,
        .eb_args = eb_args,
        .backend = vals[4] orelse "auto",
        .grace_hours = grace,
        .dry_run = seen[6],
        .module_rels = modules_list.items,
        .eb_executable = vals[3] orelse (env.get("EB_STAGED_REBUILD_EB") orelse "eb"),
        .run_id = vals[8],
        .skip_live_check = seen[10],
        .lock_timeout = lock_timeout,
        .prepare_executable = vals[11],
        .ledger_config = vals[12],
    });
}

fn engine_inList(list: []const []const u8, s: []const u8) bool {
    for (list) |x| if (std.mem.eql(u8, x, s)) return true;
    return false;
}

fn nonEmpty(v: ?[]const u8) ?[]const u8 {
    const x = v orelse return null;
    return if (x.len > 0) x else null;
}

const resume_usage =
    \\usage: eb-staged-rebuild resume [-h] --state-root STATE_ROOT
    \\                                [--lock-timeout LOCK_TIMEOUT]
    \\                                run_id
    \\
;
const resume_help = resume_usage ++
    \\
    \\positional arguments:
    \\  run_id
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\  --state-root STATE_ROOT
    \\  --lock-timeout LOCK_TIMEOUT
    \\
;

fn cmdResume(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    var opts = [_]Opt{
        .{ .name = "state-root", .takes_value = true },
        .{ .name = "lock-timeout", .takes_value = true },
    };
    const who = prog ++ " resume";
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, resume_help, resume_usage, who, &extra);
    var need: std.ArrayList([]const u8) = .empty;
    if (opts[0].value == null) need.append(a, "--state-root") catch oom();
    if (pos.len == 0) need.append(a, "run_id") catch oom();
    missing(a, resume_usage, who, need.items);
    var all_extra = extra;
    for (pos[1..]) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const timeout: ?f64 = if (opts[1].value) |v| floatArg(resume_usage, who, "lock-timeout", v) else null;
    var ctx = adminCtx(a, env);
    const run_mod = @import("run.zig");
    const res = run_mod.resumeRun(a, env, &ctx, opts[0].value.?, pos[0], timeout) catch |err| switch (err) {
        error.OutOfMemory => refuse("out of memory", .{}),
        else => refuse("{s}", .{last_why}),
    };
    return res.code;
}

// ---- probe -------------------------------------------------------------

fn cmdProbe(a: Allocator, args: []const []const u8, env: probe.Env) u8 {
    var opts = [_]Opt{
        .{ .name = "path", .takes_value = true },
        .{ .name = "eb", .takes_value = true },
        .{ .name = "json", .takes_value = false },
    };
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, probe_help, probe_usage, prog ++ " probe", &extra);
    var all_extra = extra;
    for (pos) |p| all_extra.append(a, p) catch oom();
    unrecognized(a, all_extra.items);
    const eb_default = env.get("EB_STAGED_REBUILD_EB") orelse "eb";
    const eb = opts[1].value orelse eb_default;
    const directory = blk: {
        if (opts[0].value) |p| if (p.len > 0) break :blk p;
        var rb: [sys.path_max]u8 = undefined;
        break :blk a.dupe(u8, fsx.realpath(fsx.cwd, ".", &rb) catch ".") catch ".";
    };
    const report = probe.probeAll(a, env, directory, eb) catch |err| refuse("{s}", .{osError(a, err, directory)});
    if (opts[2].seen) {
        const out = json.dumps(a, report, 2, true) catch return 1;
        cli.put(1, out);
        cli.put(1, "\n");
    } else {
        probe.printText(a, report);
    }
    return 0;
}

pub fn main(init: std.process.Init.Minimal) u8 {
    var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    const a = arena_state.allocator();
    const all = init.args.toSlice(a) catch return 2;
    const args: []const []const u8 = if (all.len > 1) @ptrCast(all[1..]) else &.{};
    const env: probe.Env = .{ .envp = init.environ.block.slice.ptr };
    // Only the -Dtest-ops test artifact can emulate a file system's ACLs.
    if (comptime common.atomic.replaceable) @import("aclmodel.zig").installFromEnv(env);

    // Internal: the live-tree snapshot as JSON, for the differential tests.
    if (args.len == 2 and std.mem.eql(u8, args[0], "__snapshot")) {
        const snap = @import("livecheck.zig").snapshot(a, args[1]) catch |err| refuse("{s}", .{osError(a, err, args[1])});
        cli.put(1, json.dumps(a, snap, 2, true) catch return 1);
        cli.put(1, "\n");
        return 0;
    }
    // The user-namespace sandbox re-executes this program.
    if (args.len > 0 and std.mem.eql(u8, args[0], probe.sandbox_command)) return probe.sandboxMain(a, env, args[1..]);

    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "-h") or (arg.len >= 3 and std.mem.startsWith(u8, "--help", arg))) {
            cli.put(1, top_help);
            return 0;
        }
        if (std.mem.eql(u8, arg, "--version") or (arg.len >= 3 and std.mem.startsWith(u8, "--version", arg))) {
            cli.put(1, prog ++ " " ++ version ++ "\n");
            return 0;
        }
        if (arg.len > 0 and arg[0] == '-') usageError(top_usage, prog, "unrecognized arguments: {s}", .{arg});
        break;
    }
    if (i >= args.len) usageError(top_usage, prog, "the following arguments are required: command", .{});
    const cmd = args[i];
    const rest = args[i + 1 ..];
    if (std.mem.eql(u8, cmd, "resolve")) return cmdResolve(a, rest);
    if (std.mem.eql(u8, cmd, "status")) return cmdStatus(a, rest);
    if (std.mem.eql(u8, cmd, "probe")) return cmdProbe(a, rest, env);
    if (std.mem.eql(u8, cmd, "ledger-maintenance")) return ledger.main(a, rest, .{ .envp = env.envp });
    if (std.mem.eql(u8, cmd, "retire")) return cmdRetire(a, rest, env);
    if (std.mem.eql(u8, cmd, "rollback")) return cmdRollback(a, rest, env);
    if (std.mem.eql(u8, cmd, "gc")) return cmdGc(a, rest, env);
    if (std.mem.eql(u8, cmd, "run")) return cmdRun(a, rest, env);
    if (std.mem.eql(u8, cmd, "resume")) return cmdResume(a, rest, env);
    if (std.mem.eql(u8, cmd, "build-events")) return @import("build_events.zig").main(a, rest, env);
    for (commands) |c| if (std.mem.eql(u8, c, cmd)) refuse("{s} is not in this build yet; use the Python eb-staged-rebuild", .{cmd});
    usageError(top_usage, prog, "argument command: invalid choice: '{s}' (choose from 'probe', 'run', 'resume', 'rollback', 'gc', 'retire', 'status', 'resolve')", .{cmd});
}

/// The message of the last refusal the engine returned as an error.
pub var last_why: []const u8 = "";

test {
    _ = @import("run.zig");
    _ = @import("publish.zig");
    _ = @import("build_events.zig");
    _ = @import("swap.zig");
    _ = @import("livecheck.zig");
    _ = @import("discover.zig");
    _ = @import("builders.zig");
    _ = admin;
    _ = engine;
    _ = ledger;
    _ = modulefile;
    _ = pyfmt;
    _ = probe;
}

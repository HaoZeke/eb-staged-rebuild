//! eb-staged-rebuild: build EasyBuild software in a staged tree, then
//! exchange the complete tree with the stable live directory.
//!
//! This build carries the read-only commands (probe, status, resolve); the
//! commands that change installations follow in the order the port plan
//! sets, and until then report that they are not in this build.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");
const probe = @import("probe.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

pub const version = "0.4.4";
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
fn refuse(comptime f: []const u8, args: anytype) noreturn {
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
            for (args[i + 1 ..]) |p| pos.append(a, p) catch {};
            break;
        }
        if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            cli.put(1, help);
            std.process.exit(0);
        }
        if (arg.len < 2 or arg[0] != '-') {
            pos.append(a, arg) catch {};
            continue;
        }
        if (!std.mem.startsWith(u8, arg, "--")) {
            extra.append(a, arg) catch {};
            continue;
        }
        const eq = std.mem.indexOfScalar(u8, arg, '=');
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
            extra.append(a, arg) catch {};
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
        if (out.items.len > 0) out.appendSlice(a, ", ") catch {};
        out.print(a, "--{s}", .{o.name}) catch {};
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

fn osError(a: Allocator, err: anyerror, path: []const u8) []const u8 {
    const t: struct { n: u32, s: []const u8 } = switch (err) {
        error.NotFound => .{ .n = 2, .s = "No such file or directory" },
        error.AccessDenied => .{ .n = 13, .s = "Permission denied" },
        error.IsDir => .{ .n = 21, .s = "Is a directory" },
        error.NotDir => .{ .n = 20, .s = "Not a directory" },
        error.SymLink => .{ .n = 40, .s = "Too many levels of symbolic links" },
        else => return std.fmt.allocPrint(a, "{t}: '{s}'", .{ err, path }) catch path,
    };
    return std.fmt.allocPrint(a, "[Errno {d}] {s}: '{s}'", .{ t.n, t.s, path }) catch path;
}

fn cmdResolve(a: Allocator, args: []const []const u8) u8 {
    var opts = [_]Opt{};
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, resolve_help, resolve_usage, prog ++ " resolve", &extra);
    if (pos.len == 0) usageError(resolve_usage, prog ++ " resolve", "the following arguments are required: module", .{});
    var all_extra = extra;
    for (pos[1..]) |p| all_extra.append(a, p) catch {};
    unrecognized(a, all_extra.items);
    const path = pos[0];
    const kind = lstatKind(path);
    var described: std.ArrayList(Py.Entry) = .empty;
    described.append(a, .{ .key = "path", .value = Py.s(path) }) catch {};
    described.append(a, .{ .key = "kind", .value = Py.s(kind) }) catch {};
    var complete = false;
    if (!std.mem.eql(u8, kind, "file")) {
        described.append(a, .{ .key = "complete", .value = .{ .bool = false } }) catch {};
        described.append(a, .{ .key = "root", .value = .null }) catch {};
    } else {
        // Opened as Python's open() does: following a link, which lstat
        // above has already ruled out.
        const f = fsx.openRegular(fsx.cwd, path, true) catch |err| refuse("{s}", .{osError(a, err, path)});
        defer sys.close(f.fd);
        const data = fsx.readAll(a, f.fd, 64 << 20) catch |err| refuse("{s}", .{osError(a, err, path)});
        if (!std.unicode.utf8ValidateSlice(data)) refuse("{s} is not UTF-8 text", .{path});
        const root = modulefile.parseRoot(data);
        complete = root != null and root.?.path.len > 0 and generationComplete(a, root.?.path);
        described.append(a, .{ .key = "syntax", .value = if (root) |r| Py.s(@tagName(r.syntax)) else .null }) catch {};
        described.append(a, .{ .key = "root", .value = if (root) |r| Py.s(r.path) else .null }) catch {};
        described.append(a, .{ .key = "root_kind", .value = if (root) |r| (if (r.path.len > 0) Py.s(lstatKind(r.path)) else .null) else .null }) catch {};
        described.append(a, .{ .key = "complete", .value = .{ .bool = complete } }) catch {};
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
fn loadJournal(a: Allocator, path: []const u8) Py {
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
    pyfmt.repr(&shown, a, sch orelse .null) catch {};
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

fn requireDirectorySwap(a: Allocator, j: Py) void {
    if (pyfmt.get(j, "publication")) |p| {
        const ok = p == .null or (p == .str and std.mem.eql(u8, p.str, "directory-swap"));
        if (!ok) {
            var shown: std.ArrayList(u8) = .empty;
            pyfmt.repr(&shown, a, p) catch {};
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

fn listJournals(a: Allocator, state_root: []const u8) []const Py {
    var found: std.ArrayList(Py) = .empty;
    const runs = std.fs.path.join(a, &.{ state_root, "runs" }) catch return &.{};
    const fd = fsx.openDir(fsx.cwd, runs, true) catch return &.{};
    defer sys.close(fd);
    var names: std.ArrayList([]const u8) = .empty;
    var it = fsx.DirIter.init(a, fd) catch return &.{};
    while (it.next() catch |err| refuse("{s}", .{osError(a, err, runs)})) |e| names.append(a, a.dupe(u8, e.name) catch continue) catch {};
    std.mem.sort([]const u8, names.items, {}, lessName);
    for (names.items) |n| {
        const p = std.fs.path.join(a, &.{ runs, n, "journal.json" }) catch continue;
        const st = fsx.statAt(fsx.cwd, p, false) catch continue;
        if (!st.isReg()) continue;
        found.append(a, loadJournal(a, p)) catch {};
    }
    return found.items;
}

fn line(a: Allocator, buf: *std.ArrayList(u8)) void {
    buf.append(a, '\n') catch {};
    cli.put(1, buf.items);
    buf.clearRetainingCapacity();
}

fn cmdStatus(a: Allocator, args: []const []const u8) u8 {
    var opts = [_]Opt{.{ .name = "state-root", .takes_value = true }};
    var extra: std.ArrayList([]const u8) = .empty;
    const pos = parseOpts(a, args, &opts, status_help, status_usage, prog ++ " status", &extra);
    if (opts[0].value == null) usageError(status_usage, prog ++ " status", "the following arguments are required: --state-root", .{});
    var all_extra = extra;
    if (pos.len > 1) for (pos[1..]) |p| all_extra.append(a, p) catch {};
    unrecognized(a, all_extra.items);
    var journals = listJournals(a, opts[0].value.?);
    if (pos.len > 0) {
        const want = pos[0];
        var keep: std.ArrayList(Py) = .empty;
        for (journals) |j| if (pyfmt.get(j, "run_id")) |r| if (r == .str and std.mem.eql(u8, r.str, want)) keep.append(a, j) catch {};
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
            pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch {};
            b.appendSlice(a, "  ") catch {};
            pyfmt.str(&b, a, pyfmt.get(j, "state")) catch {};
            b.appendSlice(a, "  retire  ") catch {};
            pyfmt.str(&b, a, pyfmt.get(j, "module_path")) catch {};
            line(a, &b);
            b.appendSlice(a, "  software directory ") catch {};
            pyfmt.str(&b, a, pyfmt.get(j, "software_dir")) catch {};
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
        pyfmt.str(&b, a, pyfmt.get(j, "run_id")) catch {};
        b.appendSlice(a, "  ") catch {};
        pyfmt.str(&b, a, pyfmt.get(j, "state")) catch {};
        b.appendSlice(a, "  backend=") catch {};
        pyfmt.str(&b, a, pyfmt.get(j, "backend")) catch {};
        b.print(a, "  modules={d}/{d}", .{ published, mods.len }) catch {};
        line(a, &b);
        if (pyfmt.truthy(pyfmt.get(j, "message"))) {
            b.appendSlice(a, "  ") catch {};
            pyfmt.str(&b, a, pyfmt.get(j, "message")) catch {};
            line(a, &b);
        }
        for (mods) |m| {
            b.appendSlice(a, "  ") catch {};
            pyfmt.leftPad(&b, a, pyfmt.get(m, "status"), 10) catch typeError("unsupported format string passed to the status value's __format__");
            b.append(a, ' ') catch {};
            pyfmt.str(&b, a, pyfmt.get(m, "rel")) catch {};
            b.appendSlice(a, "  live=") catch {};
            pyfmt.str(&b, a, pyfmt.get(m, "live_software")) catch {};
            b.appendSlice(a, " retired=") catch {};
            pyfmt.str(&b, a, pyfmt.get(m, "retired")) catch {};
            line(a, &b);
        }
    }
    return 0;
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
    for (pos) |p| all_extra.append(a, p) catch {};
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
    for (commands) |c| if (std.mem.eql(u8, c, cmd)) refuse("{s} is not in this build yet; use the Python eb-staged-rebuild", .{cmd});
    usageError(top_usage, prog, "argument command: invalid choice: '{s}' (choose from 'probe', 'run', 'resume', 'rollback', 'gc', 'retire', 'status', 'resolve')", .{cmd});
}

test {
    _ = modulefile;
    _ = pyfmt;
    _ = probe;
}

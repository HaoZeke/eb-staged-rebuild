//! Publication to an eb-stack ledger: the configuration, one record per
//! published module file, a durable per-run outbox delivered in order, and
//! the bounded `eb-stack ledger append` (and `verify`) whose receipt is
//! checked against the head file it wrote and the log line it names. A
//! record without a receipt stays pending with the same bytes and event id,
//! so a retry is acknowledged once.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const modulefile = @import("modulefile.zig");
const pyfmt = @import("pyfmt.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const Allocator = std.mem.Allocator;
const Py = json.Py;

pub const pending_exit: u8 = 75;
const output_limit = 64 * 1024;

pub const Error = error{ Refused, NeedsIntervention, OutOfMemory };

fn s(v: ?Py) []const u8 {
    const x = v orelse return "";
    return if (x == .str) x.str else "";
}

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

/// json.dumps(v, sort_keys=sort) with the default ", " and ": " separators
/// and no newlines.
pub fn dumpsInline(a: Allocator, v: Py, sort: bool) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    try inline_(a, &out, v, sort);
    return out.items;
}

fn inline_(a: Allocator, out: *std.ArrayList(u8), v: Py, sort: bool) Allocator.Error!void {
    switch (v) {
        .list => |items| {
            try out.append(a, '[');
            for (items, 0..) |it, i| {
                if (i > 0) try out.appendSlice(a, ", ");
                try inline_(a, out, it, sort);
            }
            try out.append(a, ']');
        },
        .dict => |entries| {
            const e2 = try a.dupe(Py.Entry, entries);
            if (sort) std.mem.sort(Py.Entry, e2, {}, struct {
                fn lt(_: void, x: Py.Entry, y: Py.Entry) bool {
                    return json.pyTextLess(x.key, y.key);
                }
            }.lt);
            try out.append(a, '{');
            for (e2, 0..) |e, i| {
                if (i > 0) try out.appendSlice(a, ", ");
                try json.writeCacheString(out, a, e.key);
                try out.appendSlice(a, ": ");
                try inline_(a, out, e.value, sort);
            }
            try out.append(a, '}');
        },
        else => try out.appendSlice(a, try json.dumps(a, v, 0, false)),
    }
}

// ---- configuration --------------------------------------------------------

const fields = [_][]const u8{ "actor", "stack", "target" };
const actions = [_][]const u8{ "install", "rebuild-inplace", "rebuild-bump" };

/// load_config: the ledger configuration with its defaults and "path".
pub fn loadConfig(a: Allocator, path_in: []const u8, why: *[]const u8) Error!Py {
    const path = admin.abspath(a, path_in);
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| {
        why.* = try std.fmt.allocPrint(a, "cannot read ledger config {s}: {s}", .{ path, main_mod.osError(a, err, path) });
        return error.Refused;
    };
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 16 << 20) catch {
        why.* = try std.fmt.allocPrint(a, "cannot read ledger config {s}: read error", .{path});
        return error.Refused;
    };
    const parsed = json.parse(a, raw) catch {
        why.* = try std.fmt.allocPrint(a, "cannot read ledger config {s}: not valid JSON", .{path});
        return error.Refused;
    };
    var config = json.toPy(a, parsed) catch {
        why.* = try std.fmt.allocPrint(a, "cannot read ledger config {s}: not valid JSON", .{path});
        return error.Refused;
    };
    if (config != .dict) {
        why.* = try std.fmt.allocPrint(a, "ledger config {s} is not a JSON object", .{path});
        return error.Refused;
    }
    for ([_][]const u8{ "eb_stack", "log", "key", "head" }) |k| {
        const v = pyfmt.get(config, k);
        if (v == null or v.? != .str or v.?.str.len == 0 or v.?.str[0] != '/') {
            why.* = try std.fmt.allocPrint(a, "ledger config {s} must be an absolute path", .{k});
            return error.Refused;
        }
    }
    if (pyfmt.get(config, "public_key")) |pk| if (pk != .null) {
        var b: std.ArrayList(u8) = .empty;
        try pyfmt.str(&b, a, pk);
        if (b.items.len == 0 or b.items[0] != '/') {
            why.* = "ledger config public_key must be an absolute path";
            return error.Refused;
        }
    };
    for (fields) |k| {
        const v = pyfmt.get(config, k);
        if (v == null or v.? != .str or v.?.str.len == 0) {
            why.* = try std.fmt.allocPrint(a, "ledger config {s} must be a non-empty string", .{k});
            return error.Refused;
        }
    }
    if (pyfmt.get(config, "action") == null) config = dictSet(a, config, "action", Py.s("rebuild-inplace"));
    const act = pyfmt.get(config, "action").?;
    var ok_action = false;
    if (act == .str) for (actions) |x| if (std.mem.eql(u8, x, act.str)) {
        ok_action = true;
    };
    if (!ok_action) {
        why.* = "ledger config action must be one of install, rebuild-inplace, rebuild-bump";
        return error.Refused;
    }
    if (pyfmt.get(config, "required") == null) config = dictSet(a, config, "required", .{ .bool = true });
    if (pyfmt.get(config, "required").? != .bool) {
        why.* = "ledger config required must be true or false";
        return error.Refused;
    }
    if (pyfmt.get(config, "timeout") == null) config = dictSet(a, config, "timeout", .{ .int = 120 });
    const t = pyfmt.get(config, "timeout").?;
    const positive = switch (t) {
        .int => |n| n > 0,
        .float => |x| x > 0,
        else => false,
    };
    if (!positive) {
        why.* = "ledger config timeout must be a positive number of seconds";
        return error.Refused;
    }
    config = dictSet(a, config, "path", Py.s(path));
    return config;
}

pub fn sameValue(a: Allocator, x: Py, y: Py) bool {
    const dx = json.dumps(a, x, 0, true) catch return false;
    const dy = json.dumps(a, y, 0, true) catch return false;
    return std.mem.eql(u8, dx, dy);
}

fn timeoutSeconds(config: Py) f64 {
    return switch (pyfmt.get(config, "timeout") orelse Py{ .int = 120 }) {
        .int => |n| @floatFromInt(n),
        .float => |x| x,
        else => 120,
    };
}

fn timeoutText(a: Allocator, config: Py) []const u8 {
    var b: std.ArrayList(u8) = .empty;
    pyfmt.str(&b, a, pyfmt.get(config, "timeout") orelse Py{ .int = 120 }) catch {};
    return b.items;
}

// ---- records --------------------------------------------------------------

fn utc(a: Allocator, t: f64) []const u8 {
    const secs: u64 = @intFromFloat(@max(@floor(t), 0));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    return std.fmt.allocPrint(a, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

pub const Corrects = std.StringArrayHashMapUnmanaged(Py);

/// publication_records: one record per published module file; with
/// `corrects`, the correction records of a rollback.
pub fn publicationRecords(a: Allocator, config: Py, journal: Py, stage: []const u8, now: f64, corrects: ?*const Corrects, why: *[]const u8) Error![]const Py {
    var digest: [32]u8 = undefined;
    std.crypto.hash.sha2.Sha256.hash(s(pyfmt.get(journal, "state_root")), &digest, .{});
    const hex = std.fmt.bytesToHex(digest, .lower);
    const scope = hex[0..16];
    var records: std.ArrayList(Py) = .empty;
    const run_id = s(pyfmt.get(journal, "run_id"));
    const plan = pyfmt.get(journal, "modules_plan") orelse Py{ .list = &.{} };
    for (plan.list) |m| {
        if (pyfmt.get(m, "live_software") == null) {
            why.* = "ledger records require a directory-swap plan; a generation-pointer journal is not published to the ledger";
            return error.Refused;
        }
        const rel = s(pyfmt.get(m, "rel"));
        if (corrects) |c| if (!c.contains(rel)) continue;
        var new = pyfmt.get(m, "new_hash") orelse .null;
        var old = pyfmt.get(m, "old_hash") orelse .null;
        if (corrects != null) std.mem.swap(Py, &new, &old);
        const name = if (corrects != null) try std.fmt.allocPrint(a, "rollback-{s}", .{stage}) else stage;
        const live = s(pyfmt.get(m, "live_software"));
        var entries: std.ArrayList(Py.Entry) = .empty;
        try entries.appendSlice(a, &.{
            .{ .key = "ts", .value = Py.s(utc(a, now)) },
            .{ .key = "actor", .value = pyfmt.get(config, "actor").? },
            .{ .key = "stack", .value = pyfmt.get(config, "stack").? },
            .{ .key = "target", .value = pyfmt.get(config, "target").? },
            .{ .key = "module", .value = Py.s(modulefile.relpath(a, live, s(pyfmt.get(journal, "software")))) },
            .{ .key = "location", .value = Py.s(live) },
            .{ .key = "action", .value = if (corrects != null) Py.s("correction") else pyfmt.get(config, "action").? },
            .{ .key = "stage", .value = Py.s(stage) },
            .{ .key = "modulefile", .value = .{ .dict = try a.dupe(Py.Entry, &.{
                .{ .key = "path", .value = pyfmt.get(m, "live_module") orelse .null },
                .{ .key = "sha256", .value = new },
                .{ .key = "previous_sha256", .value = old },
            }) } },
            .{ .key = "build", .value = .{ .dict = try a.dupe(Py.Entry, &.{
                .{ .key = "event_id", .value = Py.s(try std.fmt.allocPrint(a, "eb-staged-rebuild/{s}/{s}/{s}/{s}", .{ scope, run_id, rel, name })) },
                .{ .key = "run_id", .value = Py.s(run_id) },
                .{ .key = "module_file", .value = Py.s(rel) },
                .{ .key = "tool", .value = Py.s("eb-staged-rebuild") },
            }) } },
        });
        if (corrects) |c| {
            try entries.append(a, .{ .key = "corrects", .value = c.get(rel).? });
            try entries.append(a, .{ .key = "reason", .value = Py.s(try std.fmt.allocPrint(a, "run {s} was rolled back", .{run_id})) });
        }
        try records.append(a, .{ .dict = entries.items });
    }
    return records.items;
}

// ---- the outbox -------------------------------------------------------------

fn outboxPath(a: Allocator, run_dir: []const u8) []const u8 {
    return std.fs.path.join(a, &.{ run_dir, "outbox.json" }) catch "";
}

pub fn load(a: Allocator, run_dir: []const u8, why: *[]const u8) Error!Py {
    const path = outboxPath(a, run_dir);
    if (fsx.statAt(fsx.cwd, path, false)) |_| {} else |_| return .{ .dict = try a.dupe(Py.Entry, &.{ .{ .key = "schema", .value = .{ .int = 1 } }, .{ .key = "events", .value = .{ .list = &.{} } } }) };
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| {
        why.* = try std.fmt.allocPrint(a, "outbox cannot be read: {s}: {s}", .{ path, main_mod.osError(a, err, path) });
        return error.NeedsIntervention;
    };
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 256 << 20) catch {
        why.* = try std.fmt.allocPrint(a, "outbox cannot be read: {s}", .{path});
        return error.NeedsIntervention;
    };
    const data = json.toPy(a, json.parse(a, raw) catch {
        why.* = try std.fmt.allocPrint(a, "outbox cannot be read: {s}: not valid JSON", .{path});
        return error.NeedsIntervention;
    }) catch {
        why.* = try std.fmt.allocPrint(a, "outbox cannot be read: {s}: not valid JSON", .{path});
        return error.NeedsIntervention;
    };
    const sch = pyfmt.get(data, "schema");
    if (sch == null or sch.? != .int or sch.?.int != 1) {
        var b: std.ArrayList(u8) = .empty;
        try pyfmt.repr(&b, a, sch orelse .null);
        why.* = try std.fmt.allocPrint(a, "outbox schema {s} is not 1", .{b.items});
        return error.NeedsIntervention;
    }
    return data;
}

fn save(a: Allocator, run_dir: []const u8, data: Py, faults: atomic.Faults, step: []const u8, why: *[]const u8) Error!void {
    const raw = try std.fmt.allocPrint(a, "{s}\n", .{try json.dumps(a, data, 2, true)});
    var w: atomic.Why = .{};
    atomic.atomicWrite(a, outboxPath(a, run_dir), raw, .{ .faults = faults, .step = step }, &w) catch |err| {
        why.* = if (err == error.Refused) w.msg else @errorName(err);
        return error.Refused;
    };
}

pub fn pendingEvents(a: Allocator, data: Py) []const Py {
    var out: std.ArrayList(Py) = .empty;
    const events = pyfmt.get(data, "events") orelse return &.{};
    for (events.list) |e| if (!std.mem.eql(u8, s(pyfmt.get(e, "status")), "acknowledged")) out.append(a, e) catch {};
    return out.items;
}

pub fn enqueue(a: Allocator, run_dir: []const u8, records: []const Py, faults: atomic.Faults, step: []const u8, why: *[]const u8) Error!void {
    const data = try load(a, run_dir, why);
    var events: std.ArrayList(Py) = .empty;
    try events.appendSlice(a, (pyfmt.get(data, "events") orelse Py{ .list = &.{} }).list);
    var added = false;
    for (records) |rec| {
        const id = s(pyfmt.get(pyfmt.get(rec, "build").?, "event_id"));
        var known = false;
        for (events.items) |e| if (std.mem.eql(u8, s(pyfmt.get(e, "event_id")), id)) {
            known = true;
        };
        if (known) continue;
        try events.append(a, .{ .dict = try a.dupe(Py.Entry, &.{
            .{ .key = "seq", .value = .{ .int = @intCast(events.items.len) } },
            .{ .key = "event_id", .value = Py.s(id) },
            .{ .key = "record", .value = rec },
            .{ .key = "status", .value = Py.s("pending") },
            .{ .key = "attempts", .value = .{ .int = 0 } },
            .{ .key = "error", .value = .null },
            .{ .key = "receipt", .value = .null },
        }) });
        added = true;
    }
    if (added) try save(a, run_dir, dictSet(a, data, "events", .{ .list = events.items }), faults, step, why);
}

/// deliver: in order, stopping at the first event without a receipt.
/// Returns the events still pending.
pub fn deliver(a: Allocator, env_envp: common.proc.Envp, run_dir: []const u8, config: Py, faults: atomic.Faults, why: *[]const u8) Error![]const Py {
    var data = try load(a, run_dir, why);
    const events = try a.dupe(Py, (pyfmt.get(data, "events") orelse Py{ .list = &.{} }).list);
    for (events, 0..) |ev_in, i| {
        var ev = ev_in;
        if (std.mem.eql(u8, s(pyfmt.get(ev, "status")), "acknowledged")) continue;
        const id = s(pyfmt.get(ev, "event_id"));
        const seq: i64 = (pyfmt.get(ev, "seq") orelse Py{ .int = 0 }).int;
        const record_path = try std.fs.path.join(a, &.{ run_dir, "events", try std.fmt.allocPrint(a, "{d:0>4}.json", .{@as(u64, @intCast(seq))}) });
        const body = try std.fmt.allocPrint(a, "{s}\n", .{try dumpsInline(a, pyfmt.get(ev, "record").?, true)});
        const same = blk: {
            const f = fsx.openRegular(fsx.cwd, record_path, true) catch break :blk false;
            defer sys.close(f.fd);
            const cur = fsx.readAll(a, f.fd, 16 << 20) catch break :blk false;
            break :blk std.mem.eql(u8, cur, body);
        };
        if (!same) {
            var w: atomic.Why = .{};
            atomic.atomicWrite(a, record_path, body, .{ .faults = faults, .step = try std.fmt.allocPrint(a, "outbox.record:{s}", .{id}) }, &w) catch |err| {
                why.* = if (err == error.Refused) w.msg else @errorName(err);
                return error.Refused;
            };
        }
        faults.reach(try std.fmt.allocPrint(a, "ledger.append:{s}", .{id}));
        const attempts = (pyfmt.get(ev, "attempts") orelse Py{ .int = 0 }).int + 1;
        ev = dictSet(a, ev, "attempts", .{ .int = attempts });
        const receipt = append(a, env_envp, config, record_path) catch |err| switch (err) {
            error.Publisher => {
                ev = dictSet(a, ev, "error", Py.s(last_publisher_error));
                events[i] = ev;
                data = dictSet(a, data, "events", .{ .list = events });
                try save(a, run_dir, data, faults, try std.fmt.allocPrint(a, "outbox.failed:{s}", .{id}), why);
                return pendingEvents(a, data);
            },
            error.OutOfMemory => return error.OutOfMemory,
        };
        faults.reach(try std.fmt.allocPrint(a, "ledger.acked:{s}", .{id}));
        ev = dictSet(a, ev, "status", Py.s("acknowledged"));
        ev = dictSet(a, ev, "error", .null);
        ev = dictSet(a, ev, "receipt", receipt);
        events[i] = ev;
        data = dictSet(a, data, "events", .{ .list = events });
        try save(a, run_dir, data, faults, try std.fmt.allocPrint(a, "outbox.acked:{s}", .{id}), why);
    }
    return &.{};
}

// ---- eb-stack ledger append --------------------------------------------------

var last_publisher_error: []const u8 = "";

fn publisherError(a: Allocator, comptime f: []const u8, args: anytype) error{Publisher} {
    last_publisher_error = std.fmt.allocPrint(a, f, args) catch "publisher failed";
    return error.Publisher;
}

const Bounded = struct { code: u32, stdout: []const u8, stderr: []const u8 };

/// _bounded: the program in its own process group with a deadline; the
/// first 64 KiB of each stream kept.
fn bounded(a: Allocator, envp: common.proc.Envp, argv: []const []const u8, config: Py) error{ Publisher, OutOfMemory }!Bounded {
    const ms: u32 = @intFromFloat(@min(timeoutSeconds(config) * 1000, 4.0e9));
    const r = common.proc.run(a, argv[0], argv, envp, .{ .stdout_limit = 64 << 20, .stderr_limit = 64 << 20, .timeout_ms = ms, .grace_ms = 0 }) catch |err| switch (err) {
        error.TimedOut => return publisherError(a, "{s} {s} {s} timed out after {s}s", .{ argv[0], argv[1], argv[2], timeoutText(a, config) }),
        error.OutOfMemory => return error.OutOfMemory,
        else => return publisherError(a, "cannot start {s}: {s}", .{ argv[0], main_mod.osError(a, err, argv[0]) }),
    };
    const out = r.stdout[0..@min(r.stdout.len, output_limit)];
    const er = r.stderr[0..@min(r.stderr.len, output_limit)];
    return .{ .code = r.term.code(), .stdout = try common.pytext.decodeReplace(a, out), .stderr = try common.pytext.decodeReplace(a, er) };
}

fn lastChars(t: []const u8, n: usize) []const u8 {
    return if (t.len > n) t[t.len - n ..] else t;
}

fn isHex64(x: []const u8) bool {
    if (x.len != 64) return false;
    for (x) |c| if (!(std.ascii.isDigit(c) or (c >= 'a' and c <= 'f'))) return false;
    return true;
}

/// The summary line: "(appended|already recorded) record N id HEX; head
/// size N root HEX" with an optional "; ..." tail.
fn parseSummary(line: []const u8) ?struct { replayed: bool, index: u64, id: []const u8, size: u64, root: []const u8 } {
    var rest = line;
    var replayed = false;
    if (std.mem.startsWith(u8, rest, "appended record ")) {
        rest = rest["appended record ".len..];
    } else if (std.mem.startsWith(u8, rest, "already recorded record ")) {
        rest = rest["already recorded record ".len..];
        replayed = true;
    } else return null;
    const sp = std.mem.indexOfScalar(u8, rest, ' ') orelse return null;
    const index = std.fmt.parseInt(u64, rest[0..sp], 10) catch return null;
    rest = rest[sp..];
    if (!std.mem.startsWith(u8, rest, " id ")) return null;
    rest = rest[4..];
    if (rest.len < 64 or !isHex64(rest[0..64])) return null;
    const id = rest[0..64];
    rest = rest[64..];
    if (!std.mem.startsWith(u8, rest, "; head size ")) return null;
    rest = rest["; head size ".len..];
    const sp2 = std.mem.indexOfScalar(u8, rest, ' ') orelse return null;
    const size = std.fmt.parseInt(u64, rest[0..sp2], 10) catch return null;
    rest = rest[sp2..];
    if (!std.mem.startsWith(u8, rest, " root ")) return null;
    rest = rest[6..];
    if (rest.len < 64 or !isHex64(rest[0..64])) return null;
    const root = rest[0..64];
    rest = rest[64..];
    if (rest.len != 0 and !std.mem.startsWith(u8, rest, "; ")) return null;
    if (std.mem.indexOfScalar(u8, rest, '\n') != null) return null;
    return .{ .replayed = replayed, .index = index, .id = id, .size = size, .root = root };
}

fn readJson(a: Allocator, path: []const u8) ?Py {
    const f = fsx.openRegular(fsx.cwd, path, true) catch return null;
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 64 << 20) catch return null;
    return json.toPy(a, json.parse(a, raw) catch return null) catch null;
}

fn loggedEvent(a: Allocator, log_path: []const u8, index: u64) error{ Publisher, OutOfMemory }!?[]const u8 {
    const f = fsx.openRegular(fsx.cwd, log_path, true) catch |err| return publisherError(a, "cannot read ledger {s}: {s}", .{ log_path, main_mod.osError(a, err, log_path) });
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, 1 << 30) catch return publisherError(a, "cannot read ledger {s}: read error", .{log_path});
    var it = std.mem.splitScalar(u8, raw, '\n');
    var n: u64 = 0;
    while (it.next()) |line| : (n += 1) {
        if (n != index) continue;
        const v = json.toPy(a, json.parse(a, line) catch return publisherError(a, "cannot read ledger {s}: line {d} is not JSON", .{ log_path, index })) catch return publisherError(a, "cannot read ledger {s}: line {d} is not JSON", .{ log_path, index });
        const b = pyfmt.get(v, "build") orelse return null;
        if (b != .dict) return null;
        const e = pyfmt.get(b, "event_id") orelse return null;
        return if (e == .str) e.str else null;
    }
    return null;
}

fn append(a: Allocator, envp: common.proc.Envp, config: Py, record_path: []const u8) error{ Publisher, OutOfMemory }!Py {
    const ebs = s(pyfmt.get(config, "eb_stack"));
    const head = s(pyfmt.get(config, "head"));
    const log_path = s(pyfmt.get(config, "log"));
    const argv = [_][]const u8{ ebs, "ledger", "append", record_path, "--log", log_path, "--key", s(pyfmt.get(config, "key")), "--head-out", head };
    const r = try bounded(a, envp, &argv, config);
    if (r.code != 0) return publisherError(a, "eb-stack ledger append exited {d}: {s}", .{ r.code, lastChars(std.mem.trim(u8, r.stderr, " \t\n\r"), 500) });
    const out = std.mem.trim(u8, r.stdout, " \t\n\r");
    const first = out[0 .. std.mem.indexOfAny(u8, out, "\n\r") orelse out.len];
    const m = parseSummary(first) orelse {
        var rp: std.ArrayList(u8) = .empty;
        try pyfmt.reprStr(&rp, a, lastChars(out, 500));
        return publisherError(a, "eb-stack ledger append printed no receipt: {s}", .{rp.items});
    };
    const head_v = readJson(a, head) orelse return publisherError(a, "cannot read head {s}: not valid JSON", .{head});
    const hs = pyfmt.get(head_v, "size");
    const hr = pyfmt.get(head_v, "root");
    const size_ok = hs != null and hs.? == .int and hs.?.int == @as(i64, @intCast(m.size));
    const root_ok = hr != null and hr.? == .str and std.mem.eql(u8, hr.?.str, m.root);
    if (!size_ok or !root_ok or m.index >= m.size) {
        var bs: std.ArrayList(u8) = .empty;
        var br: std.ArrayList(u8) = .empty;
        try pyfmt.str(&bs, a, hs);
        try pyfmt.str(&br, a, hr);
        return publisherError(a, "head {s} is size {s} root {s}, not the reported size {d} root {s} covering record {d}", .{ head, bs.items, br.items, m.size, m.root, m.index });
    }
    const rec = readJson(a, record_path) orelse return publisherError(a, "cannot read {s}", .{record_path});
    const event_id = s(pyfmt.get(pyfmt.get(rec, "build") orelse Py{ .dict = &.{} }, "event_id"));
    const logged = try loggedEvent(a, log_path, m.index);
    if (logged == null or !std.mem.eql(u8, logged.?, event_id)) return publisherError(a, "ledger record {d} is not event {s}", .{ m.index, event_id });
    const pk = pyfmt.get(config, "public_key");
    const verified = pk != null and pyfmt.truthy(pk);
    if (verified) {
        var pkb: std.ArrayList(u8) = .empty;
        try pyfmt.str(&pkb, a, pk);
        const vargv = [_][]const u8{ ebs, "ledger", "verify", "--log", log_path, "--public-key", pkb.items, "--record", try std.fmt.allocPrint(a, "{d}", .{m.index}), "--head", head };
        const v = try bounded(a, envp, &vargv, config);
        if (v.code != 0) return publisherError(a, "eb-stack ledger verify exited {d}: {s}", .{ v.code, lastChars(std.mem.trim(u8, v.stderr, " \t\n\r"), 500) });
    }
    return .{ .dict = try a.dupe(Py.Entry, &.{
        .{ .key = "index", .value = .{ .int = @intCast(m.index) } },
        .{ .key = "id", .value = Py.s(try a.dupe(u8, m.id)) },
        .{ .key = "head_size", .value = .{ .int = @intCast(m.size) } },
        .{ .key = "head_root", .value = Py.s(try a.dupe(u8, m.root)) },
        .{ .key = "replayed", .value = .{ .bool = m.replayed } },
        .{ .key = "verified", .value = .{ .bool = verified } },
    }) };
}

pub fn pendingMessage(a: Allocator, left: []const Py) []const u8 {
    var e: std.ArrayList(u8) = .empty;
    pyfmt.str(&e, a, pyfmt.get(left[0], "error")) catch {};
    return std.fmt.allocPrint(a, "{d} ledger event(s) not acknowledged ({s}); run resume", .{ left.len, e.items }) catch "";
}

test "parseSummary" {
    const h = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const line = "appended record 3 id " ++ h ++ "; head size 4 root " ++ h ++ "; tiles 2";
    const m = parseSummary(line).?;
    try std.testing.expectEqual(@as(u64, 3), m.index);
    try std.testing.expect(!m.replayed);
    try std.testing.expect(parseSummary("appended record x id " ++ h) == null);
}

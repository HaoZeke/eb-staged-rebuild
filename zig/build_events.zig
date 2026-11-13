//! build-events: opt-in ledger events around one software-build command, the
//! Python tool's `python3 -m eb_staged_rebuild.build_events`. A `started`
//! record is delivered before the command runs (unless the configuration
//! says the ledger is not required); afterwards a `finished` record (exit 0
//! and every post-build check passed) or an `aborted` record follows. Each
//! operation keeps its state and outbox in its own directory; a retry never
//! repeats a command that already ran.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const pyfmt = @import("pyfmt.zig");
const publish = @import("publish.zig");
const builders = @import("builders.zig");
const probe = @import("probe.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const atomic = common.atomic;
const state_mod = common.state;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Py = json.Py;

const producer = "eb-staged-rebuild/build_events";
const refused_exit: u8 = 2;
const postcheck_exit: u8 = 65;
const unknown_exit: u8 = 70;
const not_started_exit: u8 = 127;
const text_limit = 1024;
const list_limit = 256;
const json_limit = 1024 * 1024;

const actions = [_][]const u8{ "install", "rebuild-inplace", "rebuild-bump" };
const config_paths = [_][]const u8{ "eb_stack", "log", "key", "outbox" };
const config_keys = [_][]const u8{ "eb_stack", "log", "key", "outbox", "public_key", "timeout", "required", "lock_timeout" };
const policy_keys = [_][]const u8{ "eb_stack", "log", "key", "public_key", "outbox", "required" };
const metadata_keys = [_][]const u8{ "operation", "actor", "stack", "target", "module", "location", "action", "easyconfig", "sources", "patches", "modulefile", "tool", "logs", "artifact" };

const Refusal = error{ Refused, OutOfMemory };

var why: []const u8 = "";

fn refuse(a: Allocator, comptime f: []const u8, args: anytype) Refusal {
    why = std.fmt.allocPrint(a, f, args) catch "refused";
    return error.Refused;
}

fn get(v: Py, k: []const u8) ?Py {
    return pyfmt.get(v, k);
}

fn s(v: ?Py) []const u8 {
    const x = v orelse return "";
    return if (x == .str) x.str else "";
}

fn has(v: Py, k: []const u8) bool {
    return get(v, k) != null;
}

fn notNone(v: Py, k: []const u8) bool {
    const x = get(v, k) orelse return false;
    return x != .null;
}

fn dict(a: Allocator, entries: []const Py.Entry) Py {
    return .{ .dict = a.dupe(Py.Entry, entries) catch &.{} };
}

fn set(a: Allocator, d: Py, k: []const u8, v: Py) Py {
    var out: std.ArrayList(Py.Entry) = .empty;
    var found = false;
    if (d == .dict) for (d.dict) |e| {
        if (std.mem.eql(u8, e.key, k)) {
            out.append(a, .{ .key = e.key, .value = v }) catch {};
            found = true;
        } else out.append(a, e) catch {};
    };
    if (!found) out.append(a, .{ .key = k, .value = v }) catch {};
    return .{ .dict = out.items };
}

fn inList(list: []const []const u8, x: []const u8) bool {
    for (list) |y| if (std.mem.eql(u8, x, y)) return true;
    return false;
}

fn charLen(t: []const u8) usize {
    var n: usize = 0;
    var i: usize = 0;
    while (i < t.len) : (n += 1) i += common.pytext.nextChar(t[i..]).len;
    return n;
}

// ---- validators -----------------------------------------------------------

fn text(a: Allocator, value: ?Py, what: []const u8, limit: usize) Refusal![]const u8 {
    const v = value orelse return refuse(a, "{s} must be a non-empty string of at most {d} characters", .{ what, limit });
    if (v != .str or v.str.len == 0 or charLen(v.str) > limit) return refuse(a, "{s} must be a non-empty string of at most {d} characters", .{ what, limit });
    for (v.str) |c| if (c < 32 or c == 127) return refuse(a, "{s} contains a control character", .{what});
    if (std.mem.indexOf(u8, v.str, "-----BEGIN") != null) return refuse(a, "{s} looks like key material; keys never go in a record", .{what});
    return v.str;
}

fn reprOf(a: Allocator, t: []const u8) []const u8 {
    var b: std.ArrayList(u8) = .empty;
    pyfmt.reprStr(&b, a, t) catch return t;
    return b.items;
}

fn cleanPath(a: Allocator, value: ?Py, what: []const u8) Refusal![]const u8 {
    const t = try text(a, value, what, 4096);
    if (t[0] != '/' or !std.mem.eql(u8, admin.abspath(a, t), t) or std.mem.startsWith(u8, t, "//"))
        return refuse(a, "{s} must be an absolute, normalized path: {s}", .{ what, reprOf(a, t) });
    return t;
}

fn inside(path: []const u8, directory: []const u8) bool {
    if (std.mem.eql(u8, path, directory)) return true;
    const d = std.mem.trimEnd(u8, directory, "/");
    return path.len > d.len and std.mem.startsWith(u8, path, d) and path[d.len] == '/';
}

fn object(a: Allocator, value: ?Py, what: []const u8, allowed: []const []const u8) Refusal!Py {
    const v = value orelse return refuse(a, "{s} must be a JSON object", .{what});
    if (v != .dict) return refuse(a, "{s} must be a JSON object", .{what});
    var unknown: std.ArrayList([]const u8) = .empty;
    for (v.dict) |e| if (!inList(allowed, e.key)) try unknown.append(a, e.key);
    if (unknown.items.len > 0) {
        std.mem.sort([]const u8, unknown.items, {}, struct {
            fn lt(_: void, x: []const u8, y: []const u8) bool {
                return json.pyTextLess(x, y);
            }
        }.lt);
        return refuse(a, "{s} has fields outside the build-event schema: {s}", .{ what, try std.mem.join(a, ", ", unknown.items) });
    }
    return v;
}

fn isHex64(t: []const u8) bool {
    if (t.len != 64) return false;
    for (t) |c| if (!(std.ascii.isDigit(c) or (c >= 'a' and c <= 'f'))) return false;
    return true;
}

fn sha(a: Allocator, value: ?Py, what: []const u8) Refusal![]const u8 {
    const v = value orelse return refuse(a, "{s} must be 64 lower-case hex characters", .{what});
    if (v != .str or !isHex64(v.str)) return refuse(a, "{s} must be 64 lower-case hex characters", .{what});
    return v.str;
}

/// urllib.parse.urlsplit, enough to see the scheme, netloc, query and fragment.
fn uri(a: Allocator, value: ?Py, what: []const u8) Refusal![]const u8 {
    const t = try text(a, value, what, 2048);
    var scheme_end: ?usize = null;
    if (std.mem.indexOfScalar(u8, t, ':')) |c| {
        var ok = c > 0 and std.ascii.isAlphabetic(t[0]);
        for (t[0..c]) |ch| if (!(std.ascii.isAlphanumeric(ch) or ch == '+' or ch == '-' or ch == '.')) {
            ok = false;
        };
        if (ok) scheme_end = c;
    }
    const rest = if (scheme_end) |e| t[e + 1 ..] else t;
    var netloc: []const u8 = "";
    if (std.mem.startsWith(u8, rest, "//")) {
        const end = std.mem.indexOfAnyPos(u8, rest, 2, "/?#") orelse rest.len;
        netloc = rest[2..end];
    }
    const has_query = if (std.mem.indexOfScalar(u8, rest, '?')) |q| blk: {
        const hash = std.mem.indexOfScalar(u8, rest, '#') orelse rest.len;
        break :blk q < hash and q + 1 < hash;
    } else false;
    const has_fragment = if (std.mem.indexOfScalar(u8, rest, '#')) |h| h + 1 < rest.len else false;
    if (scheme_end == null or std.mem.indexOfScalar(u8, netloc, '@') != null or has_query or has_fragment)
        return refuse(a, "{s} must be a URI without credentials, query or fragment", .{what});
    return t;
}

fn matchOperation(t: []const u8) bool {
    if (t.len == 0 or t.len > 128 or !std.ascii.isAlphanumeric(t[0])) return false;
    for (t[1..]) |c| if (!(std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '-')) return false;
    return true;
}

fn matchSegments(t: []const u8) bool {
    var it = std.mem.splitScalar(u8, t, '/');
    while (it.next()) |seg| {
        if (seg.len == 0) return false;
        for (seg) |c| if (!(std.ascii.isAlphanumeric(c) or c == '_' or c == '.' or c == '+' or c == '-')) return false;
    }
    return true;
}

fn matchFileName(t: []const u8) bool {
    if (t.len == 0) return false;
    const first_ok = std.ascii.isAlphanumeric(t[0]) or std.mem.indexOfScalar(u8, "_.+~-", t[0]) != null;
    if (!first_ok) return false;
    for (t[1..]) |c| if (!(std.ascii.isAlphanumeric(c) or std.mem.indexOfScalar(u8, "_.+~=,-", c) != null)) return false;
    return true;
}

fn readJson(a: Allocator, path: []const u8, what: []const u8) Refusal!Py {
    const f = fsx.openRegular(fsx.cwd, path, true) catch |err| return refuse(a, "cannot read {s} {s}: {s}", .{ what, path, main_mod.osError(a, err, path) });
    defer sys.close(f.fd);
    const raw = fsx.readAll(a, f.fd, json_limit) catch |err| switch (err) {
        error.StreamTooLong => return refuse(a, "cannot read {s} {s}: larger than {d} bytes", .{ what, path, json_limit }),
        else => return refuse(a, "cannot read {s} {s}: {s}", .{ what, path, main_mod.osError(a, err, path) }),
    };
    if (!std.unicode.utf8ValidateSlice(raw)) return refuse(a, "cannot read {s} {s}: not UTF-8 text", .{ what, path });
    const parsed = json.parse(a, raw) catch return refuse(a, "cannot read {s} {s}: not valid JSON", .{ what, path });
    const v = json.toPy(a, parsed) catch return refuse(a, "cannot read {s} {s}: not valid JSON", .{ what, path });
    if (v != .dict) return refuse(a, "{s} {s} is not a JSON object", .{ what, path });
    return v;
}

fn realpathOf(a: Allocator, p: []const u8) []const u8 {
    // os.path.realpath without strict: resolve what exists, keep the rest.
    var rb: [sys.path_max]u8 = undefined;
    if (fsx.realpath(fsx.cwd, p, &rb)) |r| return a.dupe(u8, r) catch p else |_| {}
    const parent = std.fs.path.dirname(p) orelse return p;
    if (std.mem.eql(u8, parent, p)) return p;
    return std.fs.path.join(a, &.{ realpathOf(a, parent), std.fs.path.basename(p) }) catch p;
}

/// check_config: the configuration with its defaults; unsafe or unknown
/// settings refused.
fn checkConfig(a: Allocator, raw: Py) Refusal!Py {
    var config = try object(a, raw, "build-event config", &config_keys);
    for (config_paths) |k| {
        if (!has(config, k)) return refuse(a, "build-event config needs {s}", .{k});
        _ = try cleanPath(a, get(config, k), try std.fmt.allocPrint(a, "build-event config {s}", .{k}));
    }
    if (notNone(config, "public_key")) {
        _ = try cleanPath(a, get(config, "public_key"), "build-event config public_key");
    } else if (has(config, "public_key")) {
        var out: std.ArrayList(Py.Entry) = .empty;
        for (config.dict) |e| if (!std.mem.eql(u8, e.key, "public_key")) try out.append(a, e);
        config = .{ .dict = out.items };
    }
    if (!has(config, "required")) config = set(a, config, "required", .{ .bool = true });
    if (get(config, "required").? != .bool) return refuse(a, "build-event config required must be true or false", .{});
    for ([_]struct { k: []const u8, d: i64 }{ .{ .k = "timeout", .d = 120 }, .{ .k = "lock_timeout", .d = 0 } }) |kd| {
        if (!has(config, kd.k)) config = set(a, config, kd.k, .{ .int = kd.d });
        const v = get(config, kd.k).?;
        const ok = switch (v) {
            .int => |n| n >= 0 and !(std.mem.eql(u8, kd.k, "timeout") and n == 0),
            .float => |x| x >= 0 and !(std.mem.eql(u8, kd.k, "timeout") and x == 0),
            else => false,
        };
        if (!ok) return refuse(a, "build-event config {s} must be a {s} number of seconds", .{ kd.k, if (std.mem.eql(u8, kd.k, "timeout")) "positive" else "non-negative" });
    }
    const box = s(get(config, "outbox"));
    if (fsx.statAt(fsx.cwd, box, false)) |st| {
        if (!st.isDir()) return refuse(a, "build-event outbox {s} is not a directory", .{box});
    } else |_| {}
    if (!std.mem.eql(u8, realpathOf(a, box), box)) return refuse(a, "build-event outbox {s} passes through a symbolic link", .{box});
    const log_dir = realpathOf(a, std.fs.path.dirname(s(get(config, "log"))) orelse "/");
    if (inside(box, log_dir) or inside(log_dir, box)) return refuse(a, "build-event outbox {s} and ledger directory {s} must be disjoint", .{ box, log_dir });
    const key = s(get(config, "key"));
    const kst = fsx.statAt(fsx.cwd, key, false) catch |err| return refuse(a, "signing key {s} cannot be inspected: {s}", .{ key, main_mod.osError(a, err, key) });
    if (!kst.isReg()) return refuse(a, "signing key {s} is not a regular file", .{key});
    const kr = realpathOf(a, key);
    if (inside(kr, log_dir)) return refuse(a, "signing key {s} is inside the ledger directory {s}", .{ key, log_dir });
    if (inside(kr, box)) return refuse(a, "signing key {s} is inside the build-event outbox {s}", .{ key, box });
    return config;
}

fn fileList(a: Allocator, value: ?Py, what: []const u8) Refusal!Py {
    const v = value orelse Py{ .list = &.{} };
    if (v != .list or v.list.len > list_limit) return refuse(a, "{s} must be a list of at most {d} entries", .{ what, list_limit });
    var items: std.ArrayList(Py) = .empty;
    for (v.list, 0..) |item_in, at| {
        const w = try std.fmt.allocPrint(a, "{s}[{d}]", .{ what, at });
        const item = try object(a, item_in, w, &.{ "name", "sha256" });
        const name = try text(a, get(item, "name"), try std.fmt.allocPrint(a, "{s}.name", .{w}), 255);
        if (!matchFileName(name) or std.mem.eql(u8, name, ".") or std.mem.eql(u8, name, ".."))
            return refuse(a, "{s}.name must be a plain file name: {s}", .{ w, reprOf(a, name) });
        try items.append(a, dict(a, &.{ .{ .key = "name", .value = Py.s(name) }, .{ .key = "sha256", .value = Py.s(try sha(a, get(item, "sha256"), try std.fmt.allocPrint(a, "{s}.sha256", .{w}))) } }));
    }
    return .{ .list = items.items };
}

fn checkMetadata(a: Allocator, raw: Py) Refusal!Py {
    const meta = try object(a, raw, "build-event metadata", &metadata_keys);
    var out: std.ArrayList(Py.Entry) = .empty;
    const operation = try text(a, get(meta, "operation"), "operation", 128);
    if (!matchOperation(operation) or std.mem.indexOf(u8, operation, "..") != null)
        return refuse(a, "operation must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$ without '..': {s}", .{reprOf(a, operation)});
    try out.append(a, .{ .key = "operation", .value = Py.s(operation) });
    for ([_][]const u8{ "actor", "stack", "module" }) |k| try out.append(a, .{ .key = k, .value = Py.s(try text(a, get(meta, k), k, text_limit)) });
    var i: usize = 0;
    const module = s(out.items[3].value);
    while (i < module.len) {
        const ch = common.pytext.nextChar(module[i..]);
        if (ch.cp) |cp| if (common.pytext.pyWhitespace(cp)) return refuse(a, "module must not contain whitespace", .{});
        i += ch.len;
    }
    try out.append(a, .{ .key = "location", .value = Py.s(try cleanPath(a, get(meta, "location"), "location")) });
    const target = try object(a, get(meta, "target"), "target", &.{ "cpu", "accelerator" });
    const cpu = try text(a, get(target, "cpu"), "target.cpu", 256);
    if (!matchSegments(cpu)) return refuse(a, "target.cpu must be slash-separated names: {s}", .{reprOf(a, cpu)});
    var tout: std.ArrayList(Py.Entry) = .empty;
    try tout.append(a, .{ .key = "cpu", .value = Py.s(cpu) });
    if (notNone(target, "accelerator")) {
        const acc = try text(a, get(target, "accelerator"), "target.accelerator", 256);
        if (!matchSegments(acc)) return refuse(a, "target.accelerator must be slash-separated names: {s}", .{reprOf(a, acc)});
        try tout.append(a, .{ .key = "accelerator", .value = Py.s(acc) });
    }
    try out.append(a, .{ .key = "target", .value = .{ .dict = tout.items } });
    const action = get(meta, "action") orelse Py.s("install");
    if (action != .str or !inList(&actions, action.str)) return refuse(a, "action must be one of install, rebuild-inplace, rebuild-bump", .{});
    try out.append(a, .{ .key = "action", .value = action });
    const ec = try object(a, get(meta, "easyconfig"), "easyconfig", &.{ "path", "sha256" });
    var ecout: std.ArrayList(Py.Entry) = .empty;
    try ecout.append(a, .{ .key = "path", .value = Py.s(try cleanPath(a, get(ec, "path"), "easyconfig.path")) });
    if (notNone(ec, "sha256")) try ecout.append(a, .{ .key = "sha256", .value = Py.s(try sha(a, get(ec, "sha256"), "easyconfig.sha256")) });
    try out.append(a, .{ .key = "easyconfig", .value = .{ .dict = ecout.items } });
    try out.append(a, .{ .key = "sources", .value = try fileList(a, get(meta, "sources"), "sources") });
    try out.append(a, .{ .key = "patches", .value = try fileList(a, get(meta, "patches"), "patches") });
    if (notNone(meta, "modulefile")) {
        const mf = try object(a, get(meta, "modulefile"), "modulefile", &.{ "path", "sha256" });
        var mout: std.ArrayList(Py.Entry) = .empty;
        try mout.append(a, .{ .key = "path", .value = Py.s(try cleanPath(a, get(mf, "path"), "modulefile.path")) });
        if (notNone(mf, "sha256")) try mout.append(a, .{ .key = "sha256", .value = Py.s(try sha(a, get(mf, "sha256"), "modulefile.sha256")) });
        try out.append(a, .{ .key = "modulefile", .value = .{ .dict = mout.items } });
    }
    if (notNone(meta, "tool")) {
        const tool = try object(a, get(meta, "tool"), "tool", &.{ "name", "version", "revision" });
        var tl: std.ArrayList(Py.Entry) = .empty;
        for ([_][]const u8{ "name", "version", "revision" }) |k| if (has(tool, k)) try tl.append(a, .{ .key = k, .value = Py.s(try text(a, get(tool, k), try std.fmt.allocPrint(a, "tool.{s}", .{k}), 256)) });
        if (!has(tool, "name")) return refuse(a, "tool.name is required", .{});
        try out.append(a, .{ .key = "tool", .value = .{ .dict = tl.items } });
    }
    const logs = get(meta, "logs") orelse Py{ .list = &.{} };
    if (logs != .list or logs.list.len > list_limit) return refuse(a, "logs must be a list of at most {d} references", .{list_limit});
    var lout: std.ArrayList(Py) = .empty;
    for (logs.list, 0..) |ref_in, at| {
        const w = try std.fmt.allocPrint(a, "logs[{d}]", .{at});
        const ref = try object(a, ref_in, w, &.{ "uri", "sha256" });
        var e: std.ArrayList(Py.Entry) = .empty;
        try e.append(a, .{ .key = "uri", .value = Py.s(try uri(a, get(ref, "uri"), try std.fmt.allocPrint(a, "{s}.uri", .{w}))) });
        if (notNone(ref, "sha256")) try e.append(a, .{ .key = "sha256", .value = Py.s(try sha(a, get(ref, "sha256"), try std.fmt.allocPrint(a, "{s}.sha256", .{w}))) });
        try lout.append(a, .{ .dict = e.items });
    }
    try out.append(a, .{ .key = "logs", .value = .{ .list = lout.items } });
    if (notNone(meta, "artifact")) {
        const art = try object(a, get(meta, "artifact"), "artifact", &.{ "accession", "path", "uri" });
        var aout: std.ArrayList(Py.Entry) = .empty;
        if (notNone(art, "accession")) {
            const acc = get(art, "accession").?;
            if (acc != .str or !std.mem.startsWith(u8, acc.str, "sha256:") or !isHex64(acc.str["sha256:".len..])) return refuse(a, "artifact.accession must be sha256:<64 lower-case hex>", .{});
            try aout.append(a, .{ .key = "accession", .value = acc });
        }
        if (notNone(art, "path")) try aout.append(a, .{ .key = "path", .value = Py.s(try cleanPath(a, get(art, "path"), "artifact.path")) });
        if (notNone(art, "uri")) try aout.append(a, .{ .key = "uri", .value = Py.s(try uri(a, get(art, "uri"), "artifact.uri")) });
        const ad: Py = .{ .dict = aout.items };
        if (!has(ad, "accession") and !has(ad, "path")) return refuse(a, "artifact needs an accession or a path to hash", .{});
        try out.append(a, .{ .key = "artifact", .value = ad });
    }
    return .{ .dict = out.items };
}

// ---- hashing and records ----------------------------------------------------

fn hashFile(a: Allocator, path: []const u8, problem: *[]const u8) ?struct { digest: []const u8, size: u64 } {
    const fd = sys.openat(fsx.cwd, path, .{ .NOFOLLOW = true, .NONBLOCK = true }, 0) catch |err| {
        problem.* = main_mod.osError(a, err, path);
        return null;
    };
    defer sys.close(fd);
    const st = fsx.fstat(fd) catch |err| {
        problem.* = main_mod.osError(a, err, path);
        return null;
    };
    if (!st.isReg()) {
        problem.* = std.fmt.allocPrint(a, "{s} is not a regular file", .{path}) catch "";
        return null;
    }
    var h = std.crypto.hash.sha2.Sha256.init(.{});
    var buf: [1 << 20]u8 = undefined;
    var size: u64 = 0;
    while (true) {
        const n = sys.read(fd, &buf) catch |err| {
            problem.* = main_mod.osError(a, err, path);
            return null;
        };
        if (n == 0) break;
        h.update(buf[0..n]);
        size += n;
    }
    var out: [32]u8 = undefined;
    h.final(&out);
    const hex = std.fmt.bytesToHex(out, .lower);
    return .{ .digest = a.dupe(u8, &hex) catch "", .size = size };
}

fn lexists(p: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, p, false) catch return false;
    return true;
}

/// _resolve_inputs: hash the easyconfig when its hash is absent, check it
/// when present.
fn resolveInputs(a: Allocator, meta: Py) Refusal!Py {
    var ec = get(meta, "easyconfig").?;
    const path = s(get(ec, "path"));
    if (lexists(path) or !has(ec, "sha256")) {
        var problem: []const u8 = "";
        const h = hashFile(a, path, &problem) orelse return refuse(a, "easyconfig {s} cannot be hashed: {s}", .{ path, problem });
        if (!has(ec, "sha256")) ec = set(a, ec, "sha256", Py.s(h.digest));
        if (!std.mem.eql(u8, s(get(ec, "sha256")), h.digest)) return refuse(a, "easyconfig {s} hashes to {s}, not the supplied {s}", .{ path, h.digest, s(get(ec, "sha256")) });
    }
    return set(a, meta, "easyconfig", ec);
}

fn eventId(a: Allocator, operation: []const u8, stage: []const u8) []const u8 {
    return std.fmt.allocPrint(a, "eb-build-events/{s}/{s}", .{ operation, stage }) catch "";
}

fn targetOf(a: Allocator, meta: Py) []const u8 {
    const t = get(meta, "target").?;
    if (has(t, "accelerator")) return std.fmt.allocPrint(a, "{s}/accel/{s}", .{ s(get(t, "cpu")), s(get(t, "accelerator")) }) catch "";
    return s(get(t, "cpu"));
}

fn utc(a: Allocator, t: f64) []const u8 {
    const secs: u64 = @intFromFloat(@max(@floor(t), 0));
    const es = std.time.epoch.EpochSeconds{ .secs = secs };
    const day = es.getEpochDay().calculateYearDay();
    const md = day.calculateMonthDay();
    const ds = es.getDaySeconds();
    return std.fmt.allocPrint(a, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{ day.year, md.month.numeric(), md.day_index + 1, ds.getHoursIntoDay(), ds.getMinutesIntoHour(), ds.getSecondsIntoMinute() }) catch "";
}

fn baseRecord(a: Allocator, meta: Py, stage: []const u8, now: f64) Py {
    const op = s(get(meta, "operation"));
    const target = get(meta, "target").?;
    var build: std.ArrayList(Py.Entry) = .empty;
    build.appendSlice(a, &.{
        .{ .key = "event_id", .value = Py.s(eventId(a, op, stage)) },
        .{ .key = "operation", .value = Py.s(op) },
        .{ .key = "producer", .value = Py.s(producer) },
        .{ .key = "cpu_target", .value = get(target, "cpu").? },
    }) catch {};
    if (has(target, "accelerator")) build.append(a, .{ .key = "accelerator", .value = get(target, "accelerator").? }) catch {};
    if (has(meta, "tool")) build.append(a, .{ .key = "tool", .value = get(meta, "tool").? }) catch {};
    const logs = get(meta, "logs").?;
    if (logs.list.len > 0) build.append(a, .{ .key = "logs", .value = logs }) catch {};
    var rec = dict(a, &.{
        .{ .key = "ts", .value = Py.s(utc(a, now)) },
        .{ .key = "actor", .value = get(meta, "actor").? },
        .{ .key = "stack", .value = get(meta, "stack").? },
        .{ .key = "target", .value = Py.s(targetOf(a, meta)) },
        .{ .key = "module", .value = get(meta, "module").? },
        .{ .key = "location", .value = get(meta, "location").? },
        .{ .key = "action", .value = get(meta, "action").? },
        .{ .key = "stage", .value = Py.s(stage) },
        .{ .key = "easyconfig", .value = get(meta, "easyconfig").? },
        .{ .key = "build", .value = .{ .dict = build.items } },
    });
    const sources = get(meta, "sources").?;
    const patches = get(meta, "patches").?;
    if (sources.list.len > 0 or patches.list.len > 0)
        rec = set(a, rec, "sources", dict(a, &.{ .{ .key = "files", .value = sources }, .{ .key = "patches", .value = patches } }));
    return rec;
}

fn startedRecord(a: Allocator, meta: Py, now: f64, previous: ?[]const u8) Py {
    var rec = baseRecord(a, meta, "started", now);
    if (has(meta, "modulefile")) {
        var mf = dict(a, &.{.{ .key = "path", .value = get(get(meta, "modulefile").?, "path").? }});
        if (previous) |p| if (p.len > 0) {
            mf = set(a, mf, "previous_sha256", Py.s(p));
        };
        rec = set(a, rec, "modulefile", mf);
    }
    return rec;
}

const Problem = struct { kind: []const u8, text: []const u8 };

fn postBuild(a: Allocator, meta: Py, fields: *std.ArrayList(Py.Entry)) ?Problem {
    if (has(meta, "modulefile")) {
        const mf = get(meta, "modulefile").?;
        const path = s(get(mf, "path"));
        var problem: []const u8 = "";
        const h = hashFile(a, path, &problem) orelse return .{ .kind = "modulefile-unreadable", .text = std.fmt.allocPrint(a, "module file {s} cannot be hashed: {s}", .{ path, problem }) catch "" };
        if (has(mf, "sha256") and !std.mem.eql(u8, s(get(mf, "sha256")), h.digest))
            return .{ .kind = "modulefile-mismatch", .text = std.fmt.allocPrint(a, "module file {s} hashes to {s}, not the supplied {s}", .{ path, h.digest, s(get(mf, "sha256")) }) catch "" };
        fields.append(a, .{ .key = "modulefile", .value = dict(a, &.{ .{ .key = "path", .value = Py.s(path) }, .{ .key = "sha256", .value = Py.s(h.digest) } }) }) catch {};
    }
    if (has(meta, "artifact")) {
        var art = get(meta, "artifact").?;
        if (has(art, "path")) {
            const path = s(get(art, "path"));
            var problem: []const u8 = "";
            const h = hashFile(a, path, &problem) orelse return .{ .kind = "artifact-unreadable", .text = std.fmt.allocPrint(a, "artifact {s} cannot be hashed: {s}", .{ path, problem }) catch "" };
            const accession = std.fmt.allocPrint(a, "sha256:{s}", .{h.digest}) catch "";
            if (!has(art, "accession")) art = set(a, art, "accession", Py.s(accession));
            if (!std.mem.eql(u8, s(get(art, "accession")), accession))
                return .{ .kind = "artifact-mismatch", .text = std.fmt.allocPrint(a, "artifact {s} is {s}, not the supplied {s}", .{ path, accession, s(get(art, "accession")) }) catch "" };
            art = set(a, art, "size", .{ .int = @intCast(h.size) });
        }
        fields.append(a, .{ .key = "artifact", .value = art }) catch {};
    }
    return null;
}

fn reason(a: Allocator, status: Py) []const u8 {
    const outcome = s(get(status, "outcome"));
    if (std.mem.eql(u8, outcome, "unknown")) return "the producer stopped while the command ran; the command's result is unknown";
    if (std.mem.eql(u8, outcome, "not-started")) return "the command could not be started";
    var b: std.ArrayList(u8) = .empty;
    pyfmt.str(&b, a, get(status, "exit_status")) catch {};
    return std.fmt.allocPrint(a, "the command exited {s}", .{b.items}) catch "";
}

fn exitStatus(status: Py) ?i64 {
    const v = get(status, "exit_status") orelse return null;
    return if (v == .int) v.int else null;
}

fn terminalRecord(a: Allocator, meta: Py, now: f64, status: Py, started_id: ?Py, previous: ?[]const u8) struct { record: Py, problem: ?[]const u8 } {
    var outcome = s(get(status, "outcome"));
    var problem: ?Problem = null;
    var fields: std.ArrayList(Py.Entry) = .empty;
    const exited0 = std.mem.eql(u8, outcome, "exited") and exitStatus(status) == 0;
    if (exited0) {
        problem = postBuild(a, meta, &fields);
        if (problem) |p| outcome = p.kind;
    }
    const finished = std.mem.eql(u8, outcome, "exited") and exitStatus(status) == 0;
    const stage = if (finished) "finished" else "aborted";
    var rec = baseRecord(a, meta, stage, now);
    var build = get(rec, "build").?;
    build = set(a, build, "outcome", Py.s(outcome));
    if (get(status, "exit_status")) |e| if (e != .null) {
        build = set(a, build, "exit_status", e);
    };
    if (started_id) |sid| build = set(a, build, "started_id", sid);
    const fd: Py = .{ .dict = fields.items };
    if (get(fd, "artifact")) |art| build = set(a, build, "artifact", art);
    rec = set(a, rec, "build", build);
    if (get(fd, "modulefile")) |mf| {
        rec = set(a, rec, "modulefile", mf);
    } else if (has(meta, "modulefile")) {
        rec = set(a, rec, "modulefile", dict(a, &.{.{ .key = "path", .value = get(get(meta, "modulefile").?, "path").? }}));
    }
    if (previous) |p| if (p.len > 0 and has(rec, "modulefile")) {
        rec = set(a, rec, "modulefile", set(a, get(rec, "modulefile").?, "previous_sha256", Py.s(p)));
    };
    if (!finished) rec = set(a, rec, "reason", Py.s(if (problem) |p| p.text else reason(a, status)));
    return .{ .record = rec, .problem = if (problem) |p| p.text else null };
}

fn exitCode(status: Py, problem: bool) u8 {
    const outcome = s(get(status, "outcome"));
    if (std.mem.eql(u8, outcome, "unknown")) return unknown_exit;
    if (std.mem.eql(u8, outcome, "not-started")) return not_started_exit;
    const e = exitStatus(status) orelse 0;
    if (e != 0) return @intCast(@as(u64, @intCast(e)) & 0xff);
    return if (problem) postcheck_exit else 0;
}

// ---- the operation ----------------------------------------------------------

const Op = struct {
    a: Allocator,
    env: probe.Env,
    config: Py,
    meta: Py,
    dir: []const u8,
    path: []const u8,
    ledger: Py,
    policy: Py,
    ctx: *admin.Ctx,

    fn init(a: Allocator, env: probe.Env, ctx: *admin.Ctx, config: Py, meta: Py) Op {
        const dir = std.fs.path.join(a, &.{ s(get(config, "outbox")), s(get(meta, "operation")) }) catch "";
        var ledger: std.ArrayList(Py.Entry) = .empty;
        for ([_][]const u8{ "eb_stack", "log", "key", "timeout", "required" }) |k| ledger.append(a, .{ .key = k, .value = get(config, k).? }) catch {};
        if (has(config, "public_key")) ledger.append(a, .{ .key = "public_key", .value = get(config, "public_key").? }) catch {};
        ledger.append(a, .{ .key = "head", .value = Py.s(std.fs.path.join(a, &.{ dir, "head.json" }) catch "") }) catch {};
        var policy: std.ArrayList(Py.Entry) = .empty;
        for (policy_keys) |k| if (get(config, k)) |v| policy.append(a, .{ .key = k, .value = v }) catch {};
        return .{ .a = a, .env = env, .ctx = ctx, .config = config, .meta = meta, .dir = dir, .path = std.fs.path.join(a, &.{ dir, "operation.json" }) catch "", .ledger = .{ .dict = ledger.items }, .policy = .{ .dict = policy.items } };
    }

    fn load(op: *Op) error{ Refused, OutOfMemory, NeedsIntervention }!?Py {
        if (!lexists(op.path)) return null;
        const f = fsx.openRegular(fsx.cwd, op.path, true) catch |err| {
            why = std.fmt.allocPrint(op.a, "operation state cannot be read: {s}: {s}", .{ op.path, main_mod.osError(op.a, err, op.path) }) catch "";
            return error.NeedsIntervention;
        };
        defer sys.close(f.fd);
        const raw = fsx.readAll(op.a, f.fd, 256 << 20) catch {
            why = std.fmt.allocPrint(op.a, "operation state cannot be read: {s}", .{op.path}) catch "";
            return error.NeedsIntervention;
        };
        const v = json.toPy(op.a, json.parse(op.a, raw) catch {
            why = std.fmt.allocPrint(op.a, "operation state cannot be read: {s}: not valid JSON", .{op.path}) catch "";
            return error.NeedsIntervention;
        }) catch {
            why = std.fmt.allocPrint(op.a, "operation state cannot be read: {s}: not valid JSON", .{op.path}) catch "";
            return error.NeedsIntervention;
        };
        const sch = get(v, "schema");
        if (sch == null or sch.? != .int or sch.?.int != 1) {
            var b: std.ArrayList(u8) = .empty;
            try pyfmt.repr(&b, op.a, sch orelse .null);
            why = std.fmt.allocPrint(op.a, "operation state schema {s} is not 1", .{b.items}) catch "";
            return error.NeedsIntervention;
        }
        return v;
    }

    fn save(op: *Op, st: Py, step: []const u8) Refusal!void {
        const raw = try std.fmt.allocPrint(op.a, "{s}\n", .{try json.dumps(op.a, st, 2, true)});
        var w: atomic.Why = .{};
        atomic.atomicWrite(op.a, op.path, raw, .{ .faults = op.ctx.faults(), .step = try std.fmt.allocPrint(op.a, "build_events.{s}", .{step}) }, &w) catch |err|
            return refuse(op.a, "{s}", .{if (err == error.Refused) w.msg else @errorName(err)});
    }

    /// Refuse a retry whose policy or metadata differ from the recorded one.
    fn check(op: *Op, st: Py) Refusal!void {
        const a = op.a;
        if (!publish.sameValue(a, get(st, "policy") orelse .null, op.policy))
            return refuse(a, "operation {s} was recorded with another ledger policy; use a new operation", .{s(get(op.meta, "operation"))});
        var ec = get(op.meta, "easyconfig").?;
        const path = s(get(ec, "path"));
        if (lexists(path) or !has(ec, "sha256")) {
            if (!lexists(path)) {
                ec = set(a, ec, "sha256", get(get(get(st, "metadata").?, "easyconfig").?, "sha256") orelse .null);
            } else {
                ec = get(try resolveInputs(a, set(a, Py{ .dict = &.{} }, "easyconfig", ec)), "easyconfig").?;
            }
        }
        if (!publish.sameValue(a, set(a, op.meta, "easyconfig", ec), get(st, "metadata") orelse .null))
            return refuse(a, "operation {s} was recorded with different metadata; use a new operation", .{s(get(op.meta, "operation"))});
        op.meta = get(st, "metadata").?;
    }

    fn deliver(op: *Op, recs: []const Py, step: []const u8) error{ Refused, OutOfMemory, NeedsIntervention }![]const Py {
        var w: []const u8 = "";
        if (recs.len > 0) publish.enqueue(op.a, op.dir, recs, op.ctx.faults(), step, &w) catch |err| {
            why = w;
            return err;
        };
        return publish.deliver(op.a, op.env.envp, op.dir, op.ledger, op.ctx.faults(), &w) catch |err| {
            why = w;
            return err;
        };
    }

    fn startedId(op: *Op) ?Py {
        var w: []const u8 = "";
        const data = publish.load(op.a, op.dir, &w) catch return null;
        const want = eventId(op.a, s(get(op.meta, "operation")), "started");
        for ((get(data, "events") orelse Py{ .list = &.{} }).list) |ev| {
            if (std.mem.eql(u8, s(get(ev, "event_id")), want) and std.mem.eql(u8, s(get(ev, "status")), "acknowledged"))
                return get(get(ev, "receipt") orelse Py{ .dict = &.{} }, "id");
        }
        return null;
    }
};

const Outcome = struct { code: u8, message: []const u8 };

fn pendingText(a: Allocator, left: []const Py) []const u8 {
    var e: std.ArrayList(u8) = .empty;
    pyfmt.str(&e, a, get(left[0], "error")) catch {};
    return std.fmt.allocPrint(a, "{d} ledger event(s) not acknowledged ({s})", .{ left.len, e.items }) catch "";
}

fn finish(op: *Op, st: Py, recorded: bool) error{ Refused, OutOfMemory, NeedsIntervention }!Outcome {
    const a = op.a;
    const left = try op.deliver(&.{ get(st, "started").?, get(st, "terminal").? }, "outbox.terminal");
    const status = get(st, "result").?;
    const problem = get(st, "problem");
    const has_problem = problem != null and problem.? != .null;
    var code = exitCode(status, has_problem);
    var notes: std.ArrayList([]const u8) = .empty;
    try notes.append(a, try std.fmt.allocPrint(a, "{s}: {s}", .{ s(get(get(st, "terminal").?, "stage")), reason(a, status) }));
    if (has_problem) try notes.append(a, s(problem));
    if (recorded) try notes.append(a, "recorded by an earlier attempt; the command was not repeated");
    if (left.len > 0) {
        try notes.append(a, try std.fmt.allocPrint(a, "ledger error: {s}; retry to deliver from {s}", .{ pendingText(a, left), op.dir }));
        if (code == 0) code = publish.pending_exit;
    }
    return .{ .code = code, .message = try std.mem.join(a, "; ", notes.items) };
}

fn recordExit(op: *Op, st: Py, status: Py) Refusal!Py {
    const prev = s(get(st, "previous_modulefile"));
    const t = terminalRecord(op.a, op.meta, admin.nowSeconds(), status, op.startedId(), if (prev.len > 0) prev else null);
    var next = set(op.a, st, "phase", Py.s("exited"));
    next = set(op.a, next, "result", status);
    next = set(op.a, next, "terminal", t.record);
    next = set(op.a, next, "problem", Py.opt(t.problem));
    try op.save(next, "exited");
    return next;
}

fn lockOp(a: Allocator, config: Py, op: *Op) Refusal!state_mod.RootLock {
    const box = s(get(config, "outbox"));
    if (!lexists(box)) {
        var zb: [sys.path_max:0]u8 = undefined;
        atomic.makedirs(std.fs.path.dirname(box) orelse "/") catch {};
        _ = linux.mkdirat(linux.AT.FDCWD, (sys.z(&zb, box) catch return refuse(a, "bad outbox", .{})).ptr, 0o700);
    }
    if (fsx.statAt(fsx.cwd, op.dir, false)) |st| {
        if (st.isLink() or !st.isDir()) return refuse(a, "operation directory {s} is not a plain directory", .{op.dir});
    } else |_| {}
    const lt = get(config, "lock_timeout").?;
    const secs: f64 = switch (lt) {
        .int => |n| @floatFromInt(n),
        .float => |x| x,
        else => 0,
    };
    const l = state_mod.RootLock.acquire(a, op.dir, @intFromFloat(@min(secs * 1000, 4.0e9))) catch |err| switch (err) {
        error.Busy => return refuse(a, "another eb-staged-rebuild holds the lock at {s}/.lock", .{op.dir}),
        else => return refuse(a, "{s}", .{main_mod.osError(a, err, op.dir)}),
    };
    if (!std.mem.eql(u8, realpathOf(a, op.dir), op.dir)) {
        l.release();
        return refuse(a, "operation directory {s} is not a plain directory", .{op.dir});
    }
    return l;
}

fn runBuild(a: Allocator, env: probe.Env, ctx: *admin.Ctx, config_raw: Py, metadata_raw: Py, command: []const []const u8) error{ Refused, OutOfMemory, NeedsIntervention }!Outcome {
    if (command.len == 0) return refuse(a, "pass the command after --", .{});
    const config = try checkConfig(a, config_raw);
    var meta = try checkMetadata(a, metadata_raw);
    if (fsx.statAt(fsx.cwd, "/proc/self/stat", true)) |_| {} else |_| return refuse(a, "run_build requires /proc to confirm the command's process group stopped", .{});
    var op = Op.init(a, env, ctx, config, meta);
    const l = try lockOp(a, config, &op);
    defer l.release();
    const loaded = try op.load();
    var st = loaded orelse blk: {
        meta = try resolveInputs(a, meta);
        op.meta = meta;
        var previous: Py = .null;
        if (has(meta, "modulefile")) {
            const p = s(get(get(meta, "modulefile").?, "path"));
            if (lexists(p)) {
                var problem: []const u8 = "";
                const h = hashFile(a, p, &problem) orelse return refuse(a, "existing module file {s} cannot be hashed: {s}", .{ p, problem });
                previous = Py.s(h.digest);
            }
        }
        const fresh = dict(a, &.{
            .{ .key = "schema", .value = .{ .int = 1 } },
            .{ .key = "operation", .value = get(meta, "operation").? },
            .{ .key = "phase", .value = Py.s("created") },
            .{ .key = "policy", .value = op.policy },
            .{ .key = "metadata", .value = meta },
            .{ .key = "previous_modulefile", .value = previous },
            .{ .key = "started", .value = startedRecord(a, meta, admin.nowSeconds(), if (previous == .str) previous.str else null) },
            .{ .key = "result", .value = .null },
            .{ .key = "terminal", .value = .null },
            .{ .key = "problem", .value = .null },
        });
        try op.save(fresh, "created");
        break :blk fresh;
    };
    // A recorded state is checked against this call; a fresh one is this call.
    if (loaded != null) try op.check(st);
    const phase = s(get(st, "phase"));
    if (std.mem.eql(u8, phase, "exited")) return finish(&op, st, true);
    if (std.mem.eql(u8, phase, "running")) {
        st = try recordExit(&op, st, dict(a, &.{ .{ .key = "outcome", .value = Py.s("unknown") }, .{ .key = "exit_status", .value = .null } }));
        return finish(&op, st, false);
    }
    const left = try op.deliver(&.{get(st, "started").?}, "outbox.started");
    if (left.len > 0 and pyfmt.truthy(get(config, "required")))
        return .{ .code = publish.pending_exit, .message = try std.fmt.allocPrint(a, "command not run: {s}; retry to deliver from {s}", .{ pendingText(a, left), op.dir }) };
    st = set(a, st, "phase", Py.s("running"));
    try op.save(st, "running");
    var status: Py = undefined;
    const exe = if (std.mem.indexOfScalar(u8, command[0], '/') != null) command[0] else (common.proc.which(a, command[0], env.get("PATH") orelse "/bin:/usr/bin") catch null);
    const runnable = blk: {
        const e = exe orelse break :blk false;
        var zb: [sys.path_max:0]u8 = undefined;
        const z = sys.z(&zb, e) catch break :blk false;
        if (linux.errno(linux.faccessat(linux.AT.FDCWD, z.ptr, linux.X_OK, 0)) != .SUCCESS) break :blk false;
        const fst = fsx.statAt(fsx.cwd, e, true) catch break :blk false;
        break :blk fst.isReg();
    };
    if (!runnable) {
        status = dict(a, &.{ .{ .key = "outcome", .value = Py.s("not-started") }, .{ .key = "exit_status", .value = .null } });
    } else {
        const rc = builders.runCommand(a, exe.?, command, env.envp, ctx.faults(), "build_events.command") catch |err| switch (err) {
            error.Refused => return refuse(a, "run_build requires /proc to confirm the command's process group stopped", .{}),
            error.CannotStart => @as(u8, 127),
        };
        status = dict(a, &.{ .{ .key = "outcome", .value = Py.s("exited") }, .{ .key = "exit_status", .value = .{ .int = rc } } });
    }
    st = try recordExit(&op, st, status);
    return finish(&op, st, false);
}

fn deliverOnly(a: Allocator, env: probe.Env, ctx: *admin.Ctx, config_raw: Py, metadata_raw: Py) error{ Refused, OutOfMemory, NeedsIntervention }!Outcome {
    const config = try checkConfig(a, config_raw);
    const meta = try checkMetadata(a, metadata_raw);
    var op = Op.init(a, env, ctx, config, meta);
    const l = try lockOp(a, config, &op);
    defer l.release();
    var st = (try op.load()) orelse return refuse(a, "operation {s} has no recorded state in {s}", .{ s(get(meta, "operation")), op.dir });
    try op.check(st);
    const phase = s(get(st, "phase"));
    if (std.mem.eql(u8, phase, "exited")) return finish(&op, st, true);
    if (std.mem.eql(u8, phase, "running")) {
        st = try recordExit(&op, st, dict(a, &.{ .{ .key = "outcome", .value = Py.s("unknown") }, .{ .key = "exit_status", .value = .null } }));
        return finish(&op, st, false);
    }
    const left = try op.deliver(&.{get(st, "started").?}, "outbox.started");
    if (left.len > 0) return .{ .code = publish.pending_exit, .message = try std.fmt.allocPrint(a, "command not run: {s}", .{pendingText(a, left)}) };
    return .{ .code = 0, .message = "started acknowledged; the command has not run" };
}

// ---- the command line ---------------------------------------------------------

const usage =
    \\usage: eb-staged-rebuild build-events [-h] --config CONFIG --metadata METADATA
    \\                                      [--deliver]
    \\
;
const help = usage ++
    \\
    \\Record started/finished/aborted ledger events around one build command.
    \\
    \\options:
    \\  -h, --help           show this help message and exit
    \\  --config CONFIG      build-event config JSON (ledger, key, outbox)
    \\  --metadata METADATA  build-event metadata JSON for this operation
    \\  --deliver            deliver pending events without running a command
    \\
;

fn onStop(sig: linux.SIG) callconv(.c) void {
    var buf: [128]u8 = undefined;
    const m = std.fmt.bufPrint(&buf, "build-events: stopped by signal {d}; pending events stay in the outbox\n", .{@backingInt(sig)}) catch "build-events: stopped\n";
    cli.put(2, m);
    linux.exit_group(@intCast(128 + @backingInt(sig)));
}

pub fn main(a: Allocator, args_in: []const []const u8, env: probe.Env) u8 {
    var args = args_in;
    var command: []const []const u8 = &.{};
    for (args_in, 0..) |x, i| if (std.mem.eql(u8, x, "--")) {
        args = args_in[0..i];
        command = args_in[i + 1 ..];
        break;
    };
    var config_path: ?[]const u8 = null;
    var metadata_path: ?[]const u8 = null;
    var deliver_flag = false;
    var i: usize = 0;
    const who = "eb-staged-rebuild build-events";
    while (i < args.len) : (i += 1) {
        const x = args[i];
        if (std.mem.eql(u8, x, "-h") or std.mem.eql(u8, x, "--help")) {
            cli.put(1, help);
            return 0;
        }
        inline for (.{ "config", "metadata" }) |name| {
            if (std.mem.eql(u8, x, "--" ++ name) or std.mem.startsWith(u8, x, "--" ++ name ++ "=")) {
                var v: []const u8 = undefined;
                if (std.mem.indexOfScalar(u8, x, '=')) |e| v = x[e + 1 ..] else {
                    if (i + 1 >= args.len) {
                        cli.put(2, usage);
                        cli.err("{s}: error: argument --{s}: expected one argument\n", .{ who, name });
                        return 2;
                    }
                    i += 1;
                    v = args[i];
                }
                if (std.mem.eql(u8, name, "config")) config_path = v else metadata_path = v;
                break;
            }
        } else if (std.mem.eql(u8, x, "--deliver")) {
            deliver_flag = true;
        } else {
            cli.put(2, usage);
            cli.err("{s}: error: unrecognized arguments: {s}\n", .{ who, x });
            return 2;
        }
    }
    if (config_path == null or metadata_path == null) {
        var need: std.ArrayList([]const u8) = .empty;
        if (config_path == null) need.append(a, "--config") catch {};
        if (metadata_path == null) need.append(a, "--metadata") catch {};
        cli.put(2, usage);
        cli.err("{s}: error: the following arguments are required: {s}\n", .{ who, std.mem.join(a, ", ", need.items) catch "" });
        return 2;
    }
    if (deliver_flag == (command.len > 0)) {
        cli.err("build-events: pass either --deliver or a command after --\n", .{});
        return refused_exit;
    }
    var act: linux.Sigaction = .{ .handler = .{ .handler = onStop }, .mask = linux.sigemptyset(), .flags = 0 };
    for ([_]linux.SIG{ .TERM, .HUP, .INT }) |sg| _ = linux.sigaction(sg, &act, null);
    var ctx: admin.Ctx = .{ .a = a, .env = env, .pause = .{ .env = env } };
    const outcome = blk: {
        const config = readJson(a, config_path.?, "build-event config") catch break :blk error.Refused;
        const checked = checkConfig(a, config) catch break :blk error.Refused;
        const metadata = readJson(a, metadata_path.?, "build-event metadata") catch break :blk error.Refused;
        break :blk if (deliver_flag) deliverOnly(a, env, &ctx, checked, metadata) else runBuild(a, env, &ctx, checked, metadata, command);
    } catch {
        cli.err("build-events: refused: {s}\n", .{why});
        return refused_exit;
    };
    cli.err("build-events: {s}\n", .{outcome.message});
    return outcome.code;
}

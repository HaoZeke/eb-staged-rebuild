//! ledger-maintenance: readers and maintenance tools for an eb-stack ledger.
//!
//! Three local file tools over the log that `eb-stack ledger append` writes:
//!
//! * `backfill` appends `finding` records for software observed at an
//!   explicit time, from an operator-written manifest pinned by its SHA-256.
//! * `anchor` signs the day's head with `eb-stack ledger head` and adds it to
//!   a directory of immutable daily anchors after `eb-stack ledger verify`
//!   proves it extends the latest anchor.
//! * `project` writes per-package JSON and Markdown history from a log whose
//!   head `eb-stack ledger verify` accepted.
//!
//! Every signature comes from `eb-stack ledger head` or `ledger append` and
//! every proof from `eb-stack ledger verify`. The log and head files are read
//! here only to refuse bad input early and to bind what is projected to the
//! bytes the command verified: the bytes are read once, copied into a private
//! directory, verified there, and parsed from the same buffer.
const std = @import("std");
const oom = @import("oom.zig").oom;
const linux = std.os.linux;
const common = @import("zig_common");
const pyfmt = @import("pyfmt.zig");

const sys = common.sys;
const fsx = common.fsx;
const proc = common.proc;
const atomic = common.atomic;
const state = common.state;
const cli = common.cli;
const Allocator = std.mem.Allocator;
const Sha256 = std.crypto.hash.sha2.Sha256;

pub const DOMAIN = "eb-stack-ledger-v1";
pub const MANIFEST_SCHEMA = "eb-staged-rebuild/observed-backfill/1";
pub const HISTORY_SCHEMA = "eb-staged-rebuild/ledger-history/1";
pub const BACKFILL_KIND = "observed-backfill";
pub const TOOL = "eb-staged-rebuild/ledger-maintenance";
pub const DEFAULT_TIMEOUT: f64 = 120;
pub const PENDING_EXIT: u8 = 75;
const prog = "ledger-maintenance";

const ACTIONS = [_][]const u8{ "install", "rebuild-inplace", "rebuild-bump", "retire", "unretire", "move", "access", "remove", "finding", "correction" };
const STAGES = [_][]const u8{ "started", "finished", "aborted" };
const REQUIRED = [_][]const u8{ "ts", "actor", "stack", "target", "module", "location", "action" };
const LINKS = [_][]const u8{ "supersedes", "superseded_by", "moved_from", "moved_to", "corrects" };
const STRUCTURED = [_][]const u8{ "reason", "easyconfig", "sources", "sbom", "modulefile", "build" };
const OTHER_KNOWN = [_][]const u8{ "id", "index", "sig", "stage" };
const HEAD_FIELDS = [_][]const u8{ "domain", "size", "root", "ts", "public_key", "signature" };
const STAGING_PREFIX = ".anchor-staging-";

// ---- failures --------------------------------------------------------------

/// Refused: bad input or a path that must not be used (exit 2).
/// Verification: eb-stack ledger verify did not accept (exit 2).
/// Equivocation: two signed heads disagree (exit 2).
/// Pending: an eb-stack call failed in a way that may or may not have
/// recorded something; retry the same command (exit 75).
pub const Fail = error{ Refused, Verification, Equivocation, Pending, OutOfMemory };

/// The message of the last failure. One command runs per process.
var why: std.ArrayList(u8) = .empty;
var why_alloc: Allocator = std.heap.page_allocator;

fn fail(comptime kind: Fail, comptime f: []const u8, args: anytype) Fail {
    why.clearRetainingCapacity();
    why.print(why_alloc, f, args) catch oom();
    return kind;
}

fn refuse(comptime f: []const u8, args: anytype) Fail {
    return fail(error.Refused, f, args);
}

pub fn lastMessage() []const u8 {
    return why.items;
}

/// Python's text of an OSError on `path`.
fn osError(a: Allocator, err: anyerror, path: []const u8) []const u8 {
    const text = if (common.sys.asError(err)) |e| common.sys.describe(e) else @errorName(err);
    return std.fmt.allocPrint(a, "{s}: {s}", .{ text, repr(a, path) }) catch path;
}

fn osFail(a: Allocator, err: anyerror, path: []const u8) Fail {
    if (err == error.OutOfMemory) return error.OutOfMemory;
    return refuse("{s}", .{osError(a, err, path)});
}

fn repr(a: Allocator, s: []const u8) []const u8 {
    var out: std.ArrayList(u8) = .empty;
    pyfmt.reprStr(&out, a, s) catch return s;
    return out.items;
}

// ---- strict JSON -----------------------------------------------------------

/// A JSON value with decoded strings (UTF-8; a lone surrogate escape is kept
/// as its WTF-8 bytes and later refused or re-escaped) and integer numbers.
pub const V = union(enum) {
    null,
    bool: bool,
    int: i64,
    str: []const u8,
    arr: []const V,
    obj: []const Mem,

    pub fn get(v: V, key: []const u8) ?V {
        if (v != .obj) return null;
        for (v.obj) |m| if (std.mem.eql(u8, m.key, key)) return m.val;
        return null;
    }

    pub fn has(v: V, key: []const u8) bool {
        return v.get(key) != null;
    }

    pub fn str_(v: ?V) ?[]const u8 {
        const x = v orelse return null;
        return if (x == .str) x.str else null;
    }
};

pub const Mem = struct { key: []const u8, val: V };

const Parser = struct {
    a: Allocator,
    s: []const u8,
    i: usize = 0,
    what: []const u8,
    depth: usize = 0,

    fn ws(p: *Parser) void {
        while (p.i < p.s.len and (p.s[p.i] == ' ' or p.s[p.i] == '\t' or p.s[p.i] == '\n' or p.s[p.i] == '\r')) p.i += 1;
    }

    /// json.JSONDecodeError's text: MSG: line L column C (char N), counted
    /// in characters as Python counts them.
    fn syntaxAt(p: *Parser, comptime msg: []const u8, at: usize) Fail {
        var chars: usize = 0;
        var line: usize = 1;
        var line_start: usize = 0;
        var i: usize = 0;
        while (i < at and i < p.s.len) : (i += 1) {
            if (p.s[i] & 0xc0 == 0x80) continue;
            if (p.s[i] == '\n') {
                line += 1;
                line_start = chars + 1;
            }
            chars += 1;
        }
        return refuse("{s} is not JSON: " ++ msg ++ ": line {d} column {d} (char {d})", .{ p.what, line, chars - line_start + 1, chars });
    }

    fn value(p: *Parser) Fail!V {
        p.ws();
        if (p.i >= p.s.len) return p.syntaxAt("Expecting value", p.i);
        p.depth += 1;
        defer p.depth -= 1;
        if (p.depth > 512) return p.syntaxAt("Expecting value", p.i);
        const c = p.s[p.i];
        switch (c) {
            '{' => return p.object(),
            '[' => return p.array(),
            '"' => return .{ .str = try p.string() },
            't' => return p.literal("true", .{ .bool = true }),
            'f' => return p.literal("false", .{ .bool = false }),
            'n' => return p.literal("null", .null),
            'N' => return p.constant("NaN"),
            'I' => return p.constant("Infinity"),
            '-', '0'...'9' => return p.number(),
            else => return p.syntaxAt("Expecting value", p.i),
        }
    }

    fn literal(p: *Parser, comptime word: []const u8, v: V) Fail!V {
        if (!std.mem.startsWith(u8, p.s[p.i..], word)) return p.syntaxAt("Expecting value", p.i);
        p.i += word.len;
        return v;
    }

    fn constant(p: *Parser, comptime word: []const u8) Fail!V {
        if (!std.mem.startsWith(u8, p.s[p.i..], word)) return p.syntaxAt("Expecting value", p.i);
        return refuse("{s} holds the non-integer number {s}", .{ p.what, word });
    }

    fn number(p: *Parser) Fail!V {
        const start = p.i;
        if (p.s[p.i] == '-') {
            p.i += 1;
            if (std.mem.startsWith(u8, p.s[p.i..], "Infinity")) return refuse("{s} holds the non-integer number -Infinity", .{p.what});
        }
        if (p.i >= p.s.len or !std.ascii.isDigit(p.s[p.i])) return p.syntaxAt("Expecting value", start);
        if (p.s[p.i] == '0') p.i += 1 else while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
        var float = false;
        if (p.i < p.s.len and p.s[p.i] == '.' and p.i + 1 < p.s.len and std.ascii.isDigit(p.s[p.i + 1])) {
            float = true;
            p.i += 1;
            while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
        }
        if (p.i < p.s.len and (p.s[p.i] == 'e' or p.s[p.i] == 'E')) {
            var j = p.i + 1;
            if (j < p.s.len and (p.s[j] == '+' or p.s[j] == '-')) j += 1;
            if (j < p.s.len and std.ascii.isDigit(p.s[j])) {
                float = true;
                p.i = j;
                while (p.i < p.s.len and std.ascii.isDigit(p.s[p.i])) p.i += 1;
            }
        }
        const tok = p.s[start..p.i];
        if (float) return refuse("{s} holds the non-integer number {s}", .{ p.what, tok });
        const n = std.fmt.parseInt(i64, tok, 10) catch return refuse("{s} holds the integer {s}, which is out of range", .{ p.what, tok });
        return .{ .int = n };
    }

    fn hex4(p: *Parser, u_at: usize) Fail!u21 {
        if (p.i + 4 > p.s.len) return p.syntaxAt("Invalid \\uXXXX escape", u_at);
        for (p.s[p.i .. p.i + 4]) |c| if (!std.ascii.isHex(c)) return p.syntaxAt("Invalid \\uXXXX escape", u_at);
        const v = std.fmt.parseInt(u16, p.s[p.i .. p.i + 4], 16) catch unreachable;
        p.i += 4;
        return v;
    }

    fn string(p: *Parser) Fail![]const u8 {
        const begin = p.i;
        p.i += 1; // opening quote
        var out: std.ArrayList(u8) = .empty;
        while (true) {
            if (p.i >= p.s.len) return p.syntaxAt("Unterminated string starting at", begin);
            const c = p.s[p.i];
            if (c == '"') {
                p.i += 1;
                return out.items;
            }
            if (c < 0x20) return p.syntaxAt("Invalid control character at", p.i);
            if (c != '\\') {
                try out.append(p.a, c);
                p.i += 1;
                continue;
            }
            const backslash = p.i;
            p.i += 1;
            if (p.i >= p.s.len) return p.syntaxAt("Unterminated string starting at", begin);
            const e = p.s[p.i];
            p.i += 1;
            const simple: ?u8 = switch (e) {
                '"' => '"',
                '\\' => '\\',
                '/' => '/',
                'b' => 0x08,
                'f' => 0x0c,
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                'u' => null,
                else => return p.syntaxAt("Invalid \\escape", backslash),
            };
            if (simple) |b| {
                try out.append(p.a, b);
                continue;
            }
            var cp = try p.hex4(backslash + 1);
            if (cp >= 0xd800 and cp < 0xdc00 and p.i + 6 <= p.s.len and p.s[p.i] == '\\' and p.s[p.i + 1] == 'u') {
                const save = p.i;
                p.i += 2;
                const lo = p.hex4(save + 1) catch {
                    p.i = save;
                    return p.syntaxAt("Invalid \\uXXXX escape", save + 1);
                };
                if (lo >= 0xdc00 and lo < 0xe000) {
                    cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00);
                } else p.i = save;
            }
            var buf: [4]u8 = undefined;
            // A lone surrogate is kept as its three-byte (WTF-8) form.
            const n = std.unicode.wtf8Encode(cp, &buf) catch unreachable;
            try out.appendSlice(p.a, buf[0..n]);
        }
    }

    fn array(p: *Parser) Fail!V {
        p.i += 1;
        var items: std.ArrayList(V) = .empty;
        p.ws();
        if (p.i < p.s.len and p.s[p.i] == ']') {
            p.i += 1;
            return .{ .arr = items.items };
        }
        while (true) {
            try items.append(p.a, try p.value());
            p.ws();
            if (p.i < p.s.len and p.s[p.i] == ']') {
                p.i += 1;
                return .{ .arr = items.items };
            }
            if (p.i >= p.s.len or p.s[p.i] != ',') return p.syntaxAt("Expecting ',' delimiter", p.i);
            const comma = p.i;
            p.i += 1;
            p.ws();
            if (p.i < p.s.len and p.s[p.i] == ']') return p.syntaxAt("Illegal trailing comma before end of array", comma);
        }
    }

    fn object(p: *Parser) Fail!V {
        p.i += 1;
        var members: std.ArrayList(Mem) = .empty;
        p.ws();
        if (p.i < p.s.len and p.s[p.i] == '}') {
            p.i += 1;
            return .{ .obj = members.items };
        }
        while (true) {
            if (p.i >= p.s.len or p.s[p.i] != '"') return p.syntaxAt("Expecting property name enclosed in double quotes", p.i);
            const key = try p.string();
            p.ws();
            if (p.i >= p.s.len or p.s[p.i] != ':') return p.syntaxAt("Expecting ':' delimiter", p.i);
            p.i += 1;
            const v = try p.value();
            for (members.items) |m| if (std.mem.eql(u8, m.key, key)) return refuse("{s} repeats the key {s}", .{ p.what, repr(p.a, key) });
            try members.append(p.a, .{ .key = key, .val = v });
            p.ws();
            if (p.i < p.s.len and p.s[p.i] == '}') {
                p.i += 1;
                return .{ .obj = members.items };
            }
            if (p.i >= p.s.len or p.s[p.i] != ',') return p.syntaxAt("Expecting ',' delimiter", p.i);
            const comma = p.i;
            p.i += 1;
            p.ws();
            if (p.i < p.s.len and p.s[p.i] == '}') return p.syntaxAt("Illegal trailing comma before end of object", comma);
        }
    }
};

/// json.loads with duplicate keys and non-integer numbers refused. `text`
/// must already be valid UTF-8.
pub fn strictJson(a: Allocator, text: []const u8, what: []const u8) Fail!V {
    var p: Parser = .{ .a = a, .s = text, .what = what };
    const v = try p.value();
    p.ws();
    if (p.i != text.len) return p.syntaxAt("Extra data", p.i);
    return v;
}

fn utf8(a: Allocator, data: []const u8, comptime f: []const u8, args: anytype) Fail![]const u8 {
    _ = a;
    if (!std.unicode.utf8ValidateSlice(data)) return refuse(f ++ " is not UTF-8", args);
    return data;
}

// ---- JSON writers ------------------------------------------------------------

pub const Style = struct {
    sort: bool,
    ascii: bool,
    /// null: one line; otherwise the indent per level.
    indent: ?usize = null,
    /// The separators of a one-line dump: compact (",", ":") or Python's
    /// default (", ", ": ").
    compact: bool = true,
};

fn lessKey(_: void, x: Mem, y: Mem) bool {
    // UTF-8 byte order is code point order, which is Python's str order.
    return std.mem.order(u8, x.key, y.key) == .lt;
}

fn writeStr(out: *std.ArrayList(u8), a: Allocator, s: []const u8, ascii: bool) Allocator.Error!void {
    try out.append(a, '"');
    var it = std.unicode.Wtf8Iterator{ .bytes = s, .i = 0 };
    while (true) {
        const start = it.i;
        const cp = it.nextCodepoint() orelse break;
        switch (cp) {
            '"' => try out.appendSlice(a, "\\\""),
            '\\' => try out.appendSlice(a, "\\\\"),
            '\n' => try out.appendSlice(a, "\\n"),
            '\r' => try out.appendSlice(a, "\\r"),
            '\t' => try out.appendSlice(a, "\\t"),
            0x08 => try out.appendSlice(a, "\\b"),
            0x0c => try out.appendSlice(a, "\\f"),
            else => {
                if (cp < 0x20 or (ascii and cp > 0x7e)) {
                    if (cp >= 0x10000) {
                        const v = cp - 0x10000;
                        try out.print(a, "\\u{x:0>4}\\u{x:0>4}", .{ 0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff) });
                    } else try out.print(a, "\\u{x:0>4}", .{cp});
                } else try out.appendSlice(a, s[start..it.i]);
            },
        }
    }
    try out.append(a, '"');
}

fn newline(out: *std.ArrayList(u8), a: Allocator, st: Style, level: usize) Allocator.Error!void {
    try out.append(a, '\n');
    try out.appendNTimes(a, ' ', st.indent.? * level);
}

pub fn write(out: *std.ArrayList(u8), a: Allocator, v: V, st: Style, level: usize) Allocator.Error!void {
    const item_sep: []const u8 = if (st.indent != null or st.compact) "," else ", ";
    const key_sep: []const u8 = if (st.compact and st.indent == null) ":" else ": ";
    switch (v) {
        .null => try out.appendSlice(a, "null"),
        .bool => |b| try out.appendSlice(a, if (b) "true" else "false"),
        .int => |n| try out.print(a, "{d}", .{n}),
        .str => |s| try writeStr(out, a, s, st.ascii),
        .arr => |items| {
            if (items.len == 0) return out.appendSlice(a, "[]");
            try out.append(a, '[');
            for (items, 0..) |it, i| {
                if (i > 0) try out.appendSlice(a, item_sep);
                if (st.indent != null) try newline(out, a, st, level + 1);
                try write(out, a, it, st, level + 1);
            }
            if (st.indent != null) try newline(out, a, st, level);
            try out.append(a, ']');
        },
        .obj => |members| {
            if (members.len == 0) return out.appendSlice(a, "{}");
            const ms = try a.dupe(Mem, members);
            if (st.sort) std.mem.sort(Mem, ms, {}, lessKey);
            try out.append(a, '{');
            for (ms, 0..) |m, i| {
                if (i > 0) try out.appendSlice(a, item_sep);
                if (st.indent != null) try newline(out, a, st, level + 1);
                try writeStr(out, a, m.key, st.ascii);
                try out.appendSlice(a, key_sep);
                try write(out, a, m.val, st, level + 1);
            }
            if (st.indent != null) try newline(out, a, st, level);
            try out.append(a, '}');
        },
    }
}

pub fn dump(a: Allocator, v: V, st: Style) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    try write(&out, a, v, st, 0);
    return out.items;
}

/// The ledger's canonical JSON: keys sorted, no whitespace, not ASCII-escaped.
pub fn canonical(a: Allocator, v: V) Allocator.Error![]u8 {
    return dump(a, v, .{ .sort = true, .ascii = false });
}

fn jsonBytes(a: Allocator, v: V) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    try write(&out, a, v, .{ .sort = true, .ascii = true, .indent = 2 }, 0);
    try out.append(a, '\n');
    return out.items;
}

/// Python equality of two JSON values: dicts compare without order.
fn eqlV(x: V, y: V) bool {
    if (std.meta.activeTag(x) != std.meta.activeTag(y)) return false;
    switch (x) {
        .null => return true,
        .bool => |b| return b == y.bool,
        .int => |n| return n == y.int,
        .str => |s| return std.mem.eql(u8, s, y.str),
        .arr => |items| {
            if (items.len != y.arr.len) return false;
            for (items, y.arr) |p, q| if (!eqlV(p, q)) return false;
            return true;
        },
        .obj => |ms| {
            if (ms.len != y.obj.len) return false;
            for (ms) |m| {
                const other = y.get(m.key) orelse return false;
                if (!eqlV(m.val, other)) return false;
            }
            return true;
        },
    }
}

// ---- small helpers -----------------------------------------------------------

/// An object built in order.
const Obj = struct {
    a: Allocator,
    ms: std.ArrayList(Mem) = .empty,

    fn put(o: *Obj, key: []const u8, value: V) Allocator.Error!void {
        for (o.ms.items) |*m| if (std.mem.eql(u8, m.key, key)) {
            m.val = value;
            return;
        };
        try o.ms.append(o.a, .{ .key = key, .val = value });
    }

    fn v(o: *Obj) V {
        return .{ .obj = o.ms.items };
    }
};

fn S(s: []const u8) V {
    return .{ .str = s };
}

fn optS(s: ?[]const u8) V {
    return if (s) |x| .{ .str = x } else .null;
}

fn I(n: anytype) V {
    return .{ .int = @intCast(n) };
}

fn isHex(s: []const u8, n: usize) bool {
    if (s.len != n) return false;
    for (s) |c| if (!((c >= '0' and c <= '9') or (c >= 'a' and c <= 'f'))) return false;
    return true;
}

fn hex64(v: ?V) bool {
    const s = V.str_(v) orelse return false;
    return isHex(s, 64);
}

fn nonEmpty(v: ?V) bool {
    const s = V.str_(v) orelse return false;
    return s.len != 0;
}

fn digits(s: []const u8) bool {
    for (s) |c| if (!std.ascii.isDigit(c)) return false;
    return true;
}

/// ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$, ASCII digits.
fn headTs(s: []const u8) bool {
    if (s.len < 20 or s[s.len - 1] != 'Z') return false;
    if (!baseTs(s[0..19])) return false;
    const rest = s[19 .. s.len - 1];
    if (rest.len == 0) return true;
    return rest[0] == '.' and rest.len >= 2 and rest.len <= 10 and digits(rest[1..]);
}

fn baseTs(s: []const u8) bool {
    return s.len == 19 and digits(s[0..4]) and s[4] == '-' and digits(s[5..7]) and s[7] == '-' and digits(s[8..10]) and
        s[10] == 'T' and digits(s[11..13]) and s[13] == ':' and digits(s[14..16]) and s[16] == ':' and digits(s[17..19]);
}

/// ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$
fn observedTs(s: []const u8) bool {
    return s.len == 20 and s[19] == 'Z' and baseTs(s[0..19]);
}

/// ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
fn manifestId(s: []const u8) bool {
    if (s.len == 0 or s.len > 64 or !std.ascii.isAlphanumeric(s[0])) return false;
    for (s) |c| if (!(std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '-')) return false;
    return true;
}

/// The code points _CONTROL matches.
fn isControl(cp: u21) bool {
    return cp <= 0x1f or (cp >= 0x7f and cp <= 0x9f) or cp == 0x2028 or cp == 0x2029 or
        (cp >= 0x202a and cp <= 0x202e) or (cp >= 0x2066 and cp <= 0x2069);
}

fn hasControl(s: []const u8) bool {
    var it = std.unicode.Wtf8Iterator{ .bytes = s, .i = 0 };
    while (it.nextCodepoint()) |cp| if (isControl(cp)) return true;
    return false;
}

fn hasSurrogate(s: []const u8) bool {
    var it = std.unicode.Wtf8Iterator{ .bytes = s, .i = 0 };
    while (it.nextCodepoint()) |cp| if (cp >= 0xd800 and cp < 0xe000) return true;
    return false;
}

fn sha256Hex(a: Allocator, data: []const u8) Allocator.Error![]u8 {
    var d: [32]u8 = undefined;
    Sha256.hash(data, &d, .{});
    return std.fmt.allocPrint(a, "{x}", .{&d});
}

fn contains(list: []const []const u8, s: []const u8) bool {
    for (list) |x| if (std.mem.eql(u8, x, s)) return true;
    return false;
}

// ---- time --------------------------------------------------------------------

fn daysFromCivil(year: i64, month: i64, day: i64) i64 {
    const y = if (month <= 2) year - 1 else year;
    const era = @divFloor(y, 400);
    const yoe = y - era * 400;
    const doy = @divFloor(153 * (if (month > 2) month - 3 else month + 9) + 2, 5) + day - 1;
    const doe = yoe * 365 + @divFloor(yoe, 4) - @divFloor(yoe, 100) + doy;
    return era * 146097 + doe - 719468;
}

fn civilFromDays(z_: i64) struct { y: i64, m: i64, d: i64 } {
    const z = z_ + 719468;
    const era = @divFloor(z, 146097);
    const doe = z - era * 146097;
    const yoe = @divFloor(doe - @divFloor(doe, 1460) + @divFloor(doe, 36524) - @divFloor(doe, 146096), 365);
    const doy = doe - (365 * yoe + @divFloor(yoe, 4) - @divFloor(yoe, 100));
    const mp = @divFloor(5 * doy + 2, 153);
    const d = doy - @divFloor(153 * mp + 2, 5) + 1;
    const m = if (mp < 10) mp + 3 else mp - 9;
    return .{ .y = yoe + era * 400 + @as(i64, if (m <= 2) 1 else 0), .m = m, .d = d };
}

/// time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(seconds))).
pub fn utc(a: Allocator, seconds: i64) Allocator.Error![]u8 {
    const days = @divFloor(seconds, 86400);
    const rem = seconds - days * 86400;
    const c = civilFromDays(days);
    return std.fmt.allocPrint(a, "{d:0>4}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}Z", .{
        @as(u64, @intCast(c.y)),                  @as(u64, @intCast(c.m)),                          @as(u64, @intCast(c.d)),
        @as(u64, @intCast(@divFloor(rem, 3600))), @as(u64, @intCast(@mod(@divFloor(rem, 60), 60))), @as(u64, @intCast(@mod(rem, 60))),
    });
}

/// calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) for a string that
/// already has that shape; null when it is not a time.
pub fn parseObserved(s: []const u8) ?i64 {
    const y = std.fmt.parseInt(i64, s[0..4], 10) catch return null;
    const mo = std.fmt.parseInt(i64, s[5..7], 10) catch return null;
    const d = std.fmt.parseInt(i64, s[8..10], 10) catch return null;
    const h = std.fmt.parseInt(i64, s[11..13], 10) catch return null;
    const mi = std.fmt.parseInt(i64, s[14..16], 10) catch return null;
    const sec = std.fmt.parseInt(i64, s[17..19], 10) catch return null;
    if (y < 1 or mo < 1 or mo > 12 or d < 1 or h > 23 or mi > 59 or sec > 61) return null;
    const leap = @mod(y, 4) == 0 and (@mod(y, 100) != 0 or @mod(y, 400) == 0);
    const mdays: i64 = switch (mo) {
        2 => if (leap) 29 else 28,
        4, 6, 9, 11 => 30,
        else => 31,
    };
    if (d > mdays) return null;
    return daysFromCivil(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec;
}

fn now() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.REALTIME, &ts);
    return @intCast(ts.sec);
}

// ---- paths -------------------------------------------------------------------

fn normalised(path: []const u8) bool {
    if (path.len == 0 or path[0] != '/') return false;
    if (std.mem.eql(u8, path, "/")) return true;
    if (path[path.len - 1] == '/') return false;
    var it = std.mem.splitScalar(u8, path[1..], '/');
    while (it.next()) |c| {
        if (c.len == 0 or std.mem.eql(u8, c, ".") or std.mem.eql(u8, c, "..")) return false;
    }
    return true;
}

/// os.path.realpath(path) for an absolute, normalised path: the deepest
/// existing prefix resolved, the rest appended as it is.
fn realpathLoose(a: Allocator, path: []const u8) Allocator.Error![]const u8 {
    var end = path.len;
    while (true) {
        const prefix = if (end == 0) "/" else path[0..end];
        var buf: [sys.path_max]u8 = undefined;
        if (fsx.realpath(fsx.cwd, prefix, &buf)) |r| {
            const rest = path[end..];
            if (std.mem.eql(u8, r, "/")) return if (rest.len == 0) "/" else a.dupe(u8, rest);
            return std.mem.concat(a, u8, &.{ r, rest });
        } else |_| {}
        if (end == 0) return path;
        end = std.mem.lastIndexOfScalar(u8, path[0..end], '/') orelse 0;
    }
}

/// `path` as given, refused unless absolute, normalised and symlink-free.
fn plainPath(a: Allocator, path: []const u8, what: []const u8) Fail![]const u8 {
    if (path.len == 0) return refuse("{s} must be a path", .{what});
    if (!normalised(path) or std.mem.startsWith(u8, path, "//"))
        return refuse("{s} {s} must be an absolute, normalised path", .{ what, repr(a, path) });
    const real = try realpathLoose(a, path);
    if (!std.mem.eql(u8, real, path)) return refuse("{s} {s} resolves to {s}; path aliases are refused", .{ what, path, real });
    return path;
}

fn existingFile(a: Allocator, path: []const u8, what: []const u8, single_link: bool) Fail![]const u8 {
    _ = try plainPath(a, path, what);
    const st = fsx.statAt(fsx.cwd, path, false) catch |err| switch (err) {
        error.NotFound => return refuse("{s} {s} does not exist", .{ what, path }),
        else => return osFail(a, err, path),
    };
    if (!st.isReg()) return refuse("{s} {s} is not a regular file", .{ what, path });
    if (single_link and st.nlink != 1) return refuse("{s} {s} has {d} hard links; aliases are refused", .{ what, path, st.nlink });
    return path;
}

fn lexists(path: []const u8) bool {
    _ = fsx.statAt(fsx.cwd, path, false) catch return false;
    return true;
}

fn isDirNoFollow(path: []const u8) bool {
    const st = fsx.statAt(fsx.cwd, path, false) catch return false;
    return st.isDir();
}

fn dirname(path: []const u8) []const u8 {
    const i = std.mem.lastIndexOfScalar(u8, path, '/') orelse return "";
    return if (i == 0) "/" else path[0..i];
}

fn join(a: Allocator, dir: []const u8, name: []const u8) Allocator.Error![]u8 {
    if (dir.len > 0 and dir[dir.len - 1] == '/') return std.mem.concat(a, u8, &.{ dir, name });
    return std.mem.concat(a, u8, &.{ dir, "/", name });
}

fn newOrFile(a: Allocator, path: []const u8, what: []const u8) Fail![]const u8 {
    _ = try plainPath(a, path, what);
    const parent = dirname(path);
    if (!isDirNoFollow(parent)) return refuse("the directory of {s} {s} does not exist", .{ what, path });
    if (lexists(path)) _ = try existingFile(a, path, what, true);
    return path;
}

fn existingDir(a: Allocator, path: []const u8, what: []const u8) Fail![]const u8 {
    _ = try plainPath(a, path, what);
    const st = fsx.statAt(fsx.cwd, path, false) catch |err| switch (err) {
        error.NotFound => return refuse("{s} {s} does not exist", .{ what, path }),
        else => return osFail(a, err, path),
    };
    if (!st.isDir()) return refuse("{s} {s} is not a directory", .{ what, path });
    return path;
}

fn inside(child: []const u8, parent: []const u8) bool {
    if (std.mem.eql(u8, child, parent)) return true;
    const p = std.mem.trimEnd(u8, parent, "/");
    return child.len > p.len and std.mem.startsWith(u8, child, p) and child[p.len] == '/';
}

const Named = struct { what: []const u8, path: ?[]const u8 };

/// Refuse two roles that name one file, by path or by inode.
fn distinctFiles(named: []const Named) Fail!void {
    for (named, 0..) |x, i| {
        const px = x.path orelse continue;
        const sx = fsx.statAt(fsx.cwd, px, true) catch null;
        for (named[0..i]) |y| {
            const py = y.path orelse continue;
            const sy = fsx.statAt(fsx.cwd, py, true) catch null;
            const same = if (sx != null and sy != null)
                sx.?.dev == sy.?.dev and sx.?.ino == sy.?.ino
            else
                sx == null and sy == null and std.mem.eql(u8, px, py);
            if (same) return refuse("{s} {s} is the same file as {s}", .{ x.what, px, y.what });
        }
    }
}

fn keyOutside(key: []const u8, dirs: []const Named) Fail!void {
    for (dirs) |d| {
        const dir = d.path orelse continue;
        if (inside(key, dir)) return refuse("signing key {s} is inside the {s} {s}; keep the key off that tree", .{ key, d.what, dir });
    }
}

fn executable(a: Allocator, path: []const u8) Fail![]const u8 {
    _ = try existingFile(a, path, "eb-stack executable", false);
    var zb: [sys.path_max:0]u8 = undefined;
    const zp = sys.z(&zb, path) catch return refuse("eb-stack executable {s} is not executable", .{path});
    if (linux.errno(linux.faccessat(linux.AT.FDCWD, zp.ptr, linux.X_OK, 0)) != .SUCCESS)
        return refuse("eb-stack executable {s} is not executable", .{path});
    return path;
}

/// The bytes of a regular file opened without following a symlink.
fn readRegular(a: Allocator, path: []const u8, what: []const u8) Fail![]const u8 {
    const fd = sys.openat(fsx.cwd, path, .{ .NOFOLLOW = true, .NONBLOCK = true, .CLOEXEC = true }, 0) catch |err| switch (err) {
        error.SymLink => return refuse("{s} {s} is a symbolic link", .{ what, path }),
        else => return refuse("cannot open {s} {s}: {s}", .{ what, path, osError(a, err, path) }),
    };
    defer sys.close(fd);
    const st = fsx.fstat(fd) catch |err| return osFail(a, err, path);
    if (!st.isReg()) return refuse("{s} {s} is not a regular file", .{ what, path });
    return fsx.readAll(a, fd, std.math.maxInt(usize)) catch |err| switch (err) {
        error.OutOfMemory => error.OutOfMemory,
        else => osFail(a, err, path),
    };
}

fn writePrivate(a: Allocator, path: []const u8, data: []const u8) Fail!void {
    const fd = sys.openat(fsx.cwd, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true, .CLOEXEC = true }, 0o600) catch |err| return osFail(a, err, path);
    defer sys.close(fd);
    sys.writeAll(fd, data) catch |err| return osFail(a, err, path);
    sys.fsync(fd) catch |err| return osFail(a, err, path);
}

/// mkdtemp(prefix=prefix, dir=dir): a new private directory.
fn mkdtemp(a: Allocator, dir: []const u8, prefix: []const u8) Fail![]const u8 {
    var attempt: usize = 0;
    while (attempt < 100) : (attempt += 1) {
        var rnd: [6]u8 = undefined;
        _ = sys.call(linux.getrandom, .{ &rnd, rnd.len, 0 }) catch |err| return osFail(a, err, dir);
        var name: [8]u8 = undefined;
        const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_";
        for (&name, 0..) |*c, i| c.* = alphabet[(if (i < 6) rnd[i] else rnd[i - 6] >> 3) % alphabet.len];
        const path = try std.fmt.allocPrint(a, "{s}/{s}{s}", .{ dir, prefix, name });
        var zb: [sys.path_max:0]u8 = undefined;
        const zp = sys.z(&zb, path) catch |err| return osFail(a, err, path);
        _ = sys.call(linux.mkdirat, .{ fsx.cwd, zp.ptr, @as(linux.mode_t, 0o700) }) catch |err| switch (err) {
            error.Exists => continue,
            else => return osFail(a, err, path),
        };
        return path;
    }
    return refuse("cannot create a private directory in {s}", .{dir});
}

fn tempRoot(a: Allocator, env: Env) []const u8 {
    for ([_][]const u8{ "TMPDIR", "TEMP", "TMP" }) |name| if (env.get(name)) |v| if (v.len > 0 and isDirNoFollow(v)) return v;
    _ = a;
    return "/tmp";
}

fn rmtree(a: Allocator, path: []const u8) void {
    const parent = dirname(path);
    const pfd = fsx.openDir(fsx.cwd, parent, true) catch return;
    defer sys.close(pfd);
    fsx.deleteTree(a, pfd, std.fs.path.basename(path)) catch {};
}

fn fsyncDir(a: Allocator, path: []const u8) Fail!void {
    atomic.fsyncDir(path) catch |err| return osFail(a, err, path);
}

/// Names in `dir`, sorted as Python sorts str.
fn listdir(a: Allocator, dir: []const u8) Fail![]const []const u8 {
    const fd = fsx.openDir(fsx.cwd, dir, true) catch |err| return osFail(a, err, dir);
    defer sys.close(fd);
    var it = try fsx.DirIter.init(a, fd);
    var names: std.ArrayList([]const u8) = .empty;
    while (it.next() catch |err| return osFail(a, err, dir)) |e| try names.append(a, try a.dupe(u8, e.name));
    std.mem.sort([]const u8, names.items, {}, struct {
        fn lt(_: void, x: []const u8, y: []const u8) bool {
            return std.mem.order(u8, x, y) == .lt;
        }
    }.lt);
    return names.items;
}

// ---- environment and eb-stack ----------------------------------------------------

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

const output_limit = 64 * 1024;

const Ran = struct { code: u32, stdout: []const u8, stderr: []const u8 };

/// Run eb-stack in its own session with a deadline; Pending on a failure to
/// start or on the deadline, as the Python adapter raises PublisherError.
fn bounded(a: Allocator, env: Env, argv: []const []const u8, timeout: f64) Fail!Ran {
    const ms: u32 = @intFromFloat(@min(@max(timeout * 1000.0, 1.0), 4.0e9));
    const r = proc.run(a, argv[0], argv, env.envp, .{
        .stdout_limit = 16 << 20,
        .stderr_limit = output_limit,
        .timeout_ms = ms,
        .grace_ms = 0,
        .new_session = true,
    }) catch |err| switch (err) {
        error.TimedOut => return fail(error.Pending, "{s} {s} {s} timed out after {s}s", .{ argv[0], argv[1], argv[2], pyFloat(a, timeout) }),
        error.OutOfMemory => return error.OutOfMemory,
        else => return fail(error.Pending, "cannot start {s}: {s}", .{ argv[0], osError(a, err, argv[0]) }),
    };
    const out = r.stdout[0..@min(r.stdout.len, output_limit)];
    return .{ .code = r.term.code(), .stdout = try replaceInvalid(a, out), .stderr = try replaceInvalid(a, r.stderr) };
}

fn replaceInvalid(a: Allocator, s: []const u8) Allocator.Error![]const u8 {
    if (std.unicode.utf8ValidateSlice(s)) return s;
    return common.pytext.decodeReplace(a, s);
}

/// str(float) as Python prints a command-line timeout.
fn pyFloat(a: Allocator, f: f64) []const u8 {
    if (f == @floor(f) and @abs(f) < 1e16) return std.fmt.allocPrint(a, "{d}.0", .{@as(i64, @intFromFloat(f))}) catch "?";
    return std.fmt.allocPrint(a, "{d}", .{f}) catch "?";
}

/// The last `n` characters of `s` (Python slicing by code point).
fn tailChars(s: []const u8, n: usize) []const u8 {
    var count: usize = 0;
    var i = s.len;
    while (i > 0 and count < n) {
        i -= 1;
        if (s[i] & 0xc0 != 0x80) count += 1;
    }
    return s[i..];
}

fn pyStripped(s: []const u8) []const u8 {
    return common.pytext.pyStrip(s);
}

/// str.splitlines(keepends=True) on the line ends eb-stack can print.
fn splitKeep(a: Allocator, s: []const u8) Allocator.Error![]const []const u8 {
    var out: std.ArrayList([]const u8) = .empty;
    var start: usize = 0;
    var i: usize = 0;
    while (i < s.len) : (i += 1) {
        if (s[i] == '\n') {
            try out.append(a, s[start .. i + 1]);
            start = i + 1;
        } else if (s[i] == '\r') {
            const e = if (i + 1 < s.len and s[i + 1] == '\n') i + 2 else i + 1;
            try out.append(a, s[start..e]);
            start = e;
            i = e - 1;
        }
    }
    if (start < s.len) try out.append(a, s[start..]);
    return out.items;
}

fn rstripNl(s: []const u8) []const u8 {
    return std.mem.trimEnd(u8, s, "\n");
}

/// "tile log size N root R extends the head and holds the same lines"
fn tileLine(s: []const u8) ?struct { size: []const u8, root: []const u8 } {
    const p1 = "tile log size ";
    const p2 = " extends the head and holds the same lines";
    if (!std.mem.startsWith(u8, s, p1) or !std.mem.endsWith(u8, s, p2)) return null;
    const mid = s[p1.len .. s.len - p2.len];
    const sp = std.mem.indexOf(u8, mid, " root ") orelse return null;
    const size = mid[0..sp];
    const root = mid[sp + 6 ..];
    if (size.len == 0 or !digits(size) or !isHex(root, 64)) return null;
    return .{ .size = size, .root = root };
}

/// "checkpoint ORIGIN size N root R to PATH"
fn checkpointLine(s: []const u8) ?struct { size: []const u8, root: []const u8 } {
    if (!std.mem.startsWith(u8, s, "checkpoint ")) return null;
    const rest = s["checkpoint ".len..];
    const sp = std.mem.indexOfScalar(u8, rest, ' ') orelse return null;
    if (sp == 0 or hasSpace(rest[0..sp])) return null;
    const after = rest[sp..];
    if (!std.mem.startsWith(u8, after, " size ")) return null;
    const r2 = after[6..];
    const rs = std.mem.indexOf(u8, r2, " root ") orelse return null;
    const size = r2[0..rs];
    if (size.len == 0 or !digits(size)) return null;
    const r3 = r2[rs + 6 ..];
    if (r3.len < 64 + 5) return null;
    const root = r3[0..64];
    if (!isHex(root, 64) or !std.mem.startsWith(u8, r3[64..], " to ") or r3.len == 68) return null;
    return .{ .size = size, .root = root };
}

/// "index checkpoint ORIGIN/index size N root R to PATH"
fn indexCheckpointLine(s: []const u8) bool {
    if (!std.mem.startsWith(u8, s, "index checkpoint ")) return false;
    const rest = s["index checkpoint ".len..];
    const sp = std.mem.indexOf(u8, rest, "/index size ") orelse return false;
    if (sp == 0 or hasSpace(rest[0..sp])) return false;
    const r2 = rest[sp + "/index size ".len ..];
    const rs = std.mem.indexOf(u8, r2, " root ") orelse return false;
    if (rs == 0 or !digits(r2[0..rs])) return false;
    const r3 = r2[rs + 6 ..];
    return r3.len > 68 and isHex(r3[0..64], 64) and std.mem.startsWith(u8, r3[64..], " to ");
}

fn hasSpace(s: []const u8) bool {
    for (s) |c| if (c == ' ' or c == '\t' or c == '\n' or c == '\r' or c == 0x0b or c == 0x0c) return true;
    return false;
}

// ---- log and head readers ------------------------------------------------------

pub const Head = struct {
    size: i64,
    root: []const u8,
    ts: []const u8,
    public_key: []const u8,
    value: V,
};

/// A signed head file exactly as eb-stack writes it.
pub fn parseHead(a: Allocator, data: []const u8, what: []const u8) Fail!Head {
    const text = try utf8(a, data, "{s}", .{what});
    const head = try strictJson(a, text, what);
    var ok = head == .obj and head.obj.len == HEAD_FIELDS.len;
    if (ok) for (head.obj, HEAD_FIELDS) |m, f| {
        if (!std.mem.eql(u8, m.key, f)) ok = false;
    };
    if (!ok) return refuse("{s} must hold exactly domain, size, root, ts, public_key, signature, in that order", .{what});
    const domain = head.get("domain").?;
    if (domain != .str or !std.mem.eql(u8, domain.str, DOMAIN)) return refuse("{s} domain is {s}, not " ++ DOMAIN, .{ what, pyRepr(a, domain) });
    const size = head.get("size").?;
    if (size != .int or size.int < 0) return refuse("{s} size must be a non-negative integer", .{what});
    if (!hex64(head.get("root"))) return refuse("{s} root must be 64 lower-case hex characters", .{what});
    if (!hex64(head.get("public_key"))) return refuse("{s} public_key must be 64 lower-case hex characters", .{what});
    const sig = V.str_(head.get("signature")) orelse "";
    if (!isHex(sig, 128)) return refuse("{s} signature must be 128 lower-case hex characters", .{what});
    const ts = V.str_(head.get("ts")) orelse "";
    if (!headTs(ts)) return refuse("{s} ts must be RFC 3339 UTC ending in Z", .{what});
    const again = try dump(a, head, .{ .sort = false, .ascii = true });
    if (!(text.len == again.len + 1 and std.mem.eql(u8, text[0..again.len], again) and text[again.len] == '\n'))
        return refuse("{s} is not in the form eb-stack writes", .{what});
    return .{ .size = size.int, .root = head.get("root").?.str, .ts = ts, .public_key = head.get("public_key").?.str, .value = head };
}

/// repr() of a JSON value in a message.
fn pyRepr(a: Allocator, v: V) []const u8 {
    return switch (v) {
        .str => |s| repr(a, s),
        .null => "None",
        .bool => |b| if (b) "True" else "False",
        .int => |n| std.fmt.allocPrint(a, "{d}", .{n}) catch "?",
        else => canonical(a, v) catch "?",
    };
}

pub const Record = struct {
    v: V,
    index: usize,
    id: []const u8,
    module: []const u8,
    stack: []const u8,
    action: []const u8,
};

fn checkRecord(a: Allocator, rec: V, index: usize, stack: ?[]const u8) Fail!void {
    for (rec.obj) |m| {
        if (!contains(&REQUIRED, m.key) and !contains(&LINKS, m.key) and !contains(&STRUCTURED, m.key) and !contains(&OTHER_KNOWN, m.key))
            return refuse("ledger line {d} has the unknown field {s}", .{ index, repr(a, m.key) });
    }
    for (REQUIRED) |k| if (!nonEmpty(rec.get(k))) return refuse("ledger line {d} field {s} must be a non-empty string", .{ index, k });
    const action = rec.get("action").?.str;
    if (!contains(&ACTIONS, action)) return refuse("ledger line {d} action {s} is not a ledger action", .{ index, repr(a, action) });
    for (LINKS ++ [_][]const u8{ "stage", "sig" }) |k| if (rec.has(k) and !nonEmpty(rec.get(k))) return refuse("ledger line {d} field {s} must be a non-empty string", .{ index, k });
    if (rec.get("stage")) |st| if (!contains(&STAGES, st.str)) return refuse("ledger line {d} stage {s} is not a ledger stage", .{ index, repr(a, st.str) });
    const idx = rec.get("index");
    if (idx == null or idx.? != .int or idx.?.int != @as(i64, @intCast(index)))
        return refuse("ledger line {d} carries index {s}", .{ index, if (idx) |x| pyRepr(a, x) else "None" });
    if (!hex64(rec.get("id"))) return refuse("ledger line {d} id must be 64 lower-case hex characters", .{index});
    if (std.mem.eql(u8, action, "correction") and !rec.has("corrects")) return refuse("ledger line {d} is a correction that names no record", .{index});
    if (rec.get("build")) |build| if (build == .obj) if (build.get("event_id")) |e| if (!nonEmpty(e))
        return refuse("ledger line {d} build.event_id must be a non-empty string", .{index});
    if (stack) |s| if (!std.mem.eql(u8, rec.get("stack").?.str, s))
        return refuse("ledger line {d} is stack {s} in a log of stack {s}", .{ index, repr(a, rec.get("stack").?.str), repr(a, s) });
}

/// Records of a ledger log, each line checked against the ledger format.
/// eb-stack ledger verify remains the authority; a log ending in a partial
/// line is refused rather than repaired.
pub fn parseLog(a: Allocator, data: []const u8, what: []const u8) Fail![]const Record {
    if (data.len == 0) return &.{};
    if (data[data.len - 1] != '\n') return refuse("the last line of {s} has no newline; it ends in a partial write", .{what});
    const text = try utf8(a, data, "{s}", .{what});
    var records: std.ArrayList(Record) = .empty;
    var it = std.mem.splitScalar(u8, text[0 .. text.len - 1], '\n');
    var index: usize = 0;
    while (it.next()) |line| : (index += 1) {
        const lwhat = try std.fmt.allocPrint(a, "{s} line {d}", .{ what, index });
        const rec = try strictJson(a, line, lwhat);
        if (rec != .obj) return refuse("{s} line {d} is not a JSON object", .{ what, index });
        try checkRecord(a, rec, index, if (records.items.len > 0) records.items[0].stack else null);
        if (!std.mem.eql(u8, try canonical(a, rec), line)) return refuse("{s} line {d} is not in canonical form", .{ what, index });
        try records.append(a, .{
            .v = rec,
            .index = index,
            .id = rec.get("id").?.str,
            .module = rec.get("module").?.str,
            .stack = rec.get("stack").?.str,
            .action = rec.get("action").?.str,
        });
    }
    return records.items;
}

/// The bytes up to the last newline: a concurrent append may be mid-line.
fn committedPrefix(data: []const u8) []const u8 {
    const end = std.mem.lastIndexOfScalar(u8, data, '\n') orelse return "";
    return data[0 .. end + 1];
}

/// eb-stack ledger verify against private copies of the bytes read.
const Verifier = struct {
    a: Allocator,
    env: Env,
    eb_stack: []const u8,
    public_key: []const u8,
    timeout: f64,

    /// `expected` is the receipt line; one tile-log line for `size` and
    /// `root` follows it, except for a size-0 head over an empty log.
    fn run(v: Verifier, args: []const []const u8, expected: []const u8, size: i64, root: []const u8) Fail!void {
        const a = v.a;
        const argv = try std.mem.concat(a, []const u8, &.{ &.{ v.eb_stack, "ledger", "verify" }, args });
        const r = try bounded(a, v.env, argv, v.timeout);
        if (r.code != 0) return fail(error.Verification, "eb-stack ledger verify exited {d}: {s}", .{ r.code, tailChars(pyStripped(r.stderr), 500) });
        const lines = try splitKeep(a, r.stdout);
        var good: bool = undefined;
        if (size == 0) {
            good = lines.len == 1 and std.mem.eql(u8, lines[0], expected);
        } else {
            const want_size = try std.fmt.allocPrint(a, "{d}", .{size});
            const tl = if (lines.len == 2) tileLine(rstripNl(lines[1])) else null;
            good = lines.len >= 1 and std.mem.eql(u8, lines[0], expected) and tl != null and
                std.mem.eql(u8, tl.?.size, want_size) and std.mem.eql(u8, tl.?.root, root);
        }
        if (!good) return fail(error.Verification, "eb-stack ledger verify printed an unexpected receipt: {s}", .{repr(a, tailChars(r.stdout, 500))});
    }

    /// Write the tile log beside a private copy of `log`; verify audits it.
    fn tiles(v: Verifier, log: []const u8) Fail!void {
        const r = try bounded(v.a, v.env, &.{ v.eb_stack, "ledger", "tiles", "--log", log }, v.timeout);
        if (r.code != 0) return fail(error.Verification, "eb-stack ledger tiles exited {d}: {s}", .{ r.code, tailChars(pyStripped(r.stderr), 500) });
    }

    /// The signed head is the root of the first `head.size` records of `log`.
    fn binds(v: Verifier, log: []const u8, head_path: []const u8, head: Head, records: []const Record) Fail!void {
        const a = v.a;
        if (head.size > records.len) return fail(error.Verification, "head covers {d} records but the ledger has {d}", .{ head.size, records.len });
        if (head.size == 0) {
            const exp = try std.fmt.allocPrint(a, "the head of size 0 root {s} extends the head of size 0 root {s}\n", .{ head.root, head.root });
            return v.run(&.{ "--log", log, "--public-key", v.public_key, "--from", head_path, "--to", head_path }, exp, head.size, head.root);
        }
        const last: usize = @intCast(head.size - 1);
        const exp = try std.fmt.allocPrint(a, "record {d} id {s} is in the head of size {d} root {s}\n", .{ last, records[last].id, head.size, head.root });
        const last_s = try std.fmt.allocPrint(a, "{d}", .{last});
        return v.run(&.{ "--log", log, "--public-key", v.public_key, "--record", last_s, "--head", head_path }, exp, head.size, head.root);
    }

    fn extends(v: Verifier, log: []const u8, older_path: []const u8, older: Head, newer_path: []const u8, newer: Head) Fail!void {
        const a = v.a;
        if (older.size == newer.size and !std.mem.eql(u8, older.root, newer.root))
            return fail(error.Equivocation, "two signed heads of size {d} have roots {s} and {s}", .{ older.size, older.root, newer.root });
        if (older.size > newer.size)
            return fail(error.Equivocation, "the head of size {d} is older than an anchored head of size {d}", .{ newer.size, older.size });
        const exp = try std.fmt.allocPrint(a, "the head of size {d} root {s} extends the head of size {d} root {s}\n", .{ newer.size, newer.root, older.size, older.root });
        v.run(&.{ "--log", log, "--public-key", v.public_key, "--from", older_path, "--to", newer_path }, exp, newer.size, newer.root) catch |err| switch (err) {
            error.Verification => {
                const inner = try a.dupe(u8, why.items);
                return fail(error.Equivocation, "the head of size {d} does not extend the anchored head: {s}", .{ newer.size, inner });
            },
            else => return err,
        };
    }
};

pub const VerifiedLog = struct { head: Head, records: []const Record };

/// Read `log` and `head` once and have eb-stack verify those bytes. The head
/// must be signed by `public_key` and cover every record.
fn readVerified(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, head: []const u8, public_key: []const u8, timeout: f64) Fail!VerifiedLog {
    _ = try executable(a, eb_stack);
    _ = try existingFile(a, log, "ledger", true);
    _ = try existingFile(a, head, "head", true);
    _ = try existingFile(a, public_key, "public key", false);
    try distinctFiles(&.{ .{ .what = "ledger", .path = log }, .{ .what = "head", .path = head }, .{ .what = "public key", .path = public_key } });
    const log_bytes = try readRegular(a, log, "ledger");
    const head_bytes = try readRegular(a, head, "head");
    const signed = try parseHead(a, head_bytes, try std.fmt.allocPrint(a, "head {s}", .{head}));
    const records = try parseLog(a, log_bytes, try std.fmt.allocPrint(a, "ledger {s}", .{log}));
    if (signed.size > records.len)
        return fail(error.Verification, "head {s} covers {d} records but ledger {s} has {d}", .{ head, signed.size, log, records.len });
    if (signed.size < records.len)
        return fail(error.Verification, "head {s} covers {d} of the {d} records in {s}; sign a current head before reading", .{ head, signed.size, records.len, log });
    const private = try mkdtemp(a, tempRoot(a, env), "eb-ledger-verify-");
    defer rmtree(a, private);
    const copy_log = try join(a, private, "ledger.jsonl");
    const copy_head = try join(a, private, "head.json");
    try writePrivate(a, copy_log, log_bytes);
    try writePrivate(a, copy_head, head_bytes);
    const verifier: Verifier = .{ .a = a, .env = env, .eb_stack = eb_stack, .public_key = public_key, .timeout = timeout };
    try verifier.tiles(copy_log);
    try verifier.binds(copy_log, copy_head, signed, records);
    return .{ .head = signed, .records = records };
}

// ---- observed-history backfill ---------------------------------------------------

fn text_(a: Allocator, value: ?V, what: []const u8, path: bool) Fail![]const u8 {
    const s = V.str_(value) orelse "";
    if (s.len == 0 or !std.mem.eql(u8, s, common.pytext.pyStrip(s)) or hasControl(s))
        return refuse("manifest {s} must be a non-empty string without control characters or edge spaces", .{what});
    if (hasSurrogate(s)) return refuse("manifest {s} holds an unpaired surrogate escape", .{what});
    if (path and !normalised(s)) return refuse("manifest {s} must be an absolute, normalised path", .{what});
    _ = a;
    return s;
}

fn only(a: Allocator, mapping: ?V, allowed: []const []const u8, what: []const u8) Fail!V {
    const m = mapping orelse return refuse("manifest {s} must be a JSON object", .{what});
    if (m != .obj) return refuse("manifest {s} must be a JSON object", .{what});
    var extra: std.ArrayList([]const u8) = .empty;
    for (m.obj) |mem| if (!contains(allowed, mem.key)) try extra.append(a, mem.key);
    if (extra.items.len > 0) {
        std.mem.sort([]const u8, extra.items, {}, struct {
            fn lt(_: void, x: []const u8, y: []const u8) bool {
                return std.mem.order(u8, x, y) == .lt;
            }
        }.lt);
        return refuse("manifest {s} has unknown fields {s}", .{ what, try std.mem.join(a, ", ", extra.items) });
    }
    return m;
}

pub const Manifest = struct { v: V, digest: []const u8 };

/// An observed-history manifest, refused unless its bytes hash to
/// `expected_sha256`.
pub fn loadManifest(a: Allocator, path: []const u8, expected_sha256: []const u8, now_s: i64) Fail!Manifest {
    _ = try existingFile(a, path, "backfill manifest", true);
    if (!isHex(expected_sha256, 64)) return refuse("the manifest SHA-256 must be 64 lower-case hex characters", .{});
    const data = try readRegular(a, path, "backfill manifest");
    const digest = try sha256Hex(a, data);
    if (!std.mem.eql(u8, digest, expected_sha256)) return refuse("manifest {s} hashes to {s}, not the pinned {s}", .{ path, digest, expected_sha256 });
    const text = try utf8(a, data, "manifest {s}", .{path});
    const manifest = try strictJson(a, text, try std.fmt.allocPrint(a, "manifest {s}", .{path}));
    _ = try only(a, manifest, &.{ "schema", "manifest_id", "observed_at", "observer", "stack", "target", "observations" }, "");
    const schema = V.str_(manifest.get("schema")) orelse "";
    if (!std.mem.eql(u8, schema, MANIFEST_SCHEMA) or manifest.get("schema").? != .str) return refuse("manifest schema must be " ++ MANIFEST_SCHEMA, .{});
    const mid = V.str_(manifest.get("manifest_id")) orelse "";
    if (!manifestId(mid)) return refuse("manifest manifest_id must be 1-64 of A-Z a-z 0-9 . _ - starting alphanumeric", .{});
    const observed = V.str_(manifest.get("observed_at")) orelse "";
    if (!observedTs(observed)) return refuse("manifest observed_at must be YYYY-MM-DDTHH:MM:SSZ", .{});
    const observed_s = parseObserved(observed) orelse return refuse("manifest observed_at {s} is not a time", .{observed});
    if (observed_s > now_s) return refuse("manifest observed_at {s} is after the current time {s}", .{ observed, try utc(a, now_s) });
    for ([_][]const u8{ "observer", "stack", "target" }) |k| _ = try text_(a, manifest.get(k), k, false);
    const obs = manifest.get("observations");
    if (obs == null or obs.? != .arr or obs.?.arr.len == 0) return refuse("manifest observations must be a non-empty list", .{});
    var seen: std.ArrayList([2][]const u8) = .empty;
    for (obs.?.arr, 0..) |item, number| {
        const where = try std.fmt.allocPrint(a, "observations[{d}]", .{number});
        _ = try only(a, item, &.{ "module", "location", "modulefile", "provenance", "note" }, where);
        const module = try text_(a, item.get("module"), try std.fmt.allocPrint(a, "{s}.module", .{where}), false);
        const location = try text_(a, item.get("location"), try std.fmt.allocPrint(a, "{s}.location", .{where}), true);
        for (seen.items) |p| if (std.mem.eql(u8, p[0], module) and std.mem.eql(u8, p[1], location))
            return refuse("manifest {s} repeats {s} at {s}", .{ where, module, location });
        try seen.append(a, .{ module, location });
        if (item.get("note")) |n| _ = try text_(a, n, try std.fmt.allocPrint(a, "{s}.note", .{where}), false);
        if (item.get("modulefile")) |mf| {
            const mw = try std.fmt.allocPrint(a, "{s}.modulefile", .{where});
            _ = try only(a, mf, &.{ "path", "sha256" }, mw);
            _ = try text_(a, mf.get("path"), try std.fmt.allocPrint(a, "{s}.path", .{mw}), true);
            if (!hex64(mf.get("sha256"))) return refuse("manifest {s}.sha256 must be 64 lower-case hex characters", .{mw});
        }
        const prov = item.get("provenance");
        if (prov == null or prov.? != .arr or prov.?.arr.len == 0) return refuse("manifest {s}.provenance must name at least one original reference", .{where});
        for (prov.?.arr, 0..) |ref, rn| {
            const rw = try std.fmt.allocPrint(a, "{s}.provenance[{d}]", .{ where, rn });
            _ = try only(a, ref, &.{ "ref", "kind", "sha256" }, rw);
            _ = try text_(a, ref.get("ref"), try std.fmt.allocPrint(a, "{s}.ref", .{rw}), false);
            if (ref.get("kind")) |k| _ = try text_(a, k, try std.fmt.allocPrint(a, "{s}.kind", .{rw}), false);
            if (ref.get("sha256")) |h| if (!hex64(h)) return refuse("manifest {s}.sha256 must be 64 lower-case hex characters", .{rw});
        }
    }
    return .{ .v = manifest, .digest = digest };
}

/// One `finding` record per observation, stamped with the observation time.
pub fn backfillRecords(a: Allocator, m: Manifest) Allocator.Error![]const V {
    const manifest = m.v;
    const mid = manifest.get("manifest_id").?.str;
    const observed = manifest.get("observed_at").?.str;
    var records: std.ArrayList(V) = .empty;
    for (manifest.get("observations").?.arr) |item| {
        const module = item.get("module").?.str;
        const location = item.get("location").?.str;
        const place = (try sha256Hex(a, location))[0..16];
        var build: Obj = .{ .a = a };
        try build.put("event_id", S(try std.fmt.allocPrint(a, "eb-staged-rebuild/backfill/{s}/{s}@{s}", .{ mid, module, place })));
        try build.put("tool", S(TOOL));
        try build.put("kind", S(BACKFILL_KIND));
        try build.put("manifest_id", S(mid));
        try build.put("manifest_sha256", S(m.digest));
        try build.put("observed_at", S(observed));
        try build.put("provenance", item.get("provenance").?);
        if (item.get("note")) |n| try build.put("note", n);
        var rec: Obj = .{ .a = a };
        try rec.put("ts", S(observed));
        try rec.put("actor", manifest.get("observer").?);
        try rec.put("stack", manifest.get("stack").?);
        try rec.put("target", manifest.get("target").?);
        try rec.put("module", S(module));
        try rec.put("location", S(location));
        try rec.put("action", S("finding"));
        try rec.put("reason", S(try std.fmt.allocPrint(a, "observed by backfill manifest {s} at {s}; no history before the observation is recorded", .{ mid, observed })));
        try rec.put("build", build.v());
        if (item.get("modulefile")) |mf| try rec.put("modulefile", mf);
        try records.append(a, rec.v());
    }
    return records.items;
}

fn body(a: Allocator, record: V) Allocator.Error!V {
    var o: Obj = .{ .a = a };
    for (record.obj) |m| if (!contains(&.{ "id", "index", "sig" }, m.key)) try o.put(m.key, m.val);
    return o.v();
}

/// One `eb-stack ledger append` with the receipt checks of the Python
/// adapter (EbStackLedger.append with a public key).
fn ledgerAppend(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, key: []const u8, head: []const u8, public_key: []const u8, timeout: f64, record_path: []const u8, event_id: []const u8) Fail!V {
    const r = try bounded(a, env, &.{ eb_stack, "ledger", "append", record_path, "--log", log, "--key", key, "--head-out", head }, timeout);
    if (r.code != 0) return fail(error.Pending, "eb-stack ledger append exited {d}: {s}", .{ r.code, tailChars(pyStripped(r.stderr), 500) });
    const out = pyStripped(r.stdout);
    const first = blk: {
        const nl = std.mem.indexOfAny(u8, out, "\n\r") orelse break :blk out;
        break :blk out[0..nl];
    };
    const summary = parseSummary(first) orelse return fail(error.Pending, "eb-stack ledger append printed no receipt: {s}", .{repr(a, tailChars(out, 500))});
    const head_text = readRegular(a, head, "head") catch return fail(error.Pending, "cannot read head {s}: {s}", .{ head, why.items });
    const hv = strictJson(a, head_text, "head") catch return fail(error.Pending, "cannot read head {s}: {s}", .{ head, why.items });
    const hsize = hv.get("size");
    const hroot = V.str_(hv.get("root"));
    if (hsize == null or hsize.? != .int or hsize.?.int != summary.size or hroot == null or !std.mem.eql(u8, hroot.?, summary.root) or summary.index >= summary.size)
        return fail(error.Pending, "head {s} is size {s} root {s}, not the reported size {d} root {s} covering record {d}", .{
            head, if (hsize) |x| pyRepr(a, x) else "None", if (hroot) |x| x else "None", summary.size, summary.root, summary.index,
        });
    const logged = try loggedEvent(a, log, @intCast(summary.index));
    if (logged == null or !std.mem.eql(u8, logged.?, event_id)) return fail(error.Pending, "ledger record {d} is not event {s}", .{ summary.index, event_id });
    const idx_s = try std.fmt.allocPrint(a, "{d}", .{summary.index});
    const vr = try bounded(a, env, &.{ eb_stack, "ledger", "verify", "--log", log, "--public-key", public_key, "--record", idx_s, "--head", head }, timeout);
    if (vr.code != 0) return fail(error.Pending, "eb-stack ledger verify exited {d}: {s}", .{ vr.code, tailChars(pyStripped(vr.stderr), 500) });
    var o: Obj = .{ .a = a };
    try o.put("index", I(summary.index));
    try o.put("id", S(summary.id));
    try o.put("head_size", I(summary.size));
    try o.put("head_root", S(summary.root));
    try o.put("replayed", .{ .bool = summary.replayed });
    try o.put("verified", .{ .bool = true });
    return o.v();
}

const Summary = struct { replayed: bool, index: i64, id: []const u8, size: i64, root: []const u8 };

/// ^(appended|already recorded) record (\d+) id ([0-9a-f]{64}); head size (\d+) root ([0-9a-f]{64})(?:; .*)?$
fn parseSummary(line: []const u8) ?Summary {
    var s = line;
    var replayed = false;
    if (std.mem.startsWith(u8, s, "appended record ")) {
        s = s["appended record ".len..];
    } else if (std.mem.startsWith(u8, s, "already recorded record ")) {
        s = s["already recorded record ".len..];
        replayed = true;
    } else return null;
    const sp = std.mem.indexOf(u8, s, " id ") orelse return null;
    if (sp == 0 or !digits(s[0..sp])) return null;
    const index = std.fmt.parseInt(i64, s[0..sp], 10) catch return null;
    s = s[sp + 4 ..];
    if (s.len < 64 or !isHex(s[0..64], 64)) return null;
    const id = s[0..64];
    s = s[64..];
    if (!std.mem.startsWith(u8, s, "; head size ")) return null;
    s = s["; head size ".len..];
    const rs = std.mem.indexOf(u8, s, " root ") orelse return null;
    if (rs == 0 or !digits(s[0..rs])) return null;
    const size = std.fmt.parseInt(i64, s[0..rs], 10) catch return null;
    s = s[rs + 6 ..];
    if (s.len < 64 or !isHex(s[0..64], 64)) return null;
    const root = s[0..64];
    s = s[64..];
    if (s.len != 0 and !std.mem.startsWith(u8, s, "; ")) return null;
    return .{ .replayed = replayed, .index = index, .id = id, .size = size, .root = root };
}

fn loggedEvent(a: Allocator, log: []const u8, index: usize) Fail!?[]const u8 {
    const data = readRegular(a, log, "ledger") catch return fail(error.Pending, "cannot read ledger {s}: {s}", .{ log, why.items });
    var it = std.mem.splitScalar(u8, data, '\n');
    var n: usize = 0;
    while (it.next()) |line| : (n += 1) {
        if (n != index) continue;
        const v = strictJson(a, line, "ledger line") catch return fail(error.Pending, "cannot read ledger {s}: {s}", .{ log, why.items });
        const build = v.get("build") orelse return null;
        if (build != .obj) return null;
        return V.str_(build.get("event_id"));
    }
    return null;
}

fn checkReceipt(a: Allocator, receipt: V, event_id: []const u8) Fail!V {
    const idx = receipt.get("index");
    const hs = receipt.get("head_size");
    const valid = idx != null and idx.? == .int and hs != null and hs.? == .int and
        idx.?.int >= 0 and idx.?.int < hs.?.int and hex64(receipt.get("id")) and hex64(receipt.get("head_root"));
    if (!valid) return fail(error.Pending, "malformed ledger receipt for {s}: {s}", .{ event_id, try canonical(a, receipt) });
    return receipt;
}

pub fn backfill(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, key: []const u8, head: []const u8, public_key: []const u8, manifest_path: []const u8, manifest_sha256: []const u8, timeout: f64, faults: Faults, now_s: i64) Fail!V {
    _ = try executable(a, eb_stack);
    _ = try newOrFile(a, log, "ledger");
    _ = try newOrFile(a, head, "head");
    _ = try existingFile(a, key, "signing key", true);
    _ = try existingFile(a, public_key, "public key", false);
    try distinctFiles(&.{
        .{ .what = "ledger", .path = log },                      .{ .what = "head", .path = head },
        .{ .what = "signing key", .path = key },                 .{ .what = "public key", .path = public_key },
        .{ .what = "backfill manifest", .path = manifest_path },
    });
    try keyOutside(key, &.{ .{ .what = "ledger directory", .path = dirname(log) }, .{ .what = "head directory", .path = dirname(head) } });
    const manifest = try loadManifest(a, manifest_path, manifest_sha256, now_s);
    const records = try backfillRecords(a, manifest);
    const mid = manifest.v.get("manifest_id").?.str;
    const mstack = manifest.v.get("stack").?.str;
    // eb-stack quarantines an interrupted tail on the next append.
    const existing: []const Record = if (lexists(log))
        try parseLog(a, committedPrefix(try readRegular(a, log, "ledger")), try std.fmt.allocPrint(a, "ledger {s}", .{log}))
    else
        &.{};
    if (existing.len > 0 and !std.mem.eql(u8, existing[0].stack, mstack))
        return refuse("manifest stack {s} is not the ledger's stack {s}", .{ repr(a, mstack), repr(a, existing[0].stack) });
    var by_event: std.StringArrayHashMapUnmanaged(Record) = .empty;
    for (existing) |rec| {
        const build = rec.v.get("build") orelse continue;
        if (build != .obj) continue;
        const kind = V.str_(build.get("kind"));
        const bmid = V.str_(build.get("manifest_id"));
        if (kind != null and std.mem.eql(u8, kind.?, BACKFILL_KIND) and bmid != null and std.mem.eql(u8, bmid.?, mid)) {
            const bsha = build.get("manifest_sha256");
            const same = bsha != null and bsha.? == .str and std.mem.eql(u8, bsha.?.str, manifest.digest);
            if (!same) return refuse("manifest {s} was backfilled from bytes hashing to {s}, not {s}; a backfill manifest is immutable", .{
                mid, if (bsha) |x| (if (x == .str) x.str else pyRepr(a, x)) else "None", manifest.digest,
            });
        }
        if (build.get("event_id")) |e| if (e == .str) try by_event.put(a, e.str, rec);
    }
    for (records) |rec| {
        const event_id = rec.get("build").?.get("event_id").?.str;
        if (by_event.get(event_id)) |old| if (!eqlV(try body(a, old.v), rec))
            return refuse("ledger record {d} holds {s} with other content", .{ old.index, event_id });
    }
    const private = try mkdtemp(a, tempRoot(a, env), "eb-ledger-backfill-");
    defer rmtree(a, private);
    var receipts: std.ArrayList(V) = .empty;
    var appended: usize = 0;
    var replayed: usize = 0;
    for (records, 0..) |rec, number| {
        const event_id = rec.get("build").?.get("event_id").?.str;
        const path = try std.fmt.allocPrint(a, "{s}/{d:0>6}.json", .{ private, number });
        var data = try dump(a, rec, .{ .sort = true, .ascii = true, .compact = false });
        data = try std.mem.concat(a, u8, &.{ data, "\n" });
        try writePrivate(a, path, data);
        try faults.reach(try std.fmt.allocPrint(a, "backfill.append:{s}", .{event_id}));
        const receipt = try checkReceipt(a, try ledgerAppend(a, env, eb_stack, log, key, head, public_key, timeout, path, event_id), event_id);
        if (receipt.get("replayed").?.bool) replayed += 1 else appended += 1;
        var r: Obj = .{ .a = a };
        for (receipt.obj) |m| try r.put(m.key, m.val);
        try r.put("event_id", S(event_id));
        try receipts.append(a, r.v());
    }
    var o: Obj = .{ .a = a };
    try o.put("manifest_id", S(mid));
    try o.put("manifest_sha256", S(manifest.digest));
    try o.put("appended", I(appended));
    try o.put("replayed", I(replayed));
    try o.put("receipts", .{ .arr = receipts.items });
    return o.v();
}

// ---- fault points ------------------------------------------------------------

/// The Python FaultInjector's environment hook: pause before a named step so
/// a test can kill the process there.
pub const Faults = struct {
    env: Env,

    pub fn reach(f: Faults, step: []const u8) Fail!void {
        const token = f.env.get("EB_STAGED_REBUILD_PAUSE_AT") orelse return;
        if (token.len == 0 or !std.mem.eql(u8, token, step)) return;
        if (f.env.get("EB_STAGED_REBUILD_PAUSE_READY")) |ready| if (ready.len > 0) {
            const fd = sys.openat(fsx.cwd, ready, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o644) catch null;
            if (fd) |x| sys.close(x);
        };
        const secs = std.fmt.parseFloat(f64, f.env.get("EB_STAGED_REBUILD_PAUSE_SECONDS") orelse "30") catch 30;
        const ns: u64 = @intFromFloat(@max(secs, 0) * 1e9);
        var ts: linux.timespec = .{ .sec = @intCast(ns / 1_000_000_000), .nsec = @intCast(ns % 1_000_000_000) };
        _ = linux.nanosleep(&ts, null);
    }
};

// ---- daily anchors -------------------------------------------------------------

const Anchor = struct { day: []const u8, path: []const u8, head: Head, bytes: []const u8 };

/// ^(\d{4}-\d{2}-\d{2})\.head\.json$
fn anchorDay(name: []const u8) ?[]const u8 {
    const suffix = ".head.json";
    if (name.len != 10 + suffix.len or !std.mem.endsWith(u8, name, suffix)) return null;
    const d = name[0..10];
    if (!(digits(d[0..4]) and d[4] == '-' and digits(d[5..7]) and d[7] == '-' and digits(d[8..10]))) return null;
    return d;
}

/// Anchors by day, each a single-link regular file whose head is signed that day.
fn listAnchors(a: Allocator, anchors: []const u8) Fail![]const Anchor {
    var found: std.ArrayList(Anchor) = .empty;
    for (try listdir(a, anchors)) |name| {
        const day = anchorDay(name) orelse continue;
        const path = try join(a, anchors, name);
        const st = fsx.statAt(fsx.cwd, path, false) catch |err| return osFail(a, err, path);
        if (!st.isReg() or st.nlink != 1) return refuse("anchor {s} must be a regular file with one link", .{path});
        const data = try readRegular(a, path, "anchor");
        const head = try parseHead(a, data, try std.fmt.allocPrint(a, "anchor {s}", .{path}));
        if (!std.mem.eql(u8, head.ts[0..10], day)) return refuse("anchor {s} holds a head signed at {s}, not on {s}", .{ path, head.ts, day });
        try found.append(a, .{ .day = day, .path = path, .head = head, .bytes = data });
    }
    const items = found.items;
    if (items.len > 1) for (items[0 .. items.len - 1], items[1..]) |older, newer| {
        const x = older.head;
        const y = newer.head;
        if (!std.mem.eql(u8, x.public_key, y.public_key)) return fail(error.Equivocation, "anchors {s} and {s} are signed by different keys", .{ older.path, newer.path });
        if (std.mem.order(u8, y.ts, x.ts) == .lt) return refuse("anchor {s} is signed before {s}", .{ newer.path, older.path });
        if (y.size < x.size or (y.size == x.size and !std.mem.eql(u8, y.root, x.root)))
            return fail(error.Equivocation, "anchor {s} does not extend {s}", .{ newer.path, older.path });
    };
    return items;
}

fn clearStaging(a: Allocator, anchors: []const u8) Fail!void {
    for (try listdir(a, anchors)) |name| {
        if (!std.mem.startsWith(u8, name, STAGING_PREFIX)) continue;
        rmtree(a, try join(a, anchors, name));
    }
    try fsyncDir(a, anchors);
}

fn anchorPaths(a: Allocator, eb_stack: []const u8, log: []const u8, key: ?[]const u8, public_key: []const u8, anchors: []const u8) Fail!void {
    _ = try executable(a, eb_stack);
    _ = try existingFile(a, log, "ledger", true);
    _ = try existingFile(a, public_key, "public key", false);
    _ = try existingDir(a, anchors, "anchor directory");
    if (key) |k| {
        _ = try existingFile(a, k, "signing key", true);
        try keyOutside(k, &.{ .{ .what = "ledger directory", .path = dirname(log) }, .{ .what = "anchor directory", .path = anchors } });
    }
    if (std.mem.eql(u8, anchors, dirname(log)) or inside(log, anchors)) return refuse("anchor directory {s} must not hold the ledger {s}", .{ anchors, log });
    try distinctFiles(&.{ .{ .what = "ledger", .path = log }, .{ .what = "signing key", .path = key }, .{ .what = "public key", .path = public_key } });
}

fn pin(a: Allocator, private: []const u8, name: []const u8, data: []const u8) Fail![]const u8 {
    const path = try join(a, private, name);
    try writePrivate(a, path, data);
    return path;
}

/// Publish today's signed head as <anchors>/<YYYY-MM-DD>.head.json, once.
pub fn anchor(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, key: []const u8, public_key: []const u8, anchors: []const u8, timeout: f64, faults: Faults, now_s: i64, lock_timeout_ms: u32) Fail!V {
    try anchorPaths(a, eb_stack, log, key, public_key, anchors);
    var lock = state.RootLock.acquire(a, anchors, lock_timeout_ms) catch |err| switch (err) {
        error.Busy => return refuse("another eb-staged-rebuild holds the lock at {s}/.lock", .{anchors}),
        error.NotPrivate => return refuse("record lock requires a private regular file: {s}/.lock.posix", .{anchors}),
        error.OutOfMemory => return error.OutOfMemory,
        else => return osFail(a, err, anchors),
    };
    defer lock.release();
    try clearStaging(a, anchors);
    const ts = try utc(a, now_s);
    const day = ts[0..10];
    const existing = try listAnchors(a, anchors);
    var later: ?Anchor = null;
    for (existing) |item| if (std.mem.order(u8, item.day, day) == .gt) {
        later = item;
    };
    if (later) |l| return refuse("anchor {s} is for a day after {s}; check the clock", .{ l.path, day });
    var today: ?Anchor = null;
    for (existing) |item| if (std.mem.eql(u8, item.day, day)) {
        today = item;
        break;
    };
    const prior: ?Anchor = today orelse (if (existing.len > 0) existing[existing.len - 1] else null);
    const staging = try mkdtemp(a, anchors, STAGING_PREFIX);
    defer {
        rmtree(a, staging);
        fsyncDir(a, anchors) catch {};
    }
    const candidate = try join(a, staging, "head.json");
    try faults.reach("anchor.sign");
    const r = try bounded(a, env, &.{ eb_stack, "ledger", "head", "--log", log, "--key", key, "--out", candidate, "--ts", ts }, timeout);
    if (r.code != 0) return fail(error.Pending, "eb-stack ledger head exited {d}: {s}", .{ r.code, tailChars(pyStripped(r.stderr), 500) });
    const signed_bytes = try readRegular(a, candidate, "signed head");
    const signed = try parseHead(a, signed_bytes, "the signed head");
    const lines = try splitKeep(a, r.stdout);
    const receipt = try std.fmt.allocPrint(a, "signed head size {d} root {s} at {s} to {s}\n", .{ signed.size, signed.root, ts, candidate });
    var good: bool = undefined;
    if (signed.size == 0) {
        // An empty log has no record to derive a checkpoint origin from;
        // eb-stack signs the size-0 head alone.
        good = lines.len == 1 and std.mem.eql(u8, lines[0], receipt);
    } else {
        const cp = if (lines.len == 2 or lines.len == 3) checkpointLine(rstripNl(lines[1])) else null;
        const index_ok = lines.len == 2 or (lines.len == 3 and indexCheckpointLine(rstripNl(lines[2])));
        const want_size = try std.fmt.allocPrint(a, "{d}", .{signed.size});
        good = lines.len >= 1 and std.mem.eql(u8, lines[0], receipt) and cp != null and index_ok and
            std.mem.eql(u8, cp.?.size, want_size) and std.mem.eql(u8, cp.?.root, signed.root);
    }
    if (!good) return fail(error.Pending, "eb-stack ledger head printed an unexpected receipt: {s}", .{repr(a, tailChars(r.stdout, 500))});
    if (!std.mem.eql(u8, signed.ts, ts)) return fail(error.Pending, "eb-stack ledger head signed at {s}, not {s}", .{ signed.ts, ts });
    const snapshot = committedPrefix(try readRegular(a, log, "ledger"));
    const records = try parseLog(a, snapshot, try std.fmt.allocPrint(a, "ledger {s}", .{log}));
    const verifier: Verifier = .{ .a = a, .env = env, .eb_stack = eb_stack, .public_key = public_key, .timeout = timeout };
    const private_log = try pin(a, staging, "ledger.jsonl", snapshot);
    try verifier.tiles(private_log);
    try verifier.binds(private_log, candidate, signed, records);
    if (prior) |p| {
        const prior_path = try pin(a, staging, "prior.json", p.bytes);
        try verifier.extends(private_log, prior_path, p.head, candidate, signed);
    }
    var o: Obj = .{ .a = a };
    if (today) |t| {
        try o.put("status", S("exists"));
        try o.put("day", S(day));
        try o.put("path", S(t.path));
        try o.put("size", I(t.head.size));
        try o.put("root", S(t.head.root));
        return o.v();
    }
    const target = try std.fmt.allocPrint(a, "{s}/{s}.head.json", .{ anchors, day });
    {
        const fd = sys.openat(fsx.cwd, candidate, .{ .NOFOLLOW = true }, 0) catch |err| return osFail(a, err, candidate);
        defer sys.close(fd);
        sys.fchmod(fd, 0o444) catch |err| return osFail(a, err, candidate);
        sys.fsync(fd) catch |err| return osFail(a, err, candidate);
    }
    try faults.reach("anchor.publish");
    {
        var zc: [sys.path_max:0]u8 = undefined;
        var zt: [sys.path_max:0]u8 = undefined;
        const c = sys.z(&zc, candidate) catch |err| return osFail(a, err, candidate);
        const t = sys.z(&zt, target) catch |err| return osFail(a, err, target);
        _ = sys.call(linux.linkat, .{ fsx.cwd, c.ptr, fsx.cwd, t.ptr, @as(u32, 0) }) catch |err| switch (err) {
            error.Exists => return refuse("anchor {s} appeared while this one was prepared; rerun to verify it", .{target}),
            else => return osFail(a, err, target),
        };
    }
    try fsyncDir(a, anchors);
    try faults.reach("anchor.published");
    try o.put("status", S("published"));
    try o.put("day", S(day));
    try o.put("path", S(target));
    try o.put("size", I(signed.size));
    try o.put("root", S(signed.root));
    return o.v();
}

/// Prove every anchor extends the one before it and the latest binds to the log.
pub fn verifyAnchors(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, public_key: []const u8, anchors: []const u8, timeout: f64) Fail!V {
    try anchorPaths(a, eb_stack, log, null, public_key, anchors);
    const found = try listAnchors(a, anchors);
    var o: Obj = .{ .a = a };
    if (found.len == 0) {
        try o.put("anchors", I(0));
        return o.v();
    }
    const snapshot = committedPrefix(try readRegular(a, log, "ledger"));
    const records = try parseLog(a, snapshot, try std.fmt.allocPrint(a, "ledger {s}", .{log}));
    const verifier: Verifier = .{ .a = a, .env = env, .eb_stack = eb_stack, .public_key = public_key, .timeout = timeout };
    const private = try mkdtemp(a, tempRoot(a, env), "eb-ledger-anchors-");
    defer rmtree(a, private);
    const private_log = try pin(a, private, "ledger.jsonl", snapshot);
    try verifier.tiles(private_log);
    const pinned = try a.alloc([]const u8, found.len);
    for (found, 0..) |item, n| pinned[n] = try pin(a, private, try std.fmt.allocPrint(a, "{d:0>6}.json", .{n}), item.bytes);
    var n: usize = 1;
    while (n < found.len) : (n += 1) try verifier.extends(private_log, pinned[n - 1], found[n - 1].head, pinned[n], found[n].head);
    try verifier.binds(private_log, pinned[found.len - 1], found[found.len - 1].head, records);
    const latest = found[found.len - 1];
    try o.put("anchors", I(found.len));
    try o.put("latest", S(latest.path));
    try o.put("size", I(latest.head.size));
    try o.put("root", S(latest.head.root));
    return o.v();
}

// ---- package history projections ---------------------------------------------------

pub fn packageOf(module: []const u8) []const u8 {
    const i = std.mem.indexOfScalar(u8, module, '/') orelse return module;
    return module[0..i];
}

const Problem = struct { index: usize, field: []const u8, target: []const u8, problem: []const u8 };

const Effective = struct {
    voided: std.AutoHashMapUnmanaged(usize, void),
    corrected_by: std.AutoHashMapUnmanaged(usize, std.ArrayList(usize)),
    pending_by: std.AutoHashMapUnmanaged(usize, std.ArrayList(usize)),
    problems: []const Problem,
};

/// Which records a later applied correction voided, and why.
fn effective(a: Allocator, records: []const Record) Allocator.Error!Effective {
    var by_id: std.StringHashMapUnmanaged(usize) = .empty;
    for (records) |r| try by_id.put(a, r.id, r.index);
    var e: Effective = .{ .voided = .empty, .corrected_by = .empty, .pending_by = .empty, .problems = &.{} };
    var problems: std.ArrayList(Problem) = .empty;
    for (records) |r| for (LINKS) |field| if (r.v.get(field)) |t| if (by_id.get(t.str) == null)
        try problems.append(a, .{ .index = r.index, .field = field, .target = t.str, .problem = "names no record in this log" });
    var i = records.len;
    while (i > 0) {
        i -= 1;
        const r = records[i];
        if (!std.mem.eql(u8, r.action, "correction")) continue;
        const corrects = r.v.get("corrects").?.str;
        const target = by_id.get(corrects) orelse continue;
        if (target >= r.index) {
            try problems.append(a, .{ .index = r.index, .field = "corrects", .target = corrects, .problem = "names a record that is not earlier" });
            continue;
        }
        const stage = V.str_(r.v.get("stage"));
        if (e.voided.contains(r.index) or (stage != null and std.mem.eql(u8, stage.?, "aborted"))) continue;
        if (stage != null and std.mem.eql(u8, stage.?, "started")) {
            const g = try e.pending_by.getOrPut(a, target);
            if (!g.found_existing) g.value_ptr.* = .empty;
            try g.value_ptr.append(a, r.index);
            continue;
        }
        try e.voided.put(a, target, {});
        const g = try e.corrected_by.getOrPut(a, target);
        if (!g.found_existing) g.value_ptr.* = .empty;
        try g.value_ptr.append(a, r.index);
    }
    e.problems = problems.items;
    return e;
}

fn stateOf(action: []const u8) ?[]const u8 {
    const map = [_][2][]const u8{
        .{ "install", "installed" },  .{ "rebuild-inplace", "installed" }, .{ "rebuild-bump", "installed" },
        .{ "unretire", "installed" }, .{ "retire", "retired" },            .{ "remove", "removed" },
        .{ "move", "moved" },
    };
    for (map) |m| if (std.mem.eql(u8, m[0], action)) return m[1];
    return null;
}

fn intList(a: Allocator, xs: ?std.ArrayList(usize)) Allocator.Error!V {
    const l = xs orelse return .{ .arr = &.{} };
    const out = try a.alloc(V, l.items.len);
    for (l.items, 0..) |x, i| out[i] = I(x);
    return .{ .arr = out };
}

fn problemV(a: Allocator, p: Problem) Allocator.Error!V {
    var o: Obj = .{ .a = a };
    try o.put("index", I(p.index));
    try o.put("field", S(p.field));
    try o.put("target", S(p.target));
    try o.put("problem", S(p.problem));
    return o.v();
}

const ModState = struct {
    module: []const u8,
    state: []const u8 = "unknown",
    location: ?[]const u8 = null,
    last_change: ?usize = null,
    access: std.ArrayList(usize) = .empty,
    findings: std.ArrayList(usize) = .empty,
};

/// The deterministic history model of a verified log, grouped by package.
pub fn buildHistory(a: Allocator, verified: VerifiedLog) Allocator.Error!V {
    const records = verified.records;
    const eff = try effective(a, records);
    var by_id: std.StringHashMapUnmanaged(usize) = .empty;
    for (records) |r| try by_id.put(a, r.id, r.index);
    var packages: std.StringArrayHashMapUnmanaged(std.ArrayList(V)) = .empty;
    for (records) |r| {
        const index = r.index;
        const build: V = if (r.v.get("build")) |b| (if (b == .obj) b else .{ .obj = &.{} }) else .{ .obj = &.{} };
        const kind = V.str_(build.get("kind"));
        const observed = kind != null and std.mem.eql(u8, kind.?, BACKFILL_KIND) and build.get("kind").? == .str;
        var links: Obj = .{ .a = a };
        for (LINKS) |field| if (r.v.get(field)) |t| {
            var l: Obj = .{ .a = a };
            try l.put("id", t);
            try l.put("index", if (by_id.get(t.str)) |x| I(x) else .null);
            try links.put(field, l.v());
        };
        var ev: Obj = .{ .a = a };
        try ev.put("index", I(index));
        try ev.put("id", S(r.id));
        try ev.put("ts", r.v.get("ts").?);
        try ev.put("action", S(r.action));
        try ev.put("stage", r.v.get("stage") orelse .null);
        try ev.put("actor", r.v.get("actor").?);
        try ev.put("target", r.v.get("target").?);
        try ev.put("module", S(r.module));
        try ev.put("location", r.v.get("location").?);
        try ev.put("event_id", build.get("event_id") orelse .null);
        try ev.put("reason", r.v.get("reason") orelse .null);
        try ev.put("modulefile", r.v.get("modulefile") orelse .null);
        try ev.put("links", links.v());
        try ev.put("observed", .{ .bool = observed });
        try ev.put("provenance", if (observed) (build.get("provenance") orelse .null) else .null);
        try ev.put("manifest_id", if (observed) (build.get("manifest_id") orelse .null) else .null);
        try ev.put("status", S(if (eff.voided.contains(index)) "corrected" else "effective"));
        try ev.put("corrected_by", try intList(a, eff.corrected_by.get(index)));
        try ev.put("correction_pending_by", try intList(a, eff.pending_by.get(index)));
        const g = try packages.getOrPut(a, packageOf(r.module));
        if (!g.found_existing) g.value_ptr.* = .empty;
        try g.value_ptr.append(a, ev.v());
    }
    const names = try a.dupe([]const u8, packages.keys());
    std.mem.sort([]const u8, names, {}, struct {
        fn lt(_: void, x: []const u8, y: []const u8) bool {
            return std.mem.order(u8, x, y) == .lt;
        }
    }.lt);
    var model: std.ArrayList(V) = .empty;
    for (names) |name| {
        const events = packages.get(name).?.items;
        var modules: std.StringArrayHashMapUnmanaged(ModState) = .empty;
        for (events) |event| {
            const module = event.get("module").?.str;
            const g = try modules.getOrPut(a, module);
            if (!g.found_existing) g.value_ptr.* = .{ .module = module };
            const st = g.value_ptr;
            const stage = event.get("stage").?;
            if (!std.mem.eql(u8, event.get("status").?.str, "effective") or !(stage == .null or std.mem.eql(u8, stage.str, "finished"))) continue;
            const action = event.get("action").?.str;
            const idx: usize = @intCast(event.get("index").?.int);
            if (std.mem.eql(u8, action, "access")) {
                try st.access.append(a, idx);
            } else if (std.mem.eql(u8, action, "finding")) {
                try st.findings.append(a, idx);
                if (std.mem.eql(u8, st.state, "unknown")) {
                    st.state = "observed";
                    st.location = event.get("location").?.str;
                    st.last_change = idx;
                }
            } else if (stateOf(action)) |s| {
                st.state = s;
                st.location = event.get("location").?.str;
                st.last_change = idx;
            }
        }
        const mnames = try a.dupe([]const u8, modules.keys());
        std.mem.sort([]const u8, mnames, {}, struct {
            fn lt(_: void, x: []const u8, y: []const u8) bool {
                return std.mem.order(u8, x, y) == .lt;
            }
        }.lt);
        var mods: std.ArrayList(V) = .empty;
        for (mnames) |mn| {
            const st = modules.get(mn).?;
            var mo: Obj = .{ .a = a };
            try mo.put("module", S(st.module));
            try mo.put("state", S(st.state));
            try mo.put("location", optS(st.location));
            try mo.put("last_change", if (st.last_change) |x| I(x) else .null);
            try mo.put("access", try intList(a, st.access));
            try mo.put("findings", try intList(a, st.findings));
            try mods.append(a, mo.v());
        }
        var pp: std.ArrayList(V) = .empty;
        for (eff.problems) |p| if (std.mem.eql(u8, packageOf(records[p.index].module), name)) try pp.append(a, try problemV(a, p));
        var pkg: Obj = .{ .a = a };
        try pkg.put("name", S(name));
        try pkg.put("modules", .{ .arr = mods.items });
        try pkg.put("events", .{ .arr = events });
        try pkg.put("problems", .{ .arr = pp.items });
        try model.append(a, pkg.v());
    }
    var all_problems: std.ArrayList(V) = .empty;
    for (eff.problems) |p| try all_problems.append(a, try problemV(a, p));
    var head: Obj = .{ .a = a };
    try head.put("size", I(verified.head.size));
    try head.put("root", S(verified.head.root));
    try head.put("ts", S(verified.head.ts));
    try head.put("public_key", S(verified.head.public_key));
    var o: Obj = .{ .a = a };
    try o.put("schema", S(HISTORY_SCHEMA));
    try o.put("head", head.v());
    try o.put("stack", if (records.len > 0) S(records[0].stack) else .null);
    try o.put("packages", .{ .arr = model.items });
    try o.put("problems", .{ .arr = all_problems.items });
    return o.v();
}

/// urllib.parse.quote(s, safe=SAFE): UTF-8 bytes, unreserved and SAFE kept.
fn quote(a: Allocator, s: []const u8, safe: []const u8) Allocator.Error![]u8 {
    var out: std.ArrayList(u8) = .empty;
    for (s) |c| {
        if (std.ascii.isAlphanumeric(c) or c == '_' or c == '.' or c == '-' or c == '~' or std.mem.indexOfScalar(u8, safe, c) != null) {
            try out.append(a, c);
        } else try out.print(a, "%{X:0>2}", .{c});
    }
    return out.items;
}

/// A file name for a package that cannot leave the output directory.
pub fn fileStem(a: Allocator, name: []const u8) Allocator.Error![]const u8 {
    const stem = try quote(a, name, "");
    if (stem.len == 0 or std.mem.eql(u8, stem, ".") or std.mem.eql(u8, stem, "..") or stem.len > 150) {
        const h = try sha256Hex(a, name);
        return std.mem.concat(a, u8, &.{ stem[0..@min(stem.len, 100)], "-", h[0..16] });
    }
    return stem;
}

/// Text safe in a Markdown table cell: no markup, HTML, line breaks or bidi
/// controls.
pub fn md(a: Allocator, value: ?V) Allocator.Error![]const u8 {
    const v = value orelse return "";
    const s = switch (v) {
        .null => return "",
        .str => |x| x,
        else => try canonical(a, v),
    };
    var out: std.ArrayList(u8) = .empty;
    var it = std.unicode.Wtf8Iterator{ .bytes = s, .i = 0 };
    while (true) {
        const start = it.i;
        const cp = it.nextCodepoint() orelse break;
        switch (cp) {
            '&' => try out.appendSlice(a, "&amp;"),
            '<' => try out.appendSlice(a, "&lt;"),
            '>' => try out.appendSlice(a, "&gt;"),
            '\\', '`', '*', '_', '[', ']', '(', ')', '|', '~', '!', '#' => {
                try out.append(a, '\\');
                try out.append(a, @intCast(cp));
            },
            else => if (isControl(cp)) try out.print(a, "U+{X:0>4}", .{cp}) else try out.appendSlice(a, s[start..it.i]),
        }
    }
    return out.items;
}

fn mdStr(a: Allocator, s: []const u8) Allocator.Error![]const u8 {
    return md(a, S(s));
}

fn details(a: Allocator, event: V) Allocator.Error![]const u8 {
    var parts: std.ArrayList([]const u8) = .empty;
    const links = event.get("links").?.obj;
    const sorted = try a.dupe(Mem, links);
    std.mem.sort(Mem, sorted, {}, lessKey);
    for (sorted) |m| {
        const idx = m.val.get("index").?;
        const where = if (idx != .null) try std.fmt.allocPrint(a, "#{d}", .{idx.int}) else try std.fmt.allocPrint(a, "unknown record {s}", .{m.val.get("id").?.str});
        try parts.append(a, try std.fmt.allocPrint(a, "{s} {s}", .{ m.key, where }));
    }
    for ([_]struct { key: []const u8, label: []const u8 }{ .{ .key = "corrected_by", .label = "corrected by " }, .{ .key = "correction_pending_by", .label = "correction started by " } }) |x| {
        const l = event.get(x.key).?.arr;
        if (l.len == 0) continue;
        var refs: std.ArrayList([]const u8) = .empty;
        for (l) |n| try refs.append(a, try std.fmt.allocPrint(a, "#{d}", .{n.int}));
        try parts.append(a, try std.mem.concat(a, u8, &.{ x.label, try std.mem.join(a, ", ", refs.items) }));
    }
    if (event.get("observed").?.bool) {
        var refs: std.ArrayList([]const u8) = .empty;
        for (event.get("provenance").?.arr) |ref| {
            const r = ref.get("ref").?.str;
            try refs.append(a, if (ref.get("sha256")) |h| try std.fmt.allocPrint(a, "{s} sha256 {s}", .{ r, h.str }) else r);
        }
        try parts.append(a, try std.fmt.allocPrint(a, "observed by manifest {s}; provenance {s}", .{ event.get("manifest_id").?.str, try std.mem.join(a, "; ", refs.items) }));
    }
    const reason = event.get("reason").?;
    if (reason != .null) try parts.append(a, try std.fmt.allocPrint(a, "reason {s}", .{if (reason == .str) reason.str else try canonical(a, reason)}));
    return mdStr(a, try std.mem.join(a, "; ", parts.items));
}

fn headLines(a: Allocator, model: V, lines: *std.ArrayList([]const u8)) Allocator.Error!void {
    const head = model.get("head").?;
    try lines.append(a, try std.fmt.allocPrint(a, "Stack: {s}", .{try md(a, model.get("stack").?)}));
    try lines.append(a, "");
    try lines.append(a, try std.fmt.allocPrint(a, "Verified head: size {d}, root {s}, signed {s} by {s}.", .{
        head.get("size").?.int, head.get("root").?.str, head.get("ts").?.str, head.get("public_key").?.str,
    }));
    try lines.append(a, "");
}

fn renderPackage(a: Allocator, model: V, package: V) Allocator.Error![]const u8 {
    var lines: std.ArrayList([]const u8) = .empty;
    try lines.append(a, try std.fmt.allocPrint(a, "# Package history: {s}", .{try md(a, package.get("name").?)}));
    try lines.append(a, "");
    try headLines(a, model, &lines);
    try lines.appendSlice(a, &.{ "## Modules", "", "| Module | State | Last change | Location |", "| --- | --- | --- | --- |" });
    for (package.get("modules").?.arr) |m| {
        const lc = m.get("last_change").?;
        const change = if (lc != .null) try std.fmt.allocPrint(a, "#{d}", .{lc.int}) else "";
        try lines.append(a, try std.fmt.allocPrint(a, "| {s} | {s} | {s} | {s} |", .{ try md(a, m.get("module").?), m.get("state").?.str, change, try md(a, m.get("location").?) }));
    }
    try lines.appendSlice(a, &.{ "", "## Events", "", "| # | Time (UTC) | Action | Stage | Module | Actor | Status | Details |", "| --- | --- | --- | --- | --- | --- | --- | --- |" });
    for (package.get("events").?.arr) |e| {
        const stage = e.get("stage").?;
        try lines.append(a, try std.fmt.allocPrint(a, "| {d} | {s} | {s} | {s} | {s} | {s} | {s} | {s} |", .{
            e.get("index").?.int,         try md(a, e.get("ts").?),    e.get("action").?.str, if (stage == .null) "" else stage.str,
            try md(a, e.get("module").?), try md(a, e.get("actor").?), e.get("status").?.str, try details(a, e),
        }));
    }
    const problems = package.get("problems").?.arr;
    if (problems.len > 0) {
        try lines.appendSlice(a, &.{ "", "## Problems", "" });
        for (problems) |p| try lines.append(a, try std.fmt.allocPrint(a, "* record #{d} {s} {s}: {s}", .{
            p.get("index").?.int, p.get("field").?.str, try md(a, p.get("target").?), p.get("problem").?.str,
        }));
    }
    return std.mem.concat(a, u8, &.{ try std.mem.join(a, "\n", lines.items), "\n" });
}

const File = struct { rel: []const u8, data: []const u8 };

/// Every output file, by path relative to the output directory.
fn render(a: Allocator, model: V) Fail![]const File {
    var files: std.ArrayList(File) = .empty;
    var index: std.ArrayList(V) = .empty;
    var stems: std.StringHashMapUnmanaged(void) = .empty;
    for (model.get("packages").?.arr) |package| {
        const name = package.get("name").?.str;
        const stem = try fileStem(a, name);
        if (stems.contains(stem)) return refuse("two packages map to the file name {s}", .{stem});
        try stems.put(a, stem, {});
        var doc: Obj = .{ .a = a };
        try doc.put("schema", model.get("schema").?);
        try doc.put("head", model.get("head").?);
        try doc.put("stack", model.get("stack").?);
        try doc.put("package", S(name));
        try doc.put("modules", package.get("modules").?);
        try doc.put("events", package.get("events").?);
        try doc.put("problems", package.get("problems").?);
        const jrel = try std.fmt.allocPrint(a, "packages/{s}.json", .{stem});
        const mrel = try std.fmt.allocPrint(a, "packages/{s}.md", .{stem});
        try files.append(a, .{ .rel = jrel, .data = try jsonBytes(a, doc.v()) });
        try files.append(a, .{ .rel = mrel, .data = try renderPackage(a, model, package) });
        var entry: Obj = .{ .a = a };
        try entry.put("name", S(name));
        try entry.put("json", S(jrel));
        try entry.put("markdown", S(mrel));
        try entry.put("modules", I(package.get("modules").?.arr.len));
        try entry.put("events", I(package.get("events").?.arr.len));
        try index.append(a, entry.v());
    }
    var idx: Obj = .{ .a = a };
    try idx.put("schema", model.get("schema").?);
    try idx.put("head", model.get("head").?);
    try idx.put("stack", model.get("stack").?);
    try idx.put("packages", .{ .arr = index.items });
    try idx.put("problems", model.get("problems").?);
    try files.append(a, .{ .rel = "index.json", .data = try jsonBytes(a, idx.v()) });
    var lines: std.ArrayList([]const u8) = .empty;
    try lines.appendSlice(a, &.{ "# Ledger package history", "" });
    try headLines(a, model, &lines);
    try lines.appendSlice(a, &.{ "| Package | Modules | Events |", "| --- | --- | --- |" });
    for (index.items) |entry| {
        const link = try quote(a, entry.get("markdown").?.str, "/");
        try lines.append(a, try std.fmt.allocPrint(a, "| [{s}]({s}) | {d} | {d} |", .{ try md(a, entry.get("name").?), link, entry.get("modules").?.int, entry.get("events").?.int }));
    }
    try files.append(a, .{ .rel = "index.md", .data = try std.mem.concat(a, u8, &.{ try std.mem.join(a, "\n", lines.items), "\n" }) });
    return files.items;
}

fn endsWithAny(name: []const u8, suffixes: []const []const u8) bool {
    for (suffixes) |s| if (std.mem.endsWith(u8, name, s)) return true;
    return false;
}

fn outputDir(a: Allocator, out: []const u8, log: []const u8, head: []const u8, public_key: []const u8) Fail!void {
    _ = try plainPath(a, out, "output directory");
    for ([_]Named{ .{ .what = "ledger", .path = log }, .{ .what = "head", .path = head }, .{ .what = "public key", .path = public_key } }) |n| {
        const p = n.path.?;
        if (inside(p, out) or std.mem.eql(u8, out, dirname(p))) return refuse("output directory {s} must not hold the {s} {s}", .{ out, n.what, p });
    }
    if (!lexists(out)) return;
    _ = try existingDir(a, out, "output directory");
    const names = try listdir(a, out);
    var foreign: std.ArrayList([]const u8) = .empty;
    const allowed = [_][]const u8{ "index.json", "index.md", ".index.json.new", ".index.md.new", "packages" };
    var has_packages = false;
    var has_index = false;
    for (names) |n| {
        if (!contains(&allowed, n)) try foreign.append(a, n);
        if (std.mem.eql(u8, n, "packages")) has_packages = true;
        if (std.mem.eql(u8, n, "index.json")) has_index = true;
    }
    if (has_packages) {
        const packages = try join(a, out, "packages");
        if (!isDirNoFollow(packages)) return refuse("{s} must be a directory", .{packages});
        for (try listdir(a, packages)) |n| if (!endsWithAny(n, &.{ ".json", ".md", ".json.new", ".md.new" })) {
            var dup = false;
            for (foreign.items) |f| if (std.mem.eql(u8, f, n)) {
                dup = true;
            };
            if (!dup) try foreign.append(a, n);
        };
    }
    if (foreign.items.len > 0) {
        std.mem.sort([]const u8, foreign.items, {}, struct {
            fn lt(_: void, x: []const u8, y: []const u8) bool {
                return std.mem.order(u8, x, y) == .lt;
            }
        }.lt);
        return refuse("output directory {s} holds files that are not a ledger history projection: {s}", .{ out, try std.mem.join(a, ", ", foreign.items) });
    }
    if (has_index) {
        const ipath = try join(a, out, "index.json");
        const data = try readRegular(a, ipath, "projection index");
        const ok = blk: {
            if (!std.unicode.utf8ValidateSlice(data)) break :blk false;
            const v = strictJson(a, data, "projection index") catch break :blk false;
            const s = V.str_(v.get("schema")) orelse break :blk false;
            break :blk std.mem.eql(u8, s, HISTORY_SCHEMA);
        };
        if (!ok) return refuse("output directory {s} holds an index that is not a ledger history projection", .{out});
    }
}

fn reachHook(ctx: ?*anyopaque, step: []const u8) void {
    const f: *const Faults = @ptrCast(@alignCast(ctx.?));
    f.reach(step) catch {};
}

fn writeAtomic(a: Allocator, path: []const u8, data: []const u8, step: []const u8, faults: Faults) Fail!void {
    var w: atomic.Why = .{};
    const hook: atomic.Faults = .{ .ctx = @ptrCast(@constCast(&faults)), .reach_fn = reachHook };
    atomic.atomicWrite(a, path, data, .{ .step = step, .faults = hook }, &w) catch |err| switch (err) {
        error.Refused => return refuse("{s}", .{w.msg}),
        error.OutOfMemory => return error.OutOfMemory,
        else => return osFail(a, err, path),
    };
}

/// Write JSON and Markdown history of every package in a verified log.
pub fn project(a: Allocator, env: Env, eb_stack: []const u8, log: []const u8, head: []const u8, public_key: []const u8, out: []const u8, timeout: f64, faults: Faults) Fail!V {
    try outputDir(a, out, log, head, public_key);
    const verified = try readVerified(a, env, eb_stack, log, head, public_key, timeout);
    const files = try render(a, try buildHistory(a, verified));
    const packages = try join(a, out, "packages");
    atomic.makedirs(packages) catch |err| return osFail(a, err, packages);
    if (!isDirNoFollow(packages)) return refuse("{s} must be a directory", .{packages});
    const sorted = try a.dupe(File, files);
    std.mem.sort(File, sorted, {}, struct {
        fn lt(_: void, x: File, y: File) bool {
            return std.mem.order(u8, x.rel, y.rel) == .lt;
        }
    }.lt);
    var keep: std.StringHashMapUnmanaged(void) = .empty;
    for (sorted) |f| if (std.mem.startsWith(u8, f.rel, "packages/")) {
        try writeAtomic(a, try join(a, out, f.rel), f.data, try std.fmt.allocPrint(a, "projection.{s}", .{f.rel}), faults);
        try keep.put(a, f.rel["packages/".len..], {});
    };
    for (try listdir(a, packages)) |name| {
        if (keep.contains(name) or !endsWithAny(name, &.{ ".json", ".md" })) continue;
        const p = try join(a, packages, name);
        sys.unlinkat(fsx.cwd, p, 0) catch |err| return osFail(a, err, p);
    }
    try fsyncDir(a, packages);
    for ([_][]const u8{ "index.md", "index.json" }) |rel| {
        for (files) |f| if (std.mem.eql(u8, f.rel, rel)) try writeAtomic(a, try join(a, out, rel), f.data, try std.fmt.allocPrint(a, "projection.{s}", .{rel}), faults);
    }
    var names: std.ArrayList(V) = .empty;
    for (sorted) |f| try names.append(a, S(f.rel));
    var o: Obj = .{ .a = a };
    try o.put("out", S(out));
    try o.put("size", I(verified.head.size));
    try o.put("root", S(verified.head.root));
    try o.put("packages", I(keep.count() / 2));
    try o.put("files", .{ .arr = names.items });
    return o.v();
}

// ---- command line ---------------------------------------------------------------

const usage =
    \\usage: eb-staged-rebuild ledger-maintenance [-h]
    \\                                            {backfill,anchor,verify-anchors,project} ...
    \\
;

const help = usage ++
    \\
    \\Maintain and read an eb-stack ledger.
    \\
    \\positional arguments:
    \\  {backfill,anchor,verify-anchors,project}
    \\    backfill            append observed history from a pinned manifest
    \\    anchor              publish today's immutable signed anchor
    \\    verify-anchors      prove the anchor chain against the log
    \\    project             write package history from a verified log
    \\
    \\options:
    \\  -h, --help            show this help message and exit
    \\
;

const Opt = struct { name: []const u8, value: ?[]const u8 = null, required: bool = true };

fn usageError(comptime f: []const u8, args: anytype) noreturn {
    cli.put(2, usage);
    cli.err("eb-staged-rebuild ledger-maintenance: error: " ++ f ++ "\n", args);
    std.process.exit(2);
}

/// --name VALUE, --name=VALUE, or a unique prefix of a name, as argparse.
fn parseOpts(a: Allocator, cmd: []const u8, args: []const []const u8, opts: []Opt) void {
    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            cli.out("usage: eb-staged-rebuild ledger-maintenance {s} [-h] --OPTION VALUE ...\n", .{cmd});
            for (opts) |o| cli.out("  --{s} {s}\n", .{ o.name, if (o.required) "(required)" else "" });
            std.process.exit(0);
        }
        if (!std.mem.startsWith(u8, arg, "--")) usageError("unrecognized arguments: {s}", .{arg});
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
            if (name.len > 0 and std.mem.startsWith(u8, o.name, name)) {
                match = o;
                count += 1;
            }
        }
        if (count != 1) usageError("unrecognized arguments: {s}", .{arg});
        const o = match.?;
        if (eq) |e| {
            o.value = arg[e + 1 ..];
        } else {
            if (i + 1 >= args.len) usageError("argument --{s}: expected one argument", .{o.name});
            i += 1;
            o.value = args[i];
        }
    }
    var missing: std.ArrayList([]const u8) = .empty;
    for (opts) |o| if (o.required and o.value == null) missing.append(a, std.fmt.allocPrint(a, "--{s}", .{o.name}) catch o.name) catch oom();
    if (missing.items.len > 0) usageError("the following arguments are required: {s}", .{std.mem.join(a, ", ", missing.items) catch ""});
}

fn val(opts: []const Opt, name: []const u8) ?[]const u8 {
    for (opts) |o| if (std.mem.eql(u8, o.name, name)) return o.value;
    return null;
}

pub fn main(a: Allocator, args: []const []const u8, env: Env) u8 {
    why_alloc = a;
    if (args.len == 0) usageError("the following arguments are required: command", .{});
    if (std.mem.eql(u8, args[0], "-h") or std.mem.eql(u8, args[0], "--help")) {
        cli.put(1, help);
        return 0;
    }
    const cmd = args[0];
    const choices = [_][]const u8{ "backfill", "anchor", "verify-anchors", "project" };
    if (!contains(&choices, cmd)) usageError("argument command: invalid choice: '{s}' (choose from 'backfill', 'anchor', 'verify-anchors', 'project')", .{cmd});
    var opts: std.ArrayList(Opt) = .empty;
    opts.appendSlice(a, &.{ .{ .name = "eb-stack" }, .{ .name = "log" }, .{ .name = "public-key" } }) catch return 1;
    const with_key = std.mem.eql(u8, cmd, "backfill") or std.mem.eql(u8, cmd, "anchor");
    const with_head = std.mem.eql(u8, cmd, "backfill") or std.mem.eql(u8, cmd, "project");
    if (with_key) opts.append(a, .{ .name = "key" }) catch return 1;
    if (with_head) opts.append(a, .{ .name = "head" }) catch return 1;
    opts.append(a, .{ .name = "timeout", .required = false }) catch return 1;
    if (std.mem.eql(u8, cmd, "backfill")) opts.appendSlice(a, &.{ .{ .name = "manifest" }, .{ .name = "manifest-sha256" } }) catch return 1;
    if (std.mem.eql(u8, cmd, "anchor") or std.mem.eql(u8, cmd, "verify-anchors")) opts.append(a, .{ .name = "anchors" }) catch return 1;
    if (std.mem.eql(u8, cmd, "project")) opts.append(a, .{ .name = "out" }) catch return 1;
    parseOpts(a, cmd, args[1..], opts.items);
    const o = opts.items;
    const timeout: f64 = if (val(o, "timeout")) |t| std.fmt.parseFloat(f64, t) catch usageError("argument --timeout: invalid float value: '{s}'", .{t}) else DEFAULT_TIMEOUT;
    if (!(timeout > 0) or std.math.isNan(timeout) or std.math.isInf(timeout)) {
        cli.err(prog ++ ": --timeout must be positive\n", .{});
        return 2;
    }
    const faults: Faults = .{ .env = env };
    const eb = val(o, "eb-stack").?;
    const log = val(o, "log").?;
    const pub_key = val(o, "public-key").?;
    const result: Fail!V = if (std.mem.eql(u8, cmd, "backfill"))
        backfill(a, env, eb, log, val(o, "key").?, val(o, "head").?, pub_key, val(o, "manifest").?, val(o, "manifest-sha256").?, timeout, faults, now())
    else if (std.mem.eql(u8, cmd, "anchor"))
        anchor(a, env, eb, log, val(o, "key").?, pub_key, val(o, "anchors").?, timeout, faults, now(), 60_000)
    else if (std.mem.eql(u8, cmd, "verify-anchors"))
        verifyAnchors(a, env, eb, log, pub_key, val(o, "anchors").?, timeout)
    else
        project(a, env, eb, log, val(o, "head").?, pub_key, val(o, "out").?, timeout, faults);
    const v = result catch |err| switch (err) {
        error.Pending => {
            cli.err(prog ++ ": {s}; nothing was acknowledged, retry the same command\n", .{why.items});
            return PENDING_EXIT;
        },
        error.OutOfMemory => {
            cli.err(prog ++ ": out of memory\n", .{});
            return 2;
        },
        else => {
            cli.err(prog ++ ": {s}\n", .{why.items});
            return 2;
        },
    };
    const text = dump(a, v, .{ .sort = true, .ascii = true, .compact = false }) catch return 2;
    cli.put(1, text);
    cli.put(1, "\n");
    return 0;
}

// ---- tests ------------------------------------------------------------------------

const testing = std.testing;
const r_164 = rep("1", 64);
const r_264 = rep("2", 64);
const r_A64 = rep("A", 64);
const r_a64 = rep("a", 64);
const r_b64 = rep("b", 64);
const r_c128 = rep("c", 128);
const r_f64 = rep("f", 64);
const r_x400 = rep("x", 400);

/// `s` repeated `n` times, at compile time.
fn rep(comptime s: []const u8, comptime n: usize) [s.len * n]u8 {
    var out: [s.len * n]u8 = undefined;
    for (0..n) |i| @memcpy(out[i * s.len ..][0..s.len], s);
    return out;
}

test "md escapes markup, HTML and controls" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    try testing.expectEqualStrings("a\\|b", try md(a, S("a|b")));
    try testing.expectEqualStrings("&lt;i&gt;&amp;", try md(a, S("<i>&")));
    try testing.expectEqualStrings("xU+000AyU+000DU+2028U+202EU+001B", try md(a, S("x\ny\r\u{2028}\u{202e}\x1b")));
    try testing.expectEqualStrings("\\`\\*\\_\\[\\]\\(\\)\\~\\!\\#\\\\", try md(a, S("`*_[]()~!#\\")));
    const obj: V = .{ .obj = &.{ .{ .key = "b", .val = I(1) }, .{ .key = "a", .val = S("<") } } };
    try testing.expectEqualStrings("{\"a\":\"&lt;\",\"b\":1}", try md(a, obj));
    try testing.expectEqualStrings("", try md(a, null));
}

test "file stems stay in one directory" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const long = r_x400;
    for ([_][]const u8{ "..", ".", "", "a/b", &long, "a\x00b" }) |name| {
        const stem = try fileStem(a, name);
        try testing.expect(std.mem.indexOfScalar(u8, stem, '/') == null);
        try testing.expect(!std.mem.eql(u8, stem, "") and !std.mem.eql(u8, stem, ".") and !std.mem.eql(u8, stem, ".."));
        try testing.expect(stem.len <= 150);
    }
    try testing.expect(!std.mem.eql(u8, try fileStem(a, &long), try fileStem(a, long ++ "x")));
    try testing.expectEqualStrings("PETSc", try fileStem(a, "PETSc"));
    try testing.expectEqualStrings("evil%7Cpkg", try fileStem(a, "evil|pkg"));
}

test "heads must be exactly what eb-stack writes" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const good = "{\"domain\":\"eb-stack-ledger-v1\",\"size\":1,\"root\":\"" ++ &r_a64 ++ "\",\"ts\":\"2026-11-03T12:00:00Z\",\"public_key\":\"" ++ &r_b64 ++ "\",\"signature\":\"" ++ &r_c128 ++ "\"}\n";
    const h = try parseHead(a, good, "head");
    try testing.expectEqual(@as(i64, 1), h.size);
    const bad = [_][]const u8{
        good[0 .. good.len - 1],
        try std.mem.replaceOwned(u8, a, good, "\"size\":1", "\"size\": 1"),
        try std.mem.replaceOwned(u8, a, good, "\"size\":1", "\"size\":true"),
        try std.mem.replaceOwned(u8, a, good, "\"size\":1", "\"size\":-1"),
        try std.mem.replaceOwned(u8, a, good, &r_a64, &r_A64),
        try std.mem.replaceOwned(u8, a, good, "12:00:00Z", "12:00:00+00:00"),
        try std.mem.replaceOwned(u8, a, good, "{\"domain\"", "{\"size\":1,\"domain\""),
        try std.mem.replaceOwned(u8, a, good, "\"size\":1", "\"size\":1.0"),
        try std.mem.replaceOwned(u8, a, good, "\"size\":1", "\"size\":1,\"size\":1"),
    };
    for (bad) |case| try testing.expectError(error.Refused, parseHead(a, case, "head"));
}

test "log lines must be canonical ledger records" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const line = "{\"action\":\"install\",\"actor\":\"a\",\"id\":\"" ++ &r_f64 ++ "\",\"index\":0,\"location\":\"/x\",\"module\":\"m/1\",\"stack\":\"s\",\"target\":\"t\",\"ts\":\"2026-11-03T12:00:00Z\"}\n";
    const recs = try parseLog(a, line, "ledger");
    try testing.expectEqual(@as(usize, 1), recs.len);
    const bad = [_][]const u8{
        line[0 .. line.len - 1],
        try std.mem.replaceOwned(u8, a, line, "\"actor\":\"a\"", "\"actor\": \"a\""),
        try std.mem.replaceOwned(u8, a, line, "\"index\":0", "\"index\":1"),
        try std.mem.replaceOwned(u8, a, line, "\"action\":\"install\"", "\"action\":\"reinstall\""),
        try std.mem.replaceOwned(u8, a, line, "\"actor\":\"a\",", "\"actor\":\"a\",\"licence\":\"x\","),
        try std.mem.concat(a, u8, &.{ line, try std.mem.replaceOwned(u8, a, try std.mem.replaceOwned(u8, a, line, "\"stack\":\"s\"", "\"stack\":\"other\""), "\"index\":0", "\"index\":1") }),
        try std.mem.replaceOwned(u8, a, line, "\"index\":0", "\"index\":0.0"),
        "\xff\n",
    };
    for (bad) |case| try testing.expectError(error.Refused, parseLog(a, case, "ledger"));
}

test "canonical keeps non-ASCII; ensure_ascii escapes it" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const v = try strictJson(a, "{\"b\":\"\\u00e9\\ud83d\\ude00\",\"a\":[1,null,true],\"c\":\"\\u007f\\n\"}", "x");
    try testing.expectEqualStrings("{\"a\":[1,null,true],\"b\":\"\u{e9}\u{1f600}\",\"c\":\"\x7f\\n\"}", try canonical(a, v));
    try testing.expectEqualStrings(
        "{\n  \"a\": [\n    1,\n    null,\n    true\n  ],\n  \"b\": \"\\u00e9\\ud83d\\ude00\",\n  \"c\": \"\\u007f\\n\"\n}\n",
        try jsonBytes(a, v),
    );
    try testing.expectEqualStrings("{\"a\": [1, null, true], \"b\": \"\\u00e9\\ud83d\\ude00\", \"c\": \"\\u007f\\n\"}", try dump(a, v, .{ .sort = true, .ascii = true, .compact = false }));
}

test "summary and receipt lines" {
    const id = &r_164;
    const root = &r_264;
    const s = parseSummary("appended record 3 id " ++ id ++ "; head size 4 root " ++ root ++ "; tiles 1").?;
    try testing.expectEqual(@as(i64, 3), s.index);
    try testing.expect(!s.replayed);
    try testing.expect(parseSummary("already recorded record 3 id " ++ id ++ "; head size 4 root " ++ root).?.replayed);
    try testing.expect(parseSummary("appended record 3 id " ++ id ++ "; head size 4 root " ++ root ++ "x") == null);
    try testing.expect(tileLine("tile log size 4 root " ++ root ++ " extends the head and holds the same lines") != null);
    try testing.expect(checkpointLine("checkpoint example.org/log size 4 root " ++ root ++ " to /x/y") != null);
    try testing.expect(checkpointLine("checkpoint a b size 4 root " ++ root ++ " to /x") == null);
    try testing.expect(indexCheckpointLine("index checkpoint example.org/log/index size 2 root " ++ root ++ " to /x"));
}

test "JSON errors read as Python's decoder prints them" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    why_alloc = a;
    const cases = [_][2][]const u8{
        .{ "{", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)" },
        .{ "[1,]", "Illegal trailing comma before end of array: line 1 column 3 (char 2)" },
        .{ "{\"a\":1,}", "Illegal trailing comma before end of object: line 1 column 7 (char 6)" },
        .{ "\"ab", "Unterminated string starting at: line 1 column 1 (char 0)" },
        .{ "\"a\x01\"", "Invalid control character at: line 1 column 3 (char 2)" },
        .{ "1 2", "Extra data: line 1 column 3 (char 2)" },
        .{ "", "Expecting value: line 1 column 1 (char 0)" },
        .{ "{\"a\" 1}", "Expecting ':' delimiter: line 1 column 6 (char 5)" },
        .{ "[1 2]", "Expecting ',' delimiter: line 1 column 4 (char 3)" },
        .{ "\n\n {\"a\"", "Expecting ':' delimiter: line 3 column 6 (char 7)" },
        .{ "tru", "Expecting value: line 1 column 1 (char 0)" },
        .{ "-", "Expecting value: line 1 column 1 (char 0)" },
        .{ "\"\\x\"", "Invalid \\escape: line 1 column 2 (char 1)" },
        .{ "\"\\u12\"", "Invalid \\uXXXX escape: line 1 column 3 (char 2)" },
        .{ "{\"\u{e9}\":1 x}", "Expecting ',' delimiter: line 1 column 8 (char 7)" },
        .{ "[1]\n\n x", "Extra data: line 3 column 2 (char 6)" },
    };
    for (cases) |c| {
        try testing.expectError(error.Refused, strictJson(a, c[0], "x"));
        const want = try std.fmt.allocPrint(a, "x is not JSON: {s}", .{c[1]});
        try testing.expectEqualStrings(want, lastMessage());
    }
}

test "time round trip" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    const t = parseObserved("2026-01-02T03:04:05Z").?;
    try testing.expectEqualStrings("2026-01-02T03:04:05Z", try utc(a, t));
    try testing.expect(parseObserved("2026-02-30T00:00:00Z") == null);
    try testing.expect(parseObserved("2024-02-29T00:00:00Z") != null);
    try testing.expectEqualStrings("1970-01-01T00:00:00Z", try utc(a, 0));
}

test "quote keeps the path separator only where asked" {
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    const a = arena.allocator();
    try testing.expectEqualStrings("packages/evil%257Cpkg.md", try quote(a, "packages/evil%7Cpkg.md", "/"));
    try testing.expectEqualStrings("a%2Fb", try quote(a, "a/b", ""));
}

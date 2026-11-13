//! The install-directory assignment in an EasyBuild module file, matched as
//! the Python tool's regular expressions match it:
//!   Lua  (?m)^local root\s*=\s*"((?:\\.|[^"\\])*)"\s*$
//!   Tcl  (?m)^set root\s+(?:"([^"]*)"|(\S+))\s*$
//! \s is Python's Unicode whitespace and includes newlines, so an assignment
//! may span lines; $ is the end of the text or the position before a newline.
const std = @import("std");
const common = @import("zig_common");
const pytext = common.pytext;

pub const Syntax = enum { lua, tcl };
pub const Root = struct { syntax: Syntax, path: []const u8 };

fn wsAt(s: []const u8, i: usize) ?usize {
    if (i >= s.len) return null;
    const ch = pytext.nextChar(s[i..]);
    const cp = ch.cp orelse return null;
    return if (pytext.pyWhitespace(cp)) ch.len else null;
}

fn skipWs(s: []const u8, i: usize) usize {
    var j = i;
    while (wsAt(s, j)) |n| j += n;
    return j;
}

/// \s*$ from `i`: the whitespace run reaches the end, or holds a newline.
fn tailOk(s: []const u8, i: usize) bool {
    var j = i;
    while (true) {
        if (j >= s.len or s[j] == '\n') return true;
        const n = wsAt(s, j) orelse return false;
        j += n;
    }
}

fn luaAt(s: []const u8, start: usize) ?[]const u8 {
    const lit = "local root";
    if (!std.mem.startsWith(u8, s[start..], lit)) return null;
    var i = skipWs(s, start + lit.len);
    if (i >= s.len or s[i] != '=') return null;
    i = skipWs(s, i + 1);
    if (i >= s.len or s[i] != '"') return null;
    i += 1;
    const content = i;
    while (i < s.len) {
        if (s[i] == '"') break;
        if (s[i] == '\\') {
            // \\. : any character but a newline after the backslash.
            if (i + 1 >= s.len or s[i + 1] == '\n') return null;
            i += 1 + pytext.nextChar(s[i + 1 ..]).len;
            continue;
        }
        i += pytext.nextChar(s[i..]).len;
    }
    if (i >= s.len) return null;
    const value = s[content..i];
    if (!tailOk(s, i + 1)) return null;
    return value;
}

fn tclAt(s: []const u8, start: usize) ?[]const u8 {
    const lit = "set root";
    if (!std.mem.startsWith(u8, s[start..], lit)) return null;
    const after = start + lit.len;
    const i = skipWs(s, after);
    if (i == after or i >= s.len) return null;
    if (s[i] == '"') {
        if (std.mem.indexOfScalarPos(u8, s, i + 1, '"')) |close| {
            if (tailOk(s, close + 1)) return s[i + 1 .. close];
        }
    }
    // (\S+): the whole run of non-whitespace, then \s*$.
    var j = i;
    while (j < s.len and wsAt(s, j) == null) j += pytext.nextChar(s[j..]).len;
    if (j == i or !tailOk(s, j)) return null;
    return s[i..j];
}

/// parse_root: the earliest Lua or Tcl assignment, Lua first on a tie.
pub fn parseRoot(s: []const u8) ?Root {
    var start: usize = 0;
    while (start <= s.len) {
        if (luaAt(s, start)) |v| return .{ .syntax = .lua, .path = v };
        if (tclAt(s, start)) |v| return .{ .syntax = .tcl, .path = v };
        const nl = std.mem.indexOfScalarPos(u8, s, start, '\n') orelse break;
        start = nl + 1;
    }
    return null;
}

test "parseRoot follows the Python patterns" {
    try std.testing.expectEqualStrings("/opt/a", parseRoot("x\nlocal root = \"/opt/a\"\n").?.path);
    try std.testing.expectEqual(Syntax.tcl, parseRoot("set root /opt/b\nlocal root = \"/opt/a\"\n").?.syntax);
    try std.testing.expectEqualStrings("/opt/b", parseRoot("set root    /opt/b  \n").?.path);
    try std.testing.expectEqualStrings("/q r", parseRoot("set root \"/q r\"\n").?.path);
    try std.testing.expectEqualStrings("a\\\"b", parseRoot("local root = \"a\\\"b\"").?.path);
    // \s spans lines.
    try std.testing.expectEqualStrings("/m", parseRoot("local root\n=\n\"/m\"\n").?.path);
    try std.testing.expect(parseRoot("local root = \"/x\" -- c\n") == null);
    try std.testing.expect(parseRoot("  local root = \"/x\"\n") == null);
    try std.testing.expect(parseRoot("set root\n") == null);
    try std.testing.expectEqualStrings("\"a", parseRoot("set root \"a\n").?.path);
}

// ---- dependencies, identity and rewriting ----------------------------------

const Allocator = std.mem.Allocator;

fn lineStart(s: []const u8, p: usize) bool {
    return p == 0 or s[p - 1] == '\n';
}

/// (?m)^\s*(?:load|depends_on)\("([^"]+)"\) at p: the name and the end.
fn luaLoadAt(s: []const u8, p: usize) ?struct { name: []const u8, end: usize } {
    var i = skipWs(s, p);
    for ([_][]const u8{ "load(\"", "depends_on(\"" }) |lead| {
        if (std.mem.startsWith(u8, s[i..], lead)) {
            i += lead.len;
            const close = std.mem.indexOfScalarPos(u8, s, i, '"') orelse return null;
            if (close == i or close + 1 >= s.len or s[close + 1] != ')') return null;
            return .{ .name = s[i..close], .end = close + 2 };
        }
    }
    return null;
}

/// (?m)^\s*module\s+load\s+\{?([^\s}]+)\}?\s*$ at p.
fn tclLoadAt(s: []const u8, p: usize) ?struct { name: []const u8, end: usize } {
    var i = skipWs(s, p);
    if (!std.mem.startsWith(u8, s[i..], "module")) return null;
    i += "module".len;
    var j = skipWs(s, i);
    if (j == i or !std.mem.startsWith(u8, s[j..], "load")) return null;
    j += "load".len;
    const k = skipWs(s, j);
    if (k == j) return null;
    // \{? first taking the brace, then (backtracking) leaving it to [^\s}]+.
    for ([_]bool{ true, false }) |take_brace| {
        var m = k;
        if (take_brace) {
            if (m >= s.len or s[m] != '{') continue;
            m += 1;
        }
        const start = m;
        while (m < s.len and s[m] != '}' and wsAt(s, m) == null) m += pytext.nextChar(s[m..]).len;
        if (m == start) continue;
        const name = s[start..m];
        var e = m;
        if (e < s.len and s[e] == '}') e += 1;
        if (tailOk(s, e)) {
            var t = e;
            while (t < s.len and s[t] != '\n') t += pytext.nextChar(s[t..]).len;
            return .{ .name = name, .end = t };
        }
        if (e != m and tailOk(s, m)) return .{ .name = name, .end = m };
    }
    return null;
}

/// loads(): Lua load/depends_on names, then Tcl module load names, each in
/// the order re.findall returns them.
pub fn loads(a: Allocator, s: []const u8) Allocator.Error![]const []const u8 {
    var out: std.ArrayList([]const u8) = .empty;
    inline for (.{ luaLoadAt, tclLoadAt }) |f| {
        var p: usize = 0;
        while (p <= s.len) {
            if (lineStart(s, p)) if (f(s, p)) |m| {
                try out.append(a, m.name);
                p = if (m.end > p) m.end else p + 1;
                continue;
            };
            p += 1;
        }
    }
    return out.items;
}

/// module_identity: name/version from the last two components, without the
/// extension of the last.
pub fn moduleIdentity(a: Allocator, modules_root: []const u8, module_path: []const u8) []const u8 {
    const rel = relpath(a, module_path, modules_root);
    var parts: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, rel, '/');
    while (it.next()) |x| parts.append(a, x) catch {};
    const last = parts.items[parts.items.len - 1];
    const stem = splitextStem(last);
    if (parts.items.len < 2) return stem;
    return std.fs.path.join(a, &.{ parts.items[parts.items.len - 2], stem }) catch stem;
}

/// os.path.splitext(name)[0]: leading dots are not an extension.
fn splitextStem(name: []const u8) []const u8 {
    var lead: usize = 0;
    while (lead < name.len and name[lead] == '.') lead += 1;
    const dot = std.mem.lastIndexOfScalar(u8, name, '.') orelse return name;
    if (dot < lead) return name;
    return name[0..dot];
}

/// os.path.relpath(path, start) for absolute, normalized paths.
pub fn relpath(a: Allocator, path: []const u8, start: []const u8) []const u8 {
    var pp: std.ArrayList([]const u8) = .empty;
    var sp: std.ArrayList([]const u8) = .empty;
    var it = std.mem.splitScalar(u8, path, '/');
    while (it.next()) |x| if (x.len > 0) pp.append(a, x) catch {};
    it = std.mem.splitScalar(u8, start, '/');
    while (it.next()) |x| if (x.len > 0) sp.append(a, x) catch {};
    var common_len: usize = 0;
    while (common_len < pp.items.len and common_len < sp.items.len and std.mem.eql(u8, pp.items[common_len], sp.items[common_len])) common_len += 1;
    var out: std.ArrayList([]const u8) = .empty;
    for (sp.items[common_len..]) |_| out.append(a, "..") catch {};
    out.appendSlice(a, pp.items[common_len..]) catch {};
    if (out.items.len == 0) return ".";
    return std.mem.join(a, "/", out.items) catch ".";
}

pub const RewriteError = error{Refused} || Allocator.Error;

fn pathChar(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '.' or c == '_' or c == '+' or c == '-';
}

/// replace_path: every occurrence of `old` not followed by a path character
/// becomes `new`; occurrences of `new` already there are kept.
pub fn replacePath(a: Allocator, text: []const u8, old: []const u8, new: []const u8, why: *[]const u8) RewriteError![]const u8 {
    if (std.mem.eql(u8, old, new)) return text;
    if (old.len == 0) {
        why.* = "refusing to rewrite an empty root";
        return error.Refused;
    }
    if (std.mem.indexOfAny(u8, new, "\n\"\x00") != null) {
        why.* = "refusing to write a root that contains a quote, newline, or NUL";
        return error.Refused;
    }
    const token = "\x00KEEP\x00";
    const protected = try std.mem.replaceOwned(u8, a, text, new, token);
    var out: std.ArrayList(u8) = .empty;
    var count: usize = 0;
    var i: usize = 0;
    while (i < protected.len) {
        if (std.mem.startsWith(u8, protected[i..], old)) {
            const after = i + old.len;
            if (after >= protected.len or !pathChar(protected[after])) {
                try out.appendSlice(a, new);
                i = after;
                count += 1;
                continue;
            }
        }
        try out.append(a, protected[i]);
        i += 1;
    }
    if (count < 1) {
        why.* = try std.fmt.allocPrint(a, "module file does not contain the install path {s}", .{old});
        return error.Refused;
    }
    return std.mem.replaceOwned(u8, a, out.items, token, new);
}

/// rewrite_root: point the module file at `new_root` (absolute, normalized).
pub fn rewriteRoot(a: Allocator, text: []const u8, new_root: []const u8, why: *[]const u8) RewriteError![]const u8 {
    const r = parseRoot(text) orelse {
        why.* = "module file has no root assignment";
        return error.Refused;
    };
    if (r.path.len == 0) {
        why.* = "module file has no root assignment";
        return error.Refused;
    }
    const updated = try replacePath(a, text, r.path, new_root, why);
    const again = parseRoot(updated);
    if (again == null or again.?.syntax != r.syntax or !std.mem.eql(u8, again.?.path, new_root)) {
        const got = if (again) |g| g.path else "None";
        why.* = try std.fmt.allocPrint(a, "rewritten module file root is '{s}', expected {s}", .{ got, new_root });
        return error.Refused;
    }
    return updated;
}

test "loads, identity and rewriting" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const deps = try loads(a, "load(\"GCC/13\")\n  depends_on(\"zlib/1\")\nmodule load {Tcl/8}\nmodule load x/1 y\n");
    try std.testing.expectEqual(@as(usize, 3), deps.len);
    try std.testing.expectEqualStrings("Tcl/8", deps[2]);
    try std.testing.expectEqualStrings("demo/1", moduleIdentity(a, "/m", "/m/all/demo/1.lua"));
    try std.testing.expectEqualStrings("x", moduleIdentity(a, "/m", "/m/x.lua"));
    var why: []const u8 = "";
    const t = try rewriteRoot(a, "local root = \"/s/demo/1\"\nsetenv(\"X\", \"/s/demo/1/bin\")\nsetenv(\"Y\", \"/s/demo/10\")\n", "/live/demo/1", &why);
    try std.testing.expectEqualStrings("local root = \"/live/demo/1\"\nsetenv(\"X\", \"/live/demo/1/bin\")\nsetenv(\"Y\", \"/s/demo/10\")\n", t);
    try std.testing.expectError(error.Refused, rewriteRoot(a, "x", "/y", &why));
}

test "a new path that contains the old path is not doubled" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    var why: []const u8 = "";
    const t = try replacePath(a, "local root = \"/opt/software/demo/1\"\n", "/opt/software/demo/1", "/opt/software/demo/1/gen", &why);
    try std.testing.expectEqualStrings("local root = \"/opt/software/demo/1/gen\"\n", t);
}

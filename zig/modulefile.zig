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

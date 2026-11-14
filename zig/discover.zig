//! Find the module files EasyBuild wrote and decide which install directory
//! each names.
const std = @import("std");
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");
const modulefile = @import("modulefile.zig");

const sys = common.sys;
const fsx = common.fsx;
const json = common.json;
const Allocator = std.mem.Allocator;

pub const Error = error{Refused} || Allocator.Error;

pub const Link = struct { rel: []const u8, target: []const u8 };

fn lessText(_: void, x: []const u8, y: []const u8) bool {
    return json.pyTextLess(x, y);
}

fn lessLink(_: void, x: Link, y: Link) bool {
    if (!std.mem.eql(u8, x.rel, y.rel)) return json.pyTextLess(x.rel, y.rel);
    return json.pyTextLess(x.target, y.target);
}

fn readlink(a: Allocator, path: []const u8) []const u8 {
    var zb: [sys.path_max:0]u8 = undefined;
    var buf: [sys.path_max]u8 = undefined;
    const z = sys.z(&zb, path) catch return "";
    const n = sys.call(std.os.linux.readlinkat, .{ std.os.linux.AT.FDCWD, z.ptr, &buf, buf.len }) catch return "";
    return a.dupe(u8, buf[0..n]) catch "";
}

/// walk_module_tree: the regular .lua and .tcl files (sorted), and every
/// symbolic link as (relative path, link text); linked directories are not
/// entered.
pub fn walkModuleTree(a: Allocator, modules_root: []const u8, why: *[]const u8) Error!struct { files: []const []const u8, links: []const Link } {
    if (!std.mem.eql(u8, main_mod.lstatKind(modules_root), "dir")) {
        why.* = try std.fmt.allocPrint(a, "module tree is not a real directory: {s}", .{modules_root});
        return error.Refused;
    }
    var files: std.ArrayList([]const u8) = .empty;
    var links: std.ArrayList(Link) = .empty;
    var stack: std.ArrayList([]const u8) = .empty;
    try stack.append(a, modules_root);
    while (stack.pop()) |dir| {
        const fd = fsx.openDir(fsx.cwd, dir, false) catch continue;
        defer sys.close(fd);
        var it = fsx.DirIter.init(a, fd) catch continue;
        while (it.next() catch null) |e| {
            const child = try std.fs.path.join(a, &.{ dir, e.name });
            const st = fsx.statAt(fsx.cwd, child, false) catch continue;
            if (st.isLink()) {
                try links.append(a, .{ .rel = modulefile.relpath(a, child, modules_root), .target = readlink(a, child) });
            } else if (st.isDir()) {
                try stack.append(a, child);
            } else if (st.isReg() and (std.mem.endsWith(u8, e.name, ".lua") or std.mem.endsWith(u8, e.name, ".tcl"))) {
                try files.append(a, child);
            }
        }
    }
    std.mem.sort([]const u8, files.items, {}, lessText);
    std.mem.sort(Link, links.items, {}, lessLink);
    return .{ .files = files.items, .links = links.items };
}

pub const Install = struct { staged: []const u8, live: []const u8, text: []const u8 };

/// swap_install_for: the staged tree, the stable live tree, and the module
/// text pointing at the live tree.
pub fn swapInstallFor(a: Allocator, text: []const u8, stage_software: []const u8, live_software: []const u8, why: *[]const u8) Error!Install {
    const r = modulefile.parseRoot(text) orelse {
        why.* = "module file has no root assignment";
        return error.Refused;
    };
    if (r.path.len == 0) {
        why.* = "module file has no root assignment";
        return error.Refused;
    }
    const root_abs = admin.abspath(a, r.path);
    const stage_abs = admin.abspath(a, stage_software);
    const live_abs = admin.abspath(a, live_software);
    var relative: []const u8 = undefined;
    var staged: []const u8 = undefined;
    if (admin.isWithin(a, root_abs, stage_abs)) {
        relative = modulefile.relpath(a, root_abs, stage_abs);
        staged = root_abs;
    } else if (admin.isWithin(a, root_abs, live_abs)) {
        relative = modulefile.relpath(a, root_abs, live_abs);
        staged = try std.fs.path.join(a, &.{ stage_abs, relative });
    } else {
        why.* = try std.fmt.allocPrint(a, "module root {s} is outside both the stage software tree {s} and the live software tree {s}", .{ root_abs, stage_abs, live_abs });
        return error.Refused;
    }
    const live = if (std.mem.eql(u8, relative, ".")) live_abs else try std.fs.path.join(a, &.{ live_abs, relative });
    if (!std.mem.eql(u8, main_mod.lstatKind(staged), "dir")) {
        why.* = try std.fmt.allocPrint(a, "staged installation is not a real directory: {s}", .{staged});
        return error.Refused;
    }
    const new_text = modulefile.rewriteRoot(a, text, live, why) catch |err| return switch (err) {
        error.Refused => error.Refused,
        error.OutOfMemory => error.OutOfMemory,
    };
    return .{ .staged = staged, .live = live, .text = new_text };
}

/// order_module_files: dependencies first, ties and the remainder of a
/// cycle in path order. Returns the order and each file's text.
pub fn orderModuleFiles(a: Allocator, files: []const []const u8, modules_root: []const u8) Error!struct { order: []const []const u8, texts: std.StringArrayHashMapUnmanaged([]const u8) } {
    var texts: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    for (files) |p| {
        const f = fsx.openRegular(fsx.cwd, p, true) catch return error.Refused;
        defer sys.close(f.fd);
        const t = fsx.readAll(a, f.fd, 64 << 20) catch return error.Refused;
        try texts.put(a, p, t);
    }
    var by_identity: std.StringArrayHashMapUnmanaged([]const u8) = .empty;
    for (files) |p| {
        const id = modulefile.moduleIdentity(a, modules_root, p);
        if (!by_identity.contains(id)) try by_identity.put(a, id, p);
    }
    var incoming: std.StringArrayHashMapUnmanaged(usize) = .empty;
    var children: std.StringArrayHashMapUnmanaged(std.ArrayList([]const u8)) = .empty;
    for (files) |p| {
        try incoming.put(a, p, 0);
        try children.put(a, p, .empty);
    }
    for (files) |p| {
        for (try modulefile.loads(a, texts.get(p).?)) |name| {
            const earlier = by_identity.get(name) orelse continue;
            if (std.mem.eql(u8, earlier, p)) continue;
            incoming.getPtr(p).?.* += 1;
            try children.getPtr(earlier).?.append(a, p);
        }
    }
    var ready: std.ArrayList([]const u8) = .empty;
    for (files) |p| if (incoming.get(p).? == 0) try ready.append(a, p);
    std.mem.sort([]const u8, ready.items, {}, lessText);
    var ordered: std.ArrayList([]const u8) = .empty;
    var seen: std.StringArrayHashMapUnmanaged(void) = .empty;
    while (ready.items.len > 0) {
        const p = ready.orderedRemove(0);
        if (seen.contains(p)) continue;
        try seen.put(a, p, {});
        try ordered.append(a, p);
        for (children.get(p).?.items) |c| {
            const n = incoming.getPtr(c).?;
            n.* -= 1;
            if (n.* == 0) try ready.append(a, c);
        }
        std.mem.sort([]const u8, ready.items, {}, lessText);
    }
    for (files) |p| if (!seen.contains(p)) try ordered.append(a, p);
    return .{ .order = ordered.items, .texts = texts };
}

/// map_link_target: an absolute target inside the staged module tree points
/// at the same relative path in the live tree.
pub fn mapLinkTarget(a: Allocator, target: []const u8, stage_modules: []const u8, live_modules: []const u8) []const u8 {
    if (target.len == 0 or target[0] != '/') return target;
    const target_abs = admin.abspath(a, target);
    const stage_abs = admin.abspath(a, stage_modules);
    if (admin.isWithin(a, target_abs, stage_abs)) {
        const rel = modulefile.relpath(a, target_abs, stage_abs);
        if (std.mem.eql(u8, rel, ".")) return admin.abspath(a, live_modules);
        return std.fs.path.join(a, &.{ admin.abspath(a, live_modules), rel }) catch target_abs;
    }
    return target_abs;
}

test "ordering puts dependencies first and maps links" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try std.fmt.allocPrint(a, "/tmp/esr-discover-{d}", .{std.os.linux.getpid()});
    defer fsx.deleteTree(a, fsx.cwd, base) catch {};
    const m = try std.fmt.allocPrint(a, "{s}/all", .{base});
    try common.atomic.makedirs(try std.fmt.allocPrint(a, "{s}/zlib", .{m}));
    try common.atomic.makedirs(try std.fmt.allocPrint(a, "{s}/app", .{m}));
    var why: common.atomic.Why = .{};
    try common.atomic.atomicWrite(a, try std.fmt.allocPrint(a, "{s}/app/1.lua", .{m}), "load(\"zlib/2\")\n", .{}, &why);
    try common.atomic.atomicWrite(a, try std.fmt.allocPrint(a, "{s}/zlib/2.lua", .{m}), "whatis(\"z\")\n", .{}, &why);
    var w: []const u8 = "";
    const tree = try walkModuleTree(a, m, &w);
    try std.testing.expectEqual(@as(usize, 2), tree.files.len);
    const o = try orderModuleFiles(a, tree.files, m);
    try std.testing.expect(std.mem.endsWith(u8, o.order[0], "zlib/2.lua"));
    try std.testing.expectEqualStrings("/live/all/x", mapLinkTarget(a, "/stage/all/x", "/stage", "/live"));
    try std.testing.expectEqualStrings("rel/x", mapLinkTarget(a, "rel/x", "/stage", "/live"));
}

//! Crash-aware exchange of two sibling directories: renameat2
//! RENAME_EXCHANGE, or, where the filesystem refuses rename flags, three
//! ordinary renames through a hold name, every caught failure of which
//! restores the original names. recoverInterrupted repairs a fallback that a
//! crash stopped half way.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const main_mod = @import("main.zig");
const admin = @import("admin.zig");

const sys = common.sys;
const atomic = common.atomic;
const Allocator = std.mem.Allocator;

pub const fallback_hold_suffix = ".fallback-old";

pub const Method = enum { @"renameat2-exchange", @"rename-fallback" };

/// Why a swap stopped; set before an error is returned.
pub const Why = struct { msg: []const u8 = "" };

pub const Error = error{ Refused, NeedsIntervention, Failed };

fn fail(a: Allocator, why: *Why, comptime kind: Error, comptime f: []const u8, args: anytype) Error {
    why.msg = std.fmt.allocPrint(a, f, args) catch "swap failed";
    return kind;
}

fn kindOf(p: []const u8) []const u8 {
    return main_mod.lstatKind(p);
}

fn renamePath(a: Allocator, from: []const u8, to: []const u8, why: *Why) Error!void {
    sys.renameat(common.fsx.cwd, from, common.fsx.cwd, to) catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, from)});
}

fn syncParent(a: Allocator, live: []const u8, why: *Why) Error!void {
    atomic.fsyncDir(std.fs.path.dirname(live) orelse "/") catch |err|
        return fail(a, why, error.Failed, "{s}", .{main_mod.osError(a, err, live)});
}

fn restoreFallback(a: Allocator, live: []const u8, staged: []const u8, hold: []const u8, why: *Why) Error!void {
    const lk = kindOf(live);
    const sk = kindOf(staged);
    const hk = kindOf(hold);
    if (std.mem.eql(u8, hk, "missing")) return;
    if (!std.mem.eql(u8, hk, "dir")) return fail(a, why, error.NeedsIntervention, "fallback recovery path is not a directory: {s}", .{hold});
    if (std.mem.eql(u8, lk, "missing") and std.mem.eql(u8, sk, "dir")) {
        // The old live tree was moved aside; the staged tree has not moved.
        try renamePath(a, hold, live, why);
    } else if (std.mem.eql(u8, lk, "dir") and std.mem.eql(u8, sk, "missing")) {
        // The staged tree became live; put it back, then the old tree.
        try renamePath(a, live, staged, why);
        try renamePath(a, hold, live, why);
    } else {
        return fail(a, why, error.NeedsIntervention, "cannot recover interrupted fallback swap: live={s}, staged={s}, hold={s}", .{ lk, sk, hk });
    }
    try syncParent(a, live, why);
}

/// recover_interrupted_swap: restore an interrupted fallback, if any.
pub fn recoverInterrupted(a: Allocator, live_in: []const u8, staged_in: []const u8, why: *Why) Error!void {
    const live = admin.abspath(a, live_in);
    const staged = admin.abspath(a, staged_in);
    const hold = std.fmt.allocPrint(a, "{s}" ++ fallback_hold_suffix, .{staged}) catch return error.Failed;
    try restoreFallback(a, live, staged, hold, why);
    if (!std.mem.eql(u8, kindOf(hold), "missing")) return fail(a, why, error.NeedsIntervention, "fallback recovery left its hold path behind: {s}", .{hold});
}

fn exchange(a: Allocator, first: []const u8, second: []const u8) linux.E {
    var za: [sys.path_max:0]u8 = undefined;
    var zb: [sys.path_max:0]u8 = undefined;
    const x = sys.z(&za, first) catch return .NAMETOOLONG;
    const y = sys.z(&zb, second) catch return .NAMETOOLONG;
    _ = a;
    return linux.errno(linux.renameat2(linux.AT.FDCWD, x.ptr, linux.AT.FDCWD, y.ptr, .{ .EXCHANGE = true }));
}

fn strerror(e: linux.E) []const u8 {
    return switch (e) {
        .INVAL => "Invalid argument",
        .NOSYS => "Function not implemented",
        .OPNOTSUPP => "Operation not supported",
        .PERM => "Operation not permitted",
        .ACCES => "Permission denied",
        .BUSY => "Device or resource busy",
        .XDEV => "Invalid cross-device link",
        .NOENT => "No such file or directory",
        else => @tagName(e),
    };
}

/// swap_directories: exchange `live` and `staged` (siblings, both real
/// directories). Afterwards `staged` holds the retired live tree.
pub fn swapDirectories(a: Allocator, live_in: []const u8, staged_in: []const u8, allow_exchange: bool, faults: atomic.Faults, step_prefix: []const u8, why: *Why) Error!Method {
    const live = admin.abspath(a, live_in);
    const staged = admin.abspath(a, staged_in);
    try recoverInterrupted(a, live, staged, why);
    if (!std.mem.eql(u8, std.fs.path.dirname(live) orelse "", std.fs.path.dirname(staged) orelse ""))
        return fail(a, why, error.Refused, "the live and staged directories must be siblings", .{});
    for ([_][2][]const u8{ .{ "live", live }, .{ "staged", staged } }) |pair| {
        const k = kindOf(pair[1]);
        if (!std.mem.eql(u8, k, "dir")) return fail(a, why, error.Refused, "{s} swap path is {s}, not a real directory: {s}", .{ pair[0], k, pair[1] });
    }
    if (allow_exchange) {
        faults.reach(std.fmt.allocPrint(a, "{s}.exchange.before", .{step_prefix}) catch step_prefix);
        const e = exchange(a, live, staged);
        if (e == .SUCCESS) {
            faults.reach(std.fmt.allocPrint(a, "{s}.exchange.after", .{step_prefix}) catch step_prefix);
            syncParent(a, live, why) catch |err| {
                // Undo the exchange before reporting the failure.
                if (exchange(a, live, staged) != .SUCCESS)
                    return fail(a, why, error.NeedsIntervention, "RENAME_EXCHANGE succeeded but its rollback failed for {s}: {s}", .{ live, why.msg });
                return err;
            };
            return .@"renameat2-exchange";
        }
        if (e != .INVAL and e != .NOSYS and e != .OPNOTSUPP)
            return fail(a, why, error.Failed, "[Errno {d}] renameat2(RENAME_EXCHANGE): {s}", .{ @backingInt(e), strerror(e) });
    }
    const hold = std.fmt.allocPrint(a, "{s}" ++ fallback_hold_suffix, .{staged}) catch return error.Failed;
    if (!std.mem.eql(u8, kindOf(hold), "missing")) try recoverInterrupted(a, live, staged, why);
    fallback: {
        faults.reach(std.fmt.allocPrint(a, "{s}.fallback.live-to-hold", .{step_prefix}) catch step_prefix);
        renamePath(a, live, hold, why) catch break :fallback;
        faults.reach(std.fmt.allocPrint(a, "{s}.fallback.staged-to-live", .{step_prefix}) catch step_prefix);
        renamePath(a, staged, live, why) catch break :fallback;
        faults.reach(std.fmt.allocPrint(a, "{s}.fallback.hold-to-staged", .{step_prefix}) catch step_prefix);
        renamePath(a, hold, staged, why) catch break :fallback;
        syncParent(a, live, why) catch break :fallback;
        return .@"rename-fallback";
    }
    const first = why.msg;
    var inner: Why = .{};
    restoreFallback(a, live, staged, hold, &inner) catch
        return fail(a, why, error.NeedsIntervention, "fallback swap failed and could not restore {s}: {s}", .{ live, inner.msg });
    why.msg = first;
    return error.Failed;
}

test "swap exchanges two sibling directories and back" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const base = try std.fmt.allocPrint(a, "/tmp/esr-swap-{d}", .{linux.getpid()});
    defer common.fsx.deleteTree(a, common.fsx.cwd, base) catch {};
    const live = try std.fmt.allocPrint(a, "{s}/live", .{base});
    const staged = try std.fmt.allocPrint(a, "{s}/.live.new", .{base});
    try atomic.makedirs(try std.fmt.allocPrint(a, "{s}/L", .{live}));
    try atomic.makedirs(try std.fmt.allocPrint(a, "{s}/S", .{staged}));
    var why: Why = .{};
    for ([_]bool{ true, false }) |allow| {
        _ = try swapDirectories(a, live, staged, allow, .{}, "swap", &why);
        try std.testing.expectEqualStrings("dir", kindOf(try std.fmt.allocPrint(a, "{s}/S", .{live})));
        _ = try swapDirectories(a, live, staged, allow, .{}, "swap", &why);
        try std.testing.expectEqualStrings("dir", kindOf(try std.fmt.allocPrint(a, "{s}/L", .{live})));
    }
    try std.testing.expectError(error.Refused, swapDirectories(a, live, base, true, .{}, "swap", &why));
}

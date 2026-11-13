//! Run a build or preparation command with the caller's lock held until the
//! command's whole process group has stopped. The command inherits stdin,
//! stdout and stderr and runs in its own session; SIGTERM, SIGINT and SIGHUP
//! to this process stop the group (SIGTERM, two seconds, SIGKILL) and give
//! 128 + the signal. The leader stays unreaped until the last group signal,
//! so its group id cannot be given to an unrelated process meanwhile.
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");

const sys = common.sys;
const fsx = common.fsx;
const atomic = common.atomic;
const Allocator = std.mem.Allocator;

var interrupted = std.atomic.Value(u32).init(0);

fn requestStop(sig: linux.SIG) callconv(.c) void {
    _ = interrupted.cmpxchgStrong(0, @backingInt(sig), .seq_cst, .seq_cst);
}

fn sleepMs(ms: u64) void {
    var ts: linux.timespec = .{ .sec = @intCast(ms / 1000), .nsec = @intCast((ms % 1000) * std.time.ns_per_ms) };
    _ = linux.nanosleep(&ts, null);
}

fn nowMs() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(.MONOTONIC, &ts);
    return @as(i64, ts.sec) * 1000 + @divTrunc(@as(i64, ts.nsec), 1_000_000);
}

/// True while any process in group `pgid` is alive (not a zombie).
fn groupRunning(a: Allocator, pgid: linux.pid_t) bool {
    const fd = fsx.openDir(fsx.cwd, "/proc", true) catch return false;
    defer sys.close(fd);
    var it = fsx.DirIter.init(a, fd) catch return false;
    defer it.deinit(a);
    while (it.next() catch null) |e| {
        _ = std.fmt.parseInt(u32, e.name, 10) catch continue;
        var pb: [64]u8 = undefined;
        const p = std.fmt.bufPrint(&pb, "/proc/{s}/stat", .{e.name}) catch continue;
        const f = fsx.openRegular(fsx.cwd, p, true) catch continue;
        defer sys.close(f.fd);
        var buf: [1024]u8 = undefined;
        const n = sys.read(f.fd, &buf) catch continue;
        const close = std.mem.lastIndexOfScalar(u8, buf[0..n], ')') orelse continue;
        var fields = std.mem.tokenizeAny(u8, buf[close + 1 .. n], " \n");
        const state = fields.next() orelse continue;
        _ = fields.next(); // ppid
        const pg = std.fmt.parseInt(i64, fields.next() orelse continue, 10) catch continue;
        if (pg == pgid and !std.mem.eql(u8, state, "Z") and !std.mem.eql(u8, state, "X")) return true;
    }
    return false;
}

fn killGroup(a: Allocator, pgid: linux.pid_t) void {
    _ = linux.kill(-pgid, .KILL);
    while (groupRunning(a, pgid)) sleepMs(10);
}

fn exited(pid: linux.pid_t) bool {
    var info = std.mem.zeroes(linux.siginfo_t);
    const rc = linux.waitid(.PID, pid, &info, linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT, null);
    if (linux.errno(rc) != .SUCCESS) return true;
    return @backingInt(info.signo) != 0;
}

pub const RunError = error{ Refused, CannotStart };

/// run_command: the command's exit code, 128 + signal for a signalled
/// command, or 128 + the signal that interrupted this process.
pub fn runCommand(a: Allocator, exe: []const u8, argv: []const []const u8, envp: common.proc.Envp, faults: atomic.Faults, step: []const u8) RunError!u8 {
    faults.reach(step);
    if (fsx.statAt(fsx.cwd, "/proc/self/stat", true)) |_| {} else |_| return error.Refused;
    const zargv = a.allocSentinel(?[*:0]const u8, argv.len, null) catch return error.CannotStart;
    for (argv, 0..) |x, i| zargv[i] = (a.dupeSentinel(u8, x, 0) catch return error.CannotStart).ptr;
    const zexe = a.dupeSentinel(u8, exe, 0) catch return error.CannotStart;

    interrupted.store(0, .seq_cst);
    const sigs = [_]linux.SIG{ .TERM, .INT, .HUP };
    var old: [sigs.len]linux.Sigaction = undefined;
    var act: linux.Sigaction = .{ .handler = .{ .handler = requestStop }, .mask = linux.sigemptyset(), .flags = 0 };
    for (sigs, 0..) |s, i| _ = linux.sigaction(s, &act, &old[i]);
    defer for (sigs, 0..) |s, i| {
        _ = linux.sigaction(s, &old[i], null);
    };

    const fork_rc = linux.fork();
    if (linux.errno(fork_rc) != .SUCCESS) return error.CannotStart;
    if (fork_rc == 0) {
        _ = linux.setsid();
        const empty = linux.sigemptyset();
        var dfl: linux.Sigaction = .{ .handler = .{ .handler = linux.SIG.DFL }, .mask = empty, .flags = 0 };
        var s: u32 = 1;
        while (s < linux.NSIG) : (s += 1) {
            if (s == @backingInt(linux.SIG.KILL) or s == @backingInt(linux.SIG.STOP)) continue;
            _ = linux.sigaction(@fromBackingInt(@intCast(s)), &dfl, null);
        }
        _ = linux.sigprocmask(linux.SIG.SETMASK, &empty, null);
        _ = linux.close_range(3, std.math.maxInt(linux.fd_t), .{ .UNSHARE = false, .CLOEXEC = false });
        _ = linux.execve(zexe.ptr, zargv.ptr, envp);
        linux.exit_group(127);
    }
    const pid: linux.pid_t = @intCast(fork_rc);
    while (interrupted.load(.seq_cst) == 0) {
        if (exited(pid)) break;
        sleepMs(20);
    }
    const sig = interrupted.load(.seq_cst);
    if (sig != 0) {
        _ = linux.kill(-pid, .TERM);
        const deadline = nowMs() + 2000;
        while (nowMs() < deadline) sleepMs(20);
    }
    killGroup(a, pid);
    var status: i32 = 0;
    while (true) {
        const rc = linux.wait4(pid, &status, 0, null);
        if (linux.errno(rc) != .INTR) break;
    }
    if (sig != 0) return @intCast(128 + sig);
    const st: u32 = @bitCast(status);
    if (st & 0x7f == 0) return linux.W.EXITSTATUS(st);
    return @intCast(128 + (st & 0x7f));
}

test "runCommand returns the exit code and stops the group" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const envp: common.proc.Envp = &[_:null]?[*:0]const u8{};
    try std.testing.expectEqual(@as(u8, 3), try runCommand(a, "/bin/sh", &.{ "sh", "-c", "exit 3" }, envp, .{}, "build"));
    // A background worker left in the group is killed before the return.
    try std.testing.expectEqual(@as(u8, 0), try runCommand(a, "/bin/sh", &.{ "sh", "-c", "sleep 30 & exit 0" }, envp, .{}, "build"));
}

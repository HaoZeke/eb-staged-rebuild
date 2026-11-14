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

/// _kill_group: SIGKILL the group and wait, with no limit, until no member
/// is alive, as a member keeps its descriptors until it exits.
fn killGroup(pgid: linux.pid_t) void {
    _ = linux.kill(-pgid, .KILL);
    while (common.proc.groupAlive(pgid)) sleepMs(10);
}

fn exited(pid: linux.pid_t) bool {
    var info = std.mem.zeroes(linux.siginfo_t);
    const rc = linux.waitid(.PID, pid, &info, linux.W.EXITED | linux.W.NOHANG | linux.W.NOWAIT, null);
    if (linux.errno(rc) != .SUCCESS) return true;
    return @backingInt(info.signo) != 0;
}

pub const RunError = error{ Refused, CannotStart, ExecFailed };

/// The errno of the last execve that failed in the child (error.ExecFailed).
pub var exec_errno: linux.E = .SUCCESS;
/// The step a test made fail before the command started (error.ExecFailed
/// with EIO), so callers can give Python's "injected failure at STEP".
pub var injected_at: ?[]const u8 = null;

/// The text Python's OSError gives for an errno execve returns.
pub fn strerror(e: linux.E) []const u8 {
    return common.sys.errnoText(e) orelse @tagName(e);
}

/// run_command: the command's exit code, 128 + signal for a signalled
/// command, or 128 + the signal that interrupted this process. A command
/// that cannot be executed gives error.ExecFailed with its errno in
/// `exec_errno`, as Python's Popen raises OSError in the parent.
pub fn runCommand(a: Allocator, exe: []const u8, argv: []const []const u8, envp: common.proc.Envp, faults: atomic.Faults, step: []const u8) RunError!u8 {
    injected_at = null;
    if (faults.fails(step)) {
        injected_at = step;
        exec_errno = .IO;
        return error.ExecFailed;
    }
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

    // The child reports a failed execve through a close-on-exec pipe; a
    // successful exec closes it and the read sees end of file.
    var report: [2]i32 = undefined;
    if (linux.errno(linux.pipe2(&report, .{ .CLOEXEC = true })) != .SUCCESS) return error.CannotStart;
    const fork_rc = linux.fork();
    if (linux.errno(fork_rc) != .SUCCESS) {
        _ = linux.close(report[0]);
        _ = linux.close(report[1]);
        return error.CannotStart;
    }
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
        // Close every inherited descriptor but the report pipe, which
        // execve closes.
        const w: linux.fd_t = report[1];
        if (w > 3) _ = linux.close_range(3, w - 1, .{ .UNSHARE = false, .CLOEXEC = false });
        _ = linux.close_range(w + 1, std.math.maxInt(linux.fd_t), .{ .UNSHARE = false, .CLOEXEC = false });
        const exec_rc = linux.execve(zexe.ptr, zargv.ptr, envp);
        const code: u32 = @backingInt(linux.errno(exec_rc));
        _ = linux.write(report[1], std.mem.asBytes(&code), @sizeOf(u32));
        linux.exit_group(127);
    }
    const pid: linux.pid_t = @intCast(fork_rc);
    _ = linux.close(report[1]);
    var code: u32 = 0;
    const got = while (true) {
        const rc = linux.read(report[0], std.mem.asBytes(&code), @sizeOf(u32));
        if (linux.errno(rc) != .INTR) break rc;
    };
    _ = linux.close(report[0]);
    if (linux.errno(got) == .SUCCESS and got == @sizeOf(u32)) {
        var status: i32 = 0;
        while (linux.errno(linux.wait4(pid, &status, 0, null)) == .INTR) {}
        exec_errno = @fromBackingInt(@intCast(code));
        return error.ExecFailed;
    }
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
    killGroup(pid);
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

test "runCommand reports a command that cannot be executed" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const envp: common.proc.Envp = &[_:null]?[*:0]const u8{};
    try std.testing.expectError(error.ExecFailed, runCommand(a, "/nonexistent/prepare", &.{"/nonexistent/prepare"}, envp, .{}, "prepare.invoke"));
    try std.testing.expectEqual(linux.E.NOENT, exec_errno);
    try std.testing.expectEqualStrings("No such file or directory", strerror(exec_errno));
    // A directory is not executable.
    try std.testing.expectError(error.ExecFailed, runCommand(a, "/", &.{"/"}, envp, .{}, "prepare.invoke"));
    try std.testing.expectEqual(linux.E.ACCES, exec_errno);
}

test "a command killed by a signal has a shell exit status" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const a = arena_state.allocator();
    const envp: common.proc.Envp = &[_:null]?[*:0]const u8{};
    try std.testing.expectEqual(@as(u8, 128 + 15), try runCommand(a, "/bin/sh", &.{ "sh", "-c", "kill -TERM $$" }, envp, .{}, "child.signal"));
}

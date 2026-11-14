//! File-system ACL models for the test artifact (built with -Dtest-ops,
//! refused together with -Drelease; never in a released binary).
//!
//! The Python tool's engine tests patch the descriptor xattr calls with one
//! emulation per ACL model (tests/acl_fakes.py). The test artifact does the
//! same through zig-common's `atomic.ops`, and keeps the emulated attributes
//! per inode in a store file, so a test can set them before a run, change
//! them while the run is paused at a fault point, and read them afterwards.
//!
//! EB_STAGED_REBUILD_ACL_MODEL   gpfs-native, gpfs-views, nfs4-generic, posix-access
//! EB_STAGED_REBUILD_ACL_STORE   the store file: one "MAJOR:MINOR:INO NAME HEX" line per attribute
//! EB_STAGED_REBUILD_ACL_FAULT   op=NAME[,under=DIR][,suffix=S][,path=P][,nth=N],errno=E
//!                               one call fails with E: the Nth matching call, or every
//!                               matching call when N is 0 or absent
const std = @import("std");
const linux = std.os.linux;
const common = @import("zig_common");
const atomic = common.atomic;
const sys = common.sys;
const fsx = common.fsx;

const names = atomic.acl_attributes;
const GPFS: usize = 0;
const NFS4: usize = 1;
const POSIX: usize = 2;

const Attrs = [3]?[]const u8;

const Model = struct {
    native: usize,
    supported: [3]bool,
    views: [3]?[]const u8 = .{ null, null, null },
    chmod_rewrites: bool = true,
};

const Fault = struct {
    op: []const u8 = "",
    under: ?[]const u8 = null,
    suffix: ?[]const u8 = null,
    path: ?[]const u8 = null,
    nth: usize = 0,
    errno: linux.E = .SUCCESS,
    seen: usize = 0,
};

var arena_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
var model: Model = undefined;
var store_path: []const u8 = "";
var fault: ?Fault = null;

fn A() std.mem.Allocator {
    return arena_state.allocator();
}

fn models(label: []const u8) ?Model {
    if (std.mem.eql(u8, label, "gpfs-native")) return .{ .native = GPFS, .supported = .{ true, false, false } };
    if (std.mem.eql(u8, label, "gpfs-views")) return .{ .native = GPFS, .supported = .{ true, true, true }, .views = .{ null, "generic:", "posix-projection:" } };
    if (std.mem.eql(u8, label, "nfs4-generic")) return .{ .native = NFS4, .supported = .{ false, true, false } };
    if (std.mem.eql(u8, label, "posix-access")) return .{ .native = POSIX, .supported = .{ false, false, true }, .chmod_rewrites = false };
    return null;
}

fn parseErrno(s: []const u8) linux.E {
    inline for (.{ "ACCES", "PERM", "OPNOTSUPP", "IO", "INVAL", "NODATA" }) |n| {
        if (std.mem.eql(u8, s, n) or std.mem.eql(u8, s, "E" ++ n)) return @field(linux.E, n);
    }
    return .IO;
}

fn parseFault(spec: []const u8) Fault {
    var f: Fault = .{};
    var it = std.mem.splitScalar(u8, spec, ',');
    while (it.next()) |kv| {
        const eq = std.mem.findScalar(u8, kv, '=') orelse continue;
        const k = kv[0..eq];
        const v = kv[eq + 1 ..];
        if (std.mem.eql(u8, k, "op")) f.op = v;
        if (std.mem.eql(u8, k, "under")) f.under = v;
        if (std.mem.eql(u8, k, "suffix")) f.suffix = v;
        if (std.mem.eql(u8, k, "path")) f.path = v;
        if (std.mem.eql(u8, k, "nth")) f.nth = std.fmt.parseInt(usize, v, 10) catch 0;
        if (std.mem.eql(u8, k, "errno")) f.errno = parseErrno(v);
    }
    return f;
}

/// Install the model the environment names; without one, nothing changes.
pub fn installFromEnv(get: anytype) void {
    const label = get.get("EB_STAGED_REBUILD_ACL_MODEL") orelse return;
    model = models(label) orelse {
        common.cli.err("eb-staged-rebuild: unknown EB_STAGED_REBUILD_ACL_MODEL {s}\n", .{label});
        std.process.exit(2);
    };
    store_path = get.get("EB_STAGED_REBUILD_ACL_STORE") orelse {
        common.cli.err("eb-staged-rebuild: EB_STAGED_REBUILD_ACL_STORE is not set\n", .{});
        std.process.exit(2);
    };
    if (get.get("EB_STAGED_REBUILD_ACL_FAULT")) |spec| if (spec.len > 0) {
        fault = parseFault(spec);
    };
    atomic.ops.* = .{
        .flistxattr = mList,
        .fgetxattr = mGet,
        .fsetxattr = mSet,
        .fremovexattr = mRemove,
        .fchmod = mChmod,
        .created = mCreated,
    };
}

// ---- the store ---------------------------------------------------------------

const Store = std.StringArrayHashMapUnmanaged(Attrs);

fn load() Store {
    var s: Store = .empty;
    const data = blk: {
        const f = fsx.openRegular(fsx.cwd, store_path, false) catch break :blk "";
        defer sys.close(f.fd);
        break :blk fsx.readAll(A(), f.fd, 64 << 20) catch "";
    };
    var lines = std.mem.splitScalar(u8, data, '\n');
    while (lines.next()) |line| {
        var parts = std.mem.splitScalar(u8, line, ' ');
        const key = parts.next() orelse continue;
        const name = parts.next() orelse continue;
        const hex = parts.next() orelse continue;
        var idx: ?usize = null;
        for (names, 0..) |n, i| if (std.mem.eql(u8, n, name)) {
            idx = i;
        };
        const i = idx orelse continue;
        const value = A().alloc(u8, hex.len / 2) catch continue;
        _ = std.fmt.hexToBytes(value, hex) catch continue;
        const g = s.getOrPut(A(), A().dupe(u8, key) catch continue) catch continue;
        if (!g.found_existing) g.value_ptr.* = .{ null, null, null };
        g.value_ptr[i] = value;
    }
    return s;
}

fn save(s: Store) void {
    var out: std.ArrayList(u8) = .empty;
    for (s.keys(), s.values()) |key, at| for (at, 0..) |v, i| if (v) |bytes| {
        out.print(A(), "{s} {s} {x}\n", .{ key, names[i], bytes }) catch return;
    };
    const tmp = A().print("{s}.tmp", .{store_path}) catch return;
    const fd = sys.openat(fsx.cwd, tmp, .{ .ACCMODE = .WRONLY, .CREAT = true, .TRUNC = true }, 0o600) catch return;
    sys.writeAll(fd, out.items) catch {};
    sys.close(fd);
    sys.renameat(fsx.cwd, tmp, fsx.cwd, store_path) catch {};
}

fn keyOf(fd: sys.fd_t) []const u8 {
    const st = fsx.fstat(fd) catch return "?";
    // MAJOR:MINOR:INO, as the test's Python side writes it; st_dev encodes
    // the device differently in Python and in zig-common.
    return A().print("{d}:{d}:{d}", .{ st.dev >> 32, st.dev & 0xffffffff, st.ino }) catch "?";
}

fn attrsOf(s: *Store, fd: sys.fd_t) *Attrs {
    const g = s.getOrPut(A(), keyOf(fd)) catch unreachable;
    if (!g.found_existing) g.value_ptr.* = .{ null, null, null };
    return g.value_ptr;
}

fn writeNative(at: *Attrs, value: []const u8) void {
    at[model.native] = A().dupe(u8, value) catch unreachable;
    for (model.views, 0..) |v, i| if (v) |prefix| {
        at[i] = std.mem.concat(A(), u8, &.{ prefix, value }) catch unreachable;
    };
}

/// An attribute change updates ctime, as on a real file system.
fn touch(fd: sys.fd_t) void {
    const st = fsx.fstat(fd) catch return;
    _ = linux.fchmod(fd, @intCast(st.perm()));
}

fn err(e: linux.E) usize {
    return @bitCast(-@as(isize, @backingInt(e)));
}

fn fdPath(fd: sys.fd_t) []const u8 {
    var buf: [sys.path_max]u8 = undefined;
    const link = A().print("/proc/self/fd/{d}\x00", .{fd}) catch return "";
    const n = linux.readlinkat(linux.AT.FDCWD, @ptrCast(link.ptr), &buf, buf.len);
    if (linux.errno(n) != .SUCCESS) return "";
    return A().dupe(u8, buf[0..n]) catch "";
}

/// The errno the configured fault gives this call, or SUCCESS.
fn faultFor(op: []const u8, fd: sys.fd_t) linux.E {
    const f = &(fault orelse return .SUCCESS);
    if (!std.mem.eql(u8, f.op, op)) return .SUCCESS;
    const p = fdPath(fd);
    if (f.path) |want| if (!std.mem.eql(u8, p, want)) return .SUCCESS;
    if (f.under) |dir| if (!(p.len > dir.len and std.mem.startsWith(u8, p, dir) and p[dir.len] == '/')) return .SUCCESS;
    if (f.suffix) |sfx| if (!std.mem.endsWith(u8, p, sfx)) return .SUCCESS;
    f.seen += 1;
    if (f.nth != 0 and f.seen != f.nth) return .SUCCESS;
    return f.errno;
}

fn indexOf(name: [:0]const u8) usize {
    for (names, 0..) |n, i| if (std.mem.eql(u8, n, name)) return i;
    return POSIX;
}

// ---- the calls ---------------------------------------------------------------

fn mList(fd: sys.fd_t, buf: []u8) usize {
    const e = faultFor("listxattr", fd);
    if (e != .SUCCESS) return err(e);
    var s = load();
    var n: usize = 0;
    for (attrsOf(&s, fd).*, 0..) |v, i| if (v != null) {
        const name = names[i];
        if (n + name.len + 1 > buf.len) return err(.RANGE);
        @memcpy(buf[n .. n + name.len], name);
        buf[n + name.len] = 0;
        n += name.len + 1;
    };
    return n;
}

fn mGet(fd: sys.fd_t, name: [:0]const u8, buf: []u8) usize {
    const e = faultFor("getxattr", fd);
    if (e != .SUCCESS) return err(e);
    var s = load();
    const i = indexOf(name);
    if (attrsOf(&s, fd)[i]) |v| {
        if (v.len > buf.len) return err(.RANGE);
        @memcpy(buf[0..v.len], v);
        return v.len;
    }
    if (!model.supported[i]) return err(.OPNOTSUPP);
    return err(.NODATA);
}

fn mSet(fd: sys.fd_t, name: [:0]const u8, value: []const u8) usize {
    const e = faultFor("setxattr", fd);
    if (e != .SUCCESS) return err(e);
    const i = indexOf(name);
    if (!model.supported[i]) return err(.OPNOTSUPP);
    var s = load();
    const at = attrsOf(&s, fd);
    if (i == model.native) writeNative(at, value) else writeNative(at, std.mem.concat(A(), u8, &.{ "translated:", value }) catch unreachable);
    save(s);
    touch(fd);
    return 0;
}

fn mRemove(fd: sys.fd_t, name: [:0]const u8) usize {
    const e = faultFor("removexattr", fd);
    if (e != .SUCCESS) return err(e);
    const i = indexOf(name);
    var s = load();
    const at = attrsOf(&s, fd);
    if (!model.supported[i]) return err(.OPNOTSUPP);
    if (at[i] == null) return err(.NODATA);
    if (i == model.native) for (model.views, 0..) |v, j| if (v != null) {
        at[j] = null;
    };
    at[i] = null;
    save(s);
    touch(fd);
    return 0;
}

fn mChmod(fd: sys.fd_t, mode: u32) usize {
    const e = faultFor("fchmod", fd);
    if (e != .SUCCESS) return err(e);
    const rc = linux.fchmod(fd, @intCast(mode));
    var s = load();
    const at = attrsOf(&s, fd);
    if (model.chmod_rewrites and at[model.native] != null) {
        writeNative(at, A().print("chmod-regenerated:{o}", .{mode}) catch unreachable);
        save(s);
    }
    return rc;
}

/// A new temporary file starts with no attributes, whatever an earlier
/// file with the same inode number had.
fn mCreated(fd: sys.fd_t) void {
    var s = load();
    attrsOf(&s, fd).* = .{ null, null, null };
    save(s);
}

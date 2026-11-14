const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{ .default_target = .{ .abi = .musl } });
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseSafe });
    // Debug info carries absolute build paths; a release binary leaves it
    // out so two builds of one commit are byte-identical.
    const strip = b.option(bool, "strip", "Leave out debug info (default: on for release builds)") orelse (optimize != .debug);

    // The test artifact lets a test replace the ACL calls (zig/aclmodel.zig).
    // It is never a release: -Dtest-ops with -Drelease is refused, and the
    // artifact has its own name.
    const test_ops = b.option(bool, "test-ops", "Build the test artifact eb-staged-rebuild-test-ops (never a release)") orelse false;
    if (test_ops and optimize != .debug) {
        std.debug.print("-Dtest-ops builds a test artifact; it cannot be combined with -Drelease\n", .{});
        std.process.exit(1);
    }

    const common = b.dependency("zig_common", .{ .target = target, .optimize = optimize, .@"test-ops" = test_ops });
    const mod = b.createModule(.{
        .root_source_file = b.path("zig/main.zig"),
        .target = target,
        .optimize = optimize,
        .strip = strip,
        .imports = &.{.{ .name = "zig_common", .module = common.module("zig_common") }},
    });
    const exe = b.addExecutable(.{ .name = if (test_ops) "eb-staged-rebuild-test-ops" else "eb-staged-rebuild", .root_module = mod, .linkage = .static });
    b.installArtifact(exe);

    const unit = b.addTest(.{ .root_module = mod });
    b.step("test", "Run the Zig unit tests").dependOn(&b.addRunArtifact(unit).step);
}

const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{ .default_target = .{ .abi = .musl } });
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseSafe });
    // Debug info carries absolute build paths; a release binary leaves it
    // out so two builds of one commit are byte-identical.
    const strip = b.option(bool, "strip", "Leave out debug info (default: on for release builds)") orelse (optimize != .debug);

    const common = b.dependency("zig_common", .{ .target = target, .optimize = optimize });
    const mod = b.createModule(.{
        .root_source_file = b.path("zig/main.zig"),
        .target = target,
        .optimize = optimize,
        .strip = strip,
        .imports = &.{.{ .name = "zig_common", .module = common.module("zig_common") }},
    });
    const exe = b.addExecutable(.{ .name = "eb-staged-rebuild", .root_module = mod, .linkage = .static });
    b.installArtifact(exe);

    const unit = b.addTest(.{ .root_module = mod });
    b.step("test", "Run the Zig unit tests").dependOn(&b.addRunArtifact(unit).step);
}

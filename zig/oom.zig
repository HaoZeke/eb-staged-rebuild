//! Out of memory is fatal, as Python's MemoryError is: the program stops
//! with status 1 rather than write a journal, record or line that lost a
//! field because an append failed.
const std = @import("std");
const cli = @import("zig_common").cli;

pub fn oom() noreturn {
    cli.err("eb-staged-rebuild: out of memory\n", .{});
    std.process.exit(1);
}

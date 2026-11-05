# Design

EasyBuild `--rebuild` and `--force` empty a live install while a build is still running. A later atomic rename of that same directory only shortens the empty window. It does not remove it, and it does not stop a job from seeing a mixture of two generations. This project does not publish by renaming `software/<name>/<version>`.

## What EasyBuild does to an existing install

Read from EasyBuild 5.4.2 (`easybuild/framework/easyblock.py`):

* `make_installdir` calls `make_dir` with `cleanupoldinstall` and `clean_instead_of_remove=True`.
* If the install directory exists and `cleanupoldinstall` is true, `clean_dir` empties it. If `cleanupoldinstall` is false, `make_dir` first copies the directory to `<installdir>.<timestamp>` and then still calls `clean_dir`. The copy is a backup. The live names are removed either way.
* An easyblock that sets `build_in_installdir` forces `cleanupoldbuild` on and `cleanupoldinstall` off, then cleans the install directory at the start of `make_builddir`, because the build directory is the install directory.
* `Binary.install_step` calls `clean_dir(self.installdir)` before copying files. `Tarball.install_step` does the same for the default install type.

`self.installdir` is `installpath-software` plus the naming scheme's install subdirectory (`gen_installdir`). It is not "the directory that currently exists under some other prefix". A build whose `--installpath-software` is a new directory does not call `clean_dir` on the old one.

`--remove-ghost-install-dirs` is different. `remove_module_file` deletes the install directory parsed out of a module file that already exists at the path EasyBuild is about to rewrite. The option is off by default. One local run set it true in a config file (`eb --show-config` reported `remove-ghost-install-dirs (F) = True`) and rebuilt with a fresh `--installpath-modules`. The previous install directory was still there when `eb` exited 0. That matches the code: the module file in the fresh module tree did not exist, so `remove_dir` was not called. A later run can see a module file left in the generation tree. For `--bwrap` that file names the live directory. This tool deletes `<generation>/modules` before each `eb` invocation, and refuses to do so if that path is a symlink, so a resumed build does not hand EasyBuild a module file that points at the live install.

## What was checked, and what it showed

These are checks of EasyBuild itself, before this tool's publish step existed. The easyconfig was a `Binary` easyblock that installs one local file (`stagedemo/1`). EasyBuild was 5.4.2. The filesystem was ext4 on `/dev/vdc`. A thread sampled the live file every 5 ms.

* `eb --installpath-software=<other>/software --installpath-modules=<other>/modules --rebuild` left the live directory's inode at 422688, left the file contents as `payload-v1`, and wrote a module whose `local root` was the other directory. The sampler reported 2959 samples and 0 failures.
* `eb --experimental --bwrap --bwrap-installpath=<stage> --rebuild` left the same live file in place (7703 samples, 0 failures). The staged module file's `local root` was the live path, because inside the bind mount that is the path EasyBuild sees. `bwrap_info.json` contained `bwrap_installpath_software` and `bwrap_installpath_modules`. The staged payload was `payload-v2`.

`easybuild/tools/bwrap.py` at the 5.3.0 and 5.3.1 tags does not write those two keys. The stage software directory in that file is `<bwrap_installpath>/software` and the stage module directory is `<bwrap_installpath>/modules`. The 5.4.0 and 5.4.2 tags do write the keys. 5.4.1 was not read. A missing key is not guessed except for 5.3.0 and 5.3.1, where the layout above is what the source joins.

Bubblewrap on this machine is 0.9.0. `bwrap --dev-bind / / true` exits 0. Binding a directory onto a symlink exited 1:

```
bwrap: Can't bind mount .../stage on .../live: Unable to mount source on destination: No such file or directory
```

That is not the message bubblewrap 0.12 prints, and it is not evidence about RHEL 9's 0.6.3, which the notes say follows the symlink. The tool refuses a symlink at `software/<name>/<version>` before it runs `eb`, whichever bubblewrap is installed.

`renameat2(RENAME_EXCHANGE)` on this ext4 returned 0 and exchanged two directories. `/proc/sys/user/max_user_namespaces` was 192727. Neither number is a measurement of the site's GPFS or of a host where the STIG sets the sysctl to 0.

## Designs

### A. Repair the two-rename switch

Keep the prototype: build with `--bwrap`, then `renameat2(RENAME_EXCHANGE)` or two renames. Add a journal so a failed second rename puts the live directory back.

Rejected as the way a generation becomes visible. Between the two renames the live path does not exist; a crash there leaves it missing, which is the bug the review reproduced. `RENAME_EXCHANGE` is not part of the published GPFS interface the notes cite, and Lustre and Linux NFS return `EINVAL` for rename flags. A journal can heal that after the fact. It cannot make the missing name impossible. After a successful exchange the path is the same path jobs already have open, so a job can still read one file from the old generation and the next file from the new one (ns-054).

### B. A symlink at `software/<name>/<version>`

Point the stable path at the new generation with `symlink` plus `rename`.

Rejected. The path would no longer be a real directory. Bubblewrap 0.12 refuses to mount on a symlink; 0.6.3 follows one; 0.9.0 on this host failed the mount. Tools that store `realpath` keep a path that dangles when that generation is removed. Spack dropped symlinked views for this reason (the notes, ns-052).

### C. Generation directories, and the module file as the pointer

Install into a directory that has never been the live install. Leave `software/<name>/<version>` where it is, as a real directory. Write the new module file to a temporary name in the module file's own directory, `fsync` it, and `os.replace` it onto the live module file. Same-directory `rename` over an existing file is the operation GPFS, Lustre, and NFS are being asked to do. It does not require `RENAME_EXCHANGE`.

A job that already loaded the module keeps the old paths in its environment. A job that loads the module after the rename sees one complete module file, not a partial write. The old generation stays on disk until a grace period has passed and nothing this tool publishes still resolves to it.

This is the design that was built. `--bwrap` remains an optional way to run the build. It is not the publish step. The publish step is the same module-file replace. Hooks run inside `eb`, and inside the sandbox when `--bwrap` is used, so they are not where the replace happens.

EasyBuild does not need `--bwrap` to produce the generation directory. `--installpath-software` and `--installpath-modules` are enough, and they are the default backend. `--rebuild` is added when the caller did not pass `--rebuild` or `--force`, so an existing module does not cause `eb` to skip. `--prefix`, `--installpath`, `--bwrap*`, and `--remove-ghost-install-dirs` are stripped from the arguments this tool forwards.

### D. Never-reused paths for every file a job might open

Nix store paths, Spack hashes, or a per-job image. ns-054 is right that only a path which is never reused avoids a mixed view for every opener, including a process that does not go through the module file.

Not built. It is a different install layout, and it does not fix a binary whose `DT_RPATH` already contains the old directory. Pretending a module-file update does that would be false.

## What is rejected

* Publishing by exchanging or double-renaming `software/<name>/<version>`.
* A symlink at that path.
* `cleanupoldinstall=False` as protection. The 5.4.2 `make_dir` still empties the directory after copying it aside.
* Switching from an EasyBuild hook. The hook runs in the build, which is inside the sandbox when `--bwrap` is on.
* `RENAME_EXCHANGE` as the correctness mechanism. It is probed and recorded. The publish path does not call it.
* Deleting a generation because `lsof` on this node sees no users. Other nodes are invisible. Deletion waits for a grace period, and only for directories under `<state-root>/generations/`.
* Guessing `bwrap_installpath_software` for an EasyBuild version whose `bwrap.py` was not read. 5.3.0 and 5.3.1 use the layout in their source. Anything else without the key is left unpublished.
* Falling back from a failed `bwrap` probe to an in-place rebuild. The installpath backend does not need bubblewrap. The bwrap backend stops.

## Residual risk

* A module file replace is atomic for readers of that file. It is not atomic across several module files. A crash in the middle of a multi-module publish can leave one module on the new generation and another on the old. Each file still resolves to one complete generation. Dependencies are published first. `resume` finishes the list; `rollback` restores it in reverse.
* Jobs, scripts, and `DT_RPATH` entries that do not read the module file keep the old directory. They do not see an empty prefix, and they do not see the rebuild. A security fix in a library does not move into a dependent until that dependent is rebuilt. `gc` does not search those binaries or home directories. It will not delete `software/<name>/<version>` at all. It deletes a generation under `<state-root>/generations/` only when no module file this tool tracks still resolves inside it, a later publish or a rollback recorded a time, and `--grace-hours` has passed. The default 144 is the figure from the notes (120 hours plus a day). It is not a measured site walltime.
* A local `flock` gate precedes a POSIX record lock on a distinct sidecar. The gate excludes separate acquirers in one process; the sidecar excludes nodes only when the filesystem provides coherent POSIX locks. Other code in the process must never open the sidecar, since closing any descriptor for its inode drops that process's record locks. Every node must use this protocol: a flock-only version on another node does not participate. Drain those holders before upgrading. Never unlink either lock file while the state root is active. Cross-node behavior and failed-node recovery must be measured on the target filesystem (`open-004`).
* Same-directory `rename` is what POSIX requires to be atomic. This repository has not measured it on the site's GPFS, Lustre, or NFS.
* An EasyBuild configuration file can still enable `remove-ghost-install-dirs`. The one run described above did not delete the live tree. The staged module tree is removed before `eb` so a resume does not recreate the condition the option acts on. That is not a test of every EasyBuild configuration.
* The original `software/<name>/<version>` directory remains forever unless an operator removes it by hand. Disk use grows with every generation that is still inside the grace period, and with that original directory after it is no longer the published root.

## Retiring a copy that moved elsewhere

A package can be installed in this tree and later published somewhere that sits further along `MODULEPATH`, such as a CVMFS repository. Once that copy is verified, this tree's module file must stop being what users load, and running jobs must keep their software directory.

`retire` does this with the publish primitive. The module file is fsynced and moved with one `os.replace` to `<state-root>/retired/<timestamp>/<absolute path without the leading />`, then both directories are fsynced. A reader sees the module file or does not; a job that already loaded it keeps its paths. The software directory is not touched. A rename across filesystems is not atomic, so retire checks `st_dev` of the module directory against the destination and refuses with an error naming both when they differ.

The record is a journal under `runs/retire-<timestamp>/` in the run journal format, with `kind` set to `retire`, the module path, the module hash, the software directory parsed from the module's root assignment, the reason, and the times. It is written as `retiring` before the rename and `retired` after it, so a crash between the two leaves a state `rollback` can resolve. A module file without a root assignment is refused, because the record would not know which directory it released. Retire also refuses a module path that is not a regular file (a symlink, a directory, a missing path), one already retired, and one whose state root lock is held (`--lock-timeout` defaults to 0, so it refuses at once).

`rollback retire-<timestamp>` moves the file back with the same primitive and journals `rolled_back`. It refuses to overwrite a module file that exists again at the original path.

`gc` starts the grace clock at `retired_at`. The software directory is eligible only when `--grace-hours` have passed and no module file this tool tracks resolves inside it. Tracked files are the live modules of publish runs and the original path of any retire that was rolled back or interrupted. `gc` deletes only under `<state-root>/generations/`, and the retired software directory is normally the original `software/<name>/<version>` tree, outside it. So `gc` does not delete it. It reports the directory as `manual` with "ready to remove by hand after <time>", and as `keep` before that time. The one case `gc` deletes is a retired module whose software directory lies inside a generation: the retire time counts as that generation's retirement time, and the generation is removed with the usual rules. Widening what `gc` may delete to include the original install trees would let one wrong journal entry remove a directory a tool outside this one still loads, and nothing here can see those users.

## open-004

Still open. This machine is not a build node on the shared filesystem, and it is not the CI account. The probe reports bubblewrap, `user.max_user_namespaces`, `RENAME_EXCHANGE` on the directory you point it at, and the EasyBuild version it can see. Those answers are about the host that ran the probe.

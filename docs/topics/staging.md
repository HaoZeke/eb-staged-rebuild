# Staging model

A run id names one generation. The directory is `<state-root>/generations/<run-id>`. EasyBuild writes that generation. The live tree `software/<name>/<version>` is a real directory that already exists, and it keeps that name. Publication moves the finished generation beside it and exchanges the two directories. The module file is replaced afterwards and still names the stable path.

```{graphviz}
digraph staging {
  graph [rankdir=TB, fontsize=12, labelloc=t, label="Generation, exchange, module publication"];
  node [shape=box, style="rounded", fontsize=11];
  edge [fontsize=10];

  ec [label="easyconfig\nafter --"];
  gen [label="generations/<run-id>/\nsoftware/Name/version\nmodules/..."];
  hidden [label="software/Name/.version.<run-id>\nhidden sibling"];
  live [label="software/Name/version\nstable name"];
  mod [label="module file\nroot = stable path"];

  ec -> gen [label="eb writes a\ncomplete generation"];
  gen -> hidden [label="rename onto the\nlive filesystem"];
  hidden -> live [label="renameat2\nRENAME_EXCHANGE"];
  live -> mod [label="os.replace\nafter every exchange"];
}
```

After a successful exchange the stable path names the new tree and the hidden sibling names the previous tree. `rollback` exchanges them again. The diagram's exchange edge is `renameat2(RENAME_EXCHANGE)` when the probe stored in the journal says the filesystem supports it. The ordinary-rename fallback is {doc}`publication`.

## The generation directory

`installpath` and `userns` write `<generation>/software` and `<generation>/modules`. The bubblewrap backend passes `--bwrap-installpath=<generation>` and reads `bwrap_info.json` for the stage paths. Those paths must stay inside the generation directory. A bubblewrap command that used overlayfs is refused: the stage would be an upper layer, and it is left unpublished.

The generation is complete when the staged install directory contains build evidence: a regular file named `PAYLOAD` (the fault-injection builders and `docs/examples/eb-standin` write one) or a readable `easybuild/*.eb` (EasyBuild copies the easyconfig in at the end of a build). The tool then writes `.eb-staged-rebuild-complete` into that directory, through a short writable window if the top directory is sealed. The stamp records the run id. It is how later recovery tells the new tree from the old one. The stamp alone is not build evidence: a preparation program that empties the tree leaves the stamp, and publication stops.

On the recorded `demo-prepare` run the staged path and the context field `generation` were both:

```text
/tmp/ebsr-docs-prefix/.eb-staged-rebuild/generations/demo-prepare/software/demo/1
```

## The stable name and the hidden sibling

The planner requires a real, non-symlink `Name/version` directory. It rewrites the staged module so `root` is that stable path. It then renames the staged install to:

```text
software/<name>/.<version>.<run-id>
```

For `software/demo/1` and run id `demo-run` the sibling is `software/demo/.1.demo-run`. The rename stays on one filesystem because a directory rename rewrites the directory's `..` entry and `rename` across devices fails with `EXDEV`. A sealed top directory is made owner-writable for that rename and receives its previous mode at the new name.

`renameat2` with `RENAME_EXCHANGE` (`1 << 1` in `include/uapi/linux/fs.h`) then swaps the sibling with `software/<name>/<version>`. Both names exist throughout the call. The stable path's inode changes. The file bytes at that path are the new generation. Processes that already have the old files open keep those file descriptions; the old directory is the hidden sibling.

## Module publication

Module files are published after every directory in the plan has been exchanged. The new text is written under `<state-root>/runs/<run-id>/prepared/` and installed with a same-directory replace after the file and the directory are fsynced. The recorded run's module still read:

```text
local root = "/tmp/ebsr-docs-prefix/software/demo/1"
```

`resolve` reports that path and whether the directory looks complete (`PAYLOAD`, `easybuild/*.eb`, or the run stamp).

A build with one module directory is one exchange plus one module replace. A build with several module directories exchanges every directory before any module file is replaced. Other users do not take this tool's advisory lock, so they can observe a mixed set while a multi-module transaction is in flight. See {doc}`publication` and {doc}`journal`.

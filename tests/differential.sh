#!/bin/bash
# Run the Python tool (python3 -m eb_staged_rebuild) and the Zig build
# (TOOL_BIN) on the same invocations and compare exit code, stdout and
# stderr; for commands that change a fixture, also the journals and the
# tree they leave.
#   TOOL_BIN=zig-out/bin/eb-staged-rebuild bash tests/differential.sh
set -u
: "${TOOL_BIN:?}"
repo_root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/esr-diff.XXXXXX")
trap 'chmod -R u+rwX "${work}" 2>/dev/null; rm -rf -- "${work}"' EXIT
diffs=0

compare() {
    local label=$1; shift
    (cd "${work}" && PYTHONPATH="${repo_root}" python3 -m eb_staged_rebuild "$@") > "${work}/o.out" 2> "${work}/o.err"; local oc=$?
    (cd "${work}" && "${TOOL_BIN}" "$@") > "${work}/n.out" 2> "${work}/n.err"; local nc=$?
    if [ "${oc}" != "${nc}" ] || ! cmp -s "${work}/o.out" "${work}/n.out" || ! cmp -s "${work}/o.err" "${work}/n.err"; then
        echo "DIFF ${label}: exit ${oc} vs ${nc}"
        diff "${work}/o.out" "${work}/n.out" | head -20 | sed 's/^/     out /'
        diff "${work}/o.err" "${work}/n.err" | head -20 | sed 's/^/     err /'
        diffs=$((diffs + 1))
    else
        echo "same ${label}"
    fi
}

# Module files.
m="${work}/modules"
s="${work}/software"
mkdir -p "${m}" "${s}/done/1/easybuild" "${s}/stamp/1" "${s}/payload/1" "${s}/empty/1" "${s}/ebdir/1/easybuild/x.eb"
touch "${s}/done/1/easybuild/done-1.eb" "${s}/stamp/1/.eb-staged-rebuild-complete" "${s}/payload/1/PAYLOAD"
printf 'local root = "%s/done/1"\nsetenv("EBROOTDONE", root)\n' "${s}" > "${m}/lua.lua"
printf 'set root %s/stamp/1\n' "${s}" > "${m}/tcl"
printf 'set root "%s/payload/1"\n' "${s}" > "${m}/tclq"
printf 'local root = "%s/empty/1"\n' "${s}" > "${m}/empty.lua"
printf 'local root = "%s/ebdir/1"\n' "${s}" > "${m}/ebdir.lua"
printf 'local root = "%s/missing/1"\n' "${s}" > "${m}/missing.lua"
printf 'whatis("no root")\n' > "${m}/noroot.lua"
printf 'local root\n=\n"%s/done/1"\n' "${s}" > "${m}/spread.lua"
printf 'local root = "a\\"b\\u00e9"\n' > "${m}/escaped.lua"
printf 'local root = "/tmp/\xc3\xa9t\xc3\xa9"\n' > "${m}/utf8.lua"
ln -s "${m}/lua.lua" "${m}/link.lua"
ln -s "${s}/done/1" "${s}/linkroot"
printf 'local root = "%s/linkroot"\n' "${s}" > "${m}/linkroot.lua"
for f in lua.lua tcl tclq empty.lua ebdir.lua missing.lua noroot.lua spread.lua escaped.lua utf8.lua link.lua linkroot.lua; do
    compare "resolve ${f}" resolve "${m}/${f}"
done
compare "resolve a directory" resolve "${m}"
compare "resolve nothing there" resolve "${m}/nope.lua"
compare "resolve relative" resolve modules/lua.lua
compare "resolve, no argument" resolve
compare "resolve, two arguments" resolve a b
compare "resolve -h" resolve -h
if [ "$(id -u)" != 0 ]; then
    cp "${m}/lua.lua" "${m}/unreadable.lua"; chmod 0 "${m}/unreadable.lua"
    compare "resolve an unreadable file" resolve "${m}/unreadable.lua"
    rm -f "${m}/unreadable.lua"
fi

# Journals.
st="${work}/state"
mkdir -p "${st}/runs/r1" "${st}/runs/r2" "${st}/runs/r3" "${st}/runs/r0"
cat > "${st}/runs/r1/journal.json" <<'EOF'
{"schema": 2, "run_id": "r1", "state": "done", "backend": "bwrap", "message": "all published",
 "modules_plan": [
  {"rel": "demo/1", "status": "published", "live_software": "/sw/demo/1", "retired": "/sw/demo/.1.old"},
  {"rel": "other/2", "status": "pending", "live_software": "/sw/other/2", "retired": null}
 ]}
EOF
cat > "${st}/runs/r2/journal.json" <<'EOF'
{"schema": 2, "kind": "retire", "run_id": "r2", "state": "retired", "module_path": "/m/x/1.lua", "software_dir": "/sw/x/1"}
EOF
cat > "${st}/runs/r3/journal.json" <<'EOF'
{"schema": 4, "publication": "directory-swap", "ledger_policy": {"mode": "x"}, "run_id": "r3", "state": 7, "backend": null, "modules_plan": [], "message": ""}
EOF
cat > "${st}/runs/r0/journal.json" <<'EOF'
{"schema": 2, "run_id": "r0", "state": "planned", "backend": "userns", "message": ["a", 1, null, 1.5, true], "modules_plan": [{"rel": "z/9", "status": "built", "live_software": "/x", "retired": "/y é"}]}
EOF
compare "status, four journals" status --state-root "${st}"
compare "status r3" status --state-root "${st}" r3
compare "status, no such run" status --state-root "${st}" r9
compare "status, no runs dir" status --state-root "${work}/nostate"
compare "status, abbreviated option" status --state "${st}"
compare "status, no option" status
mkdir -p "${st}/runs/r4"; echo '{"schema": 1}' > "${st}/runs/r4/journal.json"
compare "status, schema 1" status --state-root "${st}"
echo '{"schema": 5}' > "${st}/runs/r4/journal.json"
compare "status, schema 5" status --state-root "${st}"
echo '{"schema": 2, "publication": "pointer"}' > "${st}/runs/r4/journal.json"
compare "status, other publication" status --state-root "${st}"
echo '{"schema": 2, "modules_plan": [{"rel": "a"}]}' > "${st}/runs/r4/journal.json"
compare "status, plan without a swap" status --state-root "${st}"
rm -rf "${st}/runs/r4"

# Probe.
compare "probe --json" probe --path "${work}/p" --json --eb missing-eb
compare "probe text" probe --path "${work}/p" --eb missing-eb
compare "probe -h" probe -h
compare "probe --path without a value" probe --path
compare "top level, nothing"
compare "top level --help" --help
compare "--version" --version
for c in run resume rollback status gc retire; do compare "${c} -h" "${c}" -h; done
# The version the binary reports is the one build.zig.zon declares.
zon_version=$(sed -nE 's/^ *\.version = "([^"]+)".*/\1/p' "${repo_root}/build.zig.zon")
if "${TOOL_BIN}" --version | grep -qF -- "${zon_version}"; then echo "same version as build.zig.zon"; else echo "DIFF version: build.zig.zon says ${zon_version}"; diffs=$((diffs + 1)); fi
compare "unknown command" bogus

# Commands that change a fixture: each implementation gets its own copy,
# and run ids and timestamps are normalized before the comparison.
norm() {
    sed -E -e 's/[0-9]{8}T[0-9]{6}Z(-[0-9]+)?/STAMP/g' \
        -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z/ISO/g' \
        -e 's/[0-9]{10}\.[0-9]+/TIME/g' -e "s#${work}/(py|zig)#W#g" \
        -e 's#[^ ]*python[0-9.]* -m eb_staged_rebuild\.userns_sandbox#SANDBOX#' \
        -e "s#${TOOL_BIN} __userns-sandbox#SANDBOX#" |
    if [ -n "${NORM_HASH:-}" ]; then sed -E -e 's/[0-9a-f]{64}/HASH/g' -e 's#eb-staged-rebuild/[0-9a-f]{16}/#eb-staged-rebuild/SCOPE/#g'; else cat; fi |
    # The sandbox command in a journal's eb_command list, one item per line.
    perl -0pe 's/"[^"\n]*python[0-9.]*",\n\s*"-m",\n\s*"eb_staged_rebuild\.userns_sandbox",/"SANDBOX",/g; s/"\Q$ENV{TOOL_BIN}\E",\n\s*"__userns-sandbox",/"SANDBOX",/g' 
}
fixture() {
    local root=$1
    mkdir -p "${root}/modules/all/demo" "${root}/software/demo/1/easybuild" "${root}/state/runs/r1"
    touch "${root}/software/demo/1/easybuild/demo-1.eb"
    # A relative root, so both copies of the module file have the same bytes
    # and the same hash; each implementation runs from its own copy.
    printf 'local root = "software/demo/1"\n' > "${root}/modules/all/demo/1.lua"
    mkdir -p "${root}/software/old/.2.0001/sub" "${root}/software/old/2"
    touch "${root}/software/old/.2.0001/sub/file"
    printf '{"schema": 2, "run_id": "r1", "state": "complete", "backend": "bwrap", "modules_plan": [{"rel": "old/2", "status": "published", "live_software": "%s/software/old/2", "retired": "%s/software/old/.2.0001", "published_at": 1000.5}]}\n' "${root}" "${root}" > "${root}/state/runs/r1/journal.json"
    printf '#!/bin/sh\nexec python3 "%s/tests/fake_eb.py" "$@"\n' "${repo_root}" > "${root}/eb"
    chmod +x "${root}/eb"
    printf '#!/bin/sh\nexec python3 "%s/tests/fake_eb_stack.py" "$@"\n' "${repo_root}" > "${root}/ebstack"
    chmod +x "${root}/ebstack"
    printf '{"eb_stack": "%s/ebstack", "log": "%s/ledger.jsonl", "key": "%s/key.pem", "head": "%s/head.json", "actor": "ci", "stack": "s", "target": "t"}\n' "${root}" "${root}" "${root}" "${root}" > "${root}/ledger.json"
    mkdir -p "${root}/bev/log" "${root}/bev/keys"
    printf 'fake key\n' > "${root}/bev/keys/key.pem"
    printf 'name = "demo"\n' > "${root}/bev/demo-1.eb"
    printf '{"eb_stack": "%s/ebstack", "log": "%s/bev/log/ledger.jsonl", "key": "%s/bev/keys/key.pem", "outbox": "%s/bev/outbox"}\n' "${root}" "${root}" "${root}" "${root}" > "${root}/bev/config.json"
    for op in ok again fails missing pend; do
        printf '{"operation": "%s", "actor": "ci", "stack": "s", "module": "demo/1", "location": "%s/software/demo/1", "target": {"cpu": "x86_64/generic"}, "easyconfig": {"path": "%s/bev/demo-1.eb"}}\n' "${op}" "${root}" "${root}" > "${root}/bev/${op}.json"
    done
    printf '{"operation": "bad op", "actor": "ci"}\n' > "${root}/bev/bad.json"
    # Refusal fixtures: a symlinked rebuild target, a symlinked state root,
    # a symlinked module file, a module file without a root assignment, and
    # a second module whose root names a hidden tree through a symlink.
    mkdir -p "${root}/software/real/1/easybuild" "${root}/software/link" "${root}/modules/all/link"
    ln -s ../real/1 "${root}/software/link/1"
    printf 'local root = "software/link/1"\n' > "${root}/modules/all/link/1.lua"
    ln -s state "${root}/statelink"
    ln -s 1.lua "${root}/modules/all/demo/symlink.lua"
    printf 'whatis("no root")\n' > "${root}/modules/all/demo/noroot.lua"
    mkdir -p "${root}/software/kept/.3.0001" "${root}/software/kept/3" "${root}/modules/all/kept" "${root}/state/runs/r9"
    ln -s .3.0001 "${root}/software/kept/alias"
    printf 'local root = "%s/software/kept/alias"\n' "${root}" > "${root}/modules/all/kept/3.lua"
    printf '{"schema": 2, "run_id": "r9", "state": "complete", "backend": "bwrap", "modules_plan": [{"rel": "kept/3", "status": "published", "live_software": "%s/software/kept/3", "retired": "%s/software/kept/.3.0001", "live_module": "%s/modules/all/kept/3.lua", "published_at": 1000.5}]}\n' "${root}" "${root}" "${root}" > "${root}/state/runs/r9/journal.json"
    # Publication policies for --prepare.
    printf '#!/bin/sh\nexit 42\n' > "${root}/prepare-fails"
    printf '#!/bin/sh\ncount=0\n[ -f "%s/prepare-count" ] && count=$(cat "%s/prepare-count")\necho $((count + 1)) > "%s/prepare-count"\nexit 0\n' "${root}" "${root}" "${root}" > "${root}/prepare-ok"
    chmod +x "${root}/prepare-fails" "${root}/prepare-ok"
    # A bwrap that answers the probe: EasyBuild runs the sandbox itself, so
    # the tool only asks for the version and runs "--dev-bind / / true".
    mkdir -p "${root}/bin"
    printf '#!/bin/sh\nif [ "$1" = --version ]; then echo "bubblewrap 0.11.0"; exit 0; fi\nif [ -n "${FAKE_BWRAP_FAIL:-}" ]; then echo "setting up uid map: Permission denied (user namespaces disabled)" >&2; exit 1; fi\nexit 0\n' > "${root}/bin/bwrap"
    chmod +x "${root}/bin/bwrap"
    # unshare: the real one, or a refusal when FAKE_UNSHARE_FAIL is set, so
    # auto can find no sandbox at all.
    printf '#!/bin/sh\nif [ -n "${FAKE_UNSHARE_FAIL:-}" ]; then echo "unshare: unshare failed: Operation not permitted" >&2; exit 1; fi\nexec %s "$@"\n' "$(command -v unshare)" > "${root}/bin/unshare"
    chmod +x "${root}/bin/unshare"
    # Two modules for the publication barrier, and policies that see their
    # context: two remove the second generation's PAYLOAD and then disable
    # or make unreadable its easybuild directory, the remaining evidence;
    # one keeps a copy of the context and restricts each staged tree.
    for mod in alpha beta; do
        mkdir -p "${root}/software/${mod}/1/easybuild" "${root}/modules/all/${mod}"
        touch "${root}/software/${mod}/1/easybuild/${mod}-1.eb"
        printf 'local root = "software/%s/1"\n' "${mod}" > "${root}/modules/all/${mod}/1.lua"
    done
    printf '#!%s\nimport json, os, sys\nfor m in json.load(open(sys.argv[1]))["modules"]:\n    if m["rel"] == "all/beta/1.lua":\n        os.unlink(os.path.join(m["generation"], "PAYLOAD"))\n        os.rename(os.path.join(m["generation"], "easybuild"), os.path.join(m["generation"], "disabled-metadata"))\n' "$(command -v python3)" > "${root}/prepare-barrier-rename"
    printf '#!%s\nimport json, os, sys\nfor m in json.load(open(sys.argv[1]))["modules"]:\n    if m["rel"] == "all/beta/1.lua":\n        os.unlink(os.path.join(m["generation"], "PAYLOAD"))\n        os.chmod(os.path.join(m["generation"], "easybuild"), 0)\n' "$(command -v python3)" > "${root}/prepare-barrier-chmod"
    printf '#!%s\nimport json, os, shutil, sys\nshutil.copyfile(sys.argv[1], "%s/context.json")\nfor m in json.load(open(sys.argv[1]))["modules"]:\n    os.chmod(m["staged"], 0o750)\n' "$(command -v python3)" "${root}" > "${root}/prepare-context"
    chmod +x "${root}/prepare-barrier-rename" "${root}/prepare-barrier-chmod" "${root}/prepare-context"
}
compare_admin() {
    local label=$1; shift
    local impl root out a f bad=0
    for impl in py zig; do
        root="${work}/${impl}"
        out="${work}/${impl}.admin"
        local -a cmd args=()
        local rid
        if [ "${impl}" = py ]; then cmd=(env PYTHONPATH="${repo_root}" python3 -m eb_staged_rebuild); else cmd=("${TOOL_BIN}"); fi
        # The build-events entry point is its own module in the Python tool.
        local bev=0
        for a in "$@"; do [ "${a}" = build-events ] && bev=1; done
        if [ "${bev}" = 1 ] && [ "${impl}" = py ]; then cmd=(env PYTHONPATH="${repo_root}" python3 -m eb_staged_rebuild.build_events); fi
        # The retire record the last successful retire created. Names sort
        # by the second only, so two retires in one second do not order by
        # name the same way in both copies.
        rid=$(cat "${root}/.last-retire" 2>/dev/null)
        local -a before=() after=()
        local d
        for d in "${root}"/state/runs/retire-*; do [ -e "${d}" ] && before+=("${d##*/}"); done
        for a in "$@"; do
            if [ "${a}" = build-events ] && [ "${impl}" = py ]; then continue; fi
            a="${a//@ROOT@/${root}}"
            if [ "${a}" = "@CMD@" ]; then args+=("${cmd[@]}"); else args+=("${a//@RETIRE@/${rid}}"); fi
        done
        if [ "${args[0]}" = env ]; then
            (cd "${root}" && "${args[@]}") > "${out}.out" 2> "${out}.err"
        else
            (cd "${root}" && "${cmd[@]}" "${args[@]}") > "${out}.out" 2> "${out}.err"
        fi
        echo $? > "${out}.rc"
        for d in "${root}"/state/runs/retire-*; do
            [ -e "${d}" ] || continue
            [[ " ${before[*]} " == *" ${d##*/} "* ]] || after+=("${d##*/}")
        done
        [ "${#after[@]}" -gt 0 ] && echo "${after[-1]}" > "${root}/.last-retire"
        # Ordered by run directory: by path, "retire-STAMP-2/journal.json"
        # sorts before "retire-STAMP/journal.json".
        find "${root}" -name journal.json -printf '%h\0' 2>/dev/null | LC_ALL=C sort -z |
            while IFS= read -r -d '' d; do cat "${d}/journal.json"; done > "${out}.journals"
        (cd "${root}" && find . -not -name '.lock*' -not -name .last-retire 2>/dev/null) > "${out}.tree"
        for f in out err journals; do norm < "${out}.${f}" > "${out}.${f}.n"; done
        # Sorted after normalizing, so names that differ only in a stamp
        # compare by what is left.
        norm < "${out}.tree" | LC_ALL=C sort > "${out}.tree.n"
    done
    for f in rc out.n err.n journals.n tree.n; do
        if ! cmp -s "${work}/py.admin.${f}" "${work}/zig.admin.${f}"; then
            [ "${bad}" -eq 0 ] && echo "DIFF ${label}"
            bad=1
            diff "${work}/py.admin.${f}" "${work}/zig.admin.${f}" | head -20 | sed "s/^/     ${f} /"
        fi
    done
    if [ "${bad}" -eq 0 ]; then echo "same ${label}"; else diffs=$((diffs + 1)); fi
}
fixture "${work}/py"
fixture "${work}/zig"
compare_admin "retire --dry-run" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua --dry-run
compare_admin "retire" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua --reason moved
compare_admin "retire again" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua
compare_admin "status after retire" status --state-root @ROOT@/state
compare_admin "gc keeps within grace" gc --state-root @ROOT@/state --dry-run
if [ "$(id -u)" != 0 ]; then
    for impl in py zig; do chmod 0 "${work}/${impl}/modules/all/kept"; done
    compare_admin "gc, an unreadable live module" gc --state-root @ROOT@/state --grace-hours 0
    for impl in py zig; do chmod 755 "${work}/${impl}/modules/all/kept"; done
fi
compare_admin "gc removes past grace" gc --state-root @ROOT@/state --grace-hours 0
compare_admin "gc again" gc --state-root @ROOT@/state --grace-hours 0
compare_admin "rollback the retire" rollback --state-root @ROOT@/state @RETIRE@
compare_admin "rollback again" rollback --state-root @ROOT@/state @RETIRE@
compare_admin "run --dry-run, auto" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --dry-run --module demo/1 -- demo-1.eb
compare_admin "run --dry-run, installpath" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --dry-run --module demo/1 -- demo-1.eb --robot --prefix /x
compare_admin "run --dry-run, userns" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend userns --dry-run -- demo-1.eb -f
compare_admin "run, no easyconfig" run --software @ROOT@/software --modules @ROOT@/modules --dry-run
compare_admin "run, no software" run --dry-run -- x.eb
compare_admin "run, bad backend" run --backend nope -- x.eb
compare_admin "run, empty prepare" run --software @ROOT@/software --modules @ROOT@/modules --prepare= --dry-run -- x.eb
compare_admin "run, relative prepare" run --software @ROOT@/software --modules @ROOT@/modules --prepare=rel --dry-run -- x.eb
compare_admin "retire, no module file" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/none.lua
compare_admin "retire, missing options" retire
compare_admin "gc, bad grace" gc --state-root @ROOT@/state --grace-hours x
# A whole run with tests/fake_eb.py as eb. The module text names each
# copy's own paths, so hashes are compared only through the files they name.
export NORM_HASH=1
compare_admin "run, installpath" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r2 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "status after the run" status --state-root @ROOT@/state r2
compare_admin "resume a complete run" resume --state-root @ROOT@/state r2
compare_admin "rollback the run" rollback --state-root @ROOT@/state r2
compare_admin "rollback the run again" rollback --state-root @ROOT@/state r2
compare_admin "resume a rolled-back run" resume --state-root @ROOT@/state r2
compare_admin "run, userns" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend userns --eb @ROOT@/eb --run-id r4 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, ledger" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r5 --skip-live-check --ledger-config @ROOT@/ledger.json --module demo/1 -- demo-1.eb
compare_admin "status, ledger run" status --state-root @ROOT@/state r5
compare_admin "resume a complete ledger run" resume --state-root @ROOT@/state r5
compare_admin "rollback a ledger run" rollback --state-root @ROOT@/state r5
compare_admin "rollback a ledger run again" rollback --state-root @ROOT@/state r5
for impl in py zig; do echo fail-before > "${work}/${impl}/plan"; done
export FAKE_LEDGER_PLAN_NAME=plan
compare_admin "run, ledger append fails" env FAKE_LEDGER_PLAN=@ROOT@/plan @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r6 --skip-live-check --ledger-config @ROOT@/ledger.json --module demo/1 -- demo-1.eb
compare_admin "resume delivers the record" resume --state-root @ROOT@/state r6
compare_admin "ledger records so far" env cat @ROOT@/ledger.jsonl
# The ledger policy a run started with is frozen in its journal: a change
# while a record is pending, after the run completed, or during the build
# is refused, and nothing is appended.
ledger_edit() {
    local impl
    for impl in py zig; do
        python3 -c 'import json, sys; p = sys.argv[1]; d = json.load(open(p)); d[sys.argv[2]] = json.loads(sys.argv[3]); json.dump(d, open(p, "w"))' \
            "${work}/${impl}/ledger.json" "$1" "${2//@ROOT@/${work}/${impl}}"
    done
}
for impl in py zig; do cp "${work}/${impl}/ledger.json" "${work}/${impl}/ledger.json.orig"; echo fail-before > "${work}/${impl}/plan3"; done
ledger_restore() { local impl; for impl in py zig; do cp "${work}/${impl}/ledger.json.orig" "${work}/${impl}/ledger.json"; done; }
compare_admin "run, ledger append fails again" env FAKE_LEDGER_PLAN=@ROOT@/plan3 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r14 --skip-live-check --ledger-config @ROOT@/ledger.json --module demo/1 -- demo-1.eb
ledger_edit required false
compare_admin "resume after the ledger became optional" resume --state-root @ROOT@/state r14
ledger_restore; ledger_edit actor '"other"'
compare_admin "resume after the ledger actor changed" resume --state-root @ROOT@/state r14
ledger_restore
compare_admin "resume after the policy is restored" resume --state-root @ROOT@/state r14
ledger_edit log '"@ROOT@/redirected.jsonl"'
compare_admin "resume a completed run with its ledger redirected" resume --state-root @ROOT@/state r14
ledger_restore
compare_admin "run, the build changes the ledger policy" env FAKE_EB_LEDGER_EDIT=@ROOT@/ledger.json @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r15 --skip-live-check --ledger-config @ROOT@/ledger.json --module demo/1 -- demo-1.eb
ledger_restore
compare_admin "ledger records after the policy cases" env cat @ROOT@/ledger.jsonl
# A generation-pointer record (schema 3) is not applied as a directory swap.
for impl in py zig; do
    mkdir -p "${work}/${impl}/state/runs/foreign"
    printf '{"schema": 3, "publication": "generation-pointer", "kind": "publish", "run_id": "foreign", "state": "built", "software": "%s/software", "modules": "%s/modules", "state_root": "%s/state", "modules_plan": [{"rel": "all/demo/1.lua", "generation": "%s/hidden-generation", "live_module": "%s/modules/all/demo/1.lua"}], "ledger_policy": {"actor": "x"}}\n' \
        "${work}/${impl}" "${work}/${impl}" "${work}/${impl}" "${work}/${impl}" "${work}/${impl}" > "${work}/${impl}/state/runs/foreign/journal.json"
done
compare_admin "resume a generation-pointer record" resume --state-root @ROOT@/state foreign
compare_admin "status of a generation-pointer record" status --state-root @ROOT@/state foreign
for impl in py zig; do rm -rf "${work}/${impl}/state/runs/foreign"; done
compare_admin "build-events, command succeeds" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/ok.json -- true
compare_admin "build-events, recorded, not repeated" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/ok.json -- false
compare_admin "build-events, deliver a recorded one" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/ok.json --deliver
compare_admin "build-events, command fails" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/fails.json -- sh -c "exit 3"
compare_admin "build-events, command missing" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/missing.json -- no-such-command-here
for impl in py zig; do echo fail-before > "${work}/${impl}/plan2"; done
compare_admin "build-events, append fails" env FAKE_LEDGER_PLAN=@ROOT@/plan2 @CMD@ build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/pend.json -- true
compare_admin "build-events, deliver then run" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/pend.json -- true
compare_admin "build-events, bad metadata" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/bad.json -- true
compare_admin "build-events, both deliver and command" build-events --config @ROOT@/bev/config.json --metadata @ROOT@/bev/ok.json --deliver -- true
compare_admin "run, eb fails" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb /bin/false --run-id r3 --skip-live-check --module demo/1 -- demo-1.eb
# The bwrap backend, with the fake bwrap first on PATH and eb answering
# --version: present stage keys (5.4.2), the 5.3 layout without them, an
# unknown release without them, an overlay mount, and a failed probe.
bw='env PATH=@ROOT@/bin:'"${PATH}"
compare_admin "run, bwrap" ${bw} FAKE_EB_VERSION=5.4.2 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b1 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, bwrap, 5.3 layout without stage keys" ${bw} FAKE_EB_VERSION=5.3.1 FAKE_EB_KEYS=legacy @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b2 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, bwrap, unknown release without stage keys" ${bw} FAKE_EB_VERSION=5.5.0 FAKE_EB_KEYS=legacy @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b3 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, bwrap, 5.4.1 without stage keys" ${bw} FAKE_EB_VERSION=5.4.1 FAKE_EB_KEYS=legacy @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b7 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, bwrap, overlay" ${bw} FAKE_EB_VERSION=5.4.2 FAKE_EB_OVERLAY=1 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b4 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, bwrap probe fails" ${bw} FAKE_EB_VERSION=5.4.2 FAKE_BWRAP_FAIL=1 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend bwrap --eb @ROOT@/eb --run-id b5 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, auto prefers bwrap" ${bw} FAKE_EB_VERSION=5.4.2 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend auto --eb @ROOT@/eb --run-id b6 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run --dry-run, auto when bwrap fails" ${bw} FAKE_EB_VERSION=5.4.2 FAKE_BWRAP_FAIL=1 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend auto --eb @ROOT@/eb --dry-run --module demo/1 -- demo-1.eb
compare_admin "run, auto with no sandbox uses installpath" ${bw} FAKE_EB_VERSION=5.4.2 FAKE_BWRAP_FAIL=1 FAKE_UNSHARE_FAIL=1 @CMD@ run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend auto --eb @ROOT@/eb --run-id b8 --skip-live-check --module demo/1 -- demo-1.eb
for v in 5.3.0 5.3.1 5.4.0 5.4.1 5.4.2 5.5.0; do
    compare_admin "probe, EasyBuild ${v} stage keys" ${bw} FAKE_EB_VERSION=${v} @CMD@ probe --path @ROOT@/state --json --eb @ROOT@/eb
done
compare_admin "gc, retire, gc and keeps" gc --state-root @ROOT@/state --dry-run
compare_admin "retire --dry-run, a directory" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo --dry-run
compare_admin "retire, a directory" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo
compare_admin "retire, a symlinked module file" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/symlink.lua
compare_admin "retire, no root assignment" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/noroot.lua
compare_admin "resume a retire record" resume --state-root @ROOT@/state @RETIRE@
compare_admin "retire after a rollback" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua
for impl in py zig; do printf 'local root = "software/demo/1"\n-- rewritten\n' > "${work}/${impl}/modules/all/demo/1.lua"; done
compare_admin "rollback over a new module file" rollback --state-root @ROOT@/state @RETIRE@
for impl in py zig; do rm "${work}/${impl}/modules/all/demo/1.lua"; done
compare_admin "rollback after the new file is gone" rollback --state-root @ROOT@/state @RETIRE@
compare_admin "gc after the retire is rolled back" gc --state-root @ROOT@/state --grace-hours 0
# A software directory another tracked module still names is kept: the
# alias is tracked through a rolled-back retire, then the module is retired.
for impl in py zig; do cp "${work}/${impl}/modules/all/demo/1.lua" "${work}/${impl}/modules/all/demo/alias.lua"; done
compare_admin "retire an alias" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/alias.lua
compare_admin "rollback the alias retire" rollback --state-root @ROOT@/state @RETIRE@
compare_admin "retire the module again" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua
compare_admin "gc keeps a directory a live module names" gc --state-root @ROOT@/state --grace-hours 0 --dry-run
compare_admin "rollback the module retire" rollback --state-root @ROOT@/state @RETIRE@
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    shm=$(mktemp -d /dev/shm/esr-diff.XXXXXX)
    compare_admin "retire, state root on another filesystem" retire --state-root "${shm}/state" --module-file @ROOT@/modules/all/link/1.lua
    rm -rf -- "${shm}"
fi
compare_admin "run, no installed target" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r10 --skip-live-check --module nope/1 -- nope-1.eb
compare_admin "run, symlinked target" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r10 --skip-live-check --module link/1 -- demo-1.eb
compare_admin "run, module escapes" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r10 --skip-live-check --module ../outside/1 -- demo-1.eb
compare_admin "run, symlinked state root" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/statelink --backend installpath --eb @ROOT@/eb --run-id r10 --skip-live-check --module demo/1 -- demo-1.eb
compare_admin "run, missing prepare" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r11 --skip-live-check --prepare @ROOT@/no-such-policy --module demo/1 -- demo-1.eb
compare_admin "run, prepare fails" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --run-id r12 --skip-live-check --prepare @ROOT@/prepare-fails --module demo/1 -- demo-1.eb
for impl in py zig; do cp "${work}/${impl}/prepare-ok" "${work}/${impl}/prepare-fails"; done
compare_admin "resume after the prepare is fixed" resume --state-root @ROOT@/state r12
compare_admin "the prepare ran once" env cat @ROOT@/prepare-count
compare_admin "rollback a prepared run" rollback --state-root @ROOT@/state r12
# The publication barrier: a policy that leaves the second of two staged
# generations without build evidence, or unreadable, publishes neither.
pr='--software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb @ROOT@/eb --skip-live-check'
# shellcheck disable=SC2086
compare_admin "run, prepare barrier, second generation incomplete" env FAKE_EB_RELS=alpha/1,beta/1 @CMD@ run ${pr} --run-id p1 --prepare @ROOT@/prepare-barrier-rename --module alpha/1 --module beta/1 -- alpha-1.eb
# shellcheck disable=SC2086
compare_admin "run, prepare barrier, second generation unreadable" env FAKE_EB_RELS=alpha/1,beta/1 @CMD@ run ${pr} --run-id p2 --prepare @ROOT@/prepare-barrier-chmod --module alpha/1 --module beta/1 -- alpha-1.eb
for impl in py zig; do chmod -R u+rwX "${work}/${impl}/state/generations/p2"; done
compare_admin "module files after the barrier" env cat @ROOT@/modules/all/alpha/1.lua @ROOT@/modules/all/beta/1.lua
# A policy sees the publication context and restricts the staged tree
# before the module file changes.
# shellcheck disable=SC2086
compare_admin "run, prepare restricts the staged tree" run ${pr} --run-id p3 --prepare @ROOT@/prepare-context --module demo/1 -- demo-1.eb
compare_admin "the context the prepare saw" env cat @ROOT@/context.json
compare_admin "modes after a restricting prepare" env sh -c 'cd "$1" && find . -printf "%m %p\n" | sort' sh @ROOT@/software/demo/1
# A published module retired: gc removes the hidden sibling and leaves the
# live tree for the operator.
compare_admin "retire a published module" retire --state-root @ROOT@/state --module-file @ROOT@/modules/all/demo/1.lua
compare_admin "gc after a published module is retired" gc --state-root @ROOT@/state --grace-hours 0
compare_admin "rollback the published module retire" rollback --state-root @ROOT@/state @RETIRE@
unset NORM_HASH

# The live-tree snapshot a journal records, compared as JSON.
snap="${work}/snaptree"
mkdir -p "${snap}/a/b" "${snap}/c"
printf 'x' > "${snap}/a/b/f"
printf 'y' > "${snap}/top"
ln -s ../c "${snap}/a/dirlink"
ln -s top "${snap}/filelink"
for p in "${snap}" "${snap}/top" "${snap}/filelink" "${snap}/missing"; do
    PYTHONPATH="${repo_root}" python3 -c 'import json, sys; from eb_staged_rebuild.livecheck import snapshot; print(json.dumps(snapshot(sys.argv[1]), indent=2, sort_keys=True))' "${p}" > "${work}/snap.py"
    "${TOOL_BIN}" __snapshot "${p}" > "${work}/snap.zig"
    if cmp -s "${work}/snap.py" "${work}/snap.zig"; then echo "same snapshot ${p##*/}"; else echo "DIFF snapshot ${p##*/}"; diff "${work}/snap.py" "${work}/snap.zig" | head -20; diffs=$((diffs + 1)); fi
done

echo "${diffs} differences"
[ "${diffs}" -eq 0 ]

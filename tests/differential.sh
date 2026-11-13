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
trap 'rm -rf -- "${work}"' EXIT
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
compare "unknown command" bogus

# Commands that change a fixture: each implementation gets its own copy,
# and run ids and timestamps are normalized before the comparison.
norm() {
    sed -E -e 's/[0-9]{8}T[0-9]{6}Z(-[0-9]+)?/STAMP/g' \
        -e 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z/ISO/g' \
        -e 's/[0-9]{10}\.[0-9]+/TIME/g' -e "s#${work}/(py|zig)#W#g" \
        -e 's#[^ ]*python[0-9.]* -m eb_staged_rebuild\.userns_sandbox#SANDBOX#' \
        -e "s#${TOOL_BIN} __userns-sandbox#SANDBOX#" |
    if [ -n "${NORM_HASH:-}" ]; then sed -E 's/[0-9a-f]{64}/HASH/g'; else cat; fi |
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
        rid=$(cd "${root}/state/runs" 2>/dev/null && ls -d retire-* 2>/dev/null | head -1)
        for a in "$@"; do a="${a//@ROOT@/${root}}"; args+=("${a//@RETIRE@/${rid}}"); done
        (cd "${root}" && "${cmd[@]}" "${args[@]}") > "${out}.out" 2> "${out}.err"
        echo $? > "${out}.rc"
        find "${root}" -name journal.json -print0 | sort -z | xargs -0 -r cat > "${out}.journals"
        (cd "${root}" && find . -not -name '.lock*' | sort) > "${out}.tree"
        for f in out err journals tree; do norm < "${out}.${f}" > "${out}.${f}.n"; done
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
compare_admin "run, eb fails" run --software @ROOT@/software --modules @ROOT@/modules --state-root @ROOT@/state --backend installpath --eb /bin/false --run-id r3 --skip-live-check --module demo/1 -- demo-1.eb
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

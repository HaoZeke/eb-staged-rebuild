#!/bin/bash
# Run the Python tool (python3 -m eb_staged_rebuild) and the Zig build
# (TOOL_BIN) on the same read-only invocations and compare exit code,
# stdout and stderr.
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

echo "${diffs} differences"
[ "${diffs}" -eq 0 ]

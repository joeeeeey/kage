# Coexistence: moving a running system onto Kage one module at a time

Kage is extracted from a working runtime that keeps running the whole time. This is the contract between
that legacy runtime and Kage while some modules live in Kage and some do not.

## Rules

1. **Moving and changing are separate releases.** A module moves with characterisation tests that pin what it
   does today, and the move changes no behaviour. Fixes and new behaviour come in a later release.
2. **Kage never imports the legacy runtime.** What a moved module still needs from the old side reaches it as
   an argument (today's leaf modules already receive a runtime handle, `rt`), and the module documents exactly
   which attributes it uses.
3. **The legacy runtime reaches Kage only through its bridge** (`kage_bridge.py` in the legacy code), which reads
   the lock, verifies the pinned release and loads modules from it.
4. **One switch per module, in the lock.** `"modules": {"profile": "kage"}`. A switch flips in a reviewed patch
   that also carries the module's tests; the previous code stays on disk for one release so that the rollback is
   putting the old lock back.
5. **The state stays where it is.** Database files, table and column names, stored enum values and config paths
   do not change during a move. A rename (for example an owner-specific value to `owner`) is its own release with
   a migration that reads both.
6. **Copy moves with its module.** User-facing text and language patterns of a module go into the locale
   catalog in the release that moves the module, unchanged byte for byte.

## The bridge (legacy side)

- **Lock.** `kage.lock` next to the legacy scripts: `{"schema": "kage.lock/v1", "kage": {"version", "commit",
  "tree_sha256"}, "modules": {...}}`.
- **Release.** `<versions dir>/<version>-<commit12>/`. The versions directory is `$KAGE_VERSIONS_DIR`, or
  `~/.local/share/kage/versions` of the account's real home (looked up from the password database, so a test
  that points `HOME` at a temporary directory still finds the installed release).
- **Verification**, once per process: `BUNDLE.json` must match the lock (version, commit, tree hash) and every
  file must match `MANIFEST.sha256`.
- **Deployment profile.** `$KAGE_DEPLOYMENT_DIR`, or the legacy runtime's own `deploy/` directory.
- **Fallback.** If the release is missing or does not verify, or the profile does not load, the bridge serves the
  legacy values and reports `mode: legacy` with the reason. It never raises into its caller. A move is only
  switched once the legacy values equal the profile's, so a fallback is a loud event, not a behaviour change.
- **Status.** `python3 kage_bridge.py status` prints the mode, reason, release and profile directory as JSON.
  An install is accepted only when the status says `mode: kage`.

## What a move looks like

| Step | Where | Gate |
|---|---|---|
| Characterisation tests of today's behaviour | legacy repo, `/tmp` clone | green on the unmodified code |
| Module in Kage + its tests (fake adapters, offline) | Kage PR | CI green, leak scan clean |
| Release | Kage tag + bundle | deterministic bundle, `bundle.py verify` |
| Legacy side: bridge call + lock switch | legacy repo, `/tmp` clone | all legacy suites green with the release installed in a sandbox versions dir |
| Read-only rehearsal on a copy of the live state | sandbox | same outputs as the old code on real data, nothing sent |
| Guarded install | the running system's install window | evidence hashes, suites, `kage_bridge.py status` = `kage`, post-checks; any failure reverses the patch |

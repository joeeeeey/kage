# Releasing and pinning

## A release

1. `kage/__init__.py` carries `__version__ = 'X.Y.Z'`. Bump it in the PR that changes behaviour or adds a module.
2. After the PR merges and CI is green, tag the merge commit: `git tag -a vX.Y.Z -m 'Kage X.Y.Z' && git push origin vX.Y.Z`.
3. Build the bundle from the tag: `python3 tools/bundle.py build vX.Y.Z dist/`. It prints `version`, `commit`,
   `tree_sha256` and the tarball's `bundle_sha256`. The tarball is deterministic: building the same tag twice gives
   the same bytes, so anyone can rebuild it and compare.

A bundle holds only `kage/`, `MANIFEST.sha256` (sha256 of every file) and `BUNDLE.json`.

## A deployment pins one release

The deployment's lock file names the release and the modules it has switched to Kage:

```json
{
  "schema": "kage.lock/v1",
  "kage": {"version": "0.1.0", "commit": "<40 hex>", "tree_sha256": "<64 hex>"},
  "modules": {"profile": "kage"}
}
```

Releases are installed read-only and side by side under `<versions dir>/<version>-<commit12>/`
(default `~/.local/share/kage/versions/`). Installing never overwrites an existing directory; a directory that
exists but does not verify is an error, not something to repair in place.

## Upgrade and rollback

- **Upgrade** = install the new bundle next to the old one, then change the lock (one reviewed patch). Nothing
  else moves, so the old release is still on disk.
- **Rollback** = put the previous lock back. The previous release directory is still there and still verifies.
- Old release directories are removed only after the release that replaced them has run cleanly for a week.

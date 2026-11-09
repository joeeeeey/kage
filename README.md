# Kage

Kage (影分身, "shadow clone") is the engine of one person's agent runtime: it watches the owner's channels,
routes messages to tasks, runs headless agent sessions on them, and asks the owner only when a decision is
theirs. One instance serves exactly one owner; authority comes only from that owner.

The engine holds no person's or company's data. Who the owner is, which organisation conventions apply and
where an instance lives come from a **deployment profile** (`kage.profile`; example:
[`examples/northwind-mira/`](examples/northwind-mira/), a fictional owner).

Status: early. Kage is being extracted, module by module, from a working Slack-based runtime. Each module
moves only after characterisation tests pin today's behaviour, and the running system switches to it only
after it verifies. See [`docs/coexistence.md`](docs/coexistence.md).

## Layout

| Path | What |
|---|---|
| `kage/` | the engine package (Python 3.9+, standard library only) — the only thing a release ships |
| `examples/` | fictional deployments used by the tests |
| `tests/` | unit tests; offline by construction (`tests/__init__.py` refuses every socket) |
| `tools/leak_scan.py` | personal-data scan (hashed denylist of names, deployment project / task words and a deployment's assistant, reaction and sibling-skill names, real-looking chat ids, home paths, emails; released findings in `tools/leak-history-known.txt`) |
| `tools/bundle.py` | builds and verifies the release bundle a deployment installs |
| `docs/` | coexistence contract and release process |

## Install

Kage runs from a verified release, never from a checkout. Build one from a tag, install it read-only, then run the
`kage init` wizard from it (it asks about ten questions, prints the Slack app manifest, checks and stores the tokens
and proves who the owner is):

```sh
git clone <this repository> kage && cd kage
python3 tools/bundle.py build vX.Y.Z dist/            # prints the bundle path, its commit and tree_sha256
mkdir -p ~/.local/share/kage/versions && cd ~/.local/share/kage/versions
tar -xzf /path/to/kage/dist/kage-X.Y.Z-<commit12>.tar.gz && mv kage-X.Y.Z-<commit12> X.Y.Z-<commit12>
python3 /path/to/kage/tools/bundle.py verify X.Y.Z-<commit12>
PYTHONPATH=$PWD/X.Y.Z-<commit12> python3 -m kage init      # in a terminal; writes the deployment and its lock
```

`init` writes a deployment (profile, lock, the three service definitions) and tells you the next command (`doctor`).
The services are launchd jobs on macOS (`services start`); elsewhere run the same three programs under any
supervisor: `scripts/kage.py run`, `scripts/kage.py serve receiver`, `scripts/kage.py serve dashboard --port P`.

## Develop

```sh
git config core.hooksPath .githooks          # pre-commit: gitleaks + leak_scan on staged files
python3 -m unittest discover -s tests -t .   # always with -t . so the offline guard loads
python3 tools/leak_scan.py --tree
```

CI runs the tests on Python 3.9, 3.10 and 3.12, the personal-data scan over the tree and the full history, and
gitleaks over the full history. Any finding fails the build.

## Release

See [`docs/releasing.md`](docs/releasing.md): tag `vX.Y.Z`, build the bundle with
`tools/bundle.py build vX.Y.Z dist/`, and pin `version`, `commit` and `tree_sha256` in the deployment's lock.

## License

Apache License 2.0; see [`LICENSE`](LICENSE).

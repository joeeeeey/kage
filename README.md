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
| `kage/` | the engine package (Python 3.10+, standard library only) — the only thing a release ships |
| `examples/` | fictional deployments used by the tests |
| `tests/` | unit tests; offline by construction (`tests/__init__.py` refuses every socket) |
| `tools/leak_scan.py` | personal-data scan (hashed denylist of names and deployment project / task words, real-looking chat ids, home paths, emails; released findings in `tools/leak-history-known.txt`) |
| `tools/bundle.py` | builds and verifies the release bundle a deployment installs |
| `docs/` | coexistence contract and release process |

## Develop

```sh
git config core.hooksPath .githooks          # pre-commit: gitleaks + leak_scan on staged files
python3 -m unittest discover -s tests -t .   # always with -t . so the offline guard loads
python3 tools/leak_scan.py --tree
```

CI runs the tests on Python 3.10 and 3.12, the personal-data scan over the tree and the full history, and
gitleaks over the full history. Any finding fails the build.

## Release

See [`docs/releasing.md`](docs/releasing.md): tag `vX.Y.Z`, build the bundle with
`tools/bundle.py build vX.Y.Z dist/`, and pin `version`, `commit` and `tree_sha256` in the deployment's lock.

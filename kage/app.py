"""The assembly (K14g, D17): Kage builds a deployment's runtime itself.

`App(deployment_dir)` reads the deployment profile (owner / org / instance) and instance.json's `assembly` section:

  program           the deployment's program, relative to its root (the directory that holds the deployment dir):
                    the runtime's `__file__` (the paths it derives, the command lines it prints)
  lock              the deployment's kage.lock, relative to its root ($KAGE_LOCK overrides, as for the deployment's
                    bridge): every switch must be on Kage and the pinned release must be this one
  runtime_version   the runtime's VERSION when the program does not pass its own
  values            {"<unit>.<hint>": text} — the deployment's own words and names (env names, paths, help texts) the
                    generated units read with `_app.value`
  modules           {"<name>": {"path", "sha256", "note"}} — the deployment's modules Kage does not serve yet
                    (transitional): every one is verified before anything loads; a missing file or another sha256
                    refuses the whole assembly (AssemblyError), before any database, file or process is touched

`assemble(into=None)` runs the generated hub (kage/assembly/hub.py) in `into` (a module's globals; a fresh namespace
when None) with `_app` in scope: the leaves it loads are the generated units (kage/assembly/<unit>.py, one per adapter
Kage serves) or the declared transitional modules; owner-keyed names (`ask_owner`, `OWNER`) are also exported under
the deployment's names (`key_name`), and generic names under the deployment's own (`_ALIASES`). `python3 -m kage
--deployment DIR …` runs the program's command line on it (kage/__main__.py).
"""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import types

import kage
from kage import profile

SHA256_RE = re.compile(r'[0-9a-f]{64}')
UNIT_DIR = Path(__file__).with_name('assembly')


class AssemblyError(Exception):
    """The deployment cannot be assembled (said why); nothing was loaded or written."""


class Bridge:
    """The assembly's bridge: the same calls the deployment's adapters make on theirs."""

    def __init__(self, app):
        self.app = app
        self._packs = {}

    def deployment(self):
        return self.app.deployment

    def module(self, switch, name):
        if self.app.lock['modules'].get(switch) != 'kage':
            raise AssemblyError(f'switch_{switch}_not_on_kage')
        return importlib.import_module(name)

    def pack(self, name):
        if name not in self._packs:
            entry = (self.app.lock.get('packs') or {}).get(name)
            if not isinstance(entry, dict) or not SHA256_RE.fullmatch(str(entry.get('sha256'))):
                raise AssemblyError(f'pack_{name}_not_pinned')
            path = (self.app.root / entry['path']).resolve()
            if self.app.root.resolve() not in path.parents:
                raise AssemblyError(f'pack_{name}_outside_the_deployment')
            data = path.read_bytes() if path.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise AssemblyError(f'pack_{name}_missing_or_altered')
            self._packs[name] = self.app._load(f'kage_pack_{name.replace(".", "_")}', path)
        return self._packs[name]


class App:
    def __init__(self, deployment_dir, *, version=None, program=None):
        self.dir = Path(deployment_dir).expanduser().absolute()
        try:
            self.deployment = profile.load(self.dir)
            raw = json.loads((self.dir / 'instance.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise AssemblyError(f'deployment_unreadable: {type(exc).__name__}: {exc}') from None
        spec = raw.get('assembly')
        if not isinstance(spec, dict):
            raise AssemblyError('instance_json_has_no_assembly_section')
        self.spec = spec
        relative = Path(str(spec.get('program') or 'program'))
        if program:  # the program's own path as it runs (unresolved: the paths it derives are the ones it always printed)
            self.program = Path(program)
            self.root = self.program.parents[len(relative.parts) - 1]
        else:
            self.root = self.dir.parent
            self.program = self.root / relative
        lock_file = Path(os.environ.get('KAGE_LOCK') or self.root / spec.get('lock', 'kage.lock'))  # the bridge's override
        try:
            self.lock = json.loads(lock_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise AssemblyError(f'lock_unreadable: {type(exc).__name__}') from None
        off = sorted(k for k, v in (self.lock.get('modules') or {}).items() if v != 'kage')
        if off:
            raise AssemblyError('switches_not_on_kage: ' + ', '.join(off))
        pinned = (self.lock.get('kage') or {}).get('version')
        if pinned != kage.__version__:
            raise AssemblyError(f'release_mismatch: the lock pins {pinned}, this is Kage {kage.__version__}')
        self.version = version or spec.get('runtime_version')
        self.values = dict(spec.get('values') or {})
        self.modules = dict(spec.get('modules') or {})
        self.bridge = Bridge(self)
        self._leaves = {}
        self.verified = False

    # -- what the generated units call
    def value(self, key):
        try:
            return self.values[key]
        except KeyError:
            raise AssemblyError(f'value_missing: {key}') from None

    def key_name(self, generic):
        """A generic owner-keyed name (`ask_owner`, `OWNER_RE`, `--why-owner`) under this owner's key."""
        key = self.deployment.owner.key
        return generic.replace('OWNER', key.upper()).replace('Owner', key.capitalize()).replace('owner', key)

    def leaf(self, name):
        if name not in self._leaves:
            if (UNIT_DIR / f'{name}.py').is_file() and name != 'hub':
                self._leaves[name] = self._unit(name)
            elif name in self.modules:
                self._leaves[name] = self._load(f'kage_deployment_{name}', self.root / self.modules[name]['path'])
            else:
                raise AssemblyError(f'unknown_module: {name}')
        return self._leaves[name]

    # -- the assembly
    def verify(self):
        """Every declared transitional module present with its pinned sha256; else AssemblyError naming them all."""
        problems = []
        for name, entry in sorted(self.modules.items()):
            path = self.root / str(entry.get('path', ''))
            if not SHA256_RE.fullmatch(str(entry.get('sha256'))):
                problems.append(f'{name}: no sha256 pin')
            elif not path.is_file():
                problems.append(f'{name}: missing ({entry.get("path")})')
            elif hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                problems.append(f'{name}: sha256 differs ({entry.get("path")})')
        if problems:
            raise AssemblyError('transitional_modules_refused: ' + '; '.join(problems))
        self.verified = True

    def assemble(self, into=None):
        """The runtime: the hub's namespace, in `into` (a module's globals) or a fresh one. Returns the namespace."""
        self.verify()
        namespace = into if into is not None else types.ModuleType('kage_runtime').__dict__
        namespace.update({'__file__': str(self.program), '_app': self})
        self._run('hub', namespace)
        return namespace

    def _unit(self, name):
        module = types.ModuleType(f'kage_assembled_{name}')
        module.__file__ = str(self.root / 'scripts' / f'{name}.py')
        module.__dict__['_app'] = self
        self._run(name, module.__dict__)
        return module

    def _run(self, name, namespace):
        path = UNIT_DIR / f'{name}.py'
        exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
        for generic in namespace.get('_OWNER_KEYED', ()):
            if generic in namespace:  # module-level only (a renamed local is not exported)
                namespace[self.key_name(generic)] = namespace[generic]
        for generic, deployment_name in (namespace.get('_ALIASES') or {}).items():
            namespace[deployment_name] = namespace[generic]

    def _load(self, module_name, path):
        if not self.verified and module_name.startswith('kage_deployment_'):
            raise AssemblyError('transitional_modules_not_verified')
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def assemble(deployment_dir, *, into=None, version=None, program=None):
    """App(deployment_dir, version=…, program=…).assemble(into)."""
    return App(deployment_dir, version=version, program=program).assemble(into)


def refusal(exc):
    """The JSON line `python3 -m kage` prints on stderr when the assembly refuses."""
    return json.dumps({'error': 'AssemblyError', 'detail': str(exc)})


if __name__ == '__main__':  # pragma: no cover - use `python3 -m kage`
    sys.exit('use: python3 -m kage --deployment DIR …')

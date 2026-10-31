"""The dashboard's pages for one owner: templates under `pages/<language>/` whose owner and deployment words are
`{{name}}` sentinels (the owner's name and key, the time zone, the deployment's command names, channel examples …),
rendered once in memory. `Pages` is path-like for the dashboard server: `pages / 'assets' / name` and `.read_bytes()`;
a name that is not a page raises FileNotFoundError, as a missing file does.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent / 'pages'
SENTINEL = re.compile(r'\{\{([A-Za-z_]+)\}\}')
TEXT = ('.html', '.css', '.js', '.json')


class PagesError(LookupError):
    """No pages for the language, or a sentinel without a value."""


def languages():
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(('.', '_')))


def sentinels(language):
    """The sentinel names the pages of one language use."""
    return sorted({name for text in _texts(language).values() for name in SENTINEL.findall(text)})


def _root(language):
    root = ROOT / str(language)
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', str(language)) or not root.is_dir():
        raise PagesError(f'no pages for language {language!r} (have {", ".join(languages())})')
    return root


def _files(language):
    root = _root(language)
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('.')}


def _texts(language):
    return {rel: p.read_text(encoding='utf-8') for rel, p in _files(language).items() if p.suffix in TEXT}


def render(language, values):
    """{relative path: bytes} of every page file, the sentinels filled in once (a value is never re-read as one)."""
    out = {}
    for rel, path in _files(language).items():
        data = path.read_bytes()
        if path.suffix in TEXT:
            text = data.decode('utf-8')
            missing = sorted(set(SENTINEL.findall(text)) - set(values))
            if missing:
                raise PagesError(f'{rel}: no value for {", ".join(missing)}')
            data = SENTINEL.sub(lambda m: str(values[m[1]]), text).encode('utf-8')
        out[rel] = data
    return out


class Pages:
    """Rendered pages, path-like (`/`, `read_bytes`) for the server that serves them."""

    def __init__(self, files, name=''):
        self._files, self._name = files, name

    @classmethod
    def for_owner(cls, language, values):
        return cls(render(language, values))

    def __truediv__(self, part):
        return Pages(self._files, f'{self._name}/{part}' if self._name else str(part))

    def read_bytes(self):
        try:
            return self._files[self._name]
        except KeyError:
            raise FileNotFoundError(f'no page {self._name!r}') from None

    def names(self):
        return sorted(self._files)

    def __repr__(self):
        return f'Pages({len(self._files)} files{", " + self._name if self._name else ""})'

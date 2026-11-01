"""Every module of the package imports on the running Python (CI runs 3.9, the system Python macOS ships, as well as
3.10 and 3.12): annotations are strings (`from __future__ import annotations` in every module), so a `X | None` in a
signature is never evaluated. The generated assembly units are run by kage.app, not imported; each must carry the same
future import."""
import ast
import importlib
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / 'kage'


def future_annotations(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return any(isinstance(n, ast.ImportFrom) and n.module == '__future__' and any(a.name == 'annotations' for a in n.names)
               for n in tree.body)


class ImportTest(unittest.TestCase):
    def test_every_module_imports(self):
        names = sorted('.'.join(p.relative_to(PACKAGE.parent).with_suffix('').parts).replace('.__init__', '')
                       for p in PACKAGE.rglob('*.py') if 'assembly' not in p.parts)
        self.assertGreater(len(names), 50)
        for name in names:
            with self.subTest(name):
                importlib.import_module(name)

    def test_every_module_and_unit_has_future_annotations(self):
        missing = [str(p.relative_to(PACKAGE.parent)) for p in sorted(PACKAGE.rglob('*.py')) if not future_annotations(p)]
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()

"""Move a deployment's task registry to another machine (0.78.0): `python3 -m kage.migrate export|import`.

  export --db SRC --out FILE
      The registry rows of a store, opened read-only: tasks, task links, dispatch preferences and metadata, routine
      state and confirmations, pending task deadlines. One JSON file (`kage.registry-export/v1`, mode 0600). These
      tables hold ids, paths, states and times: no message text and no token.
  import --db DST --file FILE [--rewrite OLD=NEW ...] [--replace] [--apply]
      Into another deployment's store. A path column (`tasks.path`, `routine_state.file`, `task_deadlines.task_path`)
      that is OLD or starts with OLD/ gets NEW instead (the first matching rule). Session ids and providers are kept.
      Without --apply only the plan is printed (per table: new, existing, replaced, rewritten). A row whose primary
      key the destination already has stays as it is unless --replace. Only the columns both stores have are copied.
      All in one transaction; the destination's tables must exist (start its runtime once first). Pause its dispatch
      (`mode pause`) or stop its services while importing.

Exit 0: done (or the plan); 1: refused ({"error", "detail"} on stderr); 2: usage. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

SCHEMA = 'kage.registry-export/v1'
TABLES = {  # name: (primary key columns, path columns, row filter)
    'tasks': (('task_id',), ('path',), ''),
    'task_links': (('parent_task_id', 'child_task_id', 'kind'), (), ''),
    'dispatch_prefs': (('task_key',), (), ''),
    'dispatch_meta': (('task_key',), (), ''),
    'routine_state': (('task_id', 'routine_id'), ('file',), ''),
    'routine_confirmations': (('task_id', 'routine_id', 'digest'), (), ''),
    'task_deadlines': (('id',), ('task_path',), "WHERE disposition='pending'"),
}


class Refused(Exception):
    """The command must not go on (says why)."""


def _columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info({table})')]


def export(src, now=None):
    """The registry rows of the store `src` (read-only) as a document."""
    path = Path(src).expanduser()
    if not path.is_file():
        raise Refused(f'no_store: {path}')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        tables, absent = {}, []
        for table, (_, _, where) in TABLES.items():
            columns = _columns(db, table)
            if not columns:
                absent.append(table)
                continue
            rows = db.execute(f'SELECT {", ".join(columns)} FROM {table} {where}').fetchall()
            tables[table] = {'columns': columns, 'rows': [list(r) for r in rows]}
    finally:
        db.close()
    return {'schema': SCHEMA, 'exported_at': time.time() if now is None else now, 'tables': tables, 'absent': absent}


def write_export(doc, out):
    out = Path(out).expanduser()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    return out


def parse_rewrites(rules):
    out = []
    for rule in rules or ():
        old, sep, new = rule.partition('=')
        if not sep or not old.startswith('/') or not new.startswith('/') or old.endswith('/') or new.endswith('/'):
            raise Refused(f'bad_rewrite: {rule!r} (OLD=NEW, absolute paths without a trailing /)')
        out.append((old, new))
    return out


def _rewrite(value, rules):
    if not isinstance(value, str):
        return value, False
    for old, new in rules:
        if value == old or value.startswith(old + '/'):
            return new + value[len(old):], True
    return value, False


def import_(dst, doc, rewrites=(), replace=False, apply=False):
    """Plan (and with `apply` write) the document's rows into the store `dst`."""
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA:
        raise Refused(f'not_a_registry_export: schema must be {SCHEMA}')
    path = Path(dst).expanduser()
    if not path.is_file():
        raise Refused(f'no_store: {path} (start the runtime once on this machine first)')
    rules = parse_rewrites(rewrites)
    db = sqlite3.connect(str(path), timeout=30)
    try:
        report, missing, writes = {}, [], []
        for table, (keys, paths, _) in TABLES.items():
            data = (doc.get('tables') or {}).get(table)
            if data is None:
                continue
            have = _columns(db, table)
            if not have:
                missing.append(table)
                continue
            src_cols = list(data['columns'])
            if any(k not in src_cols or k not in have for k in keys):
                raise Refused(f'primary_key_columns_differ: {table}')
            cols = [c for c in src_cols if c in have]
            entry = {'rows': len(data['rows']), 'new': 0, 'existing': 0, 'replaced': 0, 'rewritten': 0,
                     'columns_dropped': sorted(set(src_cols) - set(have))}
            for raw in data['rows']:
                row = dict(zip(src_cols, raw))
                changed = False
                for column in paths:
                    if column in row:
                        row[column], hit = _rewrite(row[column], rules)
                        changed = changed or hit
                entry['rewritten'] += changed
                exists = db.execute(f'SELECT 1 FROM {table} WHERE ' + ' AND '.join(f'{k}=?' for k in keys),
                                    [row[k] for k in keys]).fetchone() is not None
                if exists and not replace:
                    entry['existing'] += 1
                    continue
                entry['replaced' if exists else 'new'] += 1
                writes.append((f'INSERT OR REPLACE INTO {table} ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                               [row[c] for c in cols]))
            report[table] = entry
        if missing:
            raise Refused('destination_tables_missing: ' + ', '.join(missing) + ' (start the runtime once first)')
        if apply:
            with db:
                for sql, values in writes:
                    db.execute(sql, values)
        return {'ok': True, 'applied': bool(apply), 'store': str(path), 'tables': report,
                'absent_in_export': doc.get('absent') or []}
    finally:
        db.close()


def parser():
    p = argparse.ArgumentParser(prog='python3 -m kage.migrate', description="Move a deployment's task registry.")
    sub = p.add_subparsers(dest='command', required=True)
    e = sub.add_parser('export', help='the registry rows of a store (read-only) into a JSON file (0600)')
    e.add_argument('--db', required=True)
    e.add_argument('--out', required=True)
    i = sub.add_parser('import', help='into another store: a dry run unless --apply')
    i.add_argument('--db', required=True)
    i.add_argument('--file', required=True)
    i.add_argument('--rewrite', action='append', default=[], metavar='OLD=NEW', help='path prefix rewrite (repeatable)')
    i.add_argument('--replace', action='store_true', help='replace rows the destination already has')
    i.add_argument('--apply', action='store_true', help='write (default: print the plan only)')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'export':
            doc = export(args.db)
            out = write_export(doc, args.out)
            result = {'ok': True, 'out': str(out), 'tables': {t: len(d['rows']) for t, d in doc['tables'].items()},
                      'absent': doc['absent']}
        else:
            doc = json.loads(Path(args.file).expanduser().read_text(encoding='utf-8'))
            result = import_(args.db, doc, args.rewrite, replace=args.replace, apply=args.apply)
    except (Refused, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': type(exc).__name__, 'detail': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

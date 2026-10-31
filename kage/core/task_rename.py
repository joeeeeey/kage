"""task-rename: rename long-lived tasks (task_id == the workspace folder name under the tasks root), e.g.
`local-task-20261019-owner-inbox` -> `owner-inbox`. No chat I/O, no model.

    <runtime CLI> [--db DB] task-rename --map OLD=NEW [--map OLD2=NEW2 ...] [--apply] [--root R]
                  [--backup-dir D] [--update-config] [--no-symlink]

Default is a DRY RUN: the rewrite is simulated on an in-memory copy of the database (online backup of a read-only
connection) and reported; nothing is written. `--apply`, in this order:
  1. refuse unless the supervisor's lock `<db>.lock` is free (fcntl LOCK_EX|LOCK_NB, held to the end) — launchd
     KeepAlive restarts a killed supervisor, so the services are booted out first, never just killed;
  2. refuse on an unsafe filesystem state (NEW already a real directory with content, a foreign symlink);
  3. BEGIN IMMEDIATE (one transaction for every map); online backup `<backup-dir>/<db name>.pre-task-rename-<UTC>`
     (dir 0700, file 0600; default `<db dir>/backups/`) + a JSON manifest next to it;
  4. rewrite every occurrence in EVERY table (generic `sqlite_master` + `PRAGMA table_info` scan, so unknown / future
     tables are covered): a TEXT value equal to OLD, an absolute workspace path `<root>/OLD[/...]`, and the string
     leaves (keys and values) of JSON documents (exact id or path prefix only — free text, `session:` keys and hash
     ids are never substring-replaced; they are reported as `unrewritten_mentions`);
  5. re-scan: zero rewritable leftovers, else roll back;
  6. move `<root>/OLD` -> `<root>/NEW` (os.rename) and leave a RELATIVE symlink `<root>/OLD -> NEW` (unless
     --no-symlink), still inside the transaction window: a filesystem failure undoes the moves and rolls the database
     back; a failed commit undoes the moves;
  7. commit; then `--update-config` sets `intake.inbox_task` / `intake.oncall_task` (and `dispatch.long_lived_tasks`
     entries) still naming OLD through worker_config.set_many(who='task-rename') — validated before step 3.
Idempotent: a second identical apply is an `already_migrated` no-op. Reversible: `--map NEW=OLD` while `<root>/OLD` is
a symlink to NEW removes that symlink, moves the folder back and rewrites the database back. Files inside the
workspace that still mention OLD (goal.md CLI examples...) are reported, never rewritten (`rules inbox-sync`
regenerates the managed blocks).
"""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time

ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}')  # = worker_config.TASK_ID_RE (a workspace folder name)
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)       # a JSON string literal (outside strings `"` starts one)
CONFIG_KEYS = ('intake.inbox_task', 'intake.oncall_task')
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 4000
MAX_REPORTED_FILES = 50


class RenameError(ValueError):
    pass


def parse_maps(values):
    """['OLD=NEW', ...] -> [(old, new)]: valid folder names, no duplicates, chains or swaps between maps."""
    maps = []
    for value in values or ():
        old, sep, new = str(value).partition('=')
        old, new = old.strip(), new.strip()
        if not sep or not ID_RE.fullmatch(old) or not ID_RE.fullmatch(new):
            raise RenameError('map_must_look_like_OLD=NEW_with_task_folder_names')
        if old == new:
            raise RenameError('map_old_and_new_must_differ')
        maps.append((old, new))
    if not maps:
        raise RenameError('at_least_one_--map_OLD=NEW')
    names = [n for pair in maps for n in pair]
    if len(set(names)) != len(names):
        raise RenameError('maps_must_not_share_names_no_chains_or_swaps')
    return maps


def _stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _roots(root):
    """The tasks root as configured and resolved (/tmp vs /private/tmp): stored paths use either form."""
    out = []
    for form in (os.path.normpath(str(root)), os.path.realpath(str(root))):
        if form not in out:
            out.append(form)
    return out


class Rewriter:
    """value -> rewritten value: an exact OLD id, an absolute `<root>/OLD[/...]` path, or the string leaves of a JSON
    document (recursively, also JSON held in a string leaf). Nothing else — never a substring of free text."""

    def __init__(self, maps, root):
        self.maps, self.ids = list(maps), dict(maps)
        self.prefixes = [(f'{top}/{old}', f'{top}/{new}') for top in _roots(root) for old, new in maps]
        self.olds = tuple(self.ids)

    def leaf(self, text):
        if text in self.ids:
            return self.ids[text]
        for old, new in self.prefixes:
            if text == old or text.startswith(old + '/'):
                return new + text[len(old):]
        return text

    def value(self, text):
        new = self.leaf(text)
        if new != text:
            return new
        if text.lstrip()[:1] in ('{', '[', '"'):
            try:
                json.loads(text)
            except ValueError:
                return text
            return STRING_RE.sub(self._literal, text)
        return text

    def _literal(self, match):
        literal = match.group(0)
        decoded = json.loads(literal)
        new = self.value(decoded)
        if new == decoded:
            return literal
        raw_unicode = any(ord(c) > 127 for c in literal)
        return json.dumps(new, ensure_ascii=not raw_unicode and '\\u' in literal)

    def mentions(self, text):
        return mention_count(text, self.maps) > 0


def mention_count(text, maps):
    """Occurrences of an OLD id in free text, not counting it inside a NEW id that contains it (a reverse map)."""
    total = 0
    for old, new in maps:
        total += (text.replace(new, '') if old in new else text).count(old)
    return total


def _tables(db):
    """Ordinary tables (no sqlite_* internals, views, virtual tables or their shadow tables)."""
    try:
        listed = [(r[1], r[2]) for r in db.execute('PRAGMA main.table_list')]
        names = [name for name, kind in listed if kind == 'table']
    except sqlite3.OperationalError:  # SQLite < 3.37
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                                          "upper(coalesce(sql,'')) NOT LIKE 'CREATE VIRTUAL TABLE%'")]
    return sorted(n for n in names if not n.startswith('sqlite_'))


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _row_key(db, table):
    """(select expression, where clause, key column count): rowid, or the primary key of a WITHOUT ROWID table."""
    try:
        db.execute(f'SELECT rowid FROM {_q(table)} LIMIT 1').fetchall()
        return 'rowid', 'rowid=?', 1
    except sqlite3.OperationalError:
        pk = [r[1] for r in sorted(db.execute(f'PRAGMA table_info({_q(table)})'), key=lambda r: r[5]) if r[5]]
        return ', '.join(map(_q, pk)), ' AND '.join(f'{_q(c)}=?' for c in pk), len(pk)


def scan(db, rewriter):
    """{(table, column): [(key values, old text, new text)]} of every rewritable TEXT value, plus the count of values
    that mention an OLD id but are not rewritable (free text) per table/column."""
    changes, mentions = {}, {}
    where = ' OR '.join(['instr({col}, ?) > 0'] * len(rewriter.olds))
    for table in _tables(db):
        key_expr, _, nkey = _row_key(db, table)
        for column in [r[1] for r in db.execute(f'PRAGMA table_info({_q(table)})')]:
            col = _q(column)
            sql = (f'SELECT {key_expr}, {col} FROM {_q(table)} WHERE typeof({col})=\'text\' AND ('
                   + where.format(col=col) + ')')
            for row in db.execute(sql, rewriter.olds):
                key, text = tuple(row[:nkey]), row[nkey]
                new = rewriter.value(text)
                if new != text:
                    changes.setdefault((table, column), []).append((key, text, new))
                if rewriter.mentions(new):
                    mentions[f'{table}.{column}'] = mentions.get(f'{table}.{column}', 0) + 1
    return changes, mentions


def counts(changes):
    out = {}
    for (table, column), rows in sorted(changes.items()):
        out.setdefault(table, {})[column] = len(rows)
    return out


def rewrite(db, changes):
    """Inside the caller's transaction. A UNIQUE violation (NEW rows already present) names its table/column."""
    for (table, column), rows in sorted(changes.items()):
        _, where, _ = _row_key(db, table)
        sql = f'UPDATE {_q(table)} SET {_q(column)}=? WHERE {where}'
        for key, _, new in rows:
            try:
                db.execute(sql, (new, *key))
            except sqlite3.IntegrityError as exc:
                raise RenameError(f'unique_conflict_{table}.{column}: {exc} (rows for the new id already exist)') from None


def verify_clean(db, rewriter):
    """Rewritable leftovers after the rewrite (must be 0)."""
    changes, _ = scan(db, rewriter)
    return sum(len(rows) for rows in changes.values())


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def _points_to(link, target):
    try:
        return os.path.islink(link) and os.path.realpath(link) == os.path.realpath(target)
    except OSError:
        return False


def fs_plan(root, old, new, symlink=True):
    """State of one map on disk -> {'state', 'action'}: move (fresh), restore (the reverse of an earlier rename:
    NEW is a symlink to OLD), migrated (nothing to do), missing (no workspace: database only); raises when unsafe."""
    src, dst = os.path.join(str(root), old), os.path.join(str(root), new)
    src_link, dst_link = os.path.islink(src), os.path.islink(dst)
    src_dir = not src_link and os.path.isdir(src)
    dst_dir = not dst_link and os.path.isdir(dst)
    base = {'old': src, 'new': dst}
    if src_dir and dst_link:
        if _points_to(dst, src):
            return {**base, 'state': 'pending', 'action': 'restore'}
        raise RenameError(f'new_name_is_a_foreign_symlink: {new}')
    if src_dir and dst_dir:
        with os.scandir(dst) as entries:
            occupied = any(entries)
        if occupied:
            raise RenameError(f'new_workspace_already_exists_with_content: {new}')
        return {**base, 'state': 'pending', 'action': 'move', 'replace_empty_dir': True, 'symlink': symlink}
    if src_dir:
        if os.path.lexists(dst):
            raise RenameError(f'new_name_exists_and_is_not_a_directory: {new}')
        return {**base, 'state': 'pending', 'action': 'move', 'symlink': symlink}
    if src_link:
        if dst_dir and _points_to(src, dst):
            return {**base, 'state': 'migrated', 'action': None}
        raise RenameError(f'old_name_is_a_foreign_symlink: {old}')
    if dst_dir:
        return {**base, 'state': 'migrated', 'action': None}
    if dst_link:
        raise RenameError(f'new_name_is_a_dangling_or_foreign_symlink: {new}')
    return {**base, 'state': 'missing', 'action': None}


def fs_apply(plan, undo):
    """Perform one planned move; every step is appended to `undo` first-in-last-out."""
    src, dst = plan['old'], plan['new']
    if plan['action'] == 'restore':
        target = os.readlink(dst)
        os.unlink(dst)
        undo.append(('symlink', target, dst))
        os.rename(src, dst)
        undo.append(('rename', dst, src))
    elif plan['action'] == 'move':
        if plan.get('replace_empty_dir'):
            mode = os.stat(dst).st_mode & 0o777
            os.rmdir(dst)
            undo.append(('mkdir', dst, mode))
        os.rename(src, dst)
        undo.append(('rename', dst, src))
        if plan.get('symlink'):
            os.symlink(os.path.basename(dst), src)  # relative: <root>/OLD -> NEW
            undo.append(('unlink', src))


def fs_undo(undo):
    errors = []
    while undo:
        step = undo.pop()
        try:
            if step[0] == 'rename':
                os.rename(step[1], step[2])
            elif step[0] == 'unlink':
                os.unlink(step[1])
            elif step[0] == 'symlink':
                os.symlink(step[1], step[2])
            elif step[0] == 'mkdir':
                os.mkdir(step[1], step[2])
        except OSError as exc:
            errors.append(f'{step[0]} {step[-1]}: {exc.strerror}')
    return errors


def workspace_mentions(path, maps):
    """Files under the workspace (no symlinks followed, hidden dirs skipped) that still mention an OLD id."""
    found, scanned = [], 0
    if not os.path.isdir(path):
        return found
    for top, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in sorted(files):
            full = os.path.join(top, name)
            if scanned >= MAX_FILES or len(found) >= MAX_REPORTED_FILES:
                return found
            try:
                if os.path.islink(full) or os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, 'rb') as stream:
                    text = stream.read().decode('utf-8', errors='ignore')
            except OSError:
                continue
            scanned += 1
            hits = mention_count(text, maps)
            if hits:
                found.append({'file': os.path.relpath(full, path), 'mentions': hits})
    return found


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def config_changes(worker_config, maps):
    """(changes [(key, value)], still_naming_old [key], error): the config keys that name an OLD id."""
    try:
        config = worker_config.load()[0]
    except Exception as exc:  # noqa: BLE001 - reported; --update-config refuses on it
        return [], [], str(exc)[:200]
    ids = dict(maps)
    changes = [(key, ids[config['intake'][key.split('.')[1]]]) for key in CONFIG_KEYS
               if config['intake'].get(key.split('.')[1]) in ids]
    lived = list(config['dispatch'].get('long_lived_tasks') or [])
    if any(t in ids for t in lived):
        changes.append(('dispatch.long_lived_tasks', [ids.get(t, t) for t in lived]))
    return changes, [k for k, _ in changes], None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _lock(db_path):
    """The supervisor's singleton lock (run_loop: `<db>.lock`, LOCK_EX|LOCK_NB). Returns the open file or None."""
    handle = open(str(db_path) + '.lock', 'a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _supervisor_alive(rt, db):
    try:
        row = db.execute('SELECT pid, birth FROM runtime WHERE id=1').fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[1] and rt.pid_birth(row[0]) == row[1])


def _in_flight(db, olds):
    marks = ','.join('?' * len(olds))
    try:
        runs = [r[0] for r in db.execute(f"SELECT run_id FROM consumer_runs WHERE state='running' AND task IN ({marks})",
                                         olds)]
        leases = [r[0] for r in db.execute(f'SELECT task_key FROM consumer_leases WHERE task_key IN ({marks})', olds)]
    except sqlite3.Error:
        return {}
    return {k: v for k, v in (('running_runs', runs), ('leases', leases)) if v}


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o700)


def _new_file(path):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _write_manifest(path, data):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
    os.chmod(path, 0o600)


def _backup_paths(backup_dir, db_path, now):
    stamp = _stamp(now)
    for n in range(100):
        suffix = stamp if n == 0 else f'{stamp}-{n}'
        backup = backup_dir / f'{db_path.name}.pre-task-rename-{suffix}'
        manifest = backup_dir / f'task-rename-{suffix}.json'
        if not backup.exists() and not manifest.exists():
            return backup, manifest
    raise RenameError('backup_name_exhausted')


def _commit(db):
    db.commit()


def run(rt, db_path, map_values, *, apply=False, root=None, backup_dir=None, update_config=False, symlink=True,
        now=None):
    now = time.time() if now is None else now
    maps = parse_maps(map_values)
    db_path = Path(db_path).expanduser().resolve()
    root = Path(root).expanduser() if root else rt.registry.root()
    if not db_path.is_file():
        raise RenameError('state_database_missing')
    backup_dir = Path(backup_dir).expanduser() if backup_dir else db_path.parent / 'backups'
    rewriter = Rewriter(maps, root)
    report = {'ok': True, 'dry_run': not apply, 'db': str(db_path), 'root': str(root),
              'maps': [{'old': o, 'new': n} for o, n in maps]}
    lock = None
    try:
        if apply:
            lock = _lock(db_path)
            if lock is None:
                raise RenameError('supervisor_running_boot_out_the_launchd_services_first (KeepAlive restarts a killed '
                                  'supervisor; `launchctl bootout` it, do not just kill it)')
        report['fs'], refused = [], []
        for old, new in maps:
            try:
                report['fs'].append(fs_plan(root, old, new, symlink))
            except RenameError as exc:
                refused.append(str(exc))
                report['fs'].append({'old': str(Path(root) / old), 'new': str(Path(root) / new), 'state': 'refused',
                                     'action': None, 'error': str(exc)})
        if refused and apply:
            raise RenameError('; '.join(refused))
        cfg_changes, naming_old, cfg_error = config_changes(rt.worker_config, maps)
        report['config'] = {'path': str(rt.worker_config.config_path()), 'still_naming_old': naming_old,
                            'update_requested': bool(update_config)}
        if cfg_error:
            report['config']['error'] = cfg_error
            if update_config and apply:
                raise RenameError('worker_config_unreadable_fix_it_before_--update-config')
        if update_config and cfg_changes:  # validated before anything is written (same rules as `config set`)
            rt.worker_config.set_many(cfg_changes, 'task-rename', dry_run=True)
            report['config']['changes'] = [{'key': k, 'new': v} for k, v in cfg_changes]
        if apply:
            db = rt.connect(db_path)
        else:  # simulate on an in-memory copy of a read-only connection: nothing is written
            source = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=20)
            db = sqlite3.connect(':memory:')
            try:
                source.backup(db)
            finally:
                source.close()
            report['supervisor_running'] = _supervisor_alive(rt, db)
        try:
            report.update(_migrate(db, db_path, maps, rewriter, report, apply, backup_dir, now))
        except RenameError as exc:
            if apply:
                raise
            report.update(ok=False, error=str(exc))  # a dry run reports why the apply would refuse
            return report
        finally:
            db.close()
        if refused:
            report.update(ok=False, error='; '.join(refused))
        if apply and update_config and cfg_changes:
            try:
                result = rt.worker_config.set_many(cfg_changes, 'task-rename')
                report['config']['applied'] = [r['key'] for r in result['changes']]
            except Exception as exc:  # noqa: BLE001 - the database / folders are migrated; say so and how to finish
                report.update(ok=False, error='config_update_failed_after_migration: ' + str(exc)[:200],
                              hint='set the listed keys with `config set` (the database and folders are migrated)')
        report['config']['still_naming_old'] = config_changes(rt.worker_config, maps)[1]
        if report['config']['still_naming_old']:  # the router would keep addressing OLD (a second task row for it)
            report['warnings'] = ['config_still_names_old_rerun_with_--update-config_or_config_set: '
                                  + ', '.join(report['config']['still_naming_old'])]
        report['already_migrated'] = bool(not report['total_changes'] and all(
            f['state'] in ('migrated', 'missing') for f in report['fs']) and not (update_config and cfg_changes))
        for entry in report['fs']:
            moved = entry['action'] and apply and report.get('applied')
            entry['workspace_mentions'] = workspace_mentions(
                entry['new'] if moved or entry['state'] == 'migrated' else entry['old'], maps)
        if report.get('manifest'):
            _write_manifest(Path(report['manifest']), {**report, 'status': 'applied' if report['ok'] else 'partial'})
        return report
    finally:
        if lock is not None:
            lock.close()


def _migrate(db, db_path, maps, rewriter, report, apply, backup_dir, now):
    out = {'in_flight': _in_flight(db, [old for old, _ in maps])}
    db.execute('BEGIN IMMEDIATE')
    undo, manifest = [], None
    try:
        changes, mentions = scan(db, rewriter)
        out.update(changes=counts(changes), total_changes=sum(len(r) for r in changes.values()),
                   unrewritten_mentions=mentions)
        fs_work = [f for f in report['fs'] if f['action']]
        if apply and (changes or fs_work):
            _private_dir(backup_dir)
            backup, manifest = _backup_paths(backup_dir, db_path, now)
            _new_file(backup)
            # Online backup through a second connection: this one holds the write lock (no writer can commit, nothing
            # is rewritten yet), so the WAL reader copies exactly the pre-rename state (a backup from the writing
            # connection itself would wait for its own lock forever).
            source, target = sqlite3.connect(str(db_path), timeout=20), sqlite3.connect(str(backup))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(backup, 0o600)
            out.update(backup=str(backup), manifest=str(manifest))
            _write_manifest(manifest, {**report, **out, 'status': 'started', 'at': _stamp(now)})
        rewrite(db, changes)
        left = verify_clean(db, rewriter)
        if left:
            raise RenameError(f'leftovers_after_rewrite: {left}')
        out['leftovers'] = 0
        if not apply:
            db.rollback()
            out['would_backup_to'] = str(backup_dir)
            return out
        for plan in fs_work:
            fs_apply(plan, undo)
        _commit(db)
    except BaseException as exc:
        db.rollback()
        errors = fs_undo(undo)
        if manifest is not None:
            _write_manifest(manifest, {**report, **out, 'status': 'rolled_back', 'error': str(exc)[:300],
                                       'undo_errors': errors})
        if errors:
            raise RenameError(f'{exc} (and undoing the folder moves failed: {"; ".join(errors)})') from None
        raise
    out['applied'] = bool(changes or fs_work)
    return out

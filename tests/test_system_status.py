"""0.78.6 (F18): system status — the cgroup / proc readers on fake trees, directory sizes, the threshold levels with
their margin, one notice per breach episode, the sparse `system` config section and the console's read model."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

from kage.core import system_status as S
from kage.core import worker_config as WC

GIB = 1024 ** 3
T0 = 1_800_000_000.0  # the size pass is due once its cadence has passed since 0


def tree(root, files):
    for rel, text in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Path(root)


def store():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE consumer_runs (task TEXT, state TEXT)")
    return db


def runtime(config):
    @contextlib.contextmanager
    def transaction(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    errors = []
    return types.SimpleNamespace(transaction=transaction, safe_error=lambda e: type(e).__name__,
                                 worker_config=types.SimpleNamespace(load=lambda: (config, None)),
                                 _meta_error=lambda db, key, exc: errors.append(key), registry=None, errors=errors)


def status(disk_pct=None, mem_pct=None):
    disks = [] if disk_pct is None else [{'mount': '/srv/owner', 'holds': ['home', 'state'], 'total': 49 * GIB,
                                          'used': int(49 * GIB * disk_pct / 100), 'free': int(49 * GIB * (100 - disk_pct) / 100),
                                          'pct': disk_pct, 'inodes_pct': 3.0}]
    memory = {'source': 'cgroup2', 'used': int(4 * GIB * (mem_pct or 0) / 100), 'limit': 4 * GIB, 'limited': True,
              'pct': mem_pct}
    return {'at': 0, 'platform': 'linux', 'disks': disks, 'memory': memory,
            'cpu': {'pct': None}, 'cpu_sample': {'source': None, 'busy_seconds': None, 'at': 0},
            'processes': {'total': 40, 'kage_children': 7, 'pids_current': None, 'pids_max': None}}


class ReadersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_cgroup_v2_memory_is_the_working_set_against_memory_max(self):
        proc = tree(self.root / 'proc', {'self/cgroup': '0::/\n', 'meminfo': 'MemTotal: 16384000 kB\nMemAvailable: 9000000 kB\n'})
        cg = tree(self.root / 'cg', {'cgroup.controllers': 'cpu memory pids', 'memory.current': str(3 * GIB),
                                      'memory.max': str(4 * GIB), 'memory.stat': f'anon 100\ninactive_file {GIB}\n',
                                      'memory.events': 'low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\n'})
        mem = S.memory(proc, cg)
        self.assertEqual((mem['source'], mem['used'], mem['limit'], mem['limited'], mem['pct'], mem['oom_kills']),
                         ('cgroup2', 2 * GIB, 4 * GIB, True, 50.0, 1))

    def test_an_unlimited_cgroup_is_measured_against_the_host_and_meminfo_is_the_fallback(self):
        proc = tree(self.root / 'proc', {'self/cgroup': '0::/\n', 'meminfo': 'MemTotal: 1000 kB\nMemAvailable: 250 kB\n'})
        cg = tree(self.root / 'cg', {'cgroup.controllers': 'memory', 'memory.current': '512000', 'memory.max': 'max\n',
                                      'memory.stat': 'inactive_file 0\n'})
        mem = S.memory(proc, cg)
        self.assertEqual((mem['limit'], mem['limited'], mem['pct']), (1024000, False, 50.0))
        mem = S.memory(proc, self.root / 'no-cgroup')
        self.assertEqual((mem['source'], mem['used'], mem['pct']), ('meminfo', 750 * 1024, 75.0))

    def test_cpu_quota_and_rate_between_two_samples(self):
        proc = tree(self.root / 'proc', {'self/cgroup': '0::/\n'})
        cg = tree(self.root / 'cg', {'cgroup.controllers': 'cpu', 'cpu.stat': 'usage_usec 10000000\n',
                                      'cpu.max': '200000 100000\n'})
        first = S.cpu_sample(proc, cg)
        self.assertEqual((first['source'], first['busy_seconds'], first['limit_cores'], first['quota']),
                         ('cgroup2', 10.0, 2.0, True))
        later = dict(first, busy_seconds=13.0, at=first['at'] + 10)
        out = S.cpu(later, first)
        self.assertEqual((out['cores_used'], out['pct']), (0.3, 15.0))
        self.assertIsNone(S.cpu(later, None)['pct'])  # one sample: no rate yet

    def test_proc_stat_is_the_cpu_fallback(self):
        proc = tree(self.root / 'proc', {'stat': 'cpu  100 0 50 1000 20 0 0 0 0 0\ncpu0 1 2 3\n'})
        sample = S.cpu_sample(proc, self.root / 'no-cgroup')
        self.assertEqual(sample['source'], 'proc_stat')
        self.assertGreater(sample['busy_seconds'], 0)

    def test_kage_children_are_the_supervisors_descendants(self):
        files = {'self/stat': '1 (x) S 0', '1/stat': '1 (init) S 0 1', '50/stat': '50 (python3) S 1 50',
                 '60/stat': '60 (claude) S 50 60', '61/stat': '61 (node (mcp)) S 60 61', '70/stat': '70 (sshd) S 1 70'}
        proc = tree(self.root / 'proc', files)
        cg = tree(self.root / 'cg', {'cgroup.controllers': 'pids', 'pids.current': '6', 'pids.max': '512'})
        (proc / 'self' / 'cgroup').write_text('0::/\n')
        out = S.processes(50, proc, cg)
        self.assertEqual((out['total'], out['kage_children'], out['pids_current'], out['pids_max']), (5, 2, 6, 512))

    def test_disks_report_one_entry_per_filesystem(self):
        (self.root / 'state').mkdir()
        out = S.disks({'home': self.root, 'state': self.root / 'state', 'gone': self.root / 'missing'})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['holds'], ['home', 'state'])
        self.assertTrue(0 <= out[0]['pct'] <= 100)
        self.assertGreater(out[0]['total'], 0)

    def test_walk_sizes_sums_files_and_marks_a_pass_cut_short(self):
        tree(self.root / 'a', {'x.txt': 'x' * 5000, 'sub/y.txt': 'y' * 9000})
        tree(self.root / 'b', {'z.txt': 'z'})
        os.symlink(self.root / 'a', self.root / 'b' / 'link')  # not followed
        out = S.walk_sizes({'a': self.root / 'a', 'b': self.root / 'b', 'none': self.root / 'nope'})
        self.assertEqual([c['label'] for c in out], ['a', 'b'])
        self.assertEqual((out[0]['files'], out[0]['partial']), (2, False))
        self.assertGreaterEqual(out[0]['bytes'], 14000)
        cut = S.walk_sizes({'a': self.root / 'a'}, max_entries=1)
        self.assertTrue(cut[0]['partial'])

    def test_watched_paths(self):
        out = S.watched_paths({'watch_paths': ['~/src', '/data/mirrors']}, home='/srv/owner',
                              state_dir='/srv/owner/.local/share/kage/state-owner', workspace='/srv/owner/tasks')
        self.assertEqual({k: str(v) for k, v in out.items()}, {
            'claude_sessions': '/srv/owner/.claude/projects', 'codex_sessions': '/srv/owner/.codex',
            'workspaces': '/srv/owner/tasks', 'state': '/srv/owner/.local/share/kage/state-owner',
            'backups': '/srv/owner/.local/share/kage/state-owner/backups', '~/src': '/srv/owner/src',
            '/data/mirrors': '/data/mirrors'})


class LevelsTest(unittest.TestCase):
    def test_levels_keep_a_margin_before_they_drop(self):
        lf = S.level_for
        self.assertEqual([lf(v, S.OK, 80, 90) for v in (79.9, 80, 90)], ['ok', 'warning', 'critical'])
        self.assertEqual(lf(87.5, S.CRITICAL, 80, 90), 'critical')
        self.assertEqual(lf(86.9, S.CRITICAL, 80, 90), 'warning')
        self.assertEqual(lf(77.5, S.WARNING, 80, 90), 'warning')
        self.assertEqual(lf(76.9, S.WARNING, 80, 90), 'ok')


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'state' / 'state.sqlite'
        self.db = store()
        self.posts = []
        self.config = {}
        self.rt = runtime(self.config)
        sizes = [{'label': 'claude_sessions', 'path': '/srv/owner/.claude/projects', 'bytes': 6 * GIB, 'files': 900,
                  'partial': False},
                 {'label': '~/src', 'path': '/srv/owner/src', 'bytes': 3 * GIB, 'files': 80000, 'partial': False}]
        self.mon = S.Monitor('zh-CN', post=lambda rt, db, metric, kind, text: self.posts.append((metric, kind, text)) or
                             f'ts{len(self.posts)}', walk=lambda paths: sizes)

    def run_at(self, now, disk=None, mem=None):
        with mock.patch.object(S, 'probe', lambda *a, **k: status(disk, mem)):
            return self.mon.evaluate(self.rt, self.db, self.db_path, now=T0 + now, wait_walk=True)

    def test_one_notice_per_breach_episode(self):
        out = self.run_at(1000.0, disk=85.0, mem=40.0)
        self.assertEqual(out, [{'metric': 'disk', 'event': 'warning', 'value': 85.0},
                               {'metric': 'disk', 'event': 'notice_warning'}])
        self.assertEqual(self.posts[0][:2], ('disk', 'warning'))
        self.assertEqual(self.posts[0][2], '磁盘快满了：/srv/owner 已用 85%（还剩 7.3 GiB，共 49.0 GiB），超过了 80% 的提醒线。\n'
                                           '占用最多的目录：/srv/owner/.claude/projects 6.0 GiB、/srv/owner/src 3.0 GiB。')
        self.assertEqual(self.run_at(1100.0, disk=86.0), [])           # within check_minutes: no read
        self.assertEqual(self.run_at(1400.0, disk=86.0, mem=40.0), [])  # same level: no second notice
        self.run_at(1800.0, disk=92.0, mem=40.0)                        # warning -> critical: a new notice
        self.assertEqual([p[1] for p in self.posts], ['warning', 'critical'])
        self.run_at(2200.0, disk=88.0, mem=40.0)                        # within the margin: still critical
        self.run_at(2600.0, disk=86.0, mem=40.0)                        # down to warning: silent
        self.assertEqual(len(self.posts), 2)
        self.run_at(3000.0, disk=91.0, mem=40.0)                        # critical again after it had dropped
        self.run_at(3400.0, disk=70.0, mem=40.0)                        # recovered: one notice
        self.assertEqual([p[1] for p in self.posts], ['warning', 'critical', 'critical', 'recovered'])
        self.assertEqual(self.posts[-1][2], '磁盘已恢复正常：/srv/owner 现在已用 70%（还剩 14.7 GiB）。')
        self.run_at(3800.0, disk=71.0, mem=40.0)
        self.assertEqual(len(self.posts), 4)
        alerts, snap, events = S.stored(self.db)
        self.assertEqual((alerts['disk']['level'], alerts['disk']['announced']), ('ok', 'ok'))
        self.assertEqual(alerts['memory']['level'], 'ok')
        self.assertEqual(json.loads(snap['consumers'])[0]['label'], 'claude_sessions')
        self.assertIn('notice_recovered', [e['kind'] for e in events])

    def test_memory_notice_names_the_children_and_english_copy(self):
        mon = S.Monitor('en', post=lambda rt, db, metric, kind, text: self.posts.append((metric, kind, text)) or 'ts',
                        walk=lambda paths: [])
        with mock.patch.object(S, 'probe', lambda *a, **k: status(50.0, 96.0)):
            mon.evaluate(self.rt, self.db, self.db_path, now=T0, wait_walk=True)
        self.assertEqual(self.posts, [('memory', 'critical', 'Memory is almost exhausted: 3.8 GiB of 4.0 GiB used (96%), '
                                       'over the 95% critical line; at the limit the system kills processes outright (OOM).\n'
                                       'Kage has 7 child processes right now (background runs and what they started).')])

    def test_a_failed_post_keeps_the_level_and_retries(self):
        calls = []

        def post(rt, db, metric, kind, text):
            calls.append(kind)
            if len(calls) == 1:
                raise RuntimeError('ratelimited')
            return 'ts'
        self.mon._post = post
        self.run_at(1000.0, disk=95.0, mem=10.0)
        self.assertEqual(S.stored(self.db)[0]['disk']['notice_error'], 'RuntimeError')
        self.run_at(1400.0, disk=95.0, mem=10.0)
        self.assertEqual(calls, ['critical', 'critical'])
        self.assertIsNone(S.stored(self.db)[0]['disk']['notice_error'])
        self.run_at(1800.0, disk=95.0, mem=10.0)
        self.assertEqual(len(calls), 2)

    def test_thresholds_from_config_and_the_kill_switch(self):
        self.config['system'] = {'disk_warn_pct': 90, 'disk_crit_pct': 97, 'check_minutes': 1}
        self.run_at(1000.0, disk=85.0, mem=10.0)
        self.assertEqual(self.posts, [])
        self.run_at(1061.0, disk=91.0, mem=10.0)
        self.assertEqual([p[1] for p in self.posts], ['warning'])
        self.config['system'] = {'enabled': False}
        self.assertEqual(self.run_at(2000.0, disk=99.0, mem=99.0), [])
        self.assertEqual(len(self.posts), 1)

    def test_the_size_pass_runs_in_the_background_at_its_own_cadence(self):
        walks = []
        self.mon.walk = lambda paths: walks.append(sorted(paths)) or []
        self.run_at(1000.0, disk=10.0, mem=10.0)
        self.run_at(1400.0, disk=10.0, mem=10.0)
        self.assertEqual(len(walks), 1)
        self.assertIn('claude_sessions', walks[0])
        self.run_at(1000.0 + 3600, disk=10.0, mem=10.0)
        self.assertEqual(len(walks), 2)

    def test_the_default_post_uses_the_health_notice_channel(self):
        mon = S.Monitor('en', walk=lambda paths: [])
        sent = []
        self.rt.provider_health = types.SimpleNamespace(
            post_notice=lambda rt, db, provider, kind, text, context_key: sent.append((provider, kind, context_key)) or 'ts')
        with mock.patch.object(S, 'probe', lambda *a, **k: status(81.0, 10.0)):
            mon.evaluate(self.rt, self.db, self.db_path, now=T0, wait_walk=True)
        self.assertEqual(sent, [('system', 'disk_warning', 'system.notice_context')])


class OverviewTest(unittest.TestCase):
    def test_live_levels_and_stored_rows(self):
        db = store()
        db.execute("INSERT INTO consumer_runs VALUES ('t', 'running')")
        with mock.patch.object(S, 'probe', lambda *a, **k: status(83.0, 30.0)):
            out = S.overview(db, {'system': {'disk_warn_pct': 82, 'disk_crit_pct': 95}}, {'home': '/'})
        self.assertEqual((out['alert'], out['levels'], out['processes']['agent_runs']),
                         ('warning', {'disk': 'warning', 'memory': 'ok'}, 1))
        self.assertEqual(out['thresholds']['disk_warn_pct'], 82)
        self.assertIsNone(out['consumers'])
        self.assertEqual(out['events'], [])

    def test_the_real_probe_runs_here(self):
        out = S.overview(None, {}, {'home': Path.home()})
        self.assertIn(out['alert'], ('ok', 'warning', 'critical'))
        self.assertTrue(out['disks'])
        self.assertIn('loadavg', out['cpu'])


class ConfigTest(unittest.TestCase):
    def test_the_sparse_system_section(self):
        WC._validate_system({'disk_warn_pct': 70, 'watch_paths': ['~/src', '/data']})
        for bad, code in (({'disk_warn_pct': 90, 'disk_crit_pct': 90}, 'system_disk_warn_pct'),
                          ({'memory_warn_pct': 96}, 'system_memory_warn_pct'),
                          ({'nope': 1}, 'unknown_key_nope'), ({'watch_paths': ['src']}, 'system_watch_paths'),
                          ({'watch_paths': ['~/a/../b']}, 'system_watch_paths'), ({'check_minutes': 0}, 'system_check_minutes'),
                          ({'enabled': 'yes'}, 'system_enabled')):
            with self.subTest(bad), self.assertRaises(ValueError) as ctx:
                WC._validate_system(bad)
            self.assertIn(code, str(ctx.exception))
        self.assertEqual(S.settings({'system': {'disk_warn_pct': 70}})['disk_crit_pct'], 90)
        self.assertEqual(S.settings({}), WC.SYSTEM_DEFAULTS)

    def test_set_value_opens_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'worker.json'
            defaults = json.loads((Path(WC.__file__).parents[1] / 'worker_defaults.json').read_text())
            wc = WC.WorkerConfig('en', defaults=defaults, config_env='KAGE_TEST_NO_SUCH_ENV', default_path=path,
                                 owner_key='owner', owner_name='Owner')
            wc.set_value('system.disk_warn_pct', '85', 'test', path=path)
            config = wc.load(path)[0]
            self.assertEqual(config['system'], {'disk_warn_pct': 85})
            with self.assertRaises(ValueError):
                wc.set_value('system.disk_warn_pct', '95', 'test', path=path)  # not below crit 90
            self.assertNotIn('system', wc.load(Path(tmp) / 'absent.json')[0])  # defaults: never filled in


if __name__ == '__main__':
    unittest.main()

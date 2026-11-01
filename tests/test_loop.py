import contextlib
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import tempfile
import types
import unittest
from unittest import mock
import urllib.error

from kage.adapters.providers import codex
from kage.core import loop as L

OWNER, BOT = 'U0000TEST01', 'U0000TEST02'


class Slack:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def api_call(self, endpoint, token, params=None, json_body=None):
        self.calls.append((endpoint, token, params or json_body))
        if self.fail:
            raise self.fail
        if endpoint == 'auth.test':
            return {'team_id': 'T0000TEST01', 'user_id': BOT if token == 'bot-token' else OWNER}
        return {'messages': [{'ts': '1.000001', 'user': 'U0000TEST03', 'text': 'x'}]}

    default_bot_token = staticmethod(lambda: 'bot-token')
    default_user_token = staticmethod(lambda: 'user-token')


def core(slack=None, **rt):
    runtime = types.SimpleNamespace(slack=slack or Slack(), transaction=contextlib.nullcontext, **rt)
    return L.LoopCore('en', owner_key='owner', owner_name='Mira', workspace='T0000TEST01', owner_slack_user=OWNER,
                      bot_user=BOT, read_cache={}, rate_limits={}, identity_cache={},
                      receipt_emoji={'claude': 'test-claude', 'codex': 'test-codex'},
                      no_autostart_env='TEST_NO_AUTOSTART', rt=runtime)


class LoopTest(unittest.TestCase):
    def test_reads_use_the_deployment_caches(self):
        c = core()
        first = c.read_api('conversations.history', 'user-token', {'channel': 'C0000TEST01'})
        self.assertEqual(c.read_api('conversations.history', 'user-token', {'channel': 'C0000TEST01'}), first)
        self.assertEqual(len(c.rt.slack.calls), 1)                              # the second read came from the cache
        self.assertEqual(len(c.read_cache), 1)
        limited = core(Slack(urllib.error.HTTPError('slack', 429, 'x', {'Retry-After': '30'}, None)))
        with self.assertRaises(urllib.error.HTTPError):
            limited.read_api('conversations.history', 't', {})
        self.assertIn('conversations.history', limited.rate_limits)

    def test_identity_is_checked_against_the_owner_and_cached(self):
        c = core()
        self.assertEqual(c.api_identity(), 'user-token')
        self.assertEqual(c.api_identity(bot=True), 'bot-token')
        self.assertEqual(set(c.identity_cache), {False, True})
        wrong = core()
        wrong.owner_slack_user = 'U0000TEST09'
        with self.assertRaisesRegex(RuntimeError, 'unexpected_slack_identity'):
            wrong.api_identity()

    def test_the_autostart_guard_uses_the_deployment_env_name(self):
        c = core()
        with mock.patch.dict(os.environ, {'TEST_NO_AUTOSTART': '1'}):
            self.assertIsNone(c.start('/nonexistent/db.sqlite'))

    def test_replaced_runtime_names_are_called_through_the_runtime(self):
        seen = []
        watch = {'provider': 'codex', 'owner_pid': 42, 'owner_birth': 'birth', 'session': 's', 'cwd': '/tmp'}
        c = core(pid_birth=lambda pid: seen.append(pid) or 'birth', load=lambda db, wid: dict(watch),
                 claude=types.SimpleNamespace(provider=lambda w: w.get('provider') or 'codex'))
        with self.assertRaisesRegex(ValueError, 'owner_still_alive_use_queue_not_resume'):
            c.recover(None, 'w1', '/tmp/db')
        self.assertEqual(seen, [42])                                             # rt.pid_birth, not the module's


SESSION = '019a3c5e-0000-7000-8000-000000000001'
SHELL, CODEX, LAUNCHER = 4242, 4241, 4000


def ps_args(prompt, mode='resume'):
    """What `ps -o args=` shows for a dispatched run: argv joined with spaces, the shell quoting gone (the B1 report)."""
    argv = codex.Codex().build_command(mode, SESSION, prompt, 'gpt-test', 'xhigh',
                                       ['--dangerously-bypass-approvals-and-sandbox'], '/tmp/kage-runs/r1')
    return ' '.join(argv)


class Ps:
    """`ps -p PID -o …` over a fixed process table: the agent's shell under a codex process."""

    def __init__(self, codex_args):
        self.table = {SHELL: (CODEX, '/bin/zsh', '-zsh'), CODEX: (LAUNCHER, '/opt/homebrew/bin/codex', codex_args)}
        self.calls = []

    def __call__(self, cmd, capture_output=False, text=False):
        self.calls.append(cmd)
        pid, fields = int(cmd[2]), cmd[4]
        ppid, comm, args = self.table.get(pid, (None, None, None))
        out = '' if comm is None else {'ppid=,comm=': f'{ppid:>6} {comm}\n', 'args=': f'{args}\n',
                                       'lstart=': 'Sun Nov  1 10:55:00 2026\n'}[fields]
        return types.SimpleNamespace(stdout=out, stderr='', returncode=0 if out else 1)


class DiscoverOwnerTest(unittest.TestCase):
    """B1: the codex parent's args may not parse as shell words; the owner is still found."""

    def discover(self, codex_args, thread=SESSION):
        ps = Ps(codex_args)
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': thread}), \
                mock.patch.object(L.os, 'getppid', return_value=SHELL), mock.patch.object(L.subprocess, 'run', ps):
            return L.discover_owner(SESSION), ps.calls

    def test_an_unbalanced_quote_in_the_prompt_falls_back_to_whitespace_words(self):
        for prompt in ("New reply in the Slack thread: the mail publish step didn't run after the deploy.",
                       'Reply from the requester: "ship it after the check'):
            args = ps_args(prompt)
            with self.assertRaisesRegex(ValueError, 'No closing quotation'):
                shlex.split(args)                                       # what made `register` fail before B1
            self.assertEqual(self.discover(args)[0], CODEX, prompt)
            self.assertEqual(self.discover(ps_args(prompt, mode='new'))[0], CODEX)

    def test_shared_servers_are_still_not_an_owner(self):
        for args in ("/opt/homebrew/bin/codex app-server --listen stdio:// it's", 'codex --remote ws://host "x',
                     'codex exec-server --port 1 "a b"'):
            self.assertIsNone(self.discover(args)[0], args)

    def test_balanced_arguments_and_other_threads(self):
        self.assertEqual(self.discover(ps_args('a reply with "quotes" and it\'s fine, isn\'t it'))[0], CODEX)
        owner, calls = self.discover(ps_args('x'), thread='another-thread')
        self.assertEqual((owner, calls), (None, []))                    # not this codex thread: ps is never asked


class Sweeps:
    """The two scripted sweeps of a run loop: each item an exception to raise or None."""

    def __init__(self, ask, cards):
        self.ask, self.cards = list(ask), list(cards)

    def ask_sweep(self, db):
        item = self.ask.pop(0) if self.ask else None
        if item:
            raise item

    def card_sweep(self, rt, db):
        item = self.cards.pop(0) if self.cards else None
        if item:
            raise item


class Anything:
    """A runtime part the loop calls but this test does not look at."""

    def __getattr__(self, name):
        return Anything()

    def __call__(self, *args, **kwargs):
        return None


class RunLoopTest(unittest.TestCase):
    """B1: ask_sweep_error / card_thread_sweep_error are cleared by the next sweep that succeeds."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / 'followup.sqlite'

    def connect(self, path):
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, pid INTEGER, birth TEXT, heartbeat REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS runtime_meta (key TEXT PRIMARY KEY, value TEXT)')
        db.commit()
        return db

    def run_loop(self, sweeps, cycles, step):
        seen, clock = [], [1_800_000_000.0]

        class Stop(Exception):
            pass

        def sleep(_):
            with contextlib.closing(sqlite3.connect(self.path)) as db:      # what another process sees
                seen.append(dict(db.execute("SELECT key, value FROM runtime_meta WHERE key LIKE '%sweep_error'")))
            clock[0] += step
            if len(seen) == cycles:
                raise Stop

        rt = Anything()
        rt.connect, rt.pid_birth, rt.VERSION = self.connect, lambda pid: 'birth', '0.0.0'
        rt.due_watches, rt.ask_sweep, rt.safe_error = (lambda db, now: []), sweeps.ask_sweep, (lambda e: type(e).__name__)
        rt.card_threads = types.SimpleNamespace(sweep=sweeps.card_sweep)
        rt.worker_config = types.SimpleNamespace(load=lambda: ({'tasks': {'sweep_interval_minutes': 60}},),
                                                 DEFAULTS={'tasks': {'sweep_interval_minutes': 60}})
        c = core()
        c.rt = rt
        fake_time = types.SimpleNamespace(time=lambda: clock[0], sleep=sleep)
        with mock.patch.object(L, 'time', fake_time), self.assertRaises(Stop):
            c.run_loop(str(self.path))
        return seen

    def test_the_ask_sweep_error_is_cleared_by_the_next_good_sweep(self):
        down = urllib.error.URLError('down')
        seen = self.run_loop(Sweeps([down, down, None], []), 4, step=3600)
        self.assertEqual(seen, [{'ask_sweep_error': 'URLError'}, {'ask_sweep_error': 'URLError'}, {}, {}])

    def test_the_card_thread_sweep_error_is_cleared_by_the_next_good_sweep(self):
        seen = self.run_loop(Sweeps([], [RuntimeError('x'), None, RuntimeError('y'), None]), 4, step=3600)
        self.assertEqual(seen, [{'card_thread_sweep_error': '1800000000 RuntimeError'}, {},
                                {'card_thread_sweep_error': f'{1_800_000_000 + 7200} RuntimeError'}, {}])

    def test_both_recover_at_the_loop_cadence(self):
        # 2 s cycles: the ask sweep backs off 60 s after a failure, the card sweep runs every HEALTH_SECONDS. A note
        # or a clear waits for a cycle without the other's uncommitted error row (they never touch an open transaction).
        down = urllib.error.URLError('down')
        seen = self.run_loop(Sweeps([down] * 3, [RuntimeError('x')] * 4), 150, step=2)
        self.assertTrue(any(len(s) == 2 for s in seen))                 # both failing at once
        self.assertEqual(seen[-1], {})
        recovered = next(i for i, s in enumerate(seen) if i > 0 and not s and seen[i - 1])
        self.assertLess(recovered * 2, 3 * 60 + 4 * L.HEALTH_SECONDS + 10)

    def test_a_good_sweep_with_nothing_recorded_writes_nothing(self):
        self.assertEqual(self.run_loop(Sweeps([], []), 2, step=3600), [{}, {}])

if __name__ == '__main__':
    unittest.main()

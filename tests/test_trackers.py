"""N2: the tracker adapter (`kage.adapters.trackers`). A deployment's tracker names itself in text; a Linear deployment
reads exactly what it read before N2 (golden hashes of 0.76.0's texts), and a deployment without a tracker never reads
the word Linear (every message and pattern, both catalogs)."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest

from kage import locale
from kage.adapters import trackers
from kage.core import lifecycle as L

CATALOGS = Path(locale.__file__).resolve().parent
# sha256[:16] of each text as Kage 0.76.0 had it (with "Linear" / "--linear" / "linear" written in).
GOLDEN = {
    'en': {'lifecycle.orphan_reply.assign': 'a89968a2f40e981f', 'lifecycle.orphan_reply.close': 'd653b636e9a3c76e',
           'lifecycle.continue_prompt.1': '4db1d2452e4ab3b9', 'lifecycle.close_prompt.1': '170d32ebf05aa48a',
           'access.next_steps.1': 'e3d52b5e1962809a', 'intake.launch_prompt.16': '8b3fee0826416391',
           'control.body.how': '393b9310dfb42048', 'control.task.goal': 'cd917f012ca3a064',
           'pattern:lifecycle.terminal_re.1': '83f4b09d864af695', 'pattern:learning.directive_re.1': 'bb35b44a2e79c95b'},
    'zh-CN': {'lifecycle.orphan_reply.assign': '7285c5fcfefd8f9e', 'lifecycle.orphan_reply.close': 'b3f6db8b2ee1a4bb',
              'lifecycle.continue_prompt.1': 'a6a97f3c353f646f', 'lifecycle.close_prompt.1': '624a333a11352195',
              'access.next_steps.1': 'e0eb5c8491bebdb0', 'intake.launch_prompt.16': '16972e30672fd141',
              'control.body.how': '393b9310dfb42048', 'control.task.goal': 'c3b58e54badd7df0',
              'pattern:lifecycle.terminal_re.1': '83f4b09d864af695',
              'pattern:learning.directive_re.1': 'bb35b44a2e79c95b'},
}
LINEAR_RE = re.compile(r'linear', re.I)


def short(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


class TrackerTest(unittest.TestCase):
    def tearDown(self):
        trackers.use(None)

    def test_names_and_terminal_states(self):
        self.assertIs(trackers.for_name(None), trackers.NONE)
        self.assertEqual((trackers.for_name('linear').display, trackers.for_name('jira').name), ('Linear', 'jira'))
        with self.assertRaises(ValueError):
            trackers.for_name('trello')
        self.assertFalse(trackers.NONE.tickets)
        self.assertTrue(trackers.is_terminal({'state': 'Done'}))
        self.assertTrue(trackers.is_terminal({'state': 'In Review', 'type': 'canceled'}))
        self.assertFalse(trackers.is_terminal({'state': 'In Review', 'type': 'started'}))
        self.assertFalse(trackers.is_terminal(None))
        self.assertIs(L._is_terminal_linear, L._is_terminal_ticket)  # the pre-N2 names stay for older assemblies

    def test_linear_reads_a_state_through_the_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'tracker.py'
            script.write_text('import json, sys\nassert sys.argv[1:4] == ["--json", "issue", "get"]\n'
                              'print(json.dumps({"identifier": sys.argv[4], "state": {"name": "In Review", "type": "started"}}))\n')
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            linear = trackers.for_name('linear')
            self.assertEqual(linear.read_state(script, 'TEST-12', 20), {'state': 'In Review', 'type': 'started'})
            self.assertIsNone(linear.read_state(Path(tmp) / 'missing.py', 'TEST-12', 20))
            self.assertIsNone(trackers.NONE.read_state(script, 'TEST-12', 20))

    def test_a_linear_deployment_reads_the_same_text_as_before(self):
        linear = trackers.for_name('linear')
        for language, keys in GOLDEN.items():
            raw = json.loads((CATALOGS / f'{language}.json').read_text(encoding='utf-8'))
            for key, digest in keys.items():
                with self.subTest(language=language, key=key):
                    if key.startswith('pattern:'):
                        text = locale.pattern(language, key.split(':', 1)[1], linear)
                    else:
                        text = raw['messages'][key].replace('{tracker}', linear.display).replace(
                            '{ticket_flag}', linear.ticket_flag)
                    self.assertEqual(short(text), digest)
        trackers.use(linear)
        self.assertIn('set Linear to Done', locale.message('en', 'lifecycle.close_prompt.1', owner='Mira'))
        self.assertIn('add --linear only when', locale.message('en', 'control.body.how', cli='kage', id='m1', name='Mira'))

    def test_without_a_tracker_no_text_names_linear(self):
        trackers.use(trackers.NONE)
        for language in locale.languages():
            raw = json.loads((CATALOGS / f'{language}.json').read_text(encoding='utf-8'))
            for key in raw['messages']:
                if key.endswith('.no_tracker'):
                    continue
                with self.subTest(language=language, key=key):
                    chosen = key + '.no_tracker' if locale.has(language, key + '.no_tracker') else key
                    text = raw['messages'][chosen]
                    if '{tracker}' in text or '{ticket_flag}' in text:
                        self.fail(f'{chosen}: a tracker placeholder with no .no_tracker variant')
                    self.assertIsNone(LINEAR_RE.search(text), chosen)
            for key in raw['patterns']:
                with self.subTest(language=language, pattern=key):
                    self.assertIsNone(LINEAR_RE.search(locale.pattern(language, key)), key)
        self.assertNotIn('Linear', locale.message('zh-CN', 'lifecycle.close_prompt.1', owner='Mira'))

    def test_an_owner_bound_class_names_its_tracker(self):
        def make(tracker):
            return L.Lifecycle('en', tz=None, ticket_task_re=re.compile('^TEST-\\d+'), ticket_id_re=re.compile('TEST-\\d+'),
                               owner_key='owner', owner_name='Mira', owner_user=lambda rt: None, tracker=tracker)
        self.assertIn("someone else's Linear ticket", make(trackers.for_name('linear')).orphan_reply['assign'])
        self.assertEqual(make(trackers.NONE).orphan_reply['assign'], '📝 OK, you change the state / assignment.')
        self.assertTrue(make(trackers.for_name('linear')).terminal_re.search('linear: done'))
        self.assertTrue(make(trackers.NONE).terminal_re.search('ticket: done'))
        self.assertTrue(make(None).is_ticket_task('TEST-12-move'))


if __name__ == '__main__':
    unittest.main()

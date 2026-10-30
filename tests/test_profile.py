import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from kage import profile

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/northwind-mira/deploy'


def example(kind):
    return json.loads((EXAMPLE / profile.FILES[kind]).read_text(encoding='utf-8'))


class DeploymentDir:
    """A private copy of the example deployment with one document replaced."""

    def __init__(self, **docs):
        self.docs = docs

    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        for kind, name in profile.FILES.items():
            target = Path(self.tmp) / name
            if kind in self.docs:
                if self.docs[kind] is not None:
                    target.write_text(json.dumps(self.docs[kind]), encoding='utf-8')
            else:
                shutil.copyfile(EXAMPLE / name, target)
            if target.exists():
                os.chmod(target, 0o644)
        return Path(self.tmp)

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp)


class LoadExampleTest(unittest.TestCase):
    def test_example_deployment_loads(self):
        d = profile.load(EXAMPLE)
        self.assertEqual(d.owner.name, 'Mira Lind')
        self.assertEqual((d.owner.short_name, d.owner.key), ('Mira', 'owner'))   # defaults
        self.assertEqual(d.owner.language, 'en')
        self.assertEqual(d.owner.slack_user, 'U0000TEST01')
        self.assertEqual(d.owner.slack_workspace, 'T0000TEST01')
        self.assertEqual(d.owner.assistant_slack_bot_user, 'U0000TEST02')
        self.assertEqual(d.owner.assistant_slack_app, 'A0000TEST01')
        self.assertEqual(d.org.tracker, 'jira')
        self.assertEqual(d.org.tracker_team, 'PM')
        self.assertEqual(d.owner.slack_workspace_url, 'https://northwind-example.slack.com')
        self.assertEqual(dict(d.org.channels), {'#ops': 'C0000TEST01', '#incidents': 'C0000TEST02'})
        self.assertEqual(d.instance.workspace_root, Path('~/kage/tasks').expanduser())
        self.assertEqual(d.instance.path('notes'), Path('/srv/kage/notes'))
        self.assertIsNone(d.instance.path('missing'))
        self.assertEqual(d.instance.paths['workspace_root'], '~/kage/tasks')   # text stays as written

    def test_profile_objects_are_frozen(self):
        d = profile.load(EXAMPLE)
        with self.assertRaises(Exception):
            d.owner.slack_user = 'U0000TEST09'
        with self.assertRaises(TypeError):
            d.org.channels['#ops'] = 'C0000TEST09'
        with self.assertRaises(TypeError):
            d.instance.paths['workspace_root'] = '/tmp'

    def test_timezone_is_the_iana_zone(self):
        tz = profile.load(EXAMPLE).owner.tz()
        winter = dt.datetime(2026, 1, 15, 12, tzinfo=tz)
        summer = dt.datetime(2026, 7, 15, 12, tzinfo=tz)
        self.assertEqual(winter.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=2))

    def test_ticket_reference_pattern(self):
        rx = profile.load(EXAMPLE).org.ticket_ref_re()
        self.assertTrue(rx.fullmatch('pm-42'))
        self.assertTrue(rx.fullmatch('OPS-123456'))
        self.assertIsNone(rx.fullmatch('OPS-1234567'))
        self.assertIsNone(rx.fullmatch('HR-1'))


class OwnerValidationTest(unittest.TestCase):
    def owner(self, mutate):
        raw = copy.deepcopy(example('owner'))
        mutate(raw)
        return raw

    def assertRefused(self, raw, fragment):
        with self.assertRaises(profile.ProfileError) as ctx:
            profile.parse_owner(raw)
        self.assertIn(fragment, str(ctx.exception))

    def test_schema_is_required(self):
        self.assertRefused(self.owner(lambda r: r.update(schema='kage.owner/v2')), 'schema')

    def test_name_language_timezone_are_required(self):
        for key in ('name', 'language', 'timezone'):
            self.assertRefused(self.owner(lambda r: r['owner'].pop(key)), f'owner.{key}')

    def test_short_name_and_key(self):
        owner = profile.parse_owner(self.owner(lambda r: r['owner'].update(short_name='M.', key='mira')))
        self.assertEqual((owner.short_name, owner.key), ('M.', 'mira'))
        for key in ('Mira', 'mira lind', '1mira', ''):
            self.assertRefused(self.owner(lambda r: r['owner'].update(key=key)), 'owner.key')

    def test_language_must_be_a_tag(self):
        self.assertRefused(self.owner(lambda r: r['owner'].update(language='English')), 'language')

    def test_unknown_timezone_is_refused(self):
        self.assertRefused(self.owner(lambda r: r['owner'].update(timezone='Mars/Olympus')), 'timezone')

    def test_fallback_offset_is_used_only_without_a_tz_database(self):
        raw = self.owner(lambda r: r['owner'].update(timezone='Asia/Shanghai', timezone_fallback_offset='+08:00'))
        with mock.patch('zoneinfo.ZoneInfo', side_effect=Exception('no tzdata')), \
                mock.patch('zoneinfo.available_timezones', return_value=set()):
            tz = profile.parse_owner(raw).tz()
        self.assertEqual(tz, dt.timezone(dt.timedelta(hours=8)))

    def test_no_tz_database_and_no_fallback_is_refused(self):
        raw = self.owner(lambda r: r['owner'].pop('timezone_fallback_offset'))
        with mock.patch('zoneinfo.ZoneInfo', side_effect=Exception('no tzdata')), \
                mock.patch('zoneinfo.available_timezones', return_value=set()):
            self.assertRefused(raw, 'fallback')

    def test_bad_fallback_offset_is_refused(self):
        self.assertRefused(self.owner(lambda r: r['owner'].update(timezone_fallback_offset='8')), 'offset')

    def test_slack_ids_are_validated(self):
        self.assertRefused(self.owner(lambda r: r['identities']['slack'].update(user='C0000TEST01')),
                           'identities.slack.user')
        self.assertRefused(self.owner(lambda r: r['identities']['slack'].update(workspace='U0000TEST01')),
                           'identities.slack.workspace')
        self.assertRefused(self.owner(lambda r: r['assistant']['slack'].update(app='U0000TEST01')),
                           'assistant.slack.app')

    def test_workspace_url_is_validated(self):
        for url in ('http://northwind-example.slack.com', 'https://slack.com.evil.example', 'northwind'):
            self.assertRefused(self.owner(lambda r: r['identities']['slack'].update(workspace_url=url)),
                               'workspace_url')

    def test_slack_identity_needs_workspace_and_user(self):
        self.assertRefused(self.owner(lambda r: r['identities']['slack'].pop('workspace')), 'both')

    def test_owner_and_assistant_differ(self):
        self.assertRefused(self.owner(lambda r: r['assistant']['slack'].update(bot_user='U0000TEST01')),
                           'different')

    def test_strings_must_be_clean(self):
        self.assertRefused(self.owner(lambda r: r['owner'].update(name=' Mira')), 'owner.name')
        self.assertRefused(self.owner(lambda r: r['owner'].update(name=7)), 'owner.name')

    def test_channel_identities_are_optional(self):
        raw = self.owner(lambda r: (r.pop('identities'), r.pop('assistant')))
        owner = profile.parse_owner(raw)
        self.assertIsNone(owner.slack_user)
        self.assertIsNone(owner.assistant_slack_app)


class OrgValidationTest(unittest.TestCase):
    def org(self, mutate):
        raw = copy.deepcopy(example('org'))
        mutate(raw)
        return raw

    def assertRefused(self, raw, fragment):
        with self.assertRaises(profile.ProfileError) as ctx:
            profile.parse_org(raw)
        self.assertIn(fragment, str(ctx.exception))

    def test_tracker_must_be_known(self):
        self.assertRefused(self.org(lambda r: r['tickets'].update(tracker='trello')), 'tickets.tracker')

    def test_tracker_needs_a_reference_pattern(self):
        self.assertRefused(self.org(lambda r: r['tickets'].pop('ref')), 'tickets.ref')

    def test_reference_pattern_must_compile(self):
        self.assertRefused(self.org(lambda r: r['tickets'].update(ref='(PM')), 'regular expression')

    def test_reference_pattern_has_no_capturing_group(self):
        self.assertRefused(self.org(lambda r: r['tickets'].update(ref='(PM|OPS)-\\d+')), 'capturing')

    def test_reference_pattern_never_matches_empty(self):
        self.assertRefused(self.org(lambda r: r['tickets'].update(ref='\\d*')), 'empty')

    def test_ignorecase_is_boolean(self):
        self.assertRefused(self.org(lambda r: r['tickets'].update(ref_ignorecase='yes')), 'ref_ignorecase')

    def test_channels_are_validated(self):
        self.assertRefused(self.org(lambda r: r['channels'].update({'ops': 'C0000TEST03'})), 'channel names')
        self.assertRefused(self.org(lambda r: r['channels'].update({'#Ops': 'C0000TEST03'})), 'channel names')
        self.assertRefused(self.org(lambda r: r['channels'].update({'#hr': 'U0000TEST03'})), 'channel id')

    def test_no_tracker(self):
        org = profile.parse_org({'schema': profile.ORG_SCHEMA, 'name': 'Solo'})
        self.assertEqual(org.tracker, 'none')
        self.assertIsNone(org.ticket_ref_re())
        self.assertEqual(dict(org.channels), {})

    def test_case_sensitive_reference(self):
        org = profile.parse_org(self.org(lambda r: r['tickets'].update(ref_ignorecase=False)))
        self.assertIsNone(org.ticket_ref_re().fullmatch('pm-1'))
        self.assertTrue(org.ticket_ref_re().fullmatch('PM-1'))


class InstanceValidationTest(unittest.TestCase):
    def parse(self, paths):
        return profile.parse_instance({'schema': profile.INSTANCE_SCHEMA, 'paths': paths})

    def test_paths_must_be_absolute_or_home_relative(self):
        for text in ('tasks', './tasks', '', ' /tmp'):
            with self.assertRaises(profile.ProfileError):
                self.parse({'workspace_root': text})

    def test_path_names_are_snake_case(self):
        with self.assertRaises(profile.ProfileError):
            self.parse({'Workspace-Root': '/tmp'})

    def test_paths_are_optional(self):
        self.assertIsNone(profile.parse_instance({'schema': profile.INSTANCE_SCHEMA}).workspace_root)

    def test_agent_marker(self):
        self.assertEqual(profile.parse_instance({'schema': profile.INSTANCE_SCHEMA}).agent_marker, 'kage_agent_sent')
        marked = profile.parse_instance({'schema': profile.INSTANCE_SCHEMA, 'agent_marker': 'mira_kage_sent'})
        self.assertEqual(marked.agent_marker, 'mira_kage_sent')
        for bad in ('Mira-sent', 'x', 'kage agent'):
            with self.assertRaises(profile.ProfileError):
                profile.parse_instance({'schema': profile.INSTANCE_SCHEMA, 'agent_marker': bad})


class FileHandlingTest(unittest.TestCase):
    def test_missing_document(self):
        with DeploymentDir(org=None) as d, self.assertRaises(profile.ProfileError) as ctx:
            profile.load(d)
        self.assertIn('org.json is missing', str(ctx.exception))

    def test_invalid_json(self):
        with DeploymentDir() as d:
            (d / 'owner.json').write_text('{', encoding='utf-8')
            with self.assertRaises(profile.ProfileError) as ctx:
                profile.load(d)
        self.assertIn('not valid JSON', str(ctx.exception))

    def test_json_must_be_an_object(self):
        with DeploymentDir(instance=[1]) as d, self.assertRaises(profile.ProfileError):
            profile.load(d)

    def test_writable_by_others_is_refused(self):
        for mode in (0o664, 0o646):
            with DeploymentDir() as d:
                os.chmod(d / 'owner.json', mode)
                with self.assertRaises(profile.ProfileError) as ctx:
                    profile.load(d)
            self.assertIn('writable', str(ctx.exception))

    def test_load_is_all_or_nothing(self):
        bad = copy.deepcopy(example('org'))
        bad['tickets']['tracker'] = 'trello'
        with DeploymentDir(org=bad) as d, self.assertRaises(profile.ProfileError):
            profile.load(d)


if __name__ == '__main__':
    unittest.main()

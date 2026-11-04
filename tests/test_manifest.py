"""O1b: the Slack app manifest for a new deployment."""
from __future__ import annotations

import json
import unittest

from kage.onboard import manifest as M


class ManifestTest(unittest.TestCase):
    def test_the_assistant_variant(self):
        m = M.manifest("Mira's kage")
        self.assertEqual(m['display_information']['name'], "Mira's kage")
        self.assertEqual(sorted(m['oauth_config']['scopes']), ['bot'])
        self.assertIn('chat:write', m['oauth_config']['scopes']['bot'])
        self.assertEqual(m['settings']['event_subscriptions']['bot_events'], ['message.im', 'message.groups'])
        self.assertTrue(m['settings']['socket_mode_enabled'] and m['settings']['interactivity']['is_enabled'])
        self.assertFalse(m['settings']['org_deploy_enabled'])

    def test_the_as_owner_variant_adds_user_scopes_and_the_steps_say_so(self):
        m = M.manifest('kage', as_owner=True)
        self.assertIn('search:read', m['oauth_config']['scopes']['user'])
        self.assertIn('the user token', M.steps(True)[3])
        self.assertNotIn('the user token', M.steps(False)[3])

    def test_render_is_json_then_steps_and_names_are_bounded(self):
        text = M.render('x' * 60)
        body, _, rest = text.partition('\n\n')
        self.assertEqual(len(json.loads(body)['display_information']['name']), 35)
        self.assertIn('connections:write', rest)
        self.assertEqual(M.manifest('  ')['display_information']['name'], 'kage')


if __name__ == '__main__':
    unittest.main()

"""Kage's neutral access-policy pack (O1c2): the organization pack of a deployment that has no policy of its own.

It defines the vocabularies and labels the engine (kage.policy.access) reads, and no organization checks: no rule
can match more than the engine's own gates, so every access plan is uncovered and goes to the owner. An
organization's own pack replaces this one and may only add rules; it never needs this one. Labels are the ids
themselves until localized labels exist. The smoke fixture is an organization's own check of its rule set, so this
pack does not provide one.
"""
from __future__ import annotations

import re

SYSTEMS = ('source_control', 'chat', 'email', 'identity_provider', 'cloud_console', 'cloud_iam', 'database',
           'storage', 'monitoring', 'ci_cd', 'secrets', 'other')
CREDENTIAL_TYPES = ('none', 'api_key', 'password', 'token', 'certificate', 'other')
DELIVERIES = ('none', 'secure_link', 'owner_handles')
BUNDLES = ()
FUNCTIONS = ('engineering', 'non_engineering', 'unknown')
PRIVILEGED_SCOPES = ('all',)
PERSONAL_DOMAINS = ('gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'icloud.com', 'yahoo.com')

SYSTEM_ZH = {system: system.replace('_', ' ') for system in SYSTEMS}
FUNCTION_ZH = {function: function.replace('_', ' ') for function in FUNCTIONS}
WHO_ZH = {'requester': 'requester', 'hr': 'HR', 'agent': 'agent', 'platform_admin': 'platform admin'}

RULE_ID_PATTERN = r'^R-\d{2}(\.[a-z][a-z0-9_]{0,23})?$'
RULE_REF_PATTERN = r'(?<![A-Za-z0-9])R-\d+'
AR_RE = re.compile(r'\bR-\d{2}\b')

CHECKS = {}
REASON_WHY = {}
PLAN_SCOPE = {}
SPEC_DEFAULTS = {}

SMOKE_USER = 'rules-smoke-test'
SMOKE_SUMMARY = {}
SMOKE_RULE = SMOKE_CHECK = SMOKE_PROPOSAL_PATH = None


def check_params(_LEVEL, RELATIONS):
    """No organization checks, so no parameters."""
    return {}


def smoke_plans(spec):
    raise ValueError('smoke_not_supported_by_the_neutral_access_pack')

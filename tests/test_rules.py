from pydantic import BaseModel

from autoregent import RouteClass, RouteRules


class _Schema(BaseModel):
    value: int


def test_unmatched_path_is_informational():
    rules = RouteRules()
    assert rules.classify("accounts/123") == RouteClass.INFORMATIONAL


def test_transactional_pattern_match():
    rules = RouteRules().transactional("*/transfer/*", "*/charge/*")
    assert rules.classify("accounts/123/transfer/456") == RouteClass.TRANSACTIONAL
    assert rules.classify("accounts/123/charge/789") == RouteClass.TRANSACTIONAL
    assert rules.classify("accounts/123") == RouteClass.INFORMATIONAL


def test_first_transactional_match_wins():
    rules = RouteRules().transactional("*/transfer/*")
    assert rules.classify("a/transfer/b") == RouteClass.TRANSACTIONAL


def test_schema_for_unregistered_path_is_none():
    rules = RouteRules()
    assert rules.schema_for("accounts/123") is None


def test_schema_for_registered_pattern():
    rules = RouteRules().expect("accounts/*", _Schema)
    assert rules.schema_for("accounts/123") is _Schema
    assert rules.schema_for("profile/123") is None


def test_transactional_and_schema_registries_are_independent():
    rules = RouteRules().transactional("*/transfer/*").expect("accounts/*", _Schema)
    # A transactional route can still have a registered schema -- the pipeline
    # never looks it up, but RouteRules itself doesn't enforce mutual exclusion.
    assert rules.classify("accounts/1/transfer/2") == RouteClass.TRANSACTIONAL
    assert rules.schema_for("accounts/1/transfer/2") is _Schema

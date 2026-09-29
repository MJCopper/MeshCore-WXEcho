from app.filters import FilterRules, should_include


RULES = FilterRules(include_exact=["Tornado Watch"], include_suffix=["Warning"], exclude_exact=[])


def test_warning_suffix_is_included():
    assert should_include("Severe Thunderstorm Warning", RULES)


def test_watch_requires_exact_match():
    assert should_include("Tornado Watch", RULES)
    assert not should_include("Severe Thunderstorm Watch", RULES)


def test_exclusion_wins():
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=["Fire Warning"])
    assert not should_include("Fire Warning", rules)

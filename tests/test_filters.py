from app.filters import FilterRules, should_include
from app.db import Database


RULES = FilterRules(include_exact=["Tornado Watch"], include_suffix=["Warning"], exclude_exact=[])


def test_warning_suffix_is_included():
    assert should_include("Severe Thunderstorm Warning", RULES)


def test_watch_requires_exact_match():
    assert should_include("Tornado Watch", RULES)
    assert not should_include("Severe Thunderstorm Watch", RULES)


def test_exclusion_wins():
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=["Fire Warning"])
    assert not should_include("Fire Warning", rules)


def test_all_warnings_includes_warning_to_audience():
    rules = FilterRules(include_exact=[], include_suffix=["Warning"], exclude_exact=[])
    assert should_include("Warning to Sheep Graziers", rules)


def test_bom_product_families_match_qualified_titles():
    rules = FilterRules(
        include_exact=["Flood Watch", "Flood Warning", "Tropical Cyclone Advice"],
        include_suffix=[],
        exclude_exact=[],
    )

    assert should_include("Initial Flood Watch", rules)
    assert should_include("Major Flood Warning", rules)
    assert should_include("Tropical Cyclone Advice Number 7", rules)
    assert not should_include("Severe Weather Warning", rules)


def test_legacy_us_default_is_migrated(tmp_path):
    path = str(tmp_path / "settings.db")
    db = Database(path)
    db.set_setting("filter_include_exact", ["Tornado Watch"])
    db.close()

    migrated = Database(path)

    assert migrated.get_setting("filter_include_exact") == []
    migrated.close()

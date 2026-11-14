import pytest

from leadhub.csv_export import LEAD_COLUMNS, escape_cell, to_csv
from leadhub.db import MAX_PAGE_SIZE, BusinessRecord, Database, NewLead


def _biz(**overrides):
    base = dict(osm_type="node", osm_id=42, name="Barber One", category="barber", lat=1.0, lon=2.0, address="",
                phone="", website="", messenger="", social="", opportunity=True, city="Riga", distance_m=10, tags={})
    return BusinessRecord(**{**base, **overrides})


def test_seen_ids_persist_across_connections(settings):
    first = Database(settings.db_path)
    assert first.mark_seen("rss", "abc") is True

    reopened = Database(settings.db_path)  # simulates a restart
    assert reopened.mark_seen("rss", "abc") is False
    assert reopened.is_seen("rss", "abc")


def test_duplicate_lead_is_not_inserted_twice(db):
    lead = NewLead("rss", "Feed", "id1", "need python bot", "", "", "2026-01-01T00:00:00+00:00", 5, ())
    assert db.insert_lead(lead) is not None
    assert db.insert_lead(lead) is None


def test_business_upsert_keeps_user_status_and_note(db):
    assert db.upsert_business(_biz()) is True
    biz_id = db.list_businesses()[0][0]["id"]
    db.update_business(biz_id, "contacted", "visited")

    assert db.upsert_business(_biz(name="Barber One Renamed", website="https://x.ee", opportunity=False)) is False
    items, total = db.list_businesses()
    assert total == 1
    assert items[0]["status"] == "contacted" and items[0]["note"] == "visited"
    assert items[0]["name"] == "Barber One Renamed" and items[0]["opportunity"] is False


def test_search_is_case_insensitive_for_cyrillic(db):
    db.insert_lead(NewLead("rss", "Feed", "c1", "Нужен Телеграм бот в Москве", "", "", "2026-01-01T00:00:00+00:00", 5, ()))
    assert db.list_leads(query="москве")[1] == 1
    assert db.list_leads(query="ТЕЛЕГРАМ")[1] == 1


def _dated(external_id, posted_at, text="need bot"):
    return NewLead("rss", "F", external_id, text, "", "", posted_at, 5, ())


def test_leads_sort_by_post_date_not_discovery_order(db):
    db.insert_lead(_dated("middle", "2026-02-01T00:00:00+00:00"))
    db.insert_lead(_dated("oldest", "2026-01-01T00:00:00+00:00"))
    db.insert_lead(_dated("newest", "2026-03-01T00:00:00+00:00"))

    ids = lambda sort: [i["external_id"] for i in db.list_leads(sort=sort)[0]]
    assert ids("newest") == ["newest", "middle", "oldest"]
    assert ids("oldest") == ["oldest", "middle", "newest"]
    assert [i["external_id"] for i in db.list_leads()[0]] == ids("newest")  # newest is the default


def test_unknown_sort_is_rejected(db):
    with pytest.raises(ValueError):
        db.list_leads(sort="found_at; DROP TABLE leads")


def test_lead_filters_for_fixed_pay_and_long_term_combine(db):
    db.insert_lead(_dated("both", "2026-01-04T00:00:00+00:00", "Full-time, fixed price, need bot"))
    db.insert_lead(_dated("fixed", "2026-01-03T00:00:00+00:00", "Fixed price, need bot"))
    db.insert_lead(_dated("long", "2026-01-02T00:00:00+00:00", "Full-time, need bot"))
    db.insert_lead(_dated("neither", "2026-01-01T00:00:00+00:00", "need bot"))

    ids = lambda **kw: [i["external_id"] for i in db.list_leads(**kw)[0]]
    assert ids(fixed_pay=True) == ["both", "fixed"]
    assert ids(long_term=True) == ["both", "long"]
    assert ids(fixed_pay=True, long_term=True) == ["both"]
    assert db.list_leads(fixed_pay=True)[1] == 2  # total honours the filter, so paging stays right


def test_one_off_filter_uses_text_and_source(db):
    db.insert_lead(NewLead("rss", "r/slavelabour", "gig", "Need a bot", "", "", "2026-01-03T00:00:00+00:00", 5, ()))
    db.insert_lead(_dated("word", "2026-01-02T00:00:00+00:00", "Разовая задача, need bot"))
    db.insert_lead(_dated("job", "2026-01-01T00:00:00+00:00", "Senior engineer, need bot"))

    items, total = db.list_leads(one_off=True)
    assert total == 2
    assert [i["external_id"] for i in items] == ["gig", "word"]
    assert items[0]["one_off"] is True


def test_stats_count_leads_by_trait(db):
    db.insert_lead(_dated("a", "2026-01-01T00:00:00+00:00", "Fixed price, full-time, need bot"))
    db.insert_lead(_dated("b", "2026-01-02T00:00:00+00:00", "Fixed price, need bot"))
    db.insert_lead(_dated("c", "2026-01-03T00:00:00+00:00", "Разовая задача, need bot"))

    leads = db.stats()["leads"]
    assert (leads["fixed_pay"], leads["long_term"], leads["one_off"]) == (2, 1, 1)


def test_stats_trait_counts_are_zero_on_an_empty_table(db):
    leads = db.stats()["leads"]
    assert (leads["fixed_pay"], leads["long_term"], leads["one_off"]) == (0, 0, 0)


def test_lead_rows_expose_their_traits(db):
    db.insert_lead(_dated("x", "2026-01-01T00:00:00+00:00", "Full-time, need bot"))
    item = db.list_leads()[0][0]
    assert item["long_term"] is True and item["fixed_pay"] is False


def test_unbounded_listing_for_export_returns_everything(db):
    for i in range(MAX_PAGE_SIZE + 20):
        db.insert_lead(NewLead("rss", "F", f"id{i}", "need bot", "", "", "2026-01-01T00:00:00+00:00", 5, ()))
    assert len(db.list_leads(limit=10_000)[0]) == MAX_PAGE_SIZE  # paged calls stay capped
    assert len(db.list_leads(limit=None)[0]) == MAX_PAGE_SIZE + 20


def test_batch_upsert_counts_only_new(db):
    assert db.upsert_businesses([_biz(osm_id=1), _biz(osm_id=2)]) == 2
    assert db.upsert_businesses([_biz(osm_id=2), _biz(osm_id=3)]) == 1


def test_monitor_state_tracks_errors_and_totals(db):
    db.record_monitor_run("rss", 10)
    db.record_monitor_run("rss", 5, "feed.example: ConnectTimeout")
    state = db.monitor_states()["rss"]
    assert state["checked_total"] == 15
    assert state["last_error"].startswith("feed.example")
    assert state["last_ok_at"] is not None


def test_escape_cell_prefixes_formula_characters():
    for risky in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert escape_cell(risky).startswith("'")
    assert escape_cell("hello") == "hello"
    assert escape_cell(-5) == "-5"  # genuine numbers are left numeric
    assert escape_cell(True) == "yes"
    assert escape_cell(None) == ""


def test_to_csv_has_bom_header_and_quotes():
    body = to_csv([{"id": 1, "text": 'say "hi", ok'}], LEAD_COLUMNS)
    assert body.startswith("﻿\"id\"")
    assert '"say ""hi"", ok"' in body

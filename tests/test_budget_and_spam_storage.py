from leadhub.db import NewLead

AD = "Устали искать заказы в десятках чатов? Рекомендуем бота, нужен сайт — пишите, сделаем всё быстро и дёшево"


def _lead(external_id, text, posted_at="2026-11-01T10:00:00+00:00", source_name="Chat A"):
    return NewLead("telegram", source_name, external_id, text, "", "", posted_at, 7, ())


def _ids(db, **filters):
    return [item["external_id"] for item in db.list_leads(**filters)[0]]


def _post_copies(db, times, prefix="ad"):
    for i, posted_at in enumerate(times):
        db.insert_lead(_lead(f"{prefix}{i}", AD, posted_at, source_name=f"Chat {i}"), skip_duplicates=True)


def test_budget_filter_and_sort(db):
    db.insert_lead(_lead("cheap", "Нужен бот, бюджет 5 000 руб"))
    db.insert_lead(_lead("mid", "Need a landing page, $400"))
    db.insert_lead(_lead("rich", "Need a web shop, budget €2,000"))
    db.insert_lead(_lead("none", "Need a python script, details in DM"))

    assert _ids(db, min_budget=300) == ["rich", "mid"]
    assert _ids(db, max_budget=100) == ["cheap"]
    assert _ids(db, min_budget=300, max_budget=1000) == ["mid"]
    assert _ids(db, sort="budget") == ["rich", "mid", "cheap", "none"]


def test_lead_rows_expose_budget_in_euro(db):
    db.insert_lead(_lead("x", "Need a landing page, budget €400"))
    assert db.list_leads()[0][0]["budget_eur"] == 400


def test_ad_repeated_for_a_day_is_hidden_as_spam(db):
    _post_copies(db, ["2026-11-01T08:00:00+00:00", "2026-11-01T14:00:00+00:00",
                      "2026-11-01T20:00:00+00:00", "2026-11-02T09:00:00+00:00"])
    assert _ids(db) == []
    assert db.stats()["leads"]["total"] == 0 and db.stats()["leads"]["spam"] == 1


def test_order_cross_posted_within_an_hour_stays(db):
    _post_copies(db, [f"2026-11-01T10:{minute:02d}:00+00:00" for minute in (0, 10, 20, 30, 40)])
    assert _ids(db) == ["ad0"]


def test_flood_of_copies_is_spam_even_within_an_hour(db):
    _post_copies(db, [f"2026-11-01T10:{minute:02d}:00+00:00" for minute in range(0, 40, 5)])
    assert _ids(db) == []


def test_lead_the_user_touched_is_never_hidden(db):
    _post_copies(db, ["2026-11-01T08:00:00+00:00"])
    lead_id = db.list_leads()[0][0]["id"]
    db.update_lead(lead_id, "contacted", None)
    _post_copies(db, ["2026-11-01T14:00:00+00:00", "2026-11-01T20:00:00+00:00",
                      "2026-11-02T09:00:00+00:00", "2026-11-02T12:00:00+00:00"], prefix="later")
    assert _ids(db) == ["ad0"]


def test_reprocessing_the_same_post_is_not_a_repeat(db):
    for _ in range(10):
        db.insert_lead(_lead("same", AD, "2026-11-01T10:00:00+00:00"), skip_duplicates=True)
    assert _ids(db) == ["same"]

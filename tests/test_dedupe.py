import asyncio
import sqlite3
from dataclasses import replace

from leadhub.db import Database, NewLead
from leadhub.dedupe import fingerprint
from leadhub.monitors import common
from leadhub.monitors.common import RawPost, process_post

AD = "Нужен разработчик: сделать телеграм бот для записи клиентов, бюджет 300$, подробности в личку"
BOILERPLATE = "Oportun is a mission driven financial services company that puts its members' best interests first. " * 3


def _post(external_id, text=AD, source="telegram", source_name="Чат А", author="@client"):
    return RawPost(source, source_name, external_id, text, f"https://t.me/x/{external_id}", author,
                   common.iso_from_timestamp(None))


def _store(settings, db, keywords, post):
    return asyncio.run(process_post(settings, db, keywords, post))


def test_fingerprint_ignores_formatting_links_mentions_and_tags():
    base = fingerprint(AD)
    assert fingerprint(AD.upper()) == base
    assert fingerprint(f"{AD} @manager https://t.me/some_chat/15 #заказ") == base
    assert fingerprint(f"[Hiring] {AD}") == base
    assert fingerprint(AD.replace(",", " !!! ").replace(":", "\n")) == base


def test_fingerprint_treats_yo_and_ye_as_the_same_letter():
    long_text = "Нужно поправить вёрстку интернет-магазина на следующей неделе, оплата сразу"
    assert fingerprint(long_text) == fingerprint(long_text.replace("ё", "е"))


def test_different_jobs_with_the_same_boilerplate_stay_distinct():
    assert fingerprint(f"Senior Data Engineer. {BOILERPLATE}") != fingerprint(f"Data Analytics Engineer. {BOILERPLATE}")


def test_short_messages_are_only_duplicates_for_the_same_author():
    assert fingerprint("нужен бот", "@alice") != fingerprint("нужен бот", "@bob")
    assert fingerprint("нужен бот", "@alice") == fingerprint("Нужен БОТ!", "@alice")


def test_long_text_is_a_duplicate_regardless_of_author():
    assert fingerprint(AD, "@alice") == fingerprint(AD, "@bob")


def test_same_ad_in_two_chats_is_stored_once(settings, db, keywords):
    assert _store(settings, db, keywords, _post("1:1", source_name="Чат А")) is not None
    assert _store(settings, db, keywords, _post("2:9", source_name="Чат Б")) is None
    assert db.list_leads()[1] == 1
    assert db.is_seen("telegram", "2:9")  # the repeat is not re-checked on the next pass


def test_same_ad_across_different_sources_is_stored_once(settings, db, keywords):
    _store(settings, db, keywords, _post("a"))
    _store(settings, db, keywords, _post("b", source="rss", source_name="Лента"))
    assert db.list_leads()[1] == 1


def test_repeat_does_not_trigger_a_second_notification(settings, db, keywords, monkeypatch):
    sent = []

    async def fake_notify(_settings, lead, client=None):
        sent.append(lead)
        return True

    monkeypatch.setattr(common, "notify_lead", fake_notify)
    _store(settings, db, keywords, _post("1:1"))
    _store(settings, db, keywords, _post("2:2"))
    assert len(sent) == 1


def test_fingerprint_is_not_exposed_by_the_api_layer(settings, db, keywords):
    _store(settings, db, keywords, _post("1:1"))
    assert "fingerprint" not in db.list_leads()[0][0]


def _old_database(path, rows):
    """A database created before the fingerprint column existed."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE leads (
           id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_name TEXT NOT NULL DEFAULT '',
           external_id TEXT NOT NULL, text TEXT NOT NULL, link TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
           posted_at TEXT NOT NULL, found_at TEXT NOT NULL, score INTEGER NOT NULL, matched_json TEXT NOT NULL DEFAULT '[]',
           status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '', UNIQUE (source, external_id))"""
    )
    conn.executemany(
        "INSERT INTO leads (source, external_id, text, posted_at, found_at, score, status, note) VALUES (?,?,?,?,?,?,?,?)",
        [("telegram", ext, text, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 5, status, note)
         for ext, text, status, note in rows],
    )
    conn.commit()
    conn.close()


def test_opening_an_old_database_removes_existing_repeats_and_keeps_the_first(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_database(path, [("a", AD, "new", ""), ("b", AD.upper(), "new", ""),
                         ("c", "Совсем другой длинный заказ на разработку сайта", "new", "")])

    items, total = Database(path).list_leads(sort="oldest")

    assert total == 2
    assert {i["external_id"] for i in items} == {"a", "c"}


def test_migration_never_deletes_a_lead_the_user_worked_on(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_database(path, [("a", AD, "new", ""), ("b", AD, "contacted", ""), ("c", AD, "new", "позвонить"), ("d", AD, "new", "")])

    items, _ = Database(path).list_leads()

    assert {i["external_id"] for i in items} == {"b", "c"}


def test_migration_backs_up_the_database_before_deleting(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_database(path, [("a", AD, "new", ""), ("b", AD, "new", "")])

    Database(path)

    backup = sqlite3.connect(tmp_path / "old.before-dedupe.sqlite3")
    assert backup.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    backup.close()


def test_no_backup_when_there_is_nothing_to_delete(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_database(path, [("a", AD, "new", "")])

    Database(path)

    assert not (tmp_path / "old.before-dedupe.sqlite3").exists()


def test_reopening_a_migrated_database_changes_nothing(tmp_path):
    path = tmp_path / "old.sqlite3"
    _old_database(path, [("a", AD, "new", ""), ("b", AD, "new", "")])
    Database(path)
    assert Database(path).list_leads()[1] == 1


def test_direct_inserts_without_skip_flag_are_unaffected(db):
    lead = NewLead("rss", "F", "1", AD, "", "", "2026-01-01T00:00:00+00:00", 5, ())
    assert db.insert_lead(lead) is not None
    assert db.insert_lead(replace(lead, external_id="2")) is not None
    assert db.insert_lead(replace(lead, external_id="3"), skip_duplicates=True) is None

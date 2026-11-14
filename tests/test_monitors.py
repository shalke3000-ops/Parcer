import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import feedparser
import httpx

from leadhub import notify
from leadhub.db import NewLead
from leadhub.monitors import common, rss_monitor
from leadhub.monitors.common import RawPost, process_post, safe_link, strip_html
from leadhub.monitors.reddit_monitor import submission_to_post
from leadhub.monitors.telegram_monitor import message_link

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Gigs Board</title>
<item><guid>g1</guid><title>Need a Python developer for a Telegram bot</title>
<link>https://gigs.example/1</link><description>&lt;p&gt;Budget $400&lt;/p&gt;</description>
<pubDate>Sat, 07 Nov 2026 10:00:00 GMT</pubDate></item>
<item><guid>g2</guid><title>[For Hire] Python dev available for hire</title><link>https://gigs.example/2</link></item>
<item><guid>g3</guid><title>Office chairs sale</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


def _post(external_id="p1", text="[Hiring] need a python scraper", link="https://x.example/1"):
    return RawPost("rss", "Feed", external_id, text, link, "", "2026-11-07T00:00:00+00:00")


def test_matching_post_is_stored_once(settings, db, keywords):
    assert asyncio.run(process_post(settings, db, keywords, _post())) is not None
    assert asyncio.run(process_post(settings, db, keywords, _post())) is None  # dedup
    assert db.list_leads()[1] == 1


def test_non_matching_post_is_marked_seen_but_not_stored(settings, db, keywords):
    assert asyncio.run(process_post(settings, db, keywords, _post("p2", "lunch menu today"))) is None
    assert db.is_seen("rss", "p2") and db.list_leads()[1] == 0


def test_failed_insert_does_not_mark_post_seen(settings, db, keywords, monkeypatch):
    def boom(_lead, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "insert_lead", boom)
    try:
        asyncio.run(process_post(settings, db, keywords, _post("p9")))
    except RuntimeError:
        pass
    assert not db.is_seen("rss", "p9")  # will be retried on the next poll


def test_backlog_posts_do_not_trigger_notifications(settings, db, keywords, monkeypatch):
    sent = []

    async def fake_notify(_settings, lead, client=None):
        sent.append(lead)
        return True

    monkeypatch.setattr(common, "notify_lead", fake_notify)
    old = replace(_post("old"), posted_at="2020-01-01T00:00:00+00:00")
    fresh = replace(_post("fresh", "[Hiring] need a django developer for a new website"), posted_at=common.iso_from_timestamp(time.time()))
    asyncio.run(process_post(settings, db, keywords, old))
    asyncio.run(process_post(settings, db, keywords, fresh))
    assert [lead.external_id for lead in sent] == ["fresh"]


def test_one_failing_subreddit_does_not_block_others():
    from leadhub.monitors.reddit_monitor import _fetch_new

    good = SimpleNamespace(
        subreddit=SimpleNamespace(display_name="forhire"), fullname="t3_ok", title="[Hiring] bot", selftext="",
        permalink="/r/forhire/x/", author=None, created_utc=time.time(),
    )

    class FakeReddit:
        def subreddit(self, name):
            if name == "private_sub":
                return SimpleNamespace(new=lambda limit: (_ for _ in ()).throw(PermissionError("403")))
            return SimpleNamespace(new=lambda limit: [good])

    posts, errors = _fetch_new(FakeReddit(), ("private_sub", "forhire"))
    assert [p.external_id for p in posts] == ["t3_ok"]
    assert errors == ["r/private_sub: PermissionError"]


def test_telegram_backfill_reads_recent_history_once(settings, db, keywords):
    from datetime import datetime, timedelta, timezone

    from leadhub.monitors.telegram_monitor import backfill_recent

    now = datetime.now(timezone.utc)

    def message(msg_id, text, age_h):
        async def get_sender():
            return SimpleNamespace(username="client1")

        return SimpleNamespace(id=msg_id, chat_id=77, raw_text=text, out=False, date=now - timedelta(hours=age_h),
                               peer_id=None, get_sender=get_sender)

    history = [
        message(3, "Нужен разработчик: телеграм бот, бюджет 300$", 1),
        message(2, "Просто болтовня в чате", 2),
        message(1, "[Hiring] need a python scraper", 50),  # older than the 24h window → never reached
    ]

    class FakeClient:
        async def iter_messages(self, chat, limit):
            for m in history:
                yield m

    chat = SimpleNamespace(title="Заказы", username="zakazy")
    cfg = replace(settings, tg_backfill_hours=24)

    assert asyncio.run(backfill_recent(FakeClient(), [chat], cfg, db, keywords)) == 2
    assert db.list_leads()[1] == 1                       # only the real order was stored
    assert asyncio.run(backfill_recent(FakeClient(), [chat], cfg, db, keywords)) == 2
    assert db.list_leads()[1] == 1                       # second start: no duplicates
    assert asyncio.run(backfill_recent(FakeClient(), [chat], replace(cfg, tg_backfill_hours=0), db, keywords)) == 0


def test_unsafe_links_are_dropped():
    assert safe_link("javascript:alert(1)") == ""
    assert safe_link("https://ok.example") == "https://ok.example"


def test_strip_html_keeps_text_only():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"


def test_rss_entries_parse_and_score(settings, db, keywords):
    posts = rss_monitor.entries_to_posts("https://gigs.example/feed", feedparser.parse(RSS))
    assert len(posts) == 3 and posts[0].source_name == "Gigs Board"
    assert "Budget $400" in posts[0].text and posts[0].posted_at.startswith("2026-11-07T10:00")

    stored = [asyncio.run(process_post(settings, db, keywords, p)) for p in posts]
    assert stored[0] is not None and stored[1] is None and stored[2] is None


def test_poll_feeds_records_errors_without_crashing(settings, db, keywords):
    def handler(request):
        if "down" in request.url.host:
            return httpx.Response(503)
        return httpx.Response(200, content=RSS.encode())

    cfg = replace(settings, rss_feeds=("https://gigs.example/feed", "https://down.example/feed"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(rss_monitor.poll_feeds(cfg, db, keywords, client)) == 3
    state = db.monitor_states()["rss"]
    assert "down.example: HTTP 503" in state["last_error"]
    assert state["last_ok_at"] is not None  # one feed worked → partial success


REDDIT_MULTI_FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>newest submissions : multi</title>
<entry><author><name>/u/client</name></author><category term="slavelabour" label="r/slavelabour"/>
<content type="html">&lt;div class="md"&gt;&lt;p&gt;Need a python script&lt;/p&gt;&lt;/div&gt; &amp;#32; submitted by &amp;#32; &lt;a href="https://www.reddit.com/user/client"&gt; /u/client &lt;/a&gt; &amp;#32; to &amp;#32; &lt;a href="https://www.reddit.com/r/slavelabour/"&gt; r/slavelabour &lt;/a&gt; &lt;br/&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/x"&gt;[link]&lt;/a&gt;&lt;/span&gt; &amp;#32; &lt;span&gt;&lt;a href="https://www.reddit.com/y"&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content>
<id>t3_abc</id><link href="https://www.reddit.com/r/slavelabour/comments/abc/"/>
<updated>2026-11-08T10:00:00+00:00</updated><title>[Task] Need a python script ($20)</title></entry></feed>"""


def test_reddit_multi_feed_labels_each_post_with_its_subreddit():
    url = "https://www.reddit.com/r/forhire+slavelabour/new.rss?limit=100"
    post = rss_monitor.entries_to_posts(url, feedparser.parse(REDDIT_MULTI_FEED))[0]
    assert post.source_name == "r/slavelabour"
    assert post.text.startswith("[Task] Need a python script")


def test_reddit_feed_footer_is_removed_from_the_text():
    url = "https://www.reddit.com/r/forhire+slavelabour/new.rss?limit=100"
    post = rss_monitor.entries_to_posts(url, feedparser.parse(REDDIT_MULTI_FEED))[0]
    assert "submitted by" not in post.text and "[comments]" not in post.text


def test_feed_labels_are_short():
    assert rss_monitor.feed_label("https://www.reddit.com/r/forhire/new/.rss", "newest submissions : forhire") == "r/forhire"
    assert rss_monitor.feed_label("https://wwr.example/x.rss", "We Work Remotely: Remote jobs in design") == "We Work Remotely"
    assert rss_monitor.feed_label("https://jobs.example/rss", "") == "jobs.example"


def test_feeds_with_generic_titles_get_a_site_label():
    assert rss_monitor.feed_label("https://career.habr.com/vacancies/rss?type=all", "Вакансии") == "Habr Career"
    assert rss_monitor.feed_label("https://hnrss.org/whoishiring/jobs", "Ask HN: Who is hiring?") == "Hacker News"


def test_reddit_submission_mapping():
    submission = SimpleNamespace(
        subreddit=SimpleNamespace(display_name="forhire"), fullname="t3_x", title="[Hiring] bot",
        selftext="details", permalink="/r/forhire/comments/x/", author=SimpleNamespace(name="alice"),
        created_utc=time.time(),
    )
    post = submission_to_post(submission)
    assert post.source_name == "r/forhire" and post.link == "https://www.reddit.com/r/forhire/comments/x/"
    assert post.author == "u/alice"


def test_telegram_links():
    from telethon.tl.types import PeerChannel, PeerChat

    assert message_link(SimpleNamespace(username="devjobs"), 5, PeerChannel(1)) == "https://t.me/devjobs/5"
    assert message_link(SimpleNamespace(username=None), 5, PeerChannel(123)) == "https://t.me/c/123/5"
    assert message_link(SimpleNamespace(username=None), 5, PeerChat(9)) == ""


def test_notification_only_above_threshold_and_never_leaks_token(settings, monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    cfg = replace(settings, tg_bot_token="123:SECRET", tg_notify_chat_id="42", notify_min_score=8)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    low = NewLead("rss", "F", "1", "need bot", "", "", "", 5, ())
    high = replace(low, score=9, text="<b>need</b> bot", link="https://x.example")

    assert asyncio.run(notify.notify_lead(cfg, low, client)) is False
    assert asyncio.run(notify.notify_lead(cfg, high, client)) is True
    body = sent[0].content.decode()
    assert '"chat_id":"42"' in body.replace(" ", "") and "parse_mode" not in body


def test_notification_disabled_without_config(settings):
    lead = NewLead("rss", "F", "1", "need bot", "", "", "", 12, ())
    assert asyncio.run(notify.notify_lead(settings, lead)) is False


def test_monitor_package_never_calls_send_apis():
    # Static guard for the read-only requirement.
    import inspect

    from leadhub.monitors import reddit_monitor, telegram_monitor

    forbidden = ("send_message", "send_file", "reply(", ".respond(", "forward_messages", "JoinChannel",
                 ".submit(", ".reply(", "upvote", "mark_read", "send_read_acknowledge")
    for module in (telegram_monitor, reddit_monitor, rss_monitor, common):
        source = inspect.getsource(module)
        for word in forbidden:
            assert word not in source, f"{module.__name__} contains {word}"

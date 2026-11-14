from pathlib import Path

import pytest

from leadhub.scoring import Keywords, load_keywords, score_text

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def keywords() -> Keywords:
    return load_keywords(ROOT / "keywords.json")


def test_hiring_post_with_topic_scores_above_threshold(keywords):
    result = score_text("[Hiring] Need a Python developer to build a Telegram bot, budget $300", keywords)

    assert result.score >= 5
    assert "telegram bot" in result.matched
    assert not result.excluded


def test_russian_request_is_matched(keywords):
    result = score_text("Нужен разработчик: сделать телеграм бот для записи клиентов", keywords)

    assert result.score >= 5
    assert "нужен" in result.matched


def test_job_seeker_post_is_excluded(keywords):
    result = score_text("[For Hire] Python developer available for hire, need work, telegram bot expert", keywords)

    assert result.excluded
    assert result.score == 0


def test_russian_job_seeker_is_excluded(keywords):
    assert score_text("Ищу заказы на разработку сайтов, бот, парсер", keywords).excluded


@pytest.mark.parametrize("text", [
    "Здравствуйте! Ищу удалённую работу в сфере веб-разработки и вёрстки, нужен проект",
    "Ищу удаленную работу, python разработчик, нужен заказ",
    "Ищу подработку: могу сделать сайт или бота",
    "Как находить заказчиков на фрилансе? Нужен разработчик? Используйте бота для поиска заказов на сайты",
])
def test_more_job_seekers_and_lead_bot_ads_are_excluded(keywords, text):
    assert score_text(text, keywords).excluded


def test_intent_without_topic_scores_zero(keywords):
    assert score_text("Looking for a good pizza place downtown", keywords).score == 0


def test_topic_without_intent_scores_zero(keywords):
    assert score_text("Python 3.14 release notes discussion", keywords).score == 0


def test_whole_word_matching_avoids_substrings(keywords):
    # "bot" must not match inside "robot"/"bottle", "need" not inside "needle"
    assert score_text("needle robot bottle", keywords).score == 0


def test_score_is_capped(keywords):
    spam = " ".join(["need hiring looking for budget"] * 5 + ["python website bot automation scraper"] * 5)
    result = score_text(spam, keywords)

    assert result.score <= 12


@pytest.mark.parametrize("text", [
    "Подработка на вечер: поправить вёрстку сайта",
    "One-off gig: fix my shopify store",
    "Разовая задача: настроить бота",
    "Quick job, need a javascript fix",
])
def test_one_off_requests_score_high_enough_to_be_kept(keywords, text):
    result = score_text(text, keywords)
    assert result.score >= 5 and not result.excluded


@pytest.mark.parametrize("text", [
    "[OFFER] I need clients, will build a telegram bot",
    "SEEKING WORK | Python developer, need a project",
    "#помогу #сайты #telegram #боты Привет! Мы студия, делаем сайты, нужен проект",
])
def test_sellers_advertising_themselves_are_dropped(keywords, text):
    assert score_text(text, keywords).excluded


@pytest.mark.parametrize("text", [
    "🔥 We Buy USDT at Premium Rates | Looking for partners, need a website",
    "Want to make money? Stable daily income, we need a developer",
    "🚀 Offering the best exchange rates 🚀 need python",
    "Проснулся. Создал чат-бота. Заработал +17.000 рублей. Пока ты едешь на «любимую работу», нужен сайт",
    "Октябрь: +240.000 рублей. Звучит как-то нереально, но нужен python разработчик",
    "Не тратьте время на поиск заказов вручную! Нужен разработчик? Бот найдёт сайт",
])
def test_chat_spam_is_dropped(keywords, text):
    assert score_text(text, keywords).excluded


def test_service_adverts_are_excluded_even_without_banned_phrases(keywords):
    ad = ("Разработка под ключ: сайты, боты. Помогаю бизнесу автоматизировать работу. "
          "Что делаю: лендинги — от 8 000 ₽, чат-боты — от 5 000 ₽. Сейчас набираю отзывы")
    assert score_text(ad, keywords).excluded


def test_order_paid_in_usdt_is_kept(keywords):
    result = score_text("[Task] Need a python script, paying 50 USDT", keywords)
    assert result.score >= 5 and not result.excluded


def test_load_keywords_rejects_missing_lists(tmp_path):
    bad = tmp_path / "kw.json"
    bad.write_text('{"intent": ["need"]}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_keywords(bad)

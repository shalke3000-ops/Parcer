import time

import pytest

from leadhub.lead_traits import EUR_PER, budget_eur


def _eur(amount: float, currency: str) -> float:
    return amount * EUR_PER[currency]


@pytest.mark.parametrize("text, amount, currency", [
    ("[Hiring] Telegram bot, $500", 500, "usd"),
    ("Budget: 300€ for a landing page", 300, "eur"),
    ("Pay £1,200 on delivery", 1200, "gbp"),
    ("Бюджет 30 000 руб на сайт", 30_000, "rub"),
    ("Заплачу 15000 рублей за парсер", 15_000, "rub"),
    ("Оплата 50 тыс. ₽", 50_000, "rub"),
    ("Сделать бота, 40к р.", 40_000, "rub"),
    ("Need a python script, paying 50 USDT", 50, "usd"),
    ("Senior Full-Stack Engineer – Remote – $84K–$100K", 100_000, "usd"),
    ("Разработчик 1С (от 160 000 до 180 000 ₽)", 180_000, "rub"),
    ("Presupuesto: 1.500 € para la web", 1500, "eur"),
    ("Price 2.5k USD", 2500, "usd"),
    ("Full Stack at OpenAI | Remote - US | $153K – $385K", 385_000, "usd"),
])
def test_budget_is_read_and_converted_to_euro(text, amount, currency):
    assert budget_eur(text) == pytest.approx(_eur(amount, currency), rel=0.01)


@pytest.mark.parametrize("text", [
    "Need a telegram bot, details in DM",
    "Rate $50/hour, long-term",
    "Senior Independent Software Developer ($90-$170/hr)",
    "We raised $5M in funding, hiring a developer",
    "Company revenue $2 million, need a site",
    "Budget 2 weeks, need it fast",
    "Python 3.12 upgrade, 2 servers",
])
def test_texts_without_a_job_price_have_no_budget(text):
    assert budget_eur(text) is None


def test_bare_budget_number_takes_the_currency_of_the_language():
    assert budget_eur("Бюджет 5000, нужен лендинг") == pytest.approx(_eur(5000, "rub"), rel=0.01)
    assert budget_eur("Budget 400, need a landing page") == pytest.approx(_eur(400, "usd"), rel=0.01)


@pytest.mark.parametrize("text", [
    "budget 12.5.3", "pay 12,5,3", "budget 12.345.67", "budget 10.5.5 k", "бюджет 10 . 5 . 5", "budget 12,,5",
])
def test_malformed_numbers_never_raise(text):
    # The parser runs inside SQLite: one exception would break every lead listing.
    value = budget_eur(text)
    assert value is None or value > 0


def test_absurd_amounts_are_not_budgets():
    assert budget_eur("pay " + "5" * 100) is None
    assert budget_eur("Need a bot, $999,999,999") is None


def test_space_separated_digits_are_not_glued_together():
    assert budget_eur("budget 12 5 days") != pytest.approx(125 * EUR_PER["usd"])


def test_budget_parsing_does_not_stall_on_hostile_text():
    started = time.perf_counter()
    budget_eur("$" + "1 " * 5000 + "x" + "9," * 5000)
    assert time.perf_counter() - started < 1

import time

import pytest

from leadhub.lead_traits import has_fixed_pay, is_long_term, is_one_off


@pytest.mark.parametrize("text", [
    "Нужен бот, оплата фикс",
    "Фиксированная оплата за проект",
    "Фикс 50 000 за лендинг",
    "Работаем на фиксе, пишите",
    "Оклад от 120 000 рублей",
    "Зарплата по договорённости",
    "Бюджет 30 000 руб на сайт",
    "Заплачу 15000 рублей за парсер",
    "[Hiring] Telegram bot, $500",
    "Fixed price project, need a Python dev",
    "Flat fee for the landing page",
    "Salary: competitive",
    "Pay range $80,000 - $120,000",
    "Budget: 800 for a small Shopify fix",
    "$5k per project",
    "Cost: 100$",
    "Бюджет до 50 000",
    "ЗП от 100 000",
    "Budget 5k",
])
def test_fixed_pay_is_detected(text):
    assert has_fixed_pay(text)


@pytest.mark.parametrize("text", [
    "Нужно фиксить баги на сайте",          # "фикс" as "bug fix"
    "Нужен фикс багов в боте",
    "Фиксированная шапка на лендинге",      # CSS, not money
    "Senior developer ($90-$170/hr)",       # hourly is not fixed pay
    "We pay $40 per hour",
    "Оплата 500 р/час",
    "A.Team builders have earned over $108 million",
    "Backed by investors, raised $5,000,000 in funding",
    "Members set aside an average of more than $1,800 annually",
    "New hire equity: $16,000-$24,000",
    "Some experts are making $250k+ per year",
    "Manage the marketing budget and roadmap",
    "Commission only, $5,000 potential",    # explicit no-fixed-pay wins over the amount
    "Unpaid internship, salary after trial",
    "Без оплаты, бюджет 0",
    "Need a developer for a website",
    "Подписчиков 10к, нужен бот",
    "Budget 2 weeks of work",
    "Paid 3 times a week",
    "I make $5k per month from this",
    "Built from own savings of $20,000",
    "No salary at this stage",
])
def test_not_fixed_pay(text):
    assert not has_fixed_pay(text)


@pytest.mark.parametrize("text", [
    "Ищем на долгосрочное сотрудничество",
    "Проект на долгосрок",
    "Работа на постоянной основе",
    "Постоянная занятость, удалённо",
    "Длительное сотрудничество с агентством",
    "Берём в штат разработчика",
    "Полная занятость",
    "FULL TIME: Software Engineer",
    "Full-time remote role",
    "Long-term contract with our team",
    "Looking for a long term partner",
    "Ongoing work for the right person",
    "Permanent position in Berlin",
    "Monthly retainer for maintenance",
    "12-month contract, remote",
    "6+ months project",
])
def test_long_term_is_detected(text):
    assert is_long_term(text)


@pytest.mark.parametrize("text", [
    "Our long-term vision is to change healthcare",   # about the company, not the job
    "Not long-term, just this week",
    "Это не долгосрочный проект",
    "Not full-time, a few hours only",
    "Part-time help with a landing page",
    "Short-term task, one-off",
    "Разовая задача на вечер",
    "3 month contract",
    "Ongoing global pandemic changed our business",
])
def test_not_long_term(text):
    assert not is_long_term(text)


@pytest.mark.parametrize("text, source", [
    ("Разовая задача на вечер", ""),
    ("Единоразовый заказ на парсер", ""),
    ("Подработка на выходных", ""),
    ("Небольшая задача по сайту", ""),
    ("One-off landing page", ""),
    ("One time job, fix my site", ""),
    ("[Task] need a bot", ""),
    ("Quick job: fix my checkout", ""),
    ("Small task for a Python dev", ""),
    ("[Hiring] Need a landing page, $300", "r/forhire"),   # gig communities post one-offs by default
    ("Need a logo for my bot", "r/slavelabour"),
    ("Scrape a site for me", "r/DoneDirtCheap"),
])
def test_one_off_work_is_detected(text, source):
    assert is_one_off(text, source)


@pytest.mark.parametrize("text, source", [
    ("Senior developer role", "We Work Remotely"),
    ("One-time signing bonus for this senior role", ""),
    ("Gig economy platform hiring engineers", ""),
    ("Not a one-off, we want a partner", ""),
    ("Это не разовая задача", ""),
    ("Full-time developer, remote", "r/forhire"),          # a gig community can still post a real job
])
def test_not_one_off(text, source):
    assert not is_one_off(text, source)


@pytest.mark.parametrize("hostile", [
    "1 " * 30_000,
    "1," * 30_000,
    "9" * 60_000 + " " * 60_000,
    "12" + " " * 60_000 + "x",
    "$1" * 30_000,
])
def test_hostile_public_posts_cannot_stall_the_classifier(hostile):
    started = time.perf_counter()
    has_fixed_pay(hostile)
    is_long_term(hostile)
    assert time.perf_counter() - started < 0.5


def test_traits_are_independent():
    text = "Fixed price, one-off landing page"
    assert has_fixed_pay(text) and not is_long_term(text)
    text = "Full-time, $40/hr"
    assert is_long_term(text) and not has_fixed_pay(text)

"""Tells a service advert ("I build bots, from 5 000 ₽") from an order ("need a bot, budget 5 000 ₽").

Both use the same words (sites, bots, automation), so a list of banned phrases
either misses adverts or drops real orders. Instead the post is weighed: signs
of someone selling themselves against signs of someone asking for work. Signals
that both sides use ("пишите в лс", "под ключ", a bare "портфолио") count for
neither. All repeats are bounded: posts come from public chats.
"""

from __future__ import annotations

import re
from functools import lru_cache

_CACHE_SIZE = 20_000
MIN_OFFER_SIGNALS = 2
PRICE_LIST_ITEMS = 2   # "сайт — от 8 000 ₽, бот — от 5 000 ₽" is a price list
PRICE_LIST_WEIGHT = 2

# A question about the reader's needs ("Нужен сайт?", "У вас бизнес, но нет сайта?") is a sales hook.
_RHETORICAL = (
    r"(?:нуж(?:ен|на|но|ны)|хотите|хотели|need|want)\b[^.!?\n]{0,50}\?"
    r"|(?<!\w)у\s+вас[^.!?\n]{0,80}\?"
)
# "Делаю ботов", "создаю сайты": the seller describing their own work. Two different verbs weigh two.
_SELLER_VERBS = re.compile(
    r"(?<!\w)(делаю|создаю|разрабатываю|автоматизирую|монтирую|превращаю|оформляю|настраиваю|верстаю"
    r"|программирую|воплощаю|внедряю)(?!\w)",
    re.IGNORECASE,
)
MAX_SELLER_VERB_WEIGHT = 2

_OFFER_SIGNALS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"(?<!\w)помога(?:ю|ем)(?!\w)|(?<!\w)помо(?:гу|жем)(?!\w)",
    r"(?<!\w)(?:что|чем)\s+(?:я\s+)?(?:делаю|занимаюсь|умею|помогу|могу\s+(?:помочь|сделать|предложить))",
    r"(?<!\w)меня\s+зовут(?!\w)|(?<!\w)частн\w+\s+(?:разработчик|программист|специалист)"
    r"|(?<!\w)я\s+(?:веб-)?(?:разработчик|программист|дизайнер|верстальщик|техспец)(?!\w)"
    r"|\bmy\s+name\s+is\b|\bi'?m\s+(?:an?\s+)?(?:[\w-]+\s+){0,2}(?:developer|designer|engineer)\b",
    r"(?<!\w)как\s+(?:я\s+|мы\s+)?работа(?:ю|ем)|этапы\s+работы",
    r"(?<!\w)(?:мои|наши)\s+услуги|(?<!\w)услуги\s*:|список\s+услуг|(?<!\w)прайс",
    r"(?<!\w)(?:беру|принимаю|берём|берем|принимаем)\s+(?:заказы|проекты)|открыт[аы]?\s+для\s+(?:заказов|проектов)"
    r"|свободн\w+\s+(?:слоты|места)",
    r"(?<!\w)набира(?:ю|ем)\s+(?:отзывы|портфолио|клиентов)",
    r"цен\w*\s+ниже\s+рыночн\w*|скидк\w*\s+(?:первым|новым)\s+клиентам|бесплатн\w+\s+консультац\w*",
    r"(?<!\w)гаранти(?:я|ю|рую|руем)(?!\w)",
    r"(?<!\w)(?:сделаю(?!\s+предоплат)|создам|разработаю|настрою|сверстаю|напишу\s+(?:бота|сайт|скрипт|парсер))(?!\w)",
    r"(?<!\w)(?:мо[её]|наше)\s+портфолио|портфолио[^.\n]{0,20}(?:в\s+профиле|по\s+ссылке|в\s+канале|в\s+шапке)",
    r"(?<!\w)отзыв\w*[^.\n]{0,12}(?:в\s+профиле|в\s+канале|по\s+ссылке)",
    r"(?<!\w)опыт\s+\d{1,2}\+?\s+(?:лет|год)",
    r"(?<!\w)наша\s+(?:студия|веб-студия|агентство)|(?<!\w)мы\s+(?:делаем|разрабатываем|создаём|создаем|занимаемся)"
    r"|(?<!\w)мы\s+предлагаем\s+услуги",
    r"(?<!\w)обращайтесь(?!\w)",
    r"\bi\s+(?:build|create|develop|design|make|offer|specialize|can\s+(?:build|create|develop|make|help))\b"
    r"|\bwe\s+(?:build|create|develop|offer|specialize)\b",
    r"\b(?:my|our)\s+(?:portfolio|services)\b|\bhire\s+me\b|\bavailable\s+for\s+(?:work|hire|projects)\b"
    r"|\bfree\s+consultation\b|\baffordable\s+(?:prices|rates)\b",
    _RHETORICAL,
))

_PRICE_LIST_ITEM = re.compile(
    r"(?<!\w)(?:от|from)\s?[$€£]?\s?\d[\d  .,]{0,9}\s?(?:₽|руб\w*|р\.|\$|€|usd|eur|[kк](?!\w)|тыс\w*)",
    re.IGNORECASE,
)

_REQUEST_SIGNALS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"(?<!\w)нуж(?:ен|на|но|ны)(?!\w)(?![^.!?\n]{0,50}\?)",
    r"(?<!\w)(?:ищу|ищем)(?!\w)",
    r"(?<!\w)требу(?:ется|ются)(?!\w)|(?<!\w)требования(?!\w)",
    r"(?<!\w)кто\s+(?:может|сможет|сделает|возьм[её]тся|умеет|знает|поможет)",
    r"(?<!\w)есть\s+(?:задача|заказ|проект)|(?<!\w)задача\s*:|(?<!\w)тз(?!\w)",
    r"(?<!\w)(?:бюджет|заплачу|оплачу)(?!\w)",
    r"#(?:ищу|нужен|нужна|заказ|задача|вакансия)(?!\w)|(?<!\w)ваканси[яи](?!\w)",
    r"\bneed(?:ed|s)?\b(?![^.!?\n]{0,50}\?)",
    r"\blooking\s+for\b|\bhiring\b|\[(?:hiring|task)\]|\bwanted\b|\bseeking\s+an?\b",
    r"\bbudget\b|\b(?:paying|will\s+pay)\b|\brequirements\b",
))


def _count(patterns: tuple[re.Pattern[str], ...], text: str) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


@lru_cache(maxsize=_CACHE_SIZE)
def looks_like_offer(text: str) -> bool:
    """True when the post sells the author's services rather than asking for work."""
    offer = _count(_OFFER_SIGNALS, text)
    verbs = {m.casefold() for m in _SELLER_VERBS.findall(text)}
    offer += min(len(verbs), MAX_SELLER_VERB_WEIGHT)
    if len(_PRICE_LIST_ITEM.findall(text)) >= PRICE_LIST_ITEMS:
        offer += PRICE_LIST_WEIGHT
    return offer >= MIN_OFFER_SIGNALS and offer > _count(_REQUEST_SIGNALS, text)

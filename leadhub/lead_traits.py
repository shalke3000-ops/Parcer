"""Traits read from a lead's free text: does it offer fixed pay, is it long-term.

Heuristic by nature: posts are unstructured, so the rules favour phrases that
name the money or the engagement over bare words ("фикс багов" and "long-term
vision" must not match). Traits are computed on read rather than stored, so a
better rule applies to every existing lead without a migration; a memo keeps
the repeated table scans of the filters cheap.
"""

from __future__ import annotations

import re
from functools import lru_cache

_TRAIT_CACHE_SIZE = 20_000  # roughly the whole table; texts are capped at 5000 chars

_PAY_NOUN = r"(?:оплат|ставк|цен|сумм|бюджет|зарплат|оклад|гонорар|стоимост)\w*"

_NO_FIXED_PAY = re.compile(
    r"\b(?:commission[\s-]only|equity[\s-]only|revenue[\s-]share[\s-]only|unpaid|no\s+(?:pay|salary))\b"
    r"|(?<!\w)без\s+(?:оплаты|зарплаты)(?!\w)|(?<!\w)только\s+(?:процент|%)",
    re.IGNORECASE,
)

_FIXED_PAY_WORDS = re.compile(
    rf"(?<!\w)фиксирован\w*\s+{_PAY_NOUN}"
    rf"|(?<!\w)фикс\w*\W{{0,3}}(?:{_PAY_NOUN}|\d)"
    r"|(?<!\w)(?:оплата|цена|ставка|бюджет|зп|оклад)\W{0,3}фикс\w*"
    r"|(?<!\w)(?:на|за)\s+фиксе?(?!\w)"
    r"|(?<!\w)(?:оклад|зарплат)\w*|(?<!\w)з/?п(?!\w)"
    r"|\bfixed[\s-]+(?:price|fee|rate|pay|payment|budget|salary|cost|compensation|amount)\b"
    r"|\bflat[\s-]+(?:fee|rate)\b"
    r"|\bsalar(?:y|ies)\b",
    re.IGNORECASE,
)

# Repeats are bounded on purpose: posts come from public sources and are parsed on
# every list request, so an unbounded run of digits and spaces would be a stall vector.
_NUMBER = r"\d[\d\s,.]{0,14}"
_AMOUNT = r"\d[\d,.]{0,14}(?:\s?k\b)?"
_CURRENCY = r"(?:тыс\w*|т\.р\.?|руб\w*|р\.?|₽|[$€£]|usdt?|долл\w*|евро|eur)(?!\w)"
_UNIT = rf"(?:[кk](?!\w)|{_CURRENCY})"
# After a word like "budget": two or more digits, or a single digit with a unit ("5k"),
# so "budget 2 weeks" and "paid 3 times" are not read as prices.
_NAMED_AMOUNT = rf"(?:\d\d[\d\s,.]{{0,14}}{_UNIT}?|\d[\d,.]{{0,3}}\s?{_UNIT})"

# Consumes a whole "$90-$170" range so the suffix check below sees what follows it.
_MONEY = re.compile(
    rf"[$€£]\s?{_AMOUNT}(?:\s{{0,3}}(?:-|–|—|to)\s{{0,3}}[$€£]?\s?{_AMOUNT})?"
    rf"|(?<![\w.]){_NUMBER}\s?{_CURRENCY}"
    r"|(?<!\w)(?:бюджет|оплат\w*|плачу|заплачу|цена|стоимость|budget(?:\s+of)?|pay|paying|paid)"
    rf"\W{{0,12}}(?:(?:до|от|около)\s+)?{_NAMED_AMOUNT}",
    re.IGNORECASE,
)

_NOT_A_PRICE_AFTER = re.compile(
    r"\s*(?:/\s*(?:h|hr|hour|ч|час)|per\s+h|an\s+hour|в\s+час|за\s+час"
    r"|million|billion|mln|bn\b|m\b|b\b|млн|млрд"
    r"|(?:in\s+|of\s+)?(?:funding|revenue|sales)|arr\b)",
    re.IGNORECASE,
)
# Sums that describe the company or other people's income rather than this job.
_NOT_A_PRICE_BEFORE = re.compile(
    r"(?:raised|funding|revenue|earned|making|(?<!\w)(?:i|we|you)\s+make|savings?|valued|worth|average|equity|привлек\w*)"
    r"\W+(?:\w+\W+){0,3}$",
    re.IGNORECASE,
)
_CONTEXT_CHARS = 28

_LONG_TERM = re.compile(
    r"(?<!не\s)(?<!\w)долгосроч\w*"
    r"|(?<!не\s)(?<!\w)(?:на\s+)?долгосрок(?!\w)"
    r"|(?<!\w)постоянн\w+\s+(?:основе|работ\w*|сотрудничеств\w*|занятост\w*|проект\w*)"
    r"|(?<!\w)длительн\w+\s+(?:сотрудничеств\w*|проект\w*|работ\w*|срок)"
    r"|(?<!\w)в\s+штат(?!\w)|(?<!\w)полн\w+\s+(?:занятост\w*|рабоч\w+\s+день)"
    r"|(?<!not\s)\blong[\s-]term\s+(?:contract|project|engagement|role|position|partner\w*|collaboration"
    r"|work|job|client|relationship|commitment|opportunity|employment|hire|basis|gig|assignment)\b"
    r"|(?<!not\s)\bfull[\s-]?time\b"
    r"|\bpermanent\s+(?:role|position|job|contract|employment|hire)\b"
    r"|\bongoing\s+(?:work|projects?|basis|role|position|engagement|collaboration|contract|support|relationship)\b"
    r"|\bretainer\b"
    r"|(?<!\d)(?:[6-9]|\d{2,3})\s{0,3}\+?[\s-]{0,3}months?\s+(?:contract|project|engagement)\b",
    re.IGNORECASE,
)


_ONE_OFF = re.compile(
    r"(?<!не\s)(?<!\w)(?:едино)?разов\w+"
    r"|(?<!not\sa\s)\bone[\s-]off\b"
    r"|\bone[\s-]time\s+(?:job|task|project|gig|fix)\b"
    r"|\[task\]"
    r"|\b(?:quick|small|tiny)\s+(?:job|task|gig|fix|project)\b"
    r"|\bpaid\s+gig\b|\bside\s+(?:gig|project)\b"
    r"|(?<!\w)подработк\w*"
    r"|(?<!\w)небольш\w+\s+(?:задач|заказ|доработк|проект)\w*"
    r"|(?<!\w)на\s+один\s+раз(?!\w)",
    re.IGNORECASE,
)
# Communities where a request is a single paid job unless the post says otherwise.
_GIG_COMMUNITIES = frozenset({"r/forhire", "r/slavelabour", "r/donedirtcheap", "r/jobs4bitcoins"})


def _is_price(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - _CONTEXT_CHARS) : match.start()]
    after = text[match.end() : match.end() + _CONTEXT_CHARS]
    return not (_NOT_A_PRICE_AFTER.match(after) or _NOT_A_PRICE_BEFORE.search(before))


@lru_cache(maxsize=_TRAIT_CACHE_SIZE)
def has_fixed_pay(text: str) -> bool:
    """True when the post names a salary or a set price; hourly-only and commission-only are not."""
    if _NO_FIXED_PAY.search(text):
        return False
    return _FIXED_PAY_WORDS.search(text) is not None or any(_is_price(text, m) for m in _MONEY.finditer(text))


@lru_cache(maxsize=_TRAIT_CACHE_SIZE)
def is_long_term(text: str) -> bool:
    """True when the post describes full-time, permanent or multi-month work."""
    return _LONG_TERM.search(text) is not None


@lru_cache(maxsize=_TRAIT_CACHE_SIZE)
def is_one_off(text: str, source_name: str = "") -> bool:
    """True for a single paid task: named as one, or posted in a gig community and not a long-term role."""
    if _ONE_OFF.search(text):
        return True
    return source_name.casefold() in _GIG_COMMUNITIES and not is_long_term(text)


# ---- budget -------------------------------------------------------------------

# Rough rates (November 2026): enough to compare budgets across currencies, not to invoice.
EUR_PER = {"eur": 1.0, "usd": 0.86, "gbp": 1.15, "rub": 0.0107}
_THOUSAND = 1000
MAX_BUDGET_EUR = 10_000_000  # above this a "price" is a typo, a phone number or a joke

# "30 000", "1,200", "1.500" (thousands) or "2.5", "99,90" (decimals). Bounded repeats only.
_BUDGET_AMOUNT = r"\d{1,3}(?:[  ,.]\d{3}){1,3}(?:[.,]\d{1,2})?|\d{1,9}(?:[.,]\d{1,2})?"
_BUDGET_UNIT = r"[$€£₽]|usdt?|usd|eur|euros?|gbp|rub|руб\w*|т\.?\s?р\.?|р\.?|долл\w*|dollars?|евро"
# A "$90-$170" range is one match, so "/hr" after its upper end disqualifies both ends.
_EXPLICIT_PRICE = re.compile(
    rf"(?P<pre_unit>[$€£])\s?(?P<pre_amount>{_BUDGET_AMOUNT})(?P<pre_k>\s?[kK](?!\w))?"
    rf"(?:\s{{0,3}}(?:-|–|—|to)\s{{0,3}}[$€£]?\s?(?P<pre_amount2>{_BUDGET_AMOUNT})(?P<pre_k2>\s?[kK](?!\w))?)?"
    rf"|(?<![\w.,])(?P<amount>{_BUDGET_AMOUNT})(?:\s?(?P<k>[kKкК](?!\w)|тыс\w*\.?))?\s?(?P<unit>{_BUDGET_UNIT})(?!\w)",
    re.IGNORECASE,
)
_NAMED_PRICE = re.compile(
    r"(?<!\w)(?P<word>бюджет|оплата|плачу|заплачу|цена|стоимость|budget(?:\s+of)?|pay|paying|paid|price|presupuesto)"
    r"\W{0,12}(?:(?:до|от|около|up\s+to|hasta)\s+)?"
    # Same number grammar as explicit prices; one digit alone only as "5k" ("budget 2 weeks" is not a price).
    rf"(?P<amount>(?=\d\d|\d[  ,.]\d{{3}}|\d(?:[.,]\d{{1,2}})?\s?[kк](?!\w))(?:{_BUDGET_AMOUNT}))"
    r"(?:\s?(?P<k>[kKкК](?!\w)|тыс\w*\.?))?",
    re.IGNORECASE,
)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def _parse_amount(raw: str) -> float | None:
    """'30 000' -> 30000, '1,200' -> 1200, '1.500' -> 1500, '2.5' -> 2.5, '99,90' -> 99.9; None if not a number.

    Never raises: it runs inside SQLite queries, where one exception breaks the whole listing.
    """
    s = raw.replace(" ", " ").strip(" ,.").replace(" ", "")
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        groups = s.split(sep)
        s = "".join(groups) if all(len(g) == 3 for g in groups[1:]) else s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


def _unit_currency(unit: str) -> str:
    u = unit.casefold()
    if u.startswith(("$", "usd", "долл", "dollar")):
        return "usd"
    if u.startswith(("€", "eur", "евро")):
        return "eur"
    if u.startswith(("£", "gbp")):
        return "gbp"
    return "rub"


def _explicit_budgets(text: str) -> list[float]:
    found = []
    for m in _EXPLICIT_PRICE.finditer(text):
        if not _is_price(text, m):
            continue
        if m["pre_unit"] and m["pre_amount2"]:  # range: its upper end
            amount, unit, thousands = m["pre_amount2"], m["pre_unit"], bool(m["pre_k2"] or m["pre_k"])
        elif m["pre_unit"]:
            amount, unit, thousands = m["pre_amount"], m["pre_unit"], bool(m["pre_k"])
        else:
            amount, unit, thousands = m["amount"], m["unit"], bool(m["k"])
            thousands = thousands or unit.casefold().startswith("т")  # "т.р." = thousand roubles
        value = _parse_amount(amount)
        if value is not None:
            found.append(value * (_THOUSAND if thousands else 1) * EUR_PER[_unit_currency(unit)])
    return found


def _named_budgets(text: str) -> list[float]:
    """'Бюджет 5000' with no currency: roubles in Russian text, euros for 'presupuesto', else dollars."""
    found = []
    for m in _NAMED_PRICE.finditer(text):
        if not _is_price(text, m):
            continue
        if _CYRILLIC.search(text):
            currency = "rub"
        else:
            currency = "eur" if m["word"].casefold() == "presupuesto" else "usd"
        value = _parse_amount(m["amount"])
        if value is not None:
            found.append(value * (_THOUSAND if m["k"] else 1) * EUR_PER[currency])
    return found


@lru_cache(maxsize=_TRAIT_CACHE_SIZE)
def budget_eur(text: str) -> float | None:
    """The highest job price named in the post, in euros; None when it names none.

    A range counts by its upper end. Hourly rates and company money (funding,
    revenue) are not budgets. A currency written next to a number wins over a
    bare "budget 5000", whose currency is only guessed.
    """
    values = _explicit_budgets(text) or _named_budgets(text)
    values = [v for v in values if 0 < v <= MAX_BUDGET_EUR]
    return round(max(values), 2) if values else None

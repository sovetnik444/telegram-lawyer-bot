import os
import re
import asyncio
import logging
import datetime
from typing import List, Optional, Dict
from collections import defaultdict, deque
from dataclasses import dataclass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters
)

try:
    from dotenv import load_dotenv  # optional
    load_dotenv()
except Exception:
    pass

# ====== НАСТРОЙКИ ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CREATOR_LINK = os.getenv("CREATOR_LINK", "https://t.me/sovetnik_moscow")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set in environment")

GROQ_MODELS = ["llama-3.1-70b", "llama3-8b-8192"]
from groq import Groq
groq_client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ai-lawyer-bot")

# Память диалога (≈4 обмена)
MEMORY_MAXLEN = 8
CHAT_MEMORY: defaultdict[int, deque] = defaultdict(lambda: deque(maxlen=MEMORY_MAXLEN))
CHAT_PROFILE: dict[int, "CaseProfile"] = {}

# ====== КНОПКА СВЯЗИ ======
def contact_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("👨‍⚖ Связаться с юристом", url=CREATOR_LINK)]])

# ====== УТИЛИТЫ ТЕКСТА ======
URL_OR_HANDLE = re.compile(r"(https?://\S+|t\.me/\S+|@\w+)", re.I)
LATIN_ANY = re.compile(r"[A-Za-z]")

def only_russian_text(s: str) -> bool:
    cleaned = URL_OR_HANDLE.sub("", s or "")
    return LATIN_ANY.search(cleaned) is None

def drop_latin_sentences(s: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", (s or "").strip())
    keep = []
    for p in parts:
        if only_russian_text(p) or p.strip() == "":
            keep.append(p)
    out = " ".join(keep).strip()
    return out if out else s.strip()

def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text, re.U))

def trim_to_70_words(text: str) -> str:
    words = text.split()
    if len(words) <= 70:
        return text.strip()
    cut = " ".join(words[:70])
    m = re.search(r"^(.+?[.!?])(\s|$)", cut)
    return (m.group(1) if m else cut).strip()

async def typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, sec: float = 0.6):
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass
    await asyncio.sleep(sec)

def call_groq(messages: List[dict], temperature: float = 0.2, max_tokens: int = 450) -> str:
    last_err = None
    for mdl in GROQ_MODELS:
        try:
            resp = groq_client.chat.completions.create(
                model=mdl, messages=messages, temperature=temperature, max_tokens=max_tokens
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("Groq call failed")

# ====== ЖЁСТКИЙ ФИЛЬТР ДОМЕНА ======
LEGAL_HINTS = [
    # базовые юр-термины
    "суд", "иск", "жалоб", "заявлен", "госпошлин", "штраф", "протокол", "постановлен",
    "кодекс", "статья", "ст.", "норма", "право", "обжал", "апелляц", "кассац",
    "договор", "оформлен", "претензи", "исков", "подсудност", "повестк", "исполнительн",
    # направления
    "гпк", "апк", "кас рф", "упк", "коап", "зозпп", "ск рф", "тк рф", "нк рф",
    "жк рф", "ук рф", "миграц", "внж", "патент", "регистрац", "развод", "алим", "дтп", "осаг", "каско",
    "аренда", "наследств", "жиль", "ипотек", "банкрот", "коллектор", "пристав", "росреестр", "мфц"
]

NONLEGAL_HINTS = [
    "рецепт", "суп", "борщ", "котлет", "жарить", "духовк", "плов", "кофе", "торт",
    "фитнес", "похуд", "диет", "пресс", "трениров", "медицин", "симптом", "лекарств",
    "космос", "ракета", "луна", "марс", "астрон", "телескоп",
    "программир", "python", "питон", "javascript", "js", "java", "код", "скрипт",
    "ремонт", "шумоизоляц", "оклейк", "покраск", "инстаграм", "тикток", "реклам",
    "путешеств", "маршрут", "виза шенген", "авиабилет", "отель",
    "шутк", "анекдот", "гороскоп", "астролог"
]

# крипта — особый случай: допустимы ТОЛЬКО юр-контексты
CRYPTO_HINTS = ["крипт", "биткоин", "usdt", "криптовалют", "бирж", "фьючерс", "битгет", "бинанс", "бингх"]
CRYPTO_LEGAL_CONTEXT = ["налог", "закон", "оформл", "лиценз", "kyc", "aml", "санкц", "договор", "суд", "штраф"]

def is_legal_query_rule_based(text: str) -> bool:
    t = text.lower()
    if any(w in t for w in NONLEGAL_HINTS):
        return False
    if any(w in t for w in LEGAL_HINTS):
        return True
    # крипта — только если есть юр-контекст
    if any(w in t for w in CRYPTO_HINTS) and any(w in t for w in CRYPTO_LEGAL_CONTEXT):
        return True
    return False

async def is_legal_query_llm(text: str) -> bool:
    """
    Подстраховка Groq: классифицируем однословной меткой LEGAL/OTHER.
    """
    prompt = [
        {"role": "system", "content":
         "Классифицируй запрос как LEGAL или OTHER. LEGAL — если это вопрос по праву РФ: суды, сроки, жалобы, договоры, штрафы, миграция, труд, семейное, ЖКХ, налоги и т.п. "
         "Если рецепт, код, медицина, космос, путешествия и т.п. — OTHER. Ответь строго одним словом: LEGAL или OTHER."},
        {"role": "user", "content": text.strip()}
    ]
    try:
        label = call_groq(prompt, temperature=0.0, max_tokens=1)
        return "LEGAL" in label.upper()
    except Exception:
        # если LLM недоступна — полагаемся на rule-based
        return is_legal_query_rule_based(text)

OFFTOPIC_REPLY = (
    "Я — AI-юрист (бета). Отвечаю только на юридические вопросы по праву РФ: суды, жалобы, договоры, штрафы, миграция, труд, семья, ЖКХ, налоги. "
    "Сформулируйте задачу в юридических терминах — подскажу алгоритм и нормы. За деталями можно обратиться к живому юристу."
)

# ====== LLM С ОБНОВЛЁННЫМ ЖЁСТКИМ SYSTEM-ПРОМПТОМ ======
def build_messages(user_text: str, memory: deque, profile_summary: Optional[str] = None) -> List[dict]:
    sys = (
        "Ты — русскоязычный помощник «AI-юрист (бета)».\n"
        "ОТВЕЧАЙ ТОЛЬКО НА ЮРИДИЧЕСКИЕ ВОПРОСЫ ПО ПРАВУ РФ. "
        "ЕСЛИ ВОПРОС НЕ ЮРИДИЧЕСКИЙ — ответь одной короткой фразой-отказом (без совета по теме) и предложи сформулировать вопрос в юридических терминах.\n"
        "Запрещены латиница и англицизмы (кроме URL/@username). Стиль: 30–70 слов; 1–2 предложения сути + 1–2 шага.\n"
        "Не пиши дисклеймеров про то, что ты модель. Заверши строго: «За деталями можно обратиться к живому юристу.»"
    )
    preface = "Я — AI-юрист (бета). "
    tail = "За деталями можно обратиться к живому юристу."
    history = list(memory)
    profile_block = (f"\nКонтекст дела: {profile_summary}" if profile_summary else "")
    current = {
        "role": "user",
        "content": (
            f"Вопрос пользователя: {user_text}{profile_block}\n\n"
            f"Ответ должен начинаться: «{preface}» и заканчиваться: «{tail}». Только русский, без латиницы."
        )
    }
    return [{"role": "system", "content": sys}] + history + [current]

def russianize(text: str) -> str:
    preface = "Я — AI-юрист (бета). "
    tail = "За деталями можно обратиться к живому юристу."
    text = drop_latin_sentences(text)
    if not only_russian_text(text):
        fixer = [
            {"role": "system", "content":
             "Перепиши текст строго на русском, без латиницы (допустимы URL/@username). 30–70 слов. "
             "Заверши: «За деталями можно обратиться к живому юристу.»"},
            {"role": "user", "content": text}
        ]
        try:
            text = call_groq(fixer, temperature=0.0, max_tokens=220)
        except Exception:
            text = re.sub(r"[A-Za-z]", "", text)
    wc = word_count(text)
    if wc < 30 or wc > 70:
        try:
            text = call_groq(
                [
                    {"role": "system", "content": "Сожми/расширь до 30–70 слов. Только русский. Без латиницы."},
                    {"role": "user", "content": text}
                ],
                temperature=0.0, max_tokens=220
            )
        except Exception:
            text = trim_to_70_words(text)
    text = text.strip()
    if not text.startswith(preface):
        text = preface + text
    if tail not in text:
        if not text.endswith((".", "!", "?")):
            text += "."
        text += f" {tail}"
    return drop_latin_sentences(text).strip()

def make_llm_answer(user_text: str, memory: deque, profile_summary: Optional[str] = None) -> str:
    draft = call_groq(build_messages(user_text, memory, profile_summary), temperature=0.2, max_tokens=420)
    return russianize(draft)

# ====== ПРОФИЛЬ ДЕЛА: ИЗВЛЕЧЕНИЕ ФАКТОВ И САММАРИ ======
def update_profile_from_text(profile: "CaseProfile", low_text: str) -> None:
    if ("развод" in low_text) or ("расторж" in low_text):
        profile.topic = profile.topic or "divorce"

    # Дети
    if re.search(r"без\s+дет", low_text):
        profile.divorce_has_children = False
    elif re.search(r"(есть|имеются)\s+дет|ребенок|дети|сын|дочь", low_text):
        profile.divorce_has_children = True

    # Согласие
    if re.search(r"обоюдн|взаимн.*соглас|оба\s+соглас|согласны", low_text):
        profile.divorce_mutual_consent = True
    elif re.search(r"не\s*соглас|против\s+развод", low_text):
        profile.divorce_mutual_consent = False

    # Спор о имуществе
    if re.search(r"раздел\s+имуществ|спор\s+об?\s+имуществ|делить\s+имуществ", low_text):
        profile.divorce_property_dispute = True

    # Супруг уклоняется/пропал/адрес неизвестен
    if re.search(r"неизвестн.*место|не\s+зна(ю|ем)\s+где|уклоняетс|не\s+являетс|пропал", low_text):
        profile.divorce_spouse_absent = True

    # Беременность
    if re.search(r"беремен", low_text):
        profile.divorce_pregnancy = True

    # Проживание детей
    if re.search(r"дет(и|ь)\s+(со\s+мной|со\s+мной)|ребенок\s+со\s+мной", low_text):
        profile.divorce_child_residence = profile.divorce_child_residence or "с заявителем"
    elif re.search(r"(дети|ребенок)\s+с\s+матер", low_text):
        profile.divorce_child_residence = "с матерью"
    elif re.search(r"(дети|ребенок)\s+с\s+отц", low_text):
        profile.divorce_child_residence = "с отцом"

    # Алименты
    if re.search(r"алименты|взыскать\s+алименты|алиментов", low_text):
        profile.divorce_child_support = True

    # Труд
    if re.search(r"(уволен|увольнени|сокращен|сокращени)", low_text):
        profile.topic = profile.topic or "labor"
        profile.labor_illegal_dismissal = True if re.search(r"незаконн|без\s+основан", low_text) else profile.labor_illegal_dismissal
    if re.search(r"не\s*выплат(или|или)\s*зарплат|задолженность\s*по\s*зарплат", low_text):
        profile.topic = profile.topic or "labor"
        profile.labor_unpaid_wages = True
    if re.search(r"отпуск|компенсаци(я|ю)\s*за\s*отпуск", low_text):
        profile.topic = profile.topic or "labor"
        profile.labor_vacation_dispute = True

    # Штрафы / КоАП
    if re.search(r"штраф|коап|постановлени\s*по\s*делу\s*об\s*админ", low_text):
        profile.topic = profile.topic or "fines"
        m = re.search(r"ст\.?\s*(\d+[\.-]?\d*)\s*коап", low_text)
        if m:
            profile.koap_article = m.group(1)
        if re.search(r"обжал|оспор", low_text):
            profile.koap_need_appeal = True

    # Аренда/Жилье
    if re.search(r"аренд|съемн|найм\s+жил", low_text):
        profile.topic = profile.topic or "rent"
        if re.search(r"я\s*сдаю|мой\s*квартирант|как\s*собственник", low_text):
            profile.rent_is_landlord = True
        if re.search(r"я\s*снимаю|арендатор", low_text):
            profile.rent_is_tenant = True
        if re.search(r"долг|задолженност", low_text):
            profile.rent_debt = True

    # Миграция
    if re.search(r"внж|рвп|патент|гражданств|миграц|пмж", low_text):
        profile.topic = profile.topic or "migration"
        m = re.search(r"(внж|рвп|патент|гражданство|пмж)", low_text)
        if m:
            profile.migration_stage = m.group(1)

    # Налоги
    if re.search(r"налог|ндфл|усн|ип\s*налог|имущественн\s*вычет", low_text):
        profile.topic = profile.topic or "taxes"
        m = re.search(r"(ндфл|усн|патент\s*ип|имущественн(ый|ые)\s*вычет)", low_text)
        if m:
            profile.tax_kind = m.group(1)

    # Договоры
    if re.search(r"договор|контракт", low_text):
        profile.topic = profile.topic or "contracts"
        m = re.search(r"(купл[ья]-продаж|подряд|аренд|поставк|услуг)", low_text)
        if m:
            profile.contract_type = m.group(1)
        if re.search(r"пен(я|и)|неусто(йк|йка)", low_text):
            profile.contract_penalty = True

    # Наследство
    if re.search(r"наследств|завещани|наследник", low_text):
        profile.topic = profile.topic or "inheritance"
        if re.search(r"по\s*завещан", low_text):
            profile.inherit_by_will = True
        if re.search(r"пропустил(и)?\s*срок|восстановить\s*срок", low_text):
            profile.inherit_deadline_issue = True

    # Банкротство
    if re.search(r"банкротств|финансов(ая|ое)\s*несостоятельност", low_text):
        profile.topic = profile.topic or "bankruptcy"
        if re.search(r"физ(ическое|лицо)|гражданин", low_text):
            profile.bankruptcy_person = "физ"
        if re.search(r"юр(ическое|лицо)|компан", low_text):
            profile.bankruptcy_person = profile.bankruptcy_person or "юр"
        m = re.search(r"(\d+[\s\u00A0]*[мМ]?[лЛ]?н?)\s*руб", low_text)
        if m:
            profile.bankruptcy_debt_sum = m.group(1)

    # Исполнительное производство
    if re.search(r"пристав|фссп|исполнител(ьн|ное)\s*производ", low_text):
        profile.topic = profile.topic or "enforcement"
        if re.search(r"дело\s*возбуждено|возбудили", low_text):
            profile.enforcement_case_opened = True
        m = re.search(r"долг\s*по\s*(алим|штраф|кредит|жкх)", low_text)
        if m:
            profile.enforcement_debt_kind = m.group(1)

    # Потребитель/ЗОЗПП
    if re.search(r"зозпп|потребител|интернет\s*магазин|маркетплейс|возврат\s*товар", low_text):
        profile.topic = profile.topic or "consumer"
        if re.search(r"дистанционн|онлайн|интернет", low_text):
            profile.consumer_distance_sale = True
        if re.search(r"хочу\s*вернуть|вернуть\s*деньги|возврат", low_text):
            profile.consumer_return_request = True

    # ДТП/страхование
    if re.search(r"дтп|авар(ия|ии)|столкновени|европротокол|осаго|каско", low_text):
        profile.topic = profile.topic or "traffic"
        if re.search(r"осаго", low_text):
            profile.car_insurance = "ОСАГО"
        if re.search(r"каско", low_text):
            profile.car_insurance = profile.car_insurance or "КАСКО"
        if re.search(r"европротокол", low_text):
            profile.car_europrotocol = True


def summarize_profile(profile: Optional["CaseProfile"]) -> Optional[str]:
    if not profile:
        return None
    parts: List[str] = []
    if profile.topic == "divorce":
        parts.append("Тема: развод")
        if profile.divorce_has_children is not None:
            parts.append(f"Есть дети: {'да' if profile.divorce_has_children else 'нет'}")
        if profile.divorce_mutual_consent is not None:
            parts.append(f"Согласие: {'есть' if profile.divorce_mutual_consent else 'нет'}")
        if profile.divorce_property_dispute is not None:
            parts.append(f"Спор об имуществе: {'да' if profile.divorce_property_dispute else 'нет'}")
        if profile.divorce_spouse_absent is not None and profile.divorce_spouse_absent:
            parts.append("Супруг уклоняется/адрес неизвестен")
        if profile.divorce_pregnancy:
            parts.append("Беременность")
        if profile.divorce_child_residence:
            parts.append(f"Дети проживают: {profile.divorce_child_residence}")
        if profile.divorce_child_support is not None:
            parts.append(f"Нужны алименты: {'да' if profile.divorce_child_support else 'нет'}")
    elif profile.topic == "labor":
        parts.append("Тема: трудовой спор")
        if profile.labor_unpaid_wages:
            parts.append("Долг по зарплате")
        if profile.labor_illegal_dismissal:
            parts.append("Незаконное увольнение")
        if profile.labor_vacation_dispute:
            parts.append("Спор по отпуску")
    elif profile.topic == "fines":
        parts.append("Тема: КоАП/штраф")
        if profile.koap_article:
            parts.append(f"Статья КоАП: {profile.koap_article}")
    elif profile.topic == "rent":
        parts.append("Тема: аренда")
        parts.append(f"Статус: {'собственник' if profile.rent_is_landlord else ('арендатор' if profile.rent_is_tenant else '—')}")
        if profile.rent_debt:
            parts.append("Есть долг")
    elif profile.topic == "migration":
        parts.append("Тема: миграция")
        if profile.migration_stage:
            parts.append(f"Этап: {profile.migration_stage}")
    elif profile.topic == "taxes":
        parts.append("Тема: налоги")
        if profile.tax_kind:
            parts.append(f"Вид: {profile.tax_kind}")
        if profile.tax_period:
            parts.append(f"Период: {profile.tax_period}")
    elif profile.topic == "contracts":
        parts.append("Тема: договор")
        if profile.contract_type:
            parts.append(f"Тип: {profile.contract_type}")
        if profile.contract_penalty:
            parts.append("Неустойка")
    elif profile.topic == "inheritance":
        parts.append("Тема: наследство")
        if profile.inherit_by_will:
            parts.append("По завещанию")
        if profile.inherit_deadline_issue:
            parts.append("Проблема со сроком")
    elif profile.topic == "bankruptcy":
        parts.append("Тема: банкротство")
        if profile.bankruptcy_person:
            parts.append(f"Лицо: {profile.bankruptcy_person}")
        if profile.bankruptcy_debt_sum:
            parts.append(f"Долг: {profile.bankruptcy_debt_sum}")
    elif profile.topic == "enforcement":
        parts.append("Тема: приставы")
        if profile.enforcement_case_opened:
            parts.append("Дело возбуждено")
        if profile.enforcement_debt_kind:
            parts.append(f"Долг: {profile.enforcement_debt_kind}")
    elif profile.topic == "consumer":
        parts.append("Тема: потребитель")
        if profile.consumer_distance_sale:
            parts.append("Дистанционная покупка")
        if profile.consumer_return_request:
            parts.append("Требуется возврат")
    elif profile.topic == "traffic":
        parts.append("Тема: ДТП")
        if profile.car_insurance:
            parts.append(f"Полис: {profile.car_insurance}")
        if profile.car_europrotocol:
            parts.append("Европротокол")
    return "; ".join(parts) if parts else None

# ====== ЮР-КАРТОЧКИ (как раньше) ======
BERLIN_TZ_DATE = datetime.datetime.now().strftime("%d.%m.%Y")

@dataclass
class SourceHit:
    source_name: str
    url: str
    law_ref: str
    snippet: str
    edition_date: str

@dataclass
class AnswerCard:
    title: str
    steps: List[str]
    facts: str
    norm: str
    sources: List[str]
    warning: Optional[str] = None

@dataclass
class CaseProfile:
    topic: Optional[str] = None
    # Развод
    divorce_has_children: Optional[bool] = None
    divorce_mutual_consent: Optional[bool] = None
    divorce_property_dispute: Optional[bool] = None
    divorce_spouse_absent: Optional[bool] = None  # неизвестно где/уклоняется
    divorce_pregnancy: Optional[bool] = None
    divorce_child_residence: Optional[str] = None  # мать/отец/с кем проживают
    divorce_child_support: Optional[bool] = None  # нужен вопрос алиментов
    city: Optional[str] = None
    # Труд
    labor_unpaid_wages: Optional[bool] = None
    labor_illegal_dismissal: Optional[bool] = None
    labor_vacation_dispute: Optional[bool] = None
    # Штрафы/КоАП
    koap_article: Optional[str] = None
    koap_need_appeal: Optional[bool] = None
    # Аренда/Жилье
    rent_is_landlord: Optional[bool] = None
    rent_is_tenant: Optional[bool] = None
    rent_debt: Optional[bool] = None
    # Миграция
    migration_stage: Optional[str] = None  # рвп/внж/гражданство/патент
    # Налоги
    tax_kind: Optional[str] = None  # ндфл/ип/усн/имущественный
    tax_period: Optional[str] = None
    # Договоры
    contract_type: Optional[str] = None  # купля-продажа/подряд/аренда/поставка
    contract_penalty: Optional[bool] = None
    # Наследство
    inherit_by_will: Optional[bool] = None
    inherit_deadline_issue: Optional[bool] = None
    # Банкротство
    bankruptcy_person: Optional[str] = None  # физ/юр
    bankruptcy_debt_sum: Optional[str] = None
    # Исполнение/приставы
    enforcement_case_opened: Optional[bool] = None
    enforcement_debt_kind: Optional[str] = None
    # Потребитель/ЗОЗПП
    consumer_distance_sale: Optional[bool] = None
    consumer_return_request: Optional[bool] = None
    # ДТП/страхование
    car_insurance: Optional[str] = None  # ОСАГО/КАСКО
    car_europrotocol: Optional[bool] = None

PROCESS_MAP = {
    "арбитраж": "АПК", "апк": "АПК",
    "гпк": "ГПК", "гражданск": "ГПК",
    "админ": "КАС", "кас": "КАС",
    "уголов": "УПК", "упк": "УПК",
}

def detect_process(user_text: str) -> Optional[str]:
    t = user_text.lower()
    for kw, code in PROCESS_MAP.items():
        if kw in t:
            return code
    if re.search(r"[а-я]-\d+/\d{2}", t):  # эвристика под арбитраж
        return "АПК"
    return None

class PravoClient:
    BASE = os.getenv("PRAVO_API_BASE", "https://publication.pravo.gov.ru")
    def find_cassation_term(self, process_code: str) -> Optional[SourceHit]:
        mapping = {
            "ГПК": ("3 месяца", "ст. 376.1, 390.3 ГПК РФ"),
            "АПК": ("2 месяца", "ст. 276, 291.2 АПК РФ"),
            "КАС": ("6 месяцев", "ст. 318 КАС РФ"),
            "УПК": ("6 месяцев", "ст. 401.3 УПК РФ"),
        }
        if process_code not in mapping: return None
        val, ref = mapping[process_code]
        return SourceHit("Публикационный портал", f"{self.BASE}", ref, f"Срок кассации: {val}.", BERLIN_TZ_DATE)

class SspClient:
    BASE = os.getenv("SSP_API_BASE", "https://ssp.example.com/api")
    KEY  = os.getenv("SSP_API_KEY",  "REPLACE_ME")
    def find_cassation_term(self, process_code: str) -> Optional[SourceHit]:
        mapping = {
            "ГПК": ("3 месяца", "ст. 376.1, 390.3 ГПК РФ"),
            "АПК": ("2 месяца", "ст. 276, 291.2 АПК РФ"),
            "КАС": ("6 месяцев", "ст. 318 КАС РФ"),
            "УПК": ("6 месяцев", "ст. 401.3 УПК РФ"),
        }
        if process_code not in mapping: return None
        val, ref = mapping[process_code]
        return SourceHit("ССП (коммерческий)", f"{self.BASE}/laws?code={process_code}", ref, f"Срок кассации: {val}.", BERLIN_TZ_DATE)

def crosscheck_terms(hits: List[SourceHit]) -> Dict:
    vals = []
    for h in hits:
        m = re.search(r"(\d+)\s*(месяц|месяца|месяцев)", h.snippet)
        if m:
            vals.append(int(m.group(1)))
    if not vals:
        return {"agreed": False, "value": None, "note": "Не удалось извлечь срок из источников."}
    agreed = len(set(vals)) == 1
    best = max(vals, key=vals.count)
    main = f"{best} месяца" if best in (2,3) else f"{best} месяцев"
    return {"agreed": agreed, "value": main, "note": None if agreed else "Источники дали разные сроки — проверьте ссылки."}

def render_short_card(title: str, steps: List[str], facts: str, norm_refs: List[SourceHit]) -> AnswerCard:
    sources = [f"{h.source_name}: {h.url}" for h in norm_refs]
    all_refs = "; ".join(h.law_ref for h in norm_refs if h.law_ref)
    norm_line = f"Норма: {all_refs}; актуально на {BERLIN_TZ_DATE}."
    return AnswerCard(title=title, steps=steps[:3], facts=facts, norm=norm_line, sources=sources)

class LegalAnswerEngine:
    def __init__(self):
        self.pravo = PravoClient()
        self.ssp   = SspClient()

    # ===== Развод: карточка на основе профиля =====
    def answer_divorce(self, text: str, profile: "CaseProfile") -> AnswerCard:
        has_kids = profile.divorce_has_children
        consent = profile.divorce_mutual_consent
        prop = profile.divorce_property_dispute
        absent = profile.divorce_spouse_absent

        title = "Развод: алгоритм и подсудность"
        steps: List[str] = []
        facts_parts: List[str] = []

        if has_kids is True or prop is True:
            steps.append("Подача иска в районный суд по месту ответчика (исключения — ст. 29 ГПК РФ).")
        else:
            steps.append("Подача заявления в ЗАГС при взаимном согласии и отсутствии детей.")

        if absent:
            steps.append("Если супруг уклоняется/место жительства неизвестно — иск в суд (ст. 21 СК РФ).")

        if has_kids:
            steps.append("Определите место жительства детей и порядок общения; при необходимости — алименты.")
        else:
            steps.append("Соберите паспорта, свидетельство о браке, квитанцию госпошлины.")

        if consent is True and not has_kids and not prop:
            facts_parts.append("При взаимном согласии и без детей — через ЗАГС (ст. 19 СК РФ).")
        else:
            facts_parts.append("Через суд при наличии детей/спора/уклонении (ст. 21 СК РФ).")

        if has_kids:
            facts_parts.append("Алименты: ст. 80–83 СК РФ; определение места жительства: ст. 65, 66 СК РФ.")

        norm_refs = [
            SourceHit("СК РФ", "https://www.consultant.ru/document/cons_doc_LAW_8982/", "ст. 19, 21, 65, 66, 80–83 СК РФ", "", BERLIN_TZ_DATE),
            SourceHit("ГПК РФ", "https://www.consultant.ru/document/cons_doc_LAW_39570/", "ст. 29 ГПК РФ", "", BERLIN_TZ_DATE),
        ]

        card = render_short_card(title, steps, " ".join(facts_parts), norm_refs)
        return card

    # ===== Труд =====
    def answer_labor(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Трудовой спор: алгоритм защиты"
        steps = [
            "Соберите доказательства: трудовой договор, табели, расчётные листки.",
            "Направьте работодателю претензию, при необходимости — жалобу в ГИТ/прокуратуру.",
            "Иск в суд: зарплата/восстановление — по месту вашей работы/жительства."
        ]
        facts = []
        if p.labor_unpaid_wages:
            facts.append("Задолженность по зарплате — ст. 236 ТК РФ (проценты).")
        if p.labor_illegal_dismissal:
            facts.append("Незаконное увольнение — восстановление и средний заработок (ст. 394 ТК РФ).")
        if p.labor_vacation_dispute:
            facts.append("Компенсация за отпуск — ст. 127 ТК РФ.")
        refs = [
            SourceHit("ТК РФ", "https://www.consultant.ru/document/cons_doc_LAW_34683/", "ст. 127, 236, 394 ТК РФ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, " ".join(facts) or "Уточните предмет спора.", refs)

    # ===== КоАП/штраф =====
    def answer_fines(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Обжалование постановления по КоАП"
        steps = [
            "Проверьте срок: 10 суток со дня вручения/получения копии постановления.",
            "Подайте жалобу через вынесший орган или напрямую в суд.",
            "Приложите доказательства, ходатайствуйте о восстановлении срока при пропуске."
        ]
        facts = f"Статья: {p.koap_article or 'уточните'}; срок — 10 суток; госпошлина не уплачивается."
        refs = [
            SourceHit("КоАП РФ", "https://www.consultant.ru/document/cons_doc_LAW_34661/", "ст. 30.1–30.3 КоАП РФ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Аренда/жильё =====
    def answer_rent(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Аренда жилья: права и действия"
        steps = [
            "Проверьте договор: срок, порядок расторжения, штрафы.",
            "Составьте претензию: задолженность/нарушения/повреждения.",
            "Иск: взыскание долга/расторжение/выселение при существенных нарушениях."
        ]
        role = "собственник" if p.rent_is_landlord else ("арендатор" if p.rent_is_tenant else "уточните статус")
        facts = f"Статус: {role}; долг: {'есть' if p.rent_debt else 'нет/неизвестно'}."
        refs = [
            SourceHit("ГК РФ", "https://www.consultant.ru/document/cons_doc_LAW_5142/", "ст. 450–452, 606–624 ГК РФ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Миграция =====
    def answer_migration(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Миграционный статус: шаги оформления"
        steps = [
            "Определите основание: работа, семья, образование, носитель русского языка.",
            "Подготовьте пакет документов и подайте в МВД/через ГУВМ.",
            "Соблюдайте сроки уведомлений и продления статуса."
        ]
        facts = f"Этап: {p.migration_stage or 'уточните (РВП/ВНЖ/гражданство/патент)'}; возможны квоты/собеседование."
        refs = [
            SourceHit("Закон о правовом положении иностр.", "https://www.consultant.ru/document/cons_doc_LAW_37868/", "ФЗ-115, ФЗ-62", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Налоги =====
    def answer_taxes(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Налоги: порядок и сроки"
        steps = [
            "Определите режим и объект налогообложения.",
            "Сдайте декларацию/отчеты и оплатите налог в срок.",
            "При доначислении — проверьте законность, подайте возражения/жалобу."
        ]
        facts = f"Вид налога: {p.tax_kind or 'уточните'}; период: {p.tax_period or '—'}; возможны вычеты."
        refs = [
            SourceHit("НК РФ", "https://www.consultant.ru/document/cons_doc_LAW_28165/", "общие положения НК РФ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Договоры =====
    def answer_contracts(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Договорный спор: доказательства и требования"
        steps = [
            "Проверьте существенные условия и переписку.",
            "Направьте претензию с расчётом неустойки/убытков.",
            "Иск по подсудности: место ответчика/исполнения договора."
        ]
        facts = f"Тип: {p.contract_type or 'уточните'}; неустойка: {'да' if p.contract_penalty else 'нет/—'}."
        refs = [
            SourceHit("ГК РФ", "https://www.consultant.ru/document/cons_doc_LAW_5142/", "общая часть; ст. 309, 330, 450–452 ГК РФ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Наследство =====
    def answer_inheritance(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Наследство: сроки и порядок"
        steps = [
            "Обратитесь к нотариусу по последнему месту жительства наследодателя.",
            "Срок принятия — 6 месяцев; при пропуске — восстановление через суд.",
            "Споры о долях/обязательной доле — исковое производство."
        ]
        facts = f"По завещанию: {'да' if p.inherit_by_will else 'нет/—'}; срок: {'пропуск' if p.inherit_deadline_issue else 'в пределах/—'}."
        refs = [
            SourceHit("ГК РФ", "https://www.consultant.ru/document/cons_doc_LAW_5142/", "раздел V. Наследственное право", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Банкротство =====
    def answer_bankruptcy(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Банкротство: критерии и шаги"
        steps = [
            "Оцените признаки неплатежеспособности и размер долга.",
            "Подготовьте заявление, выберите СРО арбитражных управляющих.",
            "Подача в арбитражный суд по месту должника."
        ]
        facts = f"Лицо: {p.bankruptcy_person or 'физ/юр?'}; долг: {p.bankruptcy_debt_sum or '—'}."
        refs = [
            SourceHit("Закон о банкротстве", "https://www.consultant.ru/document/cons_doc_LAW_39331/", "127-ФЗ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Исполнительное производство =====
    def answer_enforcement(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Исполнительное производство: права должника/взыскателя"
        steps = [
            "Проверьте постановление о возбуждении, сроки, меры взыскания.",
            "Заявите ходатайства (рассрочка/отсрочка, оспаривание ареста).",
            "Жалоба старшему приставу/в суд при нарушениях."
        ]
        facts = f"Долг: {p.enforcement_debt_kind or 'уточните'}; дело возбуждено: {'да' if p.enforcement_case_opened else '—'}."
        refs = [
            SourceHit("Закон об исполнительном производстве", "https://www.consultant.ru/document/cons_doc_LAW_34587/", "229-ФЗ", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== Потребитель =====
    def answer_consumer(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "Защита прав потребителя: претензия и иск"
        steps = [
            "Претензия продавцу: недостатки/сроки, требование возврата/замены/ремонта.",
            "При отказе — иск, штраф 50% по ст. 13 ЗоЗПП при неудовлетворении требований.",
            "Неустойка и моральный вред — по расчёту."
        ]
        facts = f"Дистанционная продажа: {'да' if p.consumer_distance_sale else 'нет/—'}; возврат: {'да' if p.consumer_return_request else '—'}."
        refs = [
            SourceHit("ЗоЗПП", "https://www.consultant.ru/document/cons_doc_LAW_305/", "ст. 18–24, 13 ЗоЗПП", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    # ===== ДТП/страхование =====
    def answer_traffic(self, text: str, p: "CaseProfile") -> AnswerCard:
        title = "ДТП и страховая выплата"
        steps = [
            "Оформите извещение (или европротокол при условиях).",
            "Уведомите страховщика и подайте заявление с комплектом документов.",
            "При недоплате — досудебная претензия и иск с неустойкой."
        ]
        ins = p.car_insurance or "ОСАГО/КАСКО?"
        facts = f"Полис: {ins}; европротокол: {'да' if p.car_europrotocol else 'нет/—'}."
        refs = [
            SourceHit("Закон об ОСАГО", "https://www.consultant.ru/document/cons_doc_LAW_39331/", "40-ФЗ; Правила страхования", "", BERLIN_TZ_DATE)
        ]
        return render_short_card(title, steps, facts, refs)

    def answer_cassation(self, user_text: str) -> AnswerCard:
        process = detect_process(user_text)
        if not process:
            title = "Срок кассационной жалобы зависит от вида процесса."
            steps = ["Выберите: ГПК / АПК / КАС / УПК.",
                     "Покажу точный срок и норму со ссылками.",
                     "При пропуске срока можно просить восстановление."]
            facts = "Типовые сроки: ГПК — 3 мес; АПК — 2 мес; КАС — 6 мес; УПК — 6 мес."
            dummy = [
                SourceHit("Публикационный портал", "https://publication.pravo.gov.ru", "", "", BERLIN_TZ_DATE),
                SourceHit("ССП (коммерческий)", "https://ssp.example.com", "", "", BERLIN_TZ_DATE),
            ]
            card = render_short_card(title, steps, facts, dummy)
            card.warning = "Уточните вид судопроизводства, чтобы избежать ошибки."
            return card

        hits = [h for h in [self.pravo.find_cassation_term(process), self.ssp.find_cassation_term(process)] if h]
        if not hits:
            title = "Не удалось получить нормы из источников."
            steps = ["Повторите позже или укажите вид процесса (ГПК/АПК/КАС/УПК).",
                     "Могу дать общий алгоритм и шаблон жалобы."]
            facts = "Сроки различаются по кодексам (2–6 месяцев)."
            dummy = [SourceHit("Нет данных", "#", "", "", BERLIN_TZ_DATE)]
            card = render_short_card(title, steps, facts, dummy)
            card.warning = "Источники временно недоступны."
            return card

        cc = crosscheck_terms(hits)
        title = f"Кассационная жалоба ({process}): срок — {cc['value'] if cc['value'] else 'уточните'}."
        steps = ["Проверьте дату вступления в силу акта.",
                 "Сформируйте жалобу с приложениями и квитанцией.",
                 "Подача — через суд/онлайн с соблюдением подсудности."]
        facts = f"Срок: {cc['value'] or '—'}; госпошлина/документы — по соответствующему кодексу."
        card = render_short_card(title, steps, facts, hits)
        if cc["note"]:
            card.warning = cc["note"]
        return card

    def maybe_compose_card(self, text: str) -> Optional[AnswerCard]:
        t = text.lower()
        if ("кассац" in t) or ("кассацион" in t):
            return self.answer_cassation(text)
        if ("развод" in t) or ("расторж" in t):
            # Профиль будет подмешан в handle_text
            return None
        return None

    @staticmethod
    def card_as_text(card: AnswerCard) -> str:
        lines = []
        lines.append(f"— {card.title}")
        if card.steps:
            lines.append("Шаги: " + " → ".join(card.steps))
        if card.facts:
            lines.append(f"Сроки/пошлина/доки: {card.facts}")
        lines.append(card.norm)
        if card.warning:
            lines.append(f"⚠️ {card.warning}")
        lines.append("[Связаться с юристом]")
        return "\n".join(lines)

ENGINE = LegalAnswerEngine()

# ====== ХЕНДЛЕРЫ ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Привет! Я — *AI-юрист (бета)*. Отвечаю ТОЛЬКО на юридические вопросы по праву РФ.\n"
        "Опишите ситуацию в юридических терминах — подскажу алгоритм. Для документов и сложных кейсов есть живой юрист.\n\n"
        "Команды: /reset — очистить контекст."
    )
    await update.message.reply_text(msg, reply_markup=contact_keyboard())

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CHAT_MEMORY.pop(chat_id, None)
    CHAT_PROFILE.pop(chat_id, None)
    await update.message.reply_text("Контекст диалога очищен. Начнём заново?", reply_markup=contact_keyboard())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    low = text.lower()

    # Служебное "кто создал"
    if re.search(r"\b(кто ты|кто тебя создал|кто твой создатель|кто админ|кто администратор)\b", low):
        reply = "Я — AI-юрист (бета). Мой администратор — здесь: " + CREATOR_LINK
        await update.message.reply_text(reply, reply_markup=contact_keyboard())
        # в память добавим кратко (это по теме бота)
        CHAT_MEMORY[chat_id].append({"role": "user", "content": text})
        CHAT_MEMORY[chat_id].append({"role": "assistant", "content": reply})
        return

    await typing(context, chat_id, 0.6)

    # ===== 1) ЖЁСТКИЙ ДОМЕННЫЙ ФИЛЬТР =====
    legal_rb = is_legal_query_rule_based(text)
    legal = legal_rb or (await is_legal_query_llm(text) if not legal_rb else True)

    if not legal:
        # Отвечаем отказом и НЕ загрязняем память оффтопом
        await update.message.reply_text(OFFTOPIC_REPLY, reply_markup=contact_keyboard())
        return

    # Если юридический вопрос — пишем в память и обновляем профиль
    CHAT_MEMORY[chat_id].append({"role": "user", "content": text})
    prof = CHAT_PROFILE.get(chat_id)
    if prof is None:
        prof = CaseProfile()
        CHAT_PROFILE[chat_id] = prof
    update_profile_from_text(prof, low)

    # ===== 2) Попытка выдать юр-карточку (короткий формат, много доменов)
    base_card = ENGINE.maybe_compose_card(text)
    if base_card:
        answer_text = russianize(ENGINE.card_as_text(base_card))
        await update.message.reply_text(answer_text, reply_markup=contact_keyboard())
        CHAT_MEMORY[chat_id].append({"role": "assistant", "content": answer_text})
        return

    # Доменная маршрутизация с учетом профиля
    routed = False
    if ("развод" in low) or ("расторж" in low) or prof.topic == "divorce":
        prof.topic = "divorce"; card = ENGINE.answer_divorce(text, prof); routed = True
    elif ("уволен" in low) or ("зарплат" in low) or prof.topic == "labor":
        prof.topic = "labor"; card = ENGINE.answer_labor(text, prof); routed = True
    elif ("штраф" in low) or ("коап" in low) or prof.topic == "fines":
        prof.topic = "fines"; card = ENGINE.answer_fines(text, prof); routed = True
    elif ("аренд" in low) or ("снимаю" in low) or ("квартирант" in low) or prof.topic == "rent":
        prof.topic = "rent"; card = ENGINE.answer_rent(text, prof); routed = True
    elif ("внж" in low) or ("рвп" in low) or ("патент" in low) or ("гражданств" in low) or prof.topic == "migration":
        prof.topic = "migration"; card = ENGINE.answer_migration(text, prof); routed = True
    elif ("налог" in low) or ("ндфл" in low) or ("усн" in low) or prof.topic == "taxes":
        prof.topic = "taxes"; card = ENGINE.answer_taxes(text, prof); routed = True
    elif ("договор" in low) or ("контракт" in low) or prof.topic == "contracts":
        prof.topic = "contracts"; card = ENGINE.answer_contracts(text, prof); routed = True
    elif ("наследств" in low) or ("завещан" in low) or prof.topic == "inheritance":
        prof.topic = "inheritance"; card = ENGINE.answer_inheritance(text, prof); routed = True
    elif ("банкрот" in low) or prof.topic == "bankruptcy":
        prof.topic = "bankruptcy"; card = ENGINE.answer_bankruptcy(text, prof); routed = True
    elif ("пристав" in low) or ("исполнител" in low) or prof.topic == "enforcement":
        prof.topic = "enforcement"; card = ENGINE.answer_enforcement(text, prof); routed = True
    elif ("потребител" in low) or ("зозпп" in low) or ("возврат\s*товар" in low) or prof.topic == "consumer":
        prof.topic = "consumer"; card = ENGINE.answer_consumer(text, prof); routed = True
    elif ("дтп" in low) or ("осаго" in low) or ("каско" in low) or ("европротокол" in low) or prof.topic == "traffic":
        prof.topic = "traffic"; card = ENGINE.answer_traffic(text, prof); routed = True

    if routed:
        answer_text = russianize(ENGINE.card_as_text(card))
        await update.message.reply_text(answer_text, reply_markup=contact_keyboard())
        CHAT_MEMORY[chat_id].append({"role": "assistant", "content": answer_text})
        return

    # ===== 3) Иначе — обычный LLM-ответ (но в рамке юрдомена)
    try:
        answer = make_llm_answer(text, CHAT_MEMORY[chat_id], summarize_profile(CHAT_PROFILE.get(chat_id)))
    except Exception as e:
        log.exception("LLM error", exc_info=e)
        answer = (
            "Я — AI-юрист (бета). Коротко отвечаю по сути, но сейчас не удалось обработать запрос. "
            "Попробуйте переформулировать в юридических терминах. За деталями можно обратиться к живому юристу."
        )

    await update.message.reply_text(answer, reply_markup=contact_keyboard())
    CHAT_MEMORY[chat_id].append({"role": "assistant", "content": answer})

async def errors(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error", exc_info=context.error)

# ====== ЗАПУСК ======
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(errors)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


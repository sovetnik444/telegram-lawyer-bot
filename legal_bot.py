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

    # ===== 2) Попытка выдать юр-карточку (короткий формат)
    card = ENGINE.maybe_compose_card(text)
    if card:
        answer_text = ENGINE.card_as_text(card)
        answer_text = russianize(answer_text)
        await update.message.reply_text(answer_text, reply_markup=contact_keyboard())
        CHAT_MEMORY[chat_id].append({"role": "assistant", "content": answer_text})
        return

    # Развод — карточка с учетом профиля
    if ("развод" in low) or ("расторж" in low) or prof.topic == "divorce":
        prof.topic = "divorce"
        dcard = ENGINE.answer_divorce(text, prof)
        answer_text = russianize(ENGINE.card_as_text(dcard))
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


import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from core import AssistantTurn, Settings, SupportTicket
from core.schemas import DialogueMessage
from services.assistant.prompts import ASSISTANT_RESPONSE_SCHEMA, SUPPORT_ASSISTANT_PROMPT

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
USER_TIMEZONE = ZoneInfo("Europe/Moscow")
MONTH_NAMES = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


class OpenAISupportAssistant:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.openai_base_url,
            timeout=httpx.Timeout(45.0, connect=12.0),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
        )

    async def generate_turn(
        self,
        current_ticket: SupportTicket,
        user_message: str,
        is_new_session: bool,
        conversation_history: list[DialogueMessage],
        last_assistant_message: str | None,
        telegram_first_name: str | None,
    ) -> AssistantTurn:
        payload = {
            "model": self._settings.openai_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SUPPORT_ASSISTANT_PROMPT},
                {
                    "role": "user",
                    "content": self._build_user_prompt(
                        current_ticket=current_ticket,
                        user_message=user_message,
                        is_new_session=is_new_session,
                        conversation_history=conversation_history,
                        last_assistant_message=last_assistant_message,
                        telegram_first_name=telegram_first_name,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": ASSISTANT_RESPONSE_SCHEMA,
            },
        }

        try:
            response = await self._post_with_retries(payload)
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            turn = AssistantTurn.model_validate(json.loads(content))
            return self._post_process_turn(
                turn=turn,
                current_ticket=current_ticket,
                user_message=user_message,
                last_assistant_message=last_assistant_message,
            )
        except Exception:
            logger.exception("Falling back to local consultation turn generation")
            turn = self._build_fallback_turn(
                current_ticket=current_ticket,
                user_message=user_message,
                is_new_session=is_new_session,
                conversation_history=conversation_history,
                last_assistant_message=last_assistant_message,
                telegram_first_name=telegram_first_name,
            )
            return self._post_process_turn(
                turn=turn,
                current_ticket=current_ticket,
                user_message=user_message,
                last_assistant_message=last_assistant_message,
            )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post_with_retries(self, payload: dict) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                if status_code not in RETRYABLE_STATUS_CODES or attempt == 3:
                    raise
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 3:
                    break

            await asyncio.sleep(0.75 * attempt)

        assert last_error is not None
        raise last_error

    @staticmethod
    def _build_user_prompt(
        current_ticket: SupportTicket,
        user_message: str,
        is_new_session: bool,
        conversation_history: list[DialogueMessage],
        last_assistant_message: str | None,
        telegram_first_name: str | None,
    ) -> str:
        ticket_json = json.dumps(current_ticket.model_dump(), ensure_ascii=False, indent=2)
        history_json = json.dumps(
            [message.model_dump() for message in conversation_history],
            ensure_ascii=False,
            indent=2,
        )
        first_name = telegram_first_name or "null"
        last_bot_message = last_assistant_message or "null"

        return (
            f"is_new_session: {str(is_new_session).lower()}\n"
            f"telegram_first_name: {first_name}\n"
            f"current_ticket:\n{ticket_json}\n\n"
            f"conversation_history:\n{history_json}\n\n"
            f"last_assistant_message:\n{last_bot_message}\n\n"
            f"latest_user_message:\n{user_message}"
        )

    def _build_fallback_turn(
        self,
        current_ticket: SupportTicket,
        user_message: str,
        is_new_session: bool,
        conversation_history: list[DialogueMessage],
        last_assistant_message: str | None,
        telegram_first_name: str | None,
    ) -> AssistantTurn:
        message = self._normalize_text(user_message)
        message_lower = message.lower()
        extracted = SupportTicket()
        requested_field = self._detect_requested_field(last_assistant_message)

        if self._is_repeat_question_request(message_lower):
            repeated_question = last_assistant_message or "Пока мы не дошли до следующего вопроса."
            return AssistantTurn(
                reply=f"Последний мой вопрос был таким: {repeated_question}",
                extracted_ticket=extracted,
                ready_to_submit=current_ticket.is_complete(),
            )

        if not current_ticket.name and self._looks_like_name(message):
            extracted.name = message

        if not current_ticket.contact:
            contact = self._extract_contact(message)
            if contact:
                extracted.contact = contact

        if requested_field == "name" and not extracted.name and self._looks_like_name(message):
            extracted.name = message
        elif requested_field == "contact" and not extracted.contact:
            extracted.contact = self._extract_contact(message)
        elif requested_field == "company_name" and not extracted.company_name:
            extracted.company_name = self._extract_company_name(message, current_ticket)
        elif requested_field == "consultation_topic" and not extracted.consultation_topic:
            extracted.consultation_topic = self._extract_consultation_topic(message, current_ticket)
        elif requested_field == "preferred_datetime" and not extracted.preferred_datetime:
            extracted.preferred_datetime = self._extract_preferred_datetime(message)
        elif requested_field == "consultation_format" and not extracted.consultation_format:
            extracted.consultation_format = self._extract_consultation_format(message_lower)

        if not extracted.company_name and not current_ticket.company_name:
            extracted.company_name = self._extract_company_name(message, current_ticket)

        if not extracted.preferred_datetime and not current_ticket.preferred_datetime:
            extracted.preferred_datetime = self._extract_preferred_datetime(message)

        if not extracted.consultation_format and not current_ticket.consultation_format:
            extracted.consultation_format = self._extract_consultation_format(message_lower)

        if (
            not extracted.consultation_topic
            and not self._looks_like_name(message)
            and not self._extract_contact(message)
            and not self._extract_preferred_datetime(message)
            and not self._extract_consultation_format(message_lower)
        ):
            extracted.consultation_topic = self._extract_consultation_topic(message, current_ticket)

        merged_ticket = current_ticket.model_copy(deep=True)
        merged_ticket.merge(extracted)

        reply = self._build_fallback_reply(
            merged_ticket=merged_ticket,
            extracted=extracted,
            is_new_session=is_new_session,
            conversation_history=conversation_history,
            telegram_first_name=telegram_first_name,
        )

        return AssistantTurn(
            reply=reply,
            extracted_ticket=extracted,
            ready_to_submit=merged_ticket.is_complete(),
        )

    def _post_process_turn(
        self,
        turn: AssistantTurn,
        current_ticket: SupportTicket,
        user_message: str,
        last_assistant_message: str | None,
    ) -> AssistantTurn:
        requested_field = self._detect_requested_field(last_assistant_message)
        extracted = turn.extracted_ticket.model_copy(deep=True)
        message = self._normalize_text(user_message)

        if requested_field == "company_name":
            company_candidate = self._extract_company_name(message, current_ticket)
            inline_topic = self._extract_inline_topic(message)

            if company_candidate:
                extracted.company_name = company_candidate

            if inline_topic:
                extracted.consultation_topic = self._expand_topic_with_project(
                    inline_topic,
                    extracted.company_name or current_ticket.company_name,
                )
            elif extracted.consultation_topic and extracted.company_name:
                if self._normalized_compare(extracted.consultation_topic, extracted.company_name):
                    extracted.consultation_topic = None

        if requested_field == "consultation_topic":
            topic_candidate = extracted.consultation_topic or self._extract_consultation_topic(message, current_ticket)
            if topic_candidate:
                extracted.consultation_topic = self._expand_topic_with_project(
                    topic_candidate,
                    extracted.company_name or current_ticket.company_name,
                )

        if extracted.consultation_topic and extracted.company_name:
            if self._normalized_compare(extracted.consultation_topic, extracted.company_name):
                extracted.consultation_topic = None

        if extracted.preferred_datetime:
            extracted.preferred_datetime = self._normalize_preferred_datetime(extracted.preferred_datetime)

        merged_ticket = current_ticket.model_copy(deep=True)
        merged_ticket.merge(extracted)

        return AssistantTurn(
            reply=turn.reply,
            extracted_ticket=extracted,
            ready_to_submit=merged_ticket.is_complete(),
        )

    def _build_fallback_reply(
        self,
        merged_ticket: SupportTicket,
        extracted: SupportTicket,
        is_new_session: bool,
        conversation_history: list[DialogueMessage],
        telegram_first_name: str | None,
    ) -> str:
        if not conversation_history and is_new_session and not extracted.name and not merged_ticket.name:
            return "Здравствуйте! Я помогу записаться на консультацию. Как вас зовут?"

        if not merged_ticket.name:
            return "Подскажите, как к вам обращаться?"

        if not merged_ticket.contact:
            return "Оставьте, пожалуйста, контакт для связи: телефон или Telegram."

        if not merged_ticket.company_name:
            name = merged_ticket.name or telegram_first_name
            prefix = f"{name}, " if name else ""
            return f"{prefix}из какой вы компании или по какому проекту хотите консультацию?"

        if not merged_ticket.consultation_topic:
            return "Коротко расскажите, пожалуйста, по какому вопросу нужна консультация."

        if not merged_ticket.preferred_datetime:
            return "Когда вам удобно созвониться или встретиться?"

        if not merged_ticket.consultation_format:
            return "Какой формат вам удобнее: онлайн, офлайн или не важно?"

        return "Спасибо! Проверяю, всё ли собрано по консультации."

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _is_repeat_question_request(message_lower: str) -> bool:
        triggers = [
            "какой был прошлый вопрос",
            "какой был предыдущий вопрос",
            "повтори вопрос",
            "повтори последний вопрос",
            "что ты спрашивал",
            "что вы спрашивали",
        ]
        return any(trigger in message_lower for trigger in triggers)

    @staticmethod
    def _detect_requested_field(last_assistant_message: str | None) -> str | None:
        if not last_assistant_message:
            return None

        message = last_assistant_message.lower()
        if any(phrase in message for phrase in ["как вас зовут", "как к вам обращаться", "как вас зовут?"]):
            return "name"
        if any(phrase in message for phrase in ["контакт", "телефон", "telegram"]):
            return "contact"
        if any(phrase in message for phrase in ["из какой вы компании", "по какому проекту", "компании или проекту"]):
            return "company_name"
        if any(phrase in message for phrase in ["по какому вопросу нужна консультация", "по какому вопросу", "тема консультации"]):
            return "consultation_topic"
        if any(phrase in message for phrase in ["когда вам удобно", "когда удобно созвониться", "когда удобно встретиться"]):
            return "preferred_datetime"
        if any(phrase in message for phrase in ["какой формат", "онлайн", "офлайн", "не важно"]):
            return "consultation_format"
        return None

    @staticmethod
    def _looks_like_name(message: str) -> bool:
        lowered = message.lower()
        blockers = [
            "компан",
            "проект",
            "консультац",
            "созвон",
            "встреч",
            "сайт",
            "прилож",
            "бренд",
            "онлайн",
            "офлайн",
            "не важно",
            "завтра",
            "сегодня",
            "в ",
        ]
        if any(blocker in lowered for blocker in blockers):
            return False
        if any(char.isdigit() for char in message):
            return False
        words = [word for word in re.split(r"\s+", message) if word]
        return 1 <= len(words) <= 3

    @staticmethod
    def _extract_contact(message: str) -> str | None:
        if message.startswith("@") and len(message) > 1:
            return message

        compact = re.sub(r"[^\d+]", "", message)
        digits = re.sub(r"\D", "", compact)
        if len(digits) >= 10:
            return compact
        return None

    def _extract_company_name(self, message: str, current_ticket: SupportTicket) -> str | None:
        cleaned = self._normalize_text(message)
        message_lower = cleaned.lower()

        if not cleaned or self._looks_like_name(cleaned) or self._extract_contact(cleaned):
            return None
        if self._extract_preferred_datetime(cleaned) or self._extract_consultation_format(message_lower):
            return None

        patterns = [
            r"(?:компания|компанию|из компании|работаю в|представляю)\s+(.+)",
            r"(?:проект|проекту|по проекту|бренд)\s+(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message_lower)
            if match:
                candidate = cleaned[match.start(1):].strip(" .,:;-")
                candidate = re.split(
                    r",|\.|;| нужна консультация| хочу консультацию| интересует консультация| нужен созвон",
                    candidate,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip(" .,:;-")
                return candidate or None

        if any(token in message_lower for token in ["ооо", "ип", "llc", "inc", "студия", "агентство", "магазин"]):
            return cleaned
        if len(cleaned.split()) <= 5 and current_ticket.company_name is None:
            return cleaned
        return None

    def _extract_consultation_topic(self, message: str, current_ticket: SupportTicket) -> str | None:
        cleaned = self._normalize_text(message)
        if not cleaned:
            return None

        lowered = cleaned.lower()
        if self._looks_like_name(cleaned) or self._extract_contact(cleaned):
            return None
        if self._extract_preferred_datetime(cleaned) or self._extract_consultation_format(lowered):
            return None

        existing = current_ticket.consultation_topic
        if existing:
            existing_lower = existing.lower()
            if lowered == existing_lower or lowered in existing_lower:
                return None
            if len(cleaned.split()) <= 5:
                return f"{existing.rstrip('.')} {cleaned}".strip()

        prefixes = [
            "нужна консультация по",
            "хочу консультацию по",
            "интересует консультация по",
            "нужен созвон по",
            "интересует",
        ]
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return cleaned[len(prefix):].strip(" .,:;-") or None

        inline_match = re.search(
            r"(?:нужна консультация по|хочу консультацию по|интересует консультация по|нужен созвон по)\s+(.+)",
            lowered,
        )
        if inline_match:
            return cleaned[inline_match.start(1):].strip(" .,:;-") or None

        return cleaned if len(cleaned.split()) >= 2 else None

    @staticmethod
    def _extract_inline_topic(message: str) -> str | None:
        lowered = message.lower()
        match = re.search(
            r"(?:,|\s)(как\s+.+|настроить\s+.+|запустить\s+.+|подключить\s+.+|автоматизировать\s+.+)$",
            lowered,
        )
        if not match:
            return None
        return message[match.start(1):].strip(" .,:;-")

    @staticmethod
    def _extract_preferred_datetime(message: str) -> str | None:
        lowered = message.lower()
        tokens = [
            "сегодня",
            "завтра",
            "послезавтра",
            "утром",
            "днем",
            "днём",
            "вечером",
            "понедельник",
            "вторник",
            "среду",
            "четверг",
            "пятницу",
            "субботу",
            "воскресенье",
            "неделе",
            "после ",
            "до ",
        ]
        if any(token in lowered for token in tokens):
            return message
        if re.search(r"\b\d{1,2}[:.]\d{2}\b", lowered):
            return message
        if re.search(r"\b\d{1,2}\s*(?:мая|июня|июля|августа|сентября|октября|ноября|декабря|января|февраля|марта|апреля)\b", lowered):
            return message
        return None

    def _normalize_preferred_datetime(self, value: str) -> str:
        cleaned = self._normalize_text(value)
        lowered = cleaned.lower()
        today = datetime.now(USER_TIMEZONE).date()
        target_date: date | None = None

        iso_dt = self._parse_iso_datetime(cleaned)
        if iso_dt is not None:
            return self._format_human_datetime(iso_dt.date(), iso_dt.strftime("%H:%M"))

        if "послезавтра" in lowered:
            target_date = today + timedelta(days=2)
        elif "завтра" in lowered:
            target_date = today + timedelta(days=1)
        elif "сегодня" in lowered:
            target_date = today
        else:
            weekday_date = self._extract_weekday_date(lowered, today)
            explicit_date = self._extract_explicit_date(lowered, today.year)
            target_date = explicit_date or weekday_date

        time_match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lowered)
        time_part = None
        if time_match:
            hours = int(time_match.group(1))
            minutes = int(time_match.group(2))
            time_part = f"{hours:02d}:{minutes:02d}"

        if target_date is not None:
            return self._format_human_datetime(target_date, time_part)

        explicit_numeric = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", lowered)
        if explicit_numeric:
            day = int(explicit_numeric.group(1))
            month = int(explicit_numeric.group(2))
            year_raw = explicit_numeric.group(3)
            year = today.year if not year_raw else int(year_raw)
            if year < 100:
                year += 2000
            try:
                normalized = date(year, month, day)
                return self._format_human_datetime(normalized, time_part)
            except ValueError:
                return cleaned

        return cleaned

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime | None:
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(USER_TIMEZONE)
        return parsed

    @staticmethod
    def _format_human_datetime(target_date: date, time_part: str | None) -> str:
        date_part = f"{target_date.day} {MONTH_NAMES[target_date.month]} {target_date.year} года"
        return f"{date_part}, {time_part}".strip(", ") if time_part else date_part

    @staticmethod
    def _extract_explicit_date(message_lower: str, default_year: int) -> date | None:
        month_map = {
            "января": 1,
            "февраля": 2,
            "марта": 3,
            "апреля": 4,
            "мая": 5,
            "июня": 6,
            "июля": 7,
            "августа": 8,
            "сентября": 9,
            "октября": 10,
            "ноября": 11,
            "декабря": 12,
        }
        match = re.search(
            r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?\b",
            message_lower,
        )
        if not match:
            return None

        day = int(match.group(1))
        month = month_map[match.group(2)]
        year = int(match.group(3)) if match.group(3) else default_year
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _extract_weekday_date(message_lower: str, today: date) -> date | None:
        weekday_map = {
            "понедельник": 0,
            "вторник": 1,
            "среду": 2,
            "среда": 2,
            "четверг": 3,
            "пятницу": 4,
            "пятница": 4,
            "субботу": 5,
            "суббота": 5,
            "воскресенье": 6,
        }
        for token, weekday in weekday_map.items():
            if token in message_lower:
                delta = (weekday - today.weekday()) % 7
                delta = 7 if delta == 0 else delta
                return today + timedelta(days=delta)
        return None

    @staticmethod
    def _normalized_compare(left: str | None, right: str | None) -> bool:
        if not left or not right:
            return False
        normalized_left = re.sub(r"\s+", " ", left).strip(" .,:;-").lower()
        normalized_right = re.sub(r"\s+", " ", right).strip(" .,:;-").lower()
        return normalized_left == normalized_right

    @staticmethod
    def _expand_topic_with_project(topic: str, project_name: str | None) -> str:
        cleaned_topic = re.sub(r"\s+", " ", topic).strip(" .,:;-")
        if not cleaned_topic:
            return cleaned_topic
        if not project_name:
            return cleaned_topic

        project = project_name.strip()
        lowered = cleaned_topic.lower()

        if "его" in lowered:
            replacements = [
                (r"\bкак его настроить\b", f"как настроить {project}"),
                (r"\bнастроить его\b", f"настроить {project}"),
                (r"\bкак его запустить\b", f"как запустить {project}"),
                (r"\bкак его подключить\b", f"как подключить {project}"),
            ]
            for pattern, replacement in replacements:
                if re.search(pattern, lowered):
                    return re.sub(pattern, replacement, lowered, count=1).strip()

        if lowered.startswith("как настроить "):
            return cleaned_topic
        if lowered == "как его настроить":
            return f"как настроить {project}"

        return cleaned_topic

    @staticmethod
    def _extract_consultation_format(message_lower: str) -> str | None:
        if "не важно" in message_lower or "без разницы" in message_lower:
            return "не важно"
        if any(token in message_lower for token in ["офлайн", "в офисе", "лично", "при встрече"]):
            return "офлайн"
        if any(token in message_lower for token in ["онлайн", "zoom", "google meet", "созвон", "видеосвяз"]):
            return "онлайн"
        return None

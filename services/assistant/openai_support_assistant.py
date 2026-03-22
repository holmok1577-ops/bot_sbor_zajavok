import asyncio
import json
import logging
import re

import httpx

from core import AssistantTurn, Settings, SupportTicket
from core.schemas import DialogueMessage
from services.assistant.prompts import ASSISTANT_RESPONSE_SCHEMA, SUPPORT_ASSISTANT_PROMPT

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


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
            return AssistantTurn.model_validate(json.loads(content))
        except Exception:
            logger.exception("Falling back to local support turn generation")
            return self._build_fallback_turn(
                current_ticket=current_ticket,
                user_message=user_message,
                is_new_session=is_new_session,
                conversation_history=conversation_history,
                last_assistant_message=last_assistant_message,
                telegram_first_name=telegram_first_name,
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
        elif requested_field == "occurred_at" and self._looks_like_time_answer(message_lower):
            extracted.occurred_at = message
        elif requested_field == "priority":
            extracted.priority = self._extract_priority(message_lower)
        elif requested_field == "location":
            extracted.location = self._extract_location(message, current_ticket)
        elif requested_field == "problem":
            extracted.problem_summary = self._extract_problem_summary(message, current_ticket)

        if not extracted.problem_summary and not current_ticket.problem_summary and not self._looks_like_name(message):
            extracted.problem_summary = self._extract_problem_summary(message, current_ticket)

        if (
            not extracted.problem_summary
            and current_ticket.problem_summary
            and len(message.split()) <= 4
            and not self._looks_like_name(message)
            and not self._looks_like_time_answer(message_lower)
            and not self._extract_priority(message_lower)
            and not self._extract_contact(message)
        ):
            extracted.problem_summary = self._extract_problem_summary(message, current_ticket)

        if not extracted.occurred_at and not current_ticket.occurred_at and self._looks_like_time_answer(message_lower):
            extracted.occurred_at = message

        if not extracted.location and not current_ticket.location:
            extracted.location = self._extract_location(message, current_ticket)

        if not extracted.priority and not current_ticket.priority:
            extracted.priority = self._extract_priority(message_lower)

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

    def _build_fallback_reply(
        self,
        merged_ticket: SupportTicket,
        extracted: SupportTicket,
        is_new_session: bool,
        conversation_history: list[DialogueMessage],
        telegram_first_name: str | None,
    ) -> str:
        if not conversation_history and is_new_session and not extracted.name and not merged_ticket.name:
            return "Здравствуйте! Я помогу вам с обращением в техподдержку. Как вас зовут?"

        if not merged_ticket.name:
            return "Подскажите, как к вам обращаться?"

        if not merged_ticket.contact:
            return "Оставьте, пожалуйста, контакт для связи: телефон или Telegram."

        if not merged_ticket.problem_summary:
            name = merged_ticket.name or telegram_first_name
            prefix = f"{name}, " if name else ""
            return f"{prefix}кратко опишите, пожалуйста, что случилось."

        if not merged_ticket.occurred_at:
            return "Подскажите, пожалуйста, когда началась эта проблема?"

        if not merged_ticket.location:
            if self._is_device_issue(merged_ticket.problem_summary):
                return "Правильно понимаю, проблема возникает с самим устройством? Если да, напишите, с каким именно."
            return "Где именно проявляется проблема: на сайте, в приложении, в функции или на устройстве?"

        if not merged_ticket.priority:
            return "Насколько это срочно: срочно, средне или низкий приоритет?"

        return "Спасибо! Проверяю, всё ли собрано по заявке."

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
        if any(phrase in message for phrase in ["как вас зовут", "как к вам обращаться", "полное имя"]):
            return "name"
        if any(phrase in message for phrase in ["контакт", "телефон для связи", "telegram"]):
            return "contact"
        if any(phrase in message for phrase in ["когда началась", "когда возникла", "когда это началось"]):
            return "occurred_at"
        if any(phrase in message for phrase in ["насколько это срочно", "какой приоритет", "срочно"]):
            return "priority"
        if any(phrase in message for phrase in ["где именно", "где проявляется", "в приложении", "на сайте", "на устройстве"]):
            return "location"
        if any(
            phrase in message
            for phrase in [
                "что случилось",
                "в чем проблема",
                "в чём проблема",
                "в чем именно проблема",
                "в чём именно проблема",
                "опишите проблему",
                "что именно",
            ]
        ):
            return "problem"
        return None

    @staticmethod
    def _looks_like_name(message: str) -> bool:
        lowered = message.lower()
        blockers = [
            "проблем",
            "ошибк",
            "не ",
            "невключ",
            "не включ",
            "телефон",
            "ноутбук",
            "прилож",
            "сайт",
            "экран",
            "кнопк",
            "срочно",
            "вчера",
            "сегодня",
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

    @staticmethod
    def _looks_like_time_answer(message_lower: str) -> bool:
        tokens = [
            "минут",
            "час",
            "день",
            "недел",
            "месяц",
            "сегодня",
            "вчера",
            "только что",
            "утром",
            "вечером",
            "назад",
        ]
        return any(token in message_lower for token in tokens)

    @staticmethod
    def _extract_priority(message_lower: str) -> str | None:
        if any(token in message_lower for token in ["срочно", "критично", "очень срочно", "горит", "важно срочно"]):
            return "срочно"
        if "низк" in message_lower:
            return "низкий приоритет"
        if any(token in message_lower for token in ["средне", "не срочно", "обычно", "терпит"]):
            return "средне"
        return None

    def _extract_location(self, message: str, current_ticket: SupportTicket) -> str | None:
        message_lower = message.lower()
        if "телефон" in message_lower:
            return "телефон"
        if "ноутбук" in message_lower:
            return "ноутбук"
        if "компьютер" in message_lower or "пк" in message_lower:
            return "компьютер"
        if "принтер" in message_lower:
            return "принтер"
        if "сайт" in message_lower:
            return "сайт"
        if "прилож" in message_lower:
            return "приложение"
        if "личн" in message_lower and "кабинет" in message_lower:
            return "личный кабинет"
        if self._is_device_issue(message) or self._is_device_issue(current_ticket.problem_summary):
            return "устройство"
        return None

    def _extract_problem_summary(self, message: str, current_ticket: SupportTicket) -> str | None:
        cleaned = self._normalize_text(message)
        if not cleaned:
            return None

        existing = current_ticket.problem_summary
        message_lower = cleaned.lower()

        if existing:
            existing_lower = existing.lower()
            if cleaned.lower() == existing_lower:
                return None
            if "не включ" in existing_lower and cleaned.lower() in {"телефон", "ноутбук", "компьютер"}:
                return f"{cleaned.capitalize()} не включается"
            if cleaned.lower() in existing_lower:
                return None
            if len(cleaned.split()) <= 4:
                return f"{existing.rstrip('.')} {cleaned}".strip()

        if self._looks_like_name(cleaned):
            return None

        return cleaned

    @staticmethod
    def _is_device_issue(value: str | None) -> bool:
        if not value:
            return False
        lowered = value.lower()
        return any(
            token in lowered
            for token in [
                "телефон",
                "ноутбук",
                "компьютер",
                "принтер",
                "устройство",
                "не включ",
                "кнопк",
                "экран",
            ]
        )

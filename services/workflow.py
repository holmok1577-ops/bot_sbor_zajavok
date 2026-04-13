import logging

from core import SupportSession
from services.assistant import OpenAISupportAssistant
from services.telegram import OperatorNotifier

logger = logging.getLogger(__name__)

FINAL_CLIENT_MESSAGE = (
    "Спасибо! Я передал вашу заявку менеджеру. "
    "С вами свяжутся для подтверждения консультации."
)


class SupportWorkflowService:
    def __init__(
        self,
        assistant: OpenAISupportAssistant,
        notifier: OperatorNotifier,
    ) -> None:
        self._assistant = assistant
        self._notifier = notifier

    async def process_message(self, session: SupportSession, message_text: str) -> str:
        if session.submitted:
            session.reset()

        self._prefill_contact_from_telegram(session)
        history_before_turn = session.recent_history()
        is_new_session = not history_before_turn and not session.started

        turn = await self._assistant.generate_turn(
            current_ticket=session.ticket,
            user_message=message_text,
            is_new_session=is_new_session,
            conversation_history=history_before_turn,
            last_assistant_message=session.last_assistant_message,
            telegram_first_name=session.telegram_first_name,
        )

        session.add_user_message(message_text)
        session.ticket.merge(turn.extracted_ticket)
        session.started = True

        if session.ticket.is_complete() and turn.ready_to_submit:
            await self._notifier.send_ticket(session)
            session.submitted = True
            session.add_assistant_message(FINAL_CLIENT_MESSAGE)
            logger.info("Consultation request submitted for user_id=%s", session.user_id)
            return FINAL_CLIENT_MESSAGE

        session.add_assistant_message(turn.reply)
        return turn.reply

    @staticmethod
    def _prefill_contact_from_telegram(session: SupportSession) -> None:
        if session.ticket.contact:
            return
        if session.telegram_username:
            session.ticket.contact = f"@{session.telegram_username}"

    async def close(self) -> None:
        await self._assistant.close()

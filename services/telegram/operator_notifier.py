from aiogram import Bot

from core import Settings, SupportSession


class OperatorNotifier:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings

    async def send_ticket(self, session: SupportSession) -> None:
        ticket = session.ticket
        lines = [
            "=== НОВАЯ ЗАЯВКА В ТП ===",
            "",
            f"Имя: {ticket.name}",
            f"Контакт: {ticket.contact}",
            "",
            "Проблема:",
            f"{ticket.problem_summary}",
            "",
            f"Когда возникло: {ticket.occurred_at}",
            f"Где: {ticket.location}",
            "",
            f"Приоритет: {ticket.priority}",
            "",
            f"Telegram user id: {session.user_id}",
            f"Telegram username: @{session.telegram_username}" if session.telegram_username else "Telegram username: -",
            "",
            "=== КОНЕЦ ===",
        ]
        await self._bot.send_message(self._settings.operator_chat_id, "\n".join(lines))

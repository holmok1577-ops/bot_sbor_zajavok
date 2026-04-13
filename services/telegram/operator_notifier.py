from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from core import Settings, SupportSession

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


class OperatorNotifier:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings

    async def send_ticket(self, session: SupportSession) -> None:
        ticket = session.ticket
        lines = [
            "=== НОВАЯ ЗАЯВКА НА КОНСУЛЬТАЦИЮ ===",
            "",
            f"Имя: {ticket.name}",
            f"Контакт: {ticket.contact}",
            "",
            f"Компания / проект: {ticket.company_name}",
            "",
            "Тема консультации:",
            f"{ticket.consultation_topic}",
            "",
            f"Когда удобно: {self._format_datetime(ticket.preferred_datetime)}",
            f"Формат: {ticket.consultation_format}",
            "",
            f"Telegram user id: {session.user_id}",
            f"Telegram username: @{session.telegram_username}" if session.telegram_username else "Telegram username: -",
            "",
            "=== КОНЕЦ ===",
        ]
        await self._bot.send_message(self._settings.operator_chat_id, "\n".join(lines))

    @staticmethod
    def _format_datetime(value: str | None) -> str:
        if not value:
            return "-"

        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(USER_TIMEZONE)

        date_part = f"{parsed.day} {MONTH_NAMES[parsed.month]} {parsed.year} года"
        return f"{date_part}, {parsed.strftime('%H:%M')}"

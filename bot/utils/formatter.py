from core import SupportTicket


def format_collected_ticket(ticket: SupportTicket) -> str:
    lines = [
        f"Имя: {ticket.name or '-'}",
        f"Контакт: {ticket.contact or '-'}",
        f"Компания / проект: {ticket.company_name or '-'}",
        f"Тема консультации: {ticket.consultation_topic or '-'}",
        f"Когда удобно: {ticket.preferred_datetime or '-'}",
        f"Формат: {ticket.consultation_format or '-'}",
    ]
    return "\n".join(lines)

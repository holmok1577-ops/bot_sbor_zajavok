from core import SupportTicket


def format_collected_ticket(ticket: SupportTicket) -> str:
    lines = [
        f"Имя: {ticket.name or '-'}",
        f"Контакт: {ticket.contact or '-'}",
        f"Проблема: {ticket.problem_summary or '-'}",
        f"Когда возникло: {ticket.occurred_at or '-'}",
        f"Где: {ticket.location or '-'}",
        f"Приоритет: {ticket.priority or '-'}",
    ]
    return "\n".join(lines)

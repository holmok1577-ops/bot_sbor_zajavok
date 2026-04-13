from typing import Literal

from pydantic import BaseModel, Field, field_validator


ConsultationFormat = Literal["онлайн", "офлайн", "не важно"]
MessageRole = Literal["user", "assistant"]


class SupportTicket(BaseModel):
    name: str | None = None
    contact: str | None = None
    company_name: str | None = None
    consultation_topic: str | None = None
    preferred_datetime: str | None = None
    consultation_format: ConsultationFormat | None = None

    @field_validator(
        "name",
        "contact",
        "company_name",
        "consultation_topic",
        "preferred_datetime",
    )
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split()).strip()
        return cleaned or None

    def merge(self, other: "SupportTicket") -> None:
        for field_name, value in other.model_dump().items():
            if value not in (None, ""):
                setattr(self, field_name, value)

    def is_complete(self) -> bool:
        return all(
            [
                self.name,
                self.contact,
                self.company_name,
                self.consultation_topic,
                self.preferred_datetime,
                self.consultation_format,
            ]
        )


class DialogueMessage(BaseModel):
    role: MessageRole
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            raise ValueError("Dialogue message cannot be empty")
        return cleaned


class AssistantTurn(BaseModel):
    reply: str = Field(min_length=1)
    extracted_ticket: SupportTicket = Field(default_factory=SupportTicket)
    ready_to_submit: bool = False


class SupportSession(BaseModel):
    user_id: int
    chat_id: int
    telegram_username: str | None = None
    telegram_first_name: str | None = None
    started: bool = False
    submitted: bool = False
    ticket: SupportTicket = Field(default_factory=SupportTicket)
    history: list[DialogueMessage] = Field(default_factory=list)

    def add_user_message(self, text: str) -> None:
        self._append_history("user", text)

    def add_assistant_message(self, text: str) -> None:
        self._append_history("assistant", text)

    def recent_history(self, limit: int = 8) -> list[DialogueMessage]:
        return list(self.history[-limit:])

    @property
    def last_assistant_message(self) -> str | None:
        for message in reversed(self.history):
            if message.role == "assistant":
                return message.text
        return None

    def reset(self) -> None:
        self.started = False
        self.submitted = False
        self.ticket = SupportTicket()
        self.history = []

    def _append_history(self, role: MessageRole, text: str) -> None:
        self.history.append(DialogueMessage(role=role, text=text))
        if len(self.history) > 20:
            self.history = self.history[-20:]

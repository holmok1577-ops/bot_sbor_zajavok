# bot_sbor_zajavok

Telegram-бот для записи клиента на консультацию.

Бот:

- общается с клиентом в чате;
- собирает обязательные поля по одному;
- использует LLM, чтобы извлечь данные из свободного текста;
- формирует структурированную заявку;
- отправляет ее в Telegram-чат менеджеров.

## Что собирает ассистент

- имя клиента;
- контакт;
- компания или проект;
- тема консультации;
- удобные дата и время;
- формат встречи.

## Структура проекта

```text
incoming_lids/
├── bot/
│   ├── handlers/
│   │   ├── __init__.py
│   │   └── support.py
│   ├── utils/
│   │   ├── __init__.py
│   │   └── formatter.py
│   ├── __init__.py
│   └── main.py
├── core/
│   ├── __init__.py
│   ├── config.py
│   ├── logging.py
│   └── schemas.py
├── services/
│   ├── assistant/
│   │   ├── __init__.py
│   │   ├── openai_support_assistant.py
│   │   └── prompts.py
│   ├── storage/
│   │   ├── __init__.py
│   │   └── session_repository.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   └── operator_notifier.py
│   ├── __init__.py
│   └── workflow.py
├── .env.example
├── .gitignore
├── main.py
├── REPORT.md
└── requirements.txt
```

## Как работает

1. Клиент пишет боту.
2. Бот сохраняет сессию в памяти.
3. `OpenAISupportAssistant` получает:
   - текущее состояние заявки;
   - новое сообщение клиента;
   - флаг первого сообщения.
4. Модель возвращает JSON:
   - ответ клиенту;
   - обновленные поля заявки;
   - признак готовности к отправке.
5. Когда обязательные поля собраны, бот отправляет заявку в чат менеджеров.
6. Клиент получает подтверждение.

## Переменные окружения

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OPERATOR_CHAT_ID=-1001234567890
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4.1-mini
OPENAI_BASE_URL=https://api.openai.com/v1
LOG_LEVEL=INFO
```

Создание локального файла конфигурации:

```powershell
Copy-Item .env.example .env
```

## Запуск

```powershell
cd C:\Users\рс\Desktop\проэкты\для портфолио\incoming_lids
py -3.12 -m pip install -r requirements.txt
py -3.12 main.py
```

## Запуск в Docker

```powershell
docker build -t support-bot .
docker run --name bot --env-file .env -d support-bot
```

## Важно

- Сессии хранятся в памяти процесса. После перезапуска бота история сбрасывается.
- Если у пользователя есть `@username`, бот использует его как Telegram-контакт по умолчанию.
- Для прода лучше добавить постоянное хранилище, retry/backoff и аудит сообщений.
- Файл `.env` не должен попадать в git. Для публикации используйте только `.env.example`.

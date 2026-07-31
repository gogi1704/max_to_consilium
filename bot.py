from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from maxapi import Bot, Dispatcher
from maxapi.types import (
    BotStarted,
    ButtonsPayload,
    CallbackButton,
    CommandStart,
    LinkButton,
    MessageCallback,
    MessageCreated,
)


LOG = logging.getLogger("consilium_max_bot")
REFRESH_LINK_CALLBACK = "refresh_auth_link"
AUTH_LINK_TEXT = "Ссылка готова. Нажмите кнопку, чтобы войти в «Консилиум»."
REFRESH_PROGRESS_TEXT = "Создаю новую ссылку…"
REFRESH_SUCCESS_TEXT = "Ссылка изменена, попробуйте открыть Консилиум"
REFRESH_ERROR_TEXT = "Не удалось создать новую ссылку. Попробуйте ещё раз."
REFRESH_DELAY_SECONDS = 2.0


class ConfigurationError(RuntimeError):
    pass


class RemoteAPIError(RuntimeError):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load the small subset of dotenv syntax needed by this project."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    max_bot_token: str
    consilium_api_url: str
    bot_integration_secret: str
    request_timeout: int = 15
    healthcheck_file: Path = Path(tempfile.gettempdir()) / "consilium-max-bot.heartbeat"
    healthcheck_max_age: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            "MAX_BOT_TOKEN": os.getenv("MAX_BOT_TOKEN", "").strip(),
            "CONSILIUM_API_URL": os.getenv("CONSILIUM_API_URL", "").strip().rstrip("/"),
            "BOT_INTEGRATION_SECRET": os.getenv(
                "BOT_INTEGRATION_SECRET", ""
            ).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigurationError(
                f"Не заполнены обязательные переменные: {', '.join(missing)}"
            )

        api_url = values["CONSILIUM_API_URL"]
        if not api_url.startswith(("http://", "https://")):
            raise ConfigurationError(
                "CONSILIUM_API_URL должен начинаться с http:// или https://"
            )

        try:
            request_timeout = int(os.getenv("REQUEST_TIMEOUT", "15"))
            healthcheck_max_age = int(os.getenv("HEALTHCHECK_MAX_AGE", "120"))
        except ValueError as exc:
            raise ConfigurationError(
                "REQUEST_TIMEOUT и HEALTHCHECK_MAX_AGE должны быть целыми числами"
            ) from exc
        if not 1 <= request_timeout <= 60:
            raise ConfigurationError("REQUEST_TIMEOUT должен быть от 1 до 60 секунд")
        if healthcheck_max_age < 30:
            raise ConfigurationError("HEALTHCHECK_MAX_AGE должен быть не меньше 30 секунд")

        return cls(
            max_bot_token=values["MAX_BOT_TOKEN"],
            consilium_api_url=api_url,
            bot_integration_secret=values["BOT_INTEGRATION_SECRET"],
            request_timeout=request_timeout,
            healthcheck_file=Path(
                os.getenv(
                    "HEALTHCHECK_FILE",
                    str(Path(tempfile.gettempdir()) / "consilium-max-bot.heartbeat"),
                )
            ),
            healthcheck_max_age=healthcheck_max_age,
        )


def mark_healthy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def healthcheck(settings: Settings) -> bool:
    try:
        age = time.time() - settings.healthcheck_file.stat().st_mtime
    except OSError:
        return False
    return -5 <= age <= settings.healthcheck_max_age


async def heartbeat(settings: Settings) -> None:
    while True:
        mark_healthy(settings.healthcheck_file)
        await asyncio.sleep(10)


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str(body.get("detail") or body.get("description") or "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise RemoteAPIError(detail or f"HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RemoteAPIError("сервис временно недоступен") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RemoteAPIError("сервис вернул некорректный ответ") from exc

    if not isinstance(result, dict):
        raise RemoteAPIError("сервис вернул некорректный ответ")
    return result


class ConsiliumClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def create_auth_link(
        self,
        max_user_id: int,
        intent_token: str = "",
    ) -> str:
        result = await asyncio.to_thread(
            post_json,
            f"{self.settings.consilium_api_url}/api/auth/messenger/link",
            {
                "provider": "max",
                "provider_user_id": str(max_user_id),
                "intent_token": intent_token,
            },
            headers={
                "Authorization": f"Bearer {self.settings.bot_integration_secret}"
            },
            timeout=self.settings.request_timeout,
        )
        auth_url = str(result.get("auth_url", "")).strip()
        if not auth_url.startswith(("http://", "https://")):
            raise RemoteAPIError("сервис не вернул ссылку для входа")
        return auth_url


def auth_attachment(auth_url: str):
    return ButtonsPayload(
        buttons=[
            [
                LinkButton(
                    text="Войти в Консилиум",
                    url=auth_url,
                )
            ],
            [
                CallbackButton(
                    text="Не работает ссылка",
                    payload=REFRESH_LINK_CALLBACK,
                )
            ]
        ]
    ).pack()


def refresh_attachment():
    return ButtonsPayload(
        buttons=[
            [
                CallbackButton(
                    text="Не работает ссылка",
                    payload=REFRESH_LINK_CALLBACK,
                )
            ]
        ]
    ).pack()


async def send_auth_link(
    *,
    bot: Bot,
    chat_id: int,
    max_user_id: int,
    intent_token: str,
    consilium: ConsiliumClient,
) -> None:
    try:
        auth_url = await consilium.create_auth_link(
            max_user_id=max_user_id,
            intent_token=intent_token,
        )
        await bot.send_message(
            chat_id=chat_id,
            text=AUTH_LINK_TEXT,
            attachments=[auth_attachment(auth_url)],
        )
    except RemoteAPIError as exc:
        LOG.warning(
            "Не удалось создать ссылку для MAX user_id=%s: %s",
            max_user_id,
            exc,
        )
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Не удалось создать ссылку для входа. "
                "Попробуйте нажать «Начать» или отправить /start ещё раз."
            ),
        )


async def refresh_auth_link(
    event: MessageCallback,
    consilium: ConsiliumClient,
) -> None:
    if event.callback.payload != REFRESH_LINK_CALLBACK:
        return

    max_user_id = event.callback.user.user_id
    if event.message is None:
        await event.ack(notification=REFRESH_ERROR_TEXT)
        return

    try:
        started_at = asyncio.get_running_loop().time()
        await event.edit(
            text=REFRESH_PROGRESS_TEXT,
            attachments=[],
            notification=REFRESH_PROGRESS_TEXT,
        )
        auth_url = await consilium.create_auth_link(max_user_id=max_user_id)
        remaining_delay = REFRESH_DELAY_SECONDS - (
            asyncio.get_running_loop().time() - started_at
        )
        if remaining_delay > 0:
            await asyncio.sleep(remaining_delay)
        await event.message.edit(
            text=REFRESH_SUCCESS_TEXT,
            attachments=[auth_attachment(auth_url)],
        )
    except RemoteAPIError as exc:
        LOG.warning(
            "Не удалось пересоздать ссылку для MAX user_id=%s: %s",
            max_user_id,
            exc,
        )
        try:
            await event.message.edit(
                text=REFRESH_ERROR_TEXT,
                attachments=[refresh_attachment()],
            )
        except Exception:
            LOG.exception("Не удалось показать ошибку обновления ссылки")


def register_handlers(
    dispatcher: Dispatcher,
    consilium: ConsiliumClient,
) -> None:
    @dispatcher.bot_started()
    async def bot_started(event: BotStarted) -> None:
        await send_auth_link(
            bot=event.bot,
            chat_id=event.chat_id,
            max_user_id=event.user.user_id,
            intent_token=(event.payload or "").strip(),
            consilium=consilium,
        )

    @dispatcher.message_created(CommandStart())
    async def start_command(event: MessageCreated) -> None:
        await send_auth_link(
            bot=event.bot,
            chat_id=event.message.recipient.chat_id,
            max_user_id=event.message.sender.user_id,
            intent_token="",
            consilium=consilium,
        )

    @dispatcher.message_callback()
    async def refresh_link_callback(event: MessageCallback) -> None:
        await refresh_auth_link(event, consilium)


async def run() -> None:
    load_dotenv()
    settings = Settings.from_env()
    bot = Bot(settings.max_bot_token)
    dispatcher = Dispatcher()
    register_handlers(dispatcher, ConsiliumClient(settings))
    await bot.delete_webhook()
    LOG.info("Webhook MAX отключён; бот запущен в режиме long polling")
    heartbeat_task = asyncio.create_task(heartbeat(settings))
    try:
        await dispatcher.start_polling(bot)
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await bot.close_session()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--healthcheck":
            load_dotenv()
            raise SystemExit(0 if healthcheck(Settings.from_env()) else 1)
        if len(sys.argv) != 1:
            raise ConfigurationError("Неизвестные аргументы командной строки")
        asyncio.run(run())
    except (ConfigurationError, ValueError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(2) from exc

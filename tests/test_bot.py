from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import (
    ConfigurationError,
    ConsiliumClient,
    RemoteAPIError,
    Settings,
    REFRESH_LINK_CALLBACK,
    healthcheck,
    mark_healthy,
    refresh_auth_link,
    register_handlers,
    send_auth_link,
)


class SettingsTests(unittest.TestCase):
    def test_settings_are_loaded_and_api_url_is_normalized(self) -> None:
        env = {
            "MAX_BOT_TOKEN": "max-token",
            "CONSILIUM_API_URL": "https://example.test/",
            "BOT_INTEGRATION_SECRET": "shared-secret",
            "REQUEST_TIMEOUT": "7",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.max_bot_token, "max-token")
        self.assertEqual(settings.consilium_api_url, "https://example.test")
        self.assertEqual(settings.request_timeout, 7)

    def test_missing_settings_are_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_healthcheck_uses_fresh_heartbeat_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            heartbeat = Path(temp_dir) / "max-bot.heartbeat"
            settings = Settings(
                "max-token",
                "https://example.test",
                "secret",
                healthcheck_file=heartbeat,
                healthcheck_max_age=60,
            )
            self.assertFalse(healthcheck(settings))
            mark_healthy(heartbeat)
            self.assertTrue(healthcheck(settings))
            old_time = time.time() - 61
            os.utime(heartbeat, (old_time, old_time))
            self.assertFalse(healthcheck(settings))


class ConsiliumClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_auth_link_uses_max_identity_from_event(self) -> None:
        settings = Settings(
            max_bot_token="max-token",
            consilium_api_url="https://example.test",
            bot_integration_secret="shared-secret",
        )
        expected = {"auth_url": "https://example.test/auth/messenger?t=one-time"}

        with patch("bot.post_json", return_value=expected) as post:
            result = await ConsiliumClient(settings).create_auth_link(
                max_user_id=123456789,
                intent_token="bind-token",
            )

        self.assertEqual(result, expected["auth_url"])
        args, kwargs = post.call_args
        self.assertEqual(
            args,
            (
                "https://example.test/api/auth/messenger/link",
                {
                    "provider": "max",
                    "provider_user_id": "123456789",
                    "intent_token": "bind-token",
                },
            ),
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer shared-secret",
        )

    async def test_invalid_auth_url_is_rejected(self) -> None:
        settings = Settings("max-token", "https://example.test", "secret")
        with patch("bot.post_json", return_value={"auth_url": "javascript:x"}):
            with self.assertRaises(RemoteAPIError):
                await ConsiliumClient(settings).create_auth_link(1)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_link_is_sent_as_link_button(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        consilium = SimpleNamespace(
            create_auth_link=AsyncMock(
                return_value="https://example.test/auth/messenger?t=token"
            )
        )

        await send_auth_link(
            bot=bot,
            chat_id=10,
            max_user_id=20,
            intent_token="intent",
            consilium=consilium,
        )

        consilium.create_auth_link.assert_awaited_once_with(
            max_user_id=20,
            intent_token="intent",
        )
        bot.send_message.assert_awaited_once()
        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 10)
        attachment = sent["attachments"][0]
        self.assertEqual(attachment.type, "inline_keyboard")
        button = attachment.payload.buttons[0][0]
        self.assertEqual(button.type.value, "link")
        self.assertEqual(
            button.url,
            "https://example.test/auth/messenger?t=token",
        )
        refresh_button = attachment.payload.buttons[1][0]
        self.assertEqual(refresh_button.type.value, "callback")
        self.assertEqual(refresh_button.payload, REFRESH_LINK_CALLBACK)

    async def test_api_error_returns_friendly_message(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        consilium = SimpleNamespace(
            create_auth_link=AsyncMock(side_effect=RemoteAPIError("offline"))
        )

        await send_auth_link(
            bot=bot,
            chat_id=10,
            max_user_id=20,
            intent_token="",
            consilium=consilium,
        )

        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 10)
        self.assertNotIn("attachments", sent)
        self.assertIn("Не удалось", sent["text"])

    async def test_manager_payload_uses_separate_binding_scenario(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        consilium = SimpleNamespace(
            bind_manager=AsyncMock(return_value={
                "display_name": "Ольга", "manager_url": "https://example.test/manager",
            }),
        )
        await send_auth_link(
            bot=bot, chat_id=10, max_user_id=20,
            intent_token="mgr_secret", consilium=consilium,
        )
        consilium.bind_manager.assert_awaited_once_with(20, 10, "mgr_secret")
        sent = bot.send_message.await_args.kwargs
        self.assertIn("менеджера", sent["text"])
        self.assertEqual(sent["attachments"][0].payload.buttons[0][0].url, "https://example.test/manager")

    async def test_bot_started_payload_is_forwarded_to_consilium(self) -> None:
        class FakeEventHook:
            def __init__(self) -> None:
                self.handler = None

            def __call__(self, *_args):
                def decorator(handler):
                    self.handler = handler
                    return handler

                return decorator

        dispatcher = SimpleNamespace(
            bot_started=FakeEventHook(),
            message_created=FakeEventHook(),
            message_callback=FakeEventHook(),
        )
        consilium = SimpleNamespace(
            create_auth_link=AsyncMock(
                return_value="https://example.test/auth/messenger?t=token"
            )
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        event = SimpleNamespace(
            bot=fake_bot,
            chat_id=10,
            user=SimpleNamespace(user_id=20),
            payload="bind-token",
        )
        register_handlers(dispatcher, consilium)

        await dispatcher.bot_started.handler(event)

        consilium.create_auth_link.assert_awaited_once_with(
            max_user_id=20,
            intent_token="bind-token",
        )

    async def test_refresh_button_replaces_link_in_same_message(self) -> None:
        message = SimpleNamespace(edit=AsyncMock())
        event = SimpleNamespace(
            callback=SimpleNamespace(
                payload=REFRESH_LINK_CALLBACK,
                user=SimpleNamespace(user_id=20),
            ),
            message=message,
            edit=AsyncMock(),
        )
        consilium = SimpleNamespace(
            create_auth_link=AsyncMock(
                return_value="https://example.test/auth/messenger?t=fresh"
            )
        )

        with patch("bot.asyncio.sleep") as sleep:
            await refresh_auth_link(event, consilium)

        event.edit.assert_awaited_once()
        consilium.create_auth_link.assert_awaited_once_with(max_user_id=20)
        message.edit.assert_awaited_once()
        updated = message.edit.await_args.kwargs
        button = updated["attachments"][0].payload.buttons[0][0]
        self.assertEqual(
            button.url,
            "https://example.test/auth/messenger?t=fresh",
        )
        sleep.assert_awaited_once()

    async def test_refresh_failure_keeps_retry_button(self) -> None:
        message = SimpleNamespace(edit=AsyncMock())
        event = SimpleNamespace(
            callback=SimpleNamespace(
                payload=REFRESH_LINK_CALLBACK,
                user=SimpleNamespace(user_id=20),
            ),
            message=message,
            edit=AsyncMock(),
        )
        consilium = SimpleNamespace(
            create_auth_link=AsyncMock(side_effect=RemoteAPIError("offline"))
        )

        await refresh_auth_link(event, consilium)

        updated = message.edit.await_args.kwargs
        retry_button = updated["attachments"][0].payload.buttons[0][0]
        self.assertEqual(retry_button.payload, REFRESH_LINK_CALLBACK)

    def test_docker_deployment_is_isolated_and_polling_disables_webhook(self) -> None:
        project = Path(__file__).resolve().parents[1]
        source = (project / "bot.py").read_text(encoding="utf-8")
        compose = (project / "docker-compose.yml").read_text(encoding="utf-8")
        production_env = (project / ".env.production.example").read_text(encoding="utf-8")
        self.assertIn("await bot.delete_webhook()", source)
        self.assertIn("container_name: consilium-max-bot", compose)
        self.assertIn("external: true", compose)
        self.assertNotIn("ports:", compose)
        self.assertIn("CONSILIUM_API_URL=http://consilium:8000", production_env)


if __name__ == "__main__":
    unittest.main()

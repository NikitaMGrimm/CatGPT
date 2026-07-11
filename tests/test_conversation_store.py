from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "patchright" not in sys.modules and importlib.util.find_spec("patchright.async_api") is None:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    async_api_mod.BrowserContext = object
    async_api_mod.Playwright = object
    async_api_mod.Frame = object
    async_api_mod.Request = object
    async_api_mod.Response = object

    async def _fake_async_playwright():
        return None

    async_api_mod.async_playwright = _fake_async_playwright
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

from fastapi import HTTPException

from src.api import openai_routes
from src.api.conversation_store import ConversationStore
from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionDefinition,
    ResponsesRequest,
    ToolDefinition,
)
from src.config import Config


class _RoutingClient:
    def __init__(self, thread_id: str = "") -> None:
        self.thread_id = thread_id
        self.new_chat_calls = 0
        self.navigate_calls: list[str] = []
        self.fail_navigation = False

    def _extract_thread_id(self) -> str:
        return self.thread_id

    async def new_chat(self) -> None:
        self.new_chat_calls += 1
        self.thread_id = ""

    async def navigate_to_thread(self, thread_id: str) -> None:
        self.navigate_calls.append(thread_id)
        if self.fail_navigation:
            raise RuntimeError("stale")
        self.thread_id = thread_id


class ConversationStoreTests(unittest.TestCase):
    def test_route_and_response_snapshots_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conversations.sqlite3"
            store = ConversationStore(path)
            route = store.save_route(
                project_key="project",
                app_key="app",
                conversation_key="conversation",
                thread_id="thread-1",
                transcript=[{"role": "user", "content": "hello"}],
                message_hashes=["hash-1"],
                contract_hash="contract",
            )
            store.save_response("resp_1", route)

            reopened = ConversationStore(path)
            loaded = reopened.get_route("project", "app", "conversation")
            response = reopened.get_response("resp_1")
            self.assertEqual(loaded.thread_id, "thread-1")
            self.assertEqual(loaded.revision, 1)
            self.assertEqual(response.transcript[0]["content"], "hello")
            self.assertEqual(response.message_hashes, ("hash-1",))


class ConversationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "routes.sqlite3"
        self.config_patch = patch.object(Config, "API_CONVERSATION_DB", self.db_path)
        self.project_patch = patch.object(Config, "CHATGPT_PROJECT_URL", "")
        self.config_patch.start()
        self.project_patch.start()
        openai_routes._conversation_store = None
        openai_routes._conversation_store_path = ""

    async def asyncTearDown(self) -> None:
        self.config_patch.stop()
        self.project_patch.stop()
        openai_routes._conversation_store = None
        openai_routes._conversation_store_path = ""
        self.tmp.cleanup()

    async def test_verified_full_history_forwards_only_delta(self) -> None:
        client = _RoutingClient()
        first = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="one")],
        )
        initial = await openai_routes._prepare_conversation_routing(
            client,
            first,
            app_key="app",
            conversation_key="conv",
        )
        assistant = {"role": "assistant", "content": "answer"}
        transcript = [*initial.transcript_input, assistant]
        store = openai_routes._get_conversation_store()
        store.save_route(
            project_key=initial.project_key,
            app_key=initial.app_key,
            conversation_key=initial.conversation_key,
            thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )

        full = ChatCompletionRequest(
            messages=[
                ChatMessage(role="user", content="one"),
                ChatMessage(role="assistant", content="answer"),
                ChatMessage(role="user", content="two"),
            ],
        )
        routing = await openai_routes._prepare_conversation_routing(
            client,
            full,
            app_key="app",
            conversation_key="conv",
        )
        self.assertEqual(routing.action, "verified-prefix-delta")
        self.assertEqual([message.content for message in routing.messages_for_browser], ["two"])
        self.assertEqual(client.navigate_calls, ["thread-1"])

    async def test_diverged_history_starts_fresh_thread(self) -> None:
        client = _RoutingClient("thread-1")
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="old")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="conv"
        )
        transcript = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key,
            app_key=initial.app_key,
            conversation_key=initial.conversation_key,
            thread_id="thread-1",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        diverged = ChatCompletionRequest(
            messages=[
                ChatMessage(role="user", content="different"),
                ChatMessage(role="assistant", content="history"),
                ChatMessage(role="user", content="new"),
            ]
        )
        routing = await openai_routes._prepare_conversation_routing(
            client, diverged, app_key="app", conversation_key="conv"
        )
        self.assertEqual(routing.action, "new-chat-history-diverged")
        self.assertGreaterEqual(client.new_chat_calls, 1)

    async def test_stale_mapping_is_removed_and_rebuilt_from_ledger(self) -> None:
        client = _RoutingClient()
        request = ChatCompletionRequest(messages=[ChatMessage(role="user", content="one")])
        initial = await openai_routes._prepare_conversation_routing(
            client, request, app_key="app", conversation_key="conv"
        )
        transcript = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "answer"}]
        openai_routes._get_conversation_store().save_route(
            project_key=initial.project_key,
            app_key=initial.app_key,
            conversation_key=initial.conversation_key,
            thread_id="missing-thread",
            transcript=transcript,
            message_hashes=[openai_routes._message_hash(item) for item in transcript],
            contract_hash=initial.contract_hash,
        )
        client.fail_navigation = True
        continuation = ChatCompletionRequest(messages=[ChatMessage(role="user", content="two")])
        routing = await openai_routes._prepare_conversation_routing(
            client, continuation, app_key="app", conversation_key="conv"
        )
        self.assertEqual(routing.action, "new-chat-stale-mapping")
        self.assertEqual(len(routing.messages_for_browser), 3)
        self.assertIsNone(
            openai_routes._get_conversation_store().get_route("global", "app", "conv")
        )

    async def test_tool_round_trip_and_delta_turns_do_not_duplicate_history(self) -> None:
        client = _RoutingClient()
        tools = [
            ToolDefinition(
                function=FunctionDefinition(
                    name="add_numbers",
                    parameters={"type": "object"},
                )
            )
        ]
        first_user = ChatMessage(role="user", content="Use the tool and remember marker-1")
        first_request = ChatCompletionRequest(messages=[first_user], tools=tools)
        first = await openai_routes._prepare_conversation_routing(
            client, first_request, app_key="app", conversation_key="conv"
        )
        assistant_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add_numbers", "arguments": '{"a":19,"b":23}'},
                }
            ],
        }
        first_transcript = [*first.transcript_input, assistant_call]
        store = openai_routes._get_conversation_store()
        route = store.save_route(
            project_key=first.project_key,
            app_key=first.app_key,
            conversation_key=first.conversation_key,
            thread_id="thread-1",
            transcript=first_transcript,
            message_hashes=[openai_routes._message_hash(item) for item in first_transcript],
            contract_hash=first.contract_hash,
        )

        tool_result = ChatMessage(role="tool", tool_call_id="call_1", content="42")
        tool_request = ChatCompletionRequest(
            messages=[first_user, ChatMessage(**assistant_call), tool_result],
            tools=tools,
        )
        second = await openai_routes._prepare_conversation_routing(
            client, tool_request, app_key="app", conversation_key="conv"
        )
        self.assertEqual(second.action, "verified-prefix-delta")
        self.assertEqual([message.role for message in second.messages_for_browser], ["tool"])
        second_transcript = [
            *second.transcript_input,
            {"role": "assistant", "content": "marker-1 and 42"},
        ]
        route = store.save_route(
            project_key=route.project_key,
            app_key=route.app_key,
            conversation_key=route.conversation_key,
            thread_id=route.thread_id,
            transcript=second_transcript,
            message_hashes=[openai_routes._message_hash(item) for item in second_transcript],
            contract_hash=second.contract_hash,
        )

        follow_up = ChatMessage(role="user", content="What marker?")
        delta_request = ChatCompletionRequest(messages=[follow_up], tools=tools)
        third = await openai_routes._prepare_conversation_routing(
            client, delta_request, app_key="app", conversation_key="conv"
        )
        self.assertEqual(third.action, "single-turn-delta")
        self.assertEqual([message.content for message in third.messages_for_browser], ["What marker?"])
        self.assertEqual(
            [message.get("role") for message in third.transcript_input],
            ["user", "assistant", "tool", "assistant", "user"],
        )
        self.assertEqual(
            sum(
                1
                for message in third.transcript_input
                if message == {"role": "user", "content": "What marker?"}
            ),
            1,
        )

    def test_responses_state_fields_are_mutually_exclusive(self) -> None:
        request = ResponsesRequest(
            input="next",
            conversation="conv_1",
            previous_response_id="resp_1",
        )
        with self.assertRaises(HTTPException) as raised:
            openai_routes._validate_responses_request(request)
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

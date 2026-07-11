#!/usr/bin/env python3
"""Exercise one durable Chat Completions route across a tool round trip.

This opt-in live check creates one real ChatGPT thread and verifies four API
responses plus the persisted transcript.

Usage:
    uv run python scripts/manual/sticky_conversation_smoke.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config


TOOL = {
    "type": "function",
    "function": {
        "name": "add_numbers",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
}
TOOL_CHOICE = {"type": "function", "function": {"name": "add_numbers"}}


def _post(
    *,
    endpoint: str,
    token: str,
    model: str,
    conversation_id: str,
    messages: list[dict],
    timeout: float,
) -> dict:
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "conversation_id": conversation_id,
            "messages": messages,
            "tools": [TOOL],
            "tool_choice": TOOL_CHOICE,
        },
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(f"API returned {response.status_code}: {response.text}")
    return response.json()


def _choice(response: dict) -> dict:
    choices = response.get("choices") or []
    if len(choices) != 1:
        raise AssertionError(f"Expected one choice, received {len(choices)}")
    return choices[0]


def _route_snapshot(db_path: Path, conversation_id: str) -> dict | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT project_key, app_key, conversation_key, thread_id,
                   transcript_json, revision
            FROM conversation_routes
            WHERE conversation_key = ?
            """,
            (conversation_id,),
        ).fetchall()
    if len(rows) != 1:
        raise AssertionError(
            f"Expected one persisted route for {conversation_id!r}, found {len(rows)}"
        )
    row = rows[0]
    return {
        "project_key": row["project_key"],
        "app_key": row["app_key"],
        "conversation_key": row["conversation_key"],
        "thread_id": row["thread_id"],
        "transcript": json.loads(row["transcript_json"]),
        "revision": int(row["revision"]),
    }


def _assistant_message(choice: dict) -> dict:
    message = choice.get("message") or {}
    result = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        result["tool_calls"] = message["tool_calls"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000/sticky-smoke/v1",
        help="OpenAI-compatible base URL, optionally including an app namespace",
    )
    parser.add_argument("--api-key", default=Config.API_TOKEN or "dummy123")
    parser.add_argument("--model", default="catgpt-browser")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--db-path", type=Path, default=Config.API_CONVERSATION_DB)
    args = parser.parse_args()

    endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
    marker = f"STICKY-{uuid.uuid4().hex[:8].upper()}"
    conversation_id = f"live-smoke:{uuid.uuid4().hex}"
    first_user = {
        "role": "user",
        "content": (
            f"Remember the exact marker {marker}. Use add_numbers exactly once "
            "with a=19 and b=23. After I provide the tool result, report both "
            "the marker and sum."
        ),
    }

    print(f"Conversation: {conversation_id}")
    print(f"Marker: {marker}")

    first = _post(
        endpoint=endpoint,
        token=args.api_key,
        model=args.model,
        conversation_id=conversation_id,
        messages=[first_user],
        timeout=args.timeout,
    )
    first_choice = _choice(first)
    calls = (first_choice.get("message") or {}).get("tool_calls") or []
    if first_choice.get("finish_reason") != "tool_calls" or len(calls) != 1:
        raise AssertionError(f"First response did not contain exactly one tool call: {first_choice}")
    call = calls[0]
    if (call.get("function") or {}).get("name") != "add_numbers":
        raise AssertionError(f"Unexpected tool call: {call}")
    arguments = json.loads(call["function"]["arguments"])
    if arguments != {"a": 19, "b": 23}:
        raise AssertionError(f"Unexpected add_numbers arguments: {arguments}")
    print("1/4 tool call: add_numbers(a=19, b=23)")

    full_history = [
        first_user,
        _assistant_message(first_choice),
        {"role": "tool", "tool_call_id": call["id"], "content": "42"},
    ]
    second = _post(
        endpoint=endpoint,
        token=args.api_key,
        model=args.model,
        conversation_id=conversation_id,
        messages=full_history,
        timeout=args.timeout,
    )
    second_text = ((_choice(second).get("message") or {}).get("content") or "").strip()
    if marker.lower() not in second_text.lower() or "42" not in second_text:
        raise AssertionError(f"Tool-result response lost context: {second_text!r}")
    print(f"2/4 tool result: {second_text}")

    third_user = {"role": "user", "content": "What exact marker are you remembering? Reply with only the marker."}
    third = _post(
        endpoint=endpoint,
        token=args.api_key,
        model=args.model,
        conversation_id=conversation_id,
        messages=[third_user],
        timeout=args.timeout,
    )
    third_text = ((_choice(third).get("message") or {}).get("content") or "").strip()
    if marker.lower() not in third_text.lower():
        raise AssertionError(f"Third response forgot the marker: {third_text!r}")
    print(f"3/4 marker recall: {third_text}")

    fourth_user = {"role": "user", "content": "What sum did the tool return earlier? Reply with only the integer."}
    fourth = _post(
        endpoint=endpoint,
        token=args.api_key,
        model=args.model,
        conversation_id=conversation_id,
        messages=[fourth_user],
        timeout=args.timeout,
    )
    fourth_text = ((_choice(fourth).get("message") or {}).get("content") or "").strip()
    if "42" not in fourth_text:
        raise AssertionError(f"Fourth response forgot the tool result: {fourth_text!r}")
    print(f"4/4 result recall: {fourth_text}")

    route = _route_snapshot(args.db_path.resolve(), conversation_id)
    if route is not None:
        transcript = route["transcript"]
        expected_roles = [
            "user", "assistant", "tool", "assistant", "user", "assistant", "user", "assistant"
        ]
        roles = [message.get("role") for message in transcript]
        if roles != expected_roles:
            raise AssertionError(f"Unexpected transcript roles (possible duplication): {roles}")
        for user_message in (first_user, third_user, fourth_user):
            matches = sum(1 for message in transcript if message == user_message)
            if matches != 1:
                raise AssertionError(
                    f"Expected one copy of user message {user_message['content']!r}, found {matches}"
                )
        if route["revision"] != 4:
            raise AssertionError(f"Expected route revision 4, found {route['revision']}")
        print(
            "Ledger: one thread, four revisions, eight canonical messages, no duplicates "
            f"(thread {route['thread_id']})"
        )
    else:
        print(f"Ledger check skipped because {args.db_path} is not accessible")

    print("Sticky conversation smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

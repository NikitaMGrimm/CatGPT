from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "patchright" not in sys.modules and importlib.util.find_spec("patchright.async_api") is None:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

from src.chatgpt.client import ChatGPTClient
from src.config import Config

try:
    from patchright.async_api import async_playwright
except ImportError:
    async_playwright = None


async def _noop_sleep(*_args, **_kwargs) -> None:
    return None


async def _launch_browser(playwright):
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    options = {"headless": True}
    if chrome.exists():
        options["executable_path"] = str(chrome)
    return await playwright.chromium.launch(**options)


async def _set_picker_dom(page, selected_model: str = "GPT-5.6 Sol") -> None:
    await page.set_content(
        """
        <main><button id="picker" class="__composer-pill" aria-haspopup="menu"
          aria-expanded="false" data-state="closed">High</button></main>
        """
    )
    await page.evaluate(
        """
        (selectedModel) => {
          const models = ["GPT-5.6 Sol", "GPT-5.5", "GPT-5.4", "GPT-5.3", "o3"];
          const efforts = {
            "GPT-5.6 Sol": ["Instant 5.5", "Medium", "High"],
            "GPT-5.5": ["Instant", "Medium", "High"],
            "GPT-5.4": ["Instant", "Medium", "High"],
            "GPT-5.3": ["Instant", "Medium", "High"],
            "o3": ["Medium"],
          };
          window.selectedModel = selectedModel;
          window.selectedEffort = Object.fromEntries(models.map((m) => [m, m === "o3" ? "Medium" : "High"]));
          window.submenuClicks = 0;
          window.modelClicks = 0;
          window.reasoningClicks = 0;
          const picker = document.querySelector("#picker");
          const close = () => {
            document.querySelectorAll("[role=menu]").forEach((el) => el.remove());
            picker.setAttribute("aria-expanded", "false");
            picker.dataset.state = "closed";
          };
          const row = (label, checked, onClick, extra = "") => {
            const el = document.createElement("div");
            el.setAttribute("role", "menuitemradio");
            el.setAttribute("aria-checked", String(checked));
            el.dataset.state = checked ? "checked" : "unchecked";
            el.innerHTML = `<div class="truncate">${label}</div>${extra}`;
            el.addEventListener("click", onClick);
            return el;
          };
          const openSubmenu = (trigger) => {
            window.submenuClicks++;
            trigger.dataset.state = "open";
            trigger.setAttribute("aria-expanded", "true");
            const menu = document.createElement("div");
            menu.setAttribute("role", "menu");
            menu.setAttribute("aria-labelledby", trigger.id);
            menu.dataset.state = "open";
            for (const model of models) {
              menu.appendChild(row(model, model === window.selectedModel, () => {
                window.modelClicks++;
                window.selectedModel = model;
                picker.textContent = window.selectedEffort[model];
                close();
              }, model === "GPT-5.4" ? "<div>Leaving later</div>" : ""));
            }
            document.body.appendChild(menu);
          };
          picker.addEventListener("click", () => {
            close();
            picker.setAttribute("aria-expanded", "true");
            picker.dataset.state = "open";
            const menu = document.createElement("div");
            menu.setAttribute("role", "menu");
            menu.dataset.state = "open";
            const heading = document.createElement("div");
            heading.textContent = "Intelligence";
            menu.appendChild(heading);
            for (const effort of efforts[window.selectedModel]) {
              menu.appendChild(row(effort, effort === window.selectedEffort[window.selectedModel], () => {
                window.reasoningClicks++;
                window.selectedEffort[window.selectedModel] = effort;
                picker.textContent = effort;
                close();
              }));
            }
            const trigger = document.createElement("div");
            trigger.id = "concrete-trigger";
            trigger.setAttribute("role", "menuitem");
            trigger.setAttribute("aria-haspopup", "menu");
            trigger.setAttribute("data-has-submenu", "");
            trigger.setAttribute("aria-expanded", "false");
            trigger.dataset.state = "closed";
            trigger.innerHTML = `<span class="truncate">${window.selectedModel}</span>`;
            trigger.addEventListener("click", () => openSubmenu(trigger));
            menu.appendChild(trigger);
            document.body.appendChild(menu);
          });
        }
        """,
        selected_model,
    )


@unittest.skipIf(async_playwright is None, "patchright is not installed")
class ChatGPTClientModelSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright_context = async_playwright()
        if self.playwright_context is None:
            self.skipTest("patchright is not installed")
        self.playwright = await self.playwright_context.__aenter__()
        self.browser = await _launch_browser(self.playwright)
        self.page = await (await self.browser.new_context()).new_page()

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.playwright_context.__aexit__(None, None, None)

    async def test_opening_submenu_is_not_model_selection(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            self.assertTrue(await client._open_model_picker())
            state = await client._open_concrete_model_submenu()
        self.assertTrue(state["submenuOpen"])
        self.assertEqual(await self.page.evaluate("window.submenuClicks"), 1)
        self.assertEqual(await self.page.evaluate("window.modelClicks"), 0)

    async def test_selects_and_confirms_concrete_model(self) -> None:
        await _set_picker_dom(self.page, "GPT-5.4")
        client = ChatGPTClient(self.page)
        with patch.object(Config, "CHATGPT_MODEL_ALIASES", ""), patch(
            "src.chatgpt.client.asyncio.sleep", _noop_sleep
        ):
            await client.ensure_model("gpt-5.6-sol")
        self.assertEqual(await self.page.evaluate("window.selectedModel"), "GPT-5.6 Sol")
        self.assertEqual(client._last_concrete_model_label, "GPT-5.6 Sol")

    async def test_secondary_row_text_does_not_break_model_match(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("gpt-5.4")
        self.assertEqual(await self.page.evaluate("window.selectedModel"), "GPT-5.4")

    async def test_instant_substring_alias_selects_instant_55(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("gpt-5.6-sol", reasoning_effort="instant")
        self.assertEqual(
            await self.page.evaluate("window.selectedEffort['GPT-5.6 Sol']"),
            "Instant 5.5",
        )
        self.assertEqual(client._last_reasoning_effort, "low")

    async def test_o3_clamps_every_effort_to_only_visible_medium(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("o3", reasoning_effort="max")
        self.assertEqual(await self.page.evaluate("window.selectedModel"), "o3")
        self.assertEqual(await self.page.evaluate("window.selectedEffort.o3"), "Medium")
        self.assertEqual(client._last_reasoning_effort, "medium")

    async def test_model_suffix_overrides_explicit_reasoning(self) -> None:
        await _set_picker_dom(self.page, "GPT-5.4")
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            await client.ensure_model("gpt-5.6-sol-high", reasoning_effort="low")
        self.assertEqual(await self.page.evaluate("window.selectedModel"), "GPT-5.6 Sol")
        self.assertEqual(await self.page.evaluate("window.selectedEffort['GPT-5.6 Sol']"), "High")

    async def test_unavailable_model_raises_in_strict_mode(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch.object(Config, "CHATGPT_MODEL_SWITCH_STRICT", True), patch(
            "src.chatgpt.client.asyncio.sleep", _noop_sleep
        ):
            with self.assertRaisesRegex(RuntimeError, "concrete-model submenu"):
                await client.ensure_model("gpt-9.9")

    async def test_explicit_model_failure_is_closed_even_without_strict_mode(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch.object(Config, "CHATGPT_MODEL_SWITCH_STRICT", False), patch(
            "src.chatgpt.client.asyncio.sleep", _noop_sleep
        ):
            with self.assertRaisesRegex(RuntimeError, "concrete-model submenu"):
                await client.ensure_model("gpt-9.9")

    async def test_checked_row_accepts_one_authoritative_signal(self) -> None:
        await _set_picker_dom(self.page)
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep):
            self.assertTrue(await client._open_model_picker())
            await self.page.evaluate(
                """
                () => document.querySelectorAll('[role=menuitemradio][aria-checked=true]')
                    .forEach((row) => row.removeAttribute('data-state'))
                """
            )
            state = await client._nested_model_picker_state()
        self.assertTrue(any(row["checked"] for row in state["reasoning"]))

    async def test_discovery_restores_initial_picker_state(self) -> None:
        await _set_picker_dom(self.page, "GPT-5.4")
        client = ChatGPTClient(self.page)
        with patch("src.chatgpt.client.asyncio.sleep", _noop_sleep), patch.object(
            Config, "CHATGPT_MODEL_DISCOVERY_TTL_SECONDS", 600
        ):
            labels = await client.discover_available_models(force=True)
        self.assertIn("GPT-5.6 Sol", labels)
        self.assertEqual(await self.page.evaluate("window.selectedModel"), "GPT-5.4")
        self.assertEqual(await self.page.evaluate("window.selectedEffort['GPT-5.4']"), "High")


if __name__ == "__main__":
    unittest.main()

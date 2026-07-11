"""
ChatGPT client — core interaction logic.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.chatgpt.model_registry import (
    choose_reasoning_label,
    get_discovered_catalog,
    model_label_to_public_id,
    normalize_model_token,
    register_discovered_models,
    register_discovered_reasoning,
    replace_discovered_catalog,
    resolve_model_request,
)
from src.config import Config
from src.selectors import Selectors
from src.browser.human import human_type, human_click, random_delay
from src.chatgpt.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    get_latest_user_turn_signature,
    is_incomplete_response_text,
    capture_response_diagnostics,
    _check_page_error,
)
from src.chatgpt.image_handler import extract_images_from_response
from src.chatgpt.audio_handler import generate_read_aloud_audio
from src.chatgpt.models import ChatResponse
from src.log import setup_logging

log = setup_logging("chatgpt_client")


class ModelSelectionError(RuntimeError):
    """Raised when an explicitly requested picker state cannot be confirmed."""


class ChatGPTClient:
    """
    High-level client for interacting with the ChatGPT web interface.

    Requires a Playwright Page that is already logged in and on chatgpt.com.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._last_model_label = ""
        self._last_concrete_model_label = ""
        self._last_reasoning_effort = ""
        self._discovered_model_labels: list[str] = []
        self._model_capabilities_complete = False
        self._model_capabilities_checked_at = 0.0
        self._recent_backend_events: list[dict] = []
        self._wire_backend_event_logger()

    @property
    def page(self) -> Page:
        return self._page

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        read_aloud: bool = False,
    ) -> ChatResponse:
        """
        Send a message to ChatGPT and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files (PDF, etc.).
            read_aloud: If True, trigger ChatGPT's "Read aloud" action and save audio.

        Steps:
        1. Simulate thinking pause
        2. Upload images if provided
        3. Find and focus chat input
        4. Type message with human-like delays
        5. Click send
        6. Wait for response to complete
        7. Extract and return the response

        Returns ChatResponse with the assistant's reply and metadata.
        """
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info(f"Sending message ({len(text)} chars, {len(all_attachments)} attachments): {text[:80]}...")
        start_time = time.time()

        # 0. Check page health — recover from DNS errors before trying to send
        page_error = await self._detect_page_error()
        if page_error:
            log.warning(f"Page error detected before send: {page_error}")
            raise RuntimeError(f"Page is in error state: {page_error}")

        # 0.5 Count existing assistant messages so we know when a new one appears
        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        pre_user_signature = await get_latest_user_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")
        log.debug(f"Latest user turn before send: {pre_user_signature}")

        # 1. Switch model if requested before interacting with the composer
        if model or reasoning_effort:
            await self.ensure_model(model or "catgpt-browser", reasoning_effort=reasoning_effort)

        # 2. Brief pause (human would take a moment to start typing)
        await random_delay(250, 700)

        # 2.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments)

        # 2. Find the chat input (retry once after dismissing overlays if not found)
        input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            # An overlay may have blocked it — dismiss and retry
            log.info("Chat input not found on first try, dismissing overlays and retrying...")
            await self._dismiss_overlays()
            await asyncio.sleep(1)
            input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message (all at once)
        await human_type(self._page, input_selector, text)

        # Small pause after pasting (like a human reviewing before send)
        await random_delay(300, 600)

        auto_submitted = False
        sent = False
        if auto_submitted:
            log.info("ChatGPT auto-submitted after text entry — skipping send button click")
        else:
            # No auto-submit — click the send button
            log.info("No auto-submit detected, clicking send button")
            sent = await self._click_send()
            if not sent:
                log.info("Send button not found, trying Enter key")
                await self._page.keyboard.press("Enter")

        submitted = await self._wait_for_message_submission(pre_user_signature, text)
        if not submitted:
            diagnostic_path = await capture_response_diagnostics(
                self._page,
                "message-not-submitted",
                previous_turn_signature=pre_turn_signature,
                extra={
                    "recent_backend_events": self._backend_events_snapshot(),
                    "prompt_length": len(text),
                    "sent_by_button": sent,
                },
            )
            detail = "Message was not submitted to ChatGPT"
            if diagnostic_path:
                detail = f"{detail}; diagnostic={diagnostic_path}"
            raise RuntimeError(detail)

        # 5. Wait for response with message count awareness
        log.info("Waiting for ChatGPT response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            log.warning("Response may not be complete (timeout)")
            await capture_response_diagnostics(
                self._page,
                "response-timeout",
                previous_turn_signature=pre_turn_signature,
                extra={
                    "recent_backend_events": self._backend_events_snapshot(),
                    "prompt_length": len(text),
                    "expected_assistant_count": expected_count,
                },
            )

        # Small buffer after completion to let DOM settle
        await asyncio.sleep(1.0)

        # 6. Check for generated images in the response FIRST
        #    (image turns have no copy button, so we must detect images
        #    before trying copy-button extraction)
        images = await extract_images_from_response(
            self._page,
            previous_turn_signature=pre_turn_signature,
        )
        has_images = len(images) > 0

        # 7. Extract text content
        if has_images:
            # Image responses don't have a copy button — extract text
            # from the turn's DOM instead (will get the image title/desc)
            response_text = await self._extract_image_turn_text(pre_turn_signature)
            log.info(f"Response contains {len(images)} generated image(s)")
            for img in images:
                log.info(f"  Image: {img.alt or img.prompt_title} → {img.local_path}")
        else:
            # Standard text response — use copy button (most reliable)
            response_text = await extract_last_response_via_copy(
                self._page,
                previous_turn_signature=pre_turn_signature,
            )

            # ChatGPT can briefly expose status text like "thinking" as a turn.
            # Retry against the same new turn before giving that transient text back.
            if is_incomplete_response_text(response_text):
                log.warning("Extracted text looks incomplete/transient; retrying for final answer")
                for attempt in range(1, 3):
                    await asyncio.sleep(2)
                    await wait_for_response_complete(
                        self._page,
                        timeout_ms=90000,
                        previous_turn_signature=pre_turn_signature,
                    )
                    retry_text = await extract_last_response_via_copy(
                        self._page,
                        previous_turn_signature=pre_turn_signature,
                    )

                    if retry_text and not is_incomplete_response_text(retry_text):
                        response_text = retry_text
                        log.info(f"Recovered final response text on retry {attempt}")
                        break

                    if retry_text:
                        response_text = retry_text
                    log.warning(f"Retry {attempt} still incomplete/transient")

            if not response_text or is_incomplete_response_text(response_text):
                await capture_response_diagnostics(
                    self._page,
                    "empty-or-incomplete-response",
                    previous_turn_signature=pre_turn_signature,
                    extra={
                        "recent_backend_events": self._backend_events_snapshot(),
                        "prompt_length": len(text),
                        "has_images": has_images,
                    },
                )

        elapsed_ms = int((time.time() - start_time) * 1000)
        thread_id = self._extract_thread_id()
        audio = None

        if read_aloud and response_text:
            audio = await generate_read_aloud_audio(
                self._page,
                previous_turn_signature=pre_turn_signature,
            )

        log.info(
            f"Response received ({elapsed_ms}ms, {len(response_text)} chars"
            f"{f', {len(images)} images' if has_images else ''}"
            f"{', audio' if audio else ''}): "
            f"{response_text[:80]}..."
        )

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            response_time_ms=elapsed_ms,
            images=images,
            has_images=has_images,
            audio=audio,
            has_audio=audio is not None,
        )

    async def generate_image(
        self,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        quality: str = "standard",
        style: str = "vivid",
    ) -> ChatResponse:
        """Generate images through the ChatGPT web UI and download results."""
        count = max(1, min(int(n or 1), 4))
        prompt_parts = [
            f"Generate exactly {count} image{'s' if count != 1 else ''} from this prompt:",
            prompt.strip(),
        ]
        if size:
            prompt_parts.append(f"Requested size/aspect: {size}.")
        if quality:
            prompt_parts.append(f"Requested quality: {quality}.")
        if style:
            prompt_parts.append(f"Requested style: {style}.")
        prompt_parts.append("Return the generated image result, not just a text description.")

        return await self.send_message("\n\n".join(part for part in prompt_parts if part.strip()))

    async def ensure_model(
        self,
        requested_model: str,
        reasoning_effort: str | None = None,
    ) -> None:
        """Select a concrete model and its closest available reasoning effort."""
        resolved = resolve_model_request(requested_model, reasoning_effort)
        target = resolved.model
        effort = resolved.reasoning_effort

        if target is not None:
            await self._dismiss_model_picker()
            if not await self._open_model_picker():
                raise ModelSelectionError(
                    f"Could not open ChatGPT model picker for '{target.ui_label}'"
                )

            selected = await self._select_model_from_nested_picker(target.ui_labels)
            if selected is not True:
                await self._dismiss_model_picker()
                raise ModelSelectionError(
                    f"Could not find ChatGPT model option '{target.ui_label}' "
                    "in the concrete-model submenu"
                )

            self._last_model_label = self._last_concrete_model_label or target.ui_label
            log.info("Model switched to %s", self._last_model_label)

        if effort:
            selected_effort = await self._select_reasoning_effort(effort)
            if not selected_effort:
                raise ModelSelectionError(
                    f"Could not select a reasoning effort for '{requested_model}'"
                )
            self._last_reasoning_effort = selected_effort
            if resolved.reasoning_from_model_id and reasoning_effort:
                log.info(
                    "Model-id reasoning suffix '%s' overrides explicit reasoning_effort=%r",
                    selected_effort,
                    reasoning_effort,
                )
            log.info("Reasoning effort resolved to %s", selected_effort)

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation.

        Strategy order:
        1. SPA button click (avoids DNS issues, preserves browser state)
        2. JavaScript location change (no DNS lookup needed if page is loaded)
        3. Full page.goto() (last resort - may fail with DNS errors)
        """
        log.info("Starting new chat...")
        project_url = Config.chatgpt_project_url()
        if project_url:
            current_url = (self._page.url or "").split("?", 1)[0].rstrip("/")
            if current_url == project_url:
                try:
                    turn_count = await self._page.evaluate(
                        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                    )
                    if turn_count == 0:
                        if not await self._wait_for_chat_input():
                            raise RuntimeError("Configured ChatGPT project composer is not ready")
                        log.info("Already on a fresh chat in the configured project")
                        return
                except Exception:
                    pass

            # The project root is itself the project's fresh-chat composer.
            # Never fall back to the global New Chat button when scoping is set.
            log.info("Starting new chat in configured ChatGPT project")
            await self._page.goto(project_url, wait_until="domcontentloaded", timeout=30000)
            page_error = await self._detect_page_error()
            if page_error:
                raise RuntimeError(f"Could not open configured ChatGPT project: {page_error}")
            if not await self._wait_for_chat_input():
                raise RuntimeError("Configured ChatGPT project composer is not ready")
            return

        # Already on a fresh chat — nothing to do
        if "chatgpt.com" in self._page.url:
            try:
                turn_count = await self._page.evaluate(
                    "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                )
                if turn_count == 0:
                    log.info("Already on a fresh chat — skipping navigation")
                    return
            except Exception:
                pass

        # Strategy 1: SPA button click
        for selector in Selectors.NEW_CHAT_BUTTON:
            try:
                btn = await self._page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"New chat via SPA button: {selector}")
                    await asyncio.sleep(1)
                    # Verify we're on a fresh chat
                    try:
                        turn_count = await self._page.evaluate(
                            "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                        )
                        if turn_count == 0:
                            await self._wait_for_chat_input()
                            return
                    except Exception:
                        pass
            except Exception:
                continue

        # Strategy 2: JavaScript navigation (avoids DNS lookup)
        try:
            log.info("New chat via JS navigation...")
            await self._page.evaluate("window.location.href = '/'")
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            page_error = await self._detect_page_error()
            if not page_error:
                log.info("New chat started (JS navigation)")
                await self._wait_for_chat_input()
                return
        except Exception as e:
            log.warning(f"JS navigation failed: {e}")

        # Strategy 3: Full page.goto() — last resort
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            log.info(f"New chat via page.goto (attempt {attempt}/{max_attempts})...")
            try:
                await self._page.goto(
                    Config.CHATGPT_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                log.warning(f"page.goto failed (attempt {attempt}): {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise

            page_error = await self._detect_page_error()
            if page_error:
                log.error(f"Page error after goto (attempt {attempt}): {page_error}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise RuntimeError(f"Page error persists after {max_attempts} attempts: {page_error}")

            log.info("New chat started (page.goto)")
            await self._wait_for_chat_input()
            return

    async def _wait_for_chat_input(self) -> bool:
        """Wait for the chat input to become visible and interactive."""
        for selector in Selectors.CHAT_INPUT:
            try:
                await self._page.wait_for_selector(selector, timeout=10000, state="visible")
                log.debug(f"Chat input ready: {selector}")
                # Brief settle for React handlers to attach
                await asyncio.sleep(0.5)
                return True
            except Exception:
                continue
        log.warning("Chat input not found — page may not be fully ready")
        return False

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation and verify the requested scope."""
        if not re.fullmatch(r"[A-Za-z0-9-]+", (thread_id or "").strip()):
            raise RuntimeError(f"Invalid ChatGPT thread id: {thread_id!r}")
        project_url = Config.chatgpt_project_url()
        if project_url:
            project_base = project_url[: -len("/project")]
            url = f"{project_base}/c/{thread_id}"
        else:
            url = f"{Config.CHATGPT_URL.rstrip('/')}/c/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await random_delay(500, 1000)
        page_error = await self._detect_page_error()
        if page_error:
            raise RuntimeError(f"Could not open ChatGPT thread {thread_id}: {page_error}")
        current_url = (self._page.url or "").split("?", 1)[0].rstrip("/")
        if self._extract_thread_id() != thread_id:
            raise RuntimeError(
                f"ChatGPT did not load requested thread {thread_id}; current URL is {current_url}"
            )
        if project_url:
            expected_prefix = project_url[: -len("/project")] + "/c/"
            if not current_url.startswith(expected_prefix):
                raise RuntimeError(
                    f"Thread {thread_id} is not available in configured ChatGPT project"
                )
        if not await self._wait_for_chat_input():
            raise RuntimeError(f"ChatGPT composer was not ready in thread {thread_id}")
        log.info(f"Thread {thread_id} loaded")

    async def get_current_thread_url(self) -> str:
        """Get the current page URL (contains thread ID if in a conversation)."""
        return self._page.url

    # ── Sidebar ─────────────────────────────────────────────────

    async def list_threads(self) -> list[dict]:
        """
        Scrape the sidebar for recent conversation threads.

        Returns a list of dicts: [{id, title, url}, ...]
        """
        threads = []
        for selector in Selectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    match = re.search(r"/c/([a-f0-9-]+)", href)
                    if match:
                        threads.append({
                            "id": match.group(1),
                            "title": title,
                            "url": f"{Config.CHATGPT_URL}{href}",
                        })
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    async def delete_thread(self, thread_id: str) -> bool:
        """
        Delete a ChatGPT conversation thread via the web UI.

        Navigates to the thread, opens the sidebar context menu, clicks Delete,
        and confirms in the modal dialog. Returns True on success, False otherwise.

        This is best-effort: failures are logged but never raised.
        """
        log.info(f"Attempting to delete ChatGPT thread: {thread_id}")
        try:
            # Navigate to the thread so the sidebar item is visible/interactable
            await self.navigate_to_thread(thread_id)
            await asyncio.sleep(2)

            # Locate the sidebar thread item for this specific thread
            thread_href = f"/c/{thread_id}"
            thread_el = None
            for sel in Selectors.SIDEBAR_THREAD_ITEM:
                try:
                    elements = await self._page.query_selector_all(sel)
                    for el in elements:
                        href = (await el.get_attribute("href") or "").rstrip("/")
                        if thread_href in href or href.endswith(f"/c/{thread_id}"):
                            thread_el = el
                            break
                    if thread_el:
                        break
                except Exception:
                    continue

            if not thread_el:
                log.warning(f"Could not find sidebar item for thread {thread_id}")
                return False

            # Hover over the thread item to reveal the menu button
            try:
                await thread_el.hover()
                await asyncio.sleep(0.5)
            except Exception as e:
                log.debug(f"Hover on thread item failed (non-fatal): {e}")

            # Click the three-dot / overflow menu button
            menu_clicked = False
            for sel in Selectors.SIDEBAR_THREAD_MENU_BUTTON:
                try:
                    # Try within the thread item's parent row first
                    parent = await thread_el.evaluate_handle("el => el.closest('li') || el.closest('div[class*=\"group\"]')")
                    btn = await parent.query_selector(sel)
                    if btn:
                        await btn.click(timeout=3000)
                        menu_clicked = True
                        break
                except Exception:
                    continue

            if not menu_clicked:
                log.warning(f"Could not open context menu for thread {thread_id}")
                return False

            await asyncio.sleep(0.5)

            # Click "Delete" in the context menu
            delete_clicked = False
            for sel in Selectors.THREAD_DELETE_OPTION:
                try:
                    btn = await self._page.wait_for_selector(sel, timeout=3000, state="visible")
                    if btn:
                        await btn.click(timeout=3000)
                        delete_clicked = True
                        break
                except Exception:
                    continue

            if not delete_clicked:
                log.warning(f"Could not click Delete option for thread {thread_id}")
                return False

            await asyncio.sleep(0.5)

            # Confirm deletion in the modal dialog
            confirm_clicked = False
            for sel in Selectors.THREAD_CONFIRM_DELETE_BUTTON:
                try:
                    btn = await self._page.wait_for_selector(sel, timeout=3000, state="visible")
                    if btn:
                        await btn.click(timeout=3000)
                        confirm_clicked = True
                        break
                except Exception:
                    continue

            if not confirm_clicked:
                log.warning(f"Could not confirm deletion for thread {thread_id}")
                return False

            # Allow time for the deletion request to process
            await asyncio.sleep(2)
            log.info(f"Successfully deleted ChatGPT thread: {thread_id}")
            return True

        except Exception as e:
            log.warning(f"Failed to delete thread {thread_id}: {e}", exc_info=True)
            return False

    # ── Private Helpers ─────────────────────────────────────────

    def _wire_backend_event_logger(self) -> None:
        """Record recent ChatGPT backend responses for timeout diagnostics."""
        try:
            self._page.on("response", self._record_backend_response)
        except Exception as e:
            log.debug(f"Could not attach backend response logger: {e}")

    def _record_backend_response(self, response) -> None:
        """Best-effort synchronous Playwright response event handler."""
        try:
            url = getattr(response, "url", "") or ""
            if not any(marker in url for marker in ("backend-api", "conversation", "sentinel", "chat-requirements")):
                return
            status = getattr(response, "status", None)
            self._recent_backend_events.append(
                {
                    "ts": time.time(),
                    "status": status,
                    "url": url[:500],
                }
            )
            self._recent_backend_events = self._recent_backend_events[-80:]
        except Exception:
            return

    def _backend_events_snapshot(self) -> list[dict]:
        """Return recent backend events with small, serializable fields."""
        return list(self._recent_backend_events[-40:])

    async def _detect_page_error(self) -> str | None:
        """Return the current browser/page error state, if one is visible."""
        return await _check_page_error(self._page)

    async def _wait_for_message_submission(
        self,
        previous_user_signature: str | None,
        sent_text: str,
        timeout_ms: int = 15000,
    ) -> bool:
        """
        Confirm that ChatGPT accepted the outgoing prompt.

        This catches stale send selectors and Enter-key-newline failures before
        the response detector spends a full timeout waiting for a reply.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        prompt_prefix = sent_text.strip()[:160]

        while time.monotonic() < deadline:
            latest_user_signature = await get_latest_user_turn_signature(self._page)
            if latest_user_signature and latest_user_signature != previous_user_signature:
                log.debug(f"Message submission confirmed by user turn: {latest_user_signature}")
                return True

            state = await self._composer_state()
            if not state:
                await asyncio.sleep(0.5)
                continue
            if state.get("hasStopButton"):
                log.debug("Message submission confirmed by visible stop button")
                return True

            composer_text = str(state.get("composerText") or "").strip()
            if not composer_text:
                log.debug("Message submission confirmed by cleared composer")
                return True

            if prompt_prefix and prompt_prefix not in composer_text:
                log.debug("Message submission likely confirmed by composer text change")
                return True

            await asyncio.sleep(0.5)

        log.warning("Timed out waiting for prompt submission confirmation")
        return False

    async def _composer_state(self) -> dict:
        """Read composer/stop-button state from the page."""
        try:
            state = await self._page.evaluate(
                """
                () => {
                    const textOf = (el) => ((el && (el.innerText || el.textContent || el.value)) || '').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                    };
                    const composer = document.querySelector(
                        "#prompt-textarea, div[contenteditable='true'][id='prompt-textarea'], div[contenteditable='true'], textarea"
                    );
                    const stopSelector = [
                        'button[data-testid="stop-button"]',
                        'button[aria-label="Stop answering"]',
                        'button[aria-label="Stop generating"]',
                        'button[aria-label*="stop" i]'
                    ].join(',');
                    return {
                        composerText: textOf(composer),
                        hasStopButton: Array.from(document.querySelectorAll(stopSelector)).some(isVisible),
                    };
                }
                """
            )
            return state if isinstance(state, dict) else {}
        except Exception as e:
            log.debug(f"Composer state read failed: {e}")
            return {}

    async def _extract_image_turn_text(self, previous_turn_signature: str | None = None) -> str:
        """
        Extract any text content from the latest turn (for image responses).

        Image turns may contain a title/description like:
        "Creating image • Adorable orange tabby kitten close-up"
        """
        return await extract_last_response_via_copy(
            self._page,
            previous_turn_signature=previous_turn_signature,
        )

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """
        Try each selector in the fallback list. Return the first one that matches.
        """
        for selector in selectors:
            try:
                el = await self._page.wait_for_selector(
                    selector,
                    timeout=Config.SELECTOR_TIMEOUT,
                    state="visible",
                )
                if el:
                    log.debug(f"Found {name} via: {selector}")
                    return selector
            except Exception:
                log.debug(f"Selector miss for {name}: {selector}")
                continue

        log.warning(f"No working selector found for: {name}")
        return None

    async def _dismiss_overlays(self) -> None:
        """Check for and dismiss any blocking dialogs/overlays on the page."""
        try:
            result = await self._page.evaluate(
                """
                () => {
                    const info = { dismissed: [], found: [] };

                    // Check for role="dialog" overlays
                    const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]');
                    for (const d of dialogs) {
                        const text = (d.innerText || '').trim().substring(0, 200);
                        info.found.push('dialog: ' + text);

                        // Try to find and click dismiss/close buttons
                        const closeBtn = d.querySelector(
                            'button[aria-label="Close"], button[aria-label="Dismiss"], ' +
                            'button:has(svg[data-testid="close"]), button.close'
                        );
                        if (closeBtn) {
                            closeBtn.click();
                            info.dismissed.push('dialog-close');
                        }
                    }

                    // Check for "Continue generating" button
                    const allButtons = document.querySelectorAll('button');
                    for (const btn of allButtons) {
                        const btnText = (btn.innerText || '').trim().toLowerCase();
                        if (btnText.includes('continue generating')) {
                            btn.click();
                            info.dismissed.push('continue-generating');
                        }
                    }

                    // Check for rate limit or error banners
                    const banners = document.querySelectorAll('[class*="banner"], [class*="toast"], [class*="alert"]');
                    for (const b of banners) {
                        const text = (b.innerText || '').trim().substring(0, 200);
                        if (text) info.found.push('banner: ' + text);
                    }

                    return info;
                }
                """
            )
            if result and isinstance(result, dict):
                if result.get("dismissed"):
                    log.info(f"Dismissed overlays: {result['dismissed']}")
                if result.get("found"):
                    log.debug(f"Page overlays found: {result['found']}")
        except Exception as e:
            log.debug(f"Overlay check failed: {e}")

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        # Check send button state before clicking
        btn_state = await self._page.evaluate(
            """
            () => {
                const selectors = [
                    'button[data-testid="send-button"]',
                    '#composer-submit-button',
                    "button[aria-label='Send prompt']",
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) {
                        return {
                            selector: sel,
                            disabled: btn.disabled,
                            ariaDisabled: btn.getAttribute('aria-disabled'),
                            visible: btn.offsetParent !== null,
                            classes: btn.className.substring(0, 100),
                        };
                    }
                }
                return null;
            }
            """
        )
        log.debug(f"Send button state: {btn_state}")

        # Don't click a disabled send button — the input wasn't recognized
        if isinstance(btn_state, dict) and btn_state.get("disabled"):
            log.warning("Send button is disabled — text may not have been inserted properly")
            return False

        selector = await self._find_selector(Selectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.info(f"Send button clicked via: {selector}")
            return True
        return False

    async def _detect_current_model_label(self) -> str:
        """Read the composer pill (usually an intelligence label, not a model)."""
        try:
            return await self._page.evaluate(
                r"""
                () => {
                    const el = document.querySelector(
                        "main button.__composer-pill[aria-haspopup='menu'],"
                        + "button[data-testid='model-switcher-dropdown-button']"
                    );
                    return el ? (el.innerText || el.textContent || "").trim() : "";
                }
                """
            )
        except Exception as exc:
            log.debug("Could not read composer model pill: %s", exc)
            return ""

    async def _dismiss_model_picker(self) -> None:
        for _ in range(2):
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                return
            await asyncio.sleep(0.05)

    async def _nested_model_picker_state(self) -> dict:
        """Inspect the open nested picker through roles and ARIA linkage."""
        try:
            state = await self._page.evaluate(
                r"""
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const primaryText = (el) => {
                        const primary = el.querySelector(".truncate");
                        if (primary && (primary.innerText || "").trim()) {
                            return primary.innerText.trim();
                        }
                        const first = Array.from(el.children).find(
                            (child) => (child.innerText || "").trim()
                        );
                        const raw = first
                            ? first.innerText
                            : (el.getAttribute("aria-label") || el.innerText || el.textContent || "");
                        return raw.trim().split(/\n+/)[0].trim();
                    };
                    const rowInfo = (el) => {
                        const rect = el.getBoundingClientRect();
                        const ariaChecked = el.getAttribute("aria-checked");
                        const dataState = el.getAttribute("data-state");
                        const checked = ariaChecked !== null && dataState !== null
                            ? ariaChecked === "true" && dataState === "checked"
                            : ariaChecked !== null
                                ? ariaChecked === "true"
                                : dataState === "checked";
                        return {
                            label: primaryText(el),
                            text: (el.innerText || el.textContent || "").trim().replace(/\s+/g, " "),
                            ariaChecked,
                            state: dataState,
                            checked,
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                        };
                    };
                    const menus = Array.from(document.querySelectorAll("[role='menu']"))
                        .filter(visible);
                    const topMenu = menus.find((menu) =>
                        menu.querySelector(
                            "[role='menuitem'][aria-haspopup='menu'][data-has-submenu]"
                        )
                    ) || null;
                    const opener = topMenu
                        ? topMenu.querySelector(
                            "[role='menuitem'][aria-haspopup='menu'][data-has-submenu]"
                        )
                        : null;
                    const submenu = opener && opener.id
                        ? menus.find((menu) => menu.getAttribute("aria-labelledby") === opener.id)
                        : null;
                    const openerRect = opener ? opener.getBoundingClientRect() : null;
                    return {
                        topMenuOpen: Boolean(topMenu),
                        submenuOpen: Boolean(submenu),
                        opener: opener && openerRect ? {
                            id: opener.id,
                            label: primaryText(opener),
                            state: opener.getAttribute("data-state"),
                            expanded: opener.getAttribute("aria-expanded"),
                            x: openerRect.left + openerRect.width / 2,
                            y: openerRect.top + openerRect.height / 2,
                        } : null,
                        reasoning: topMenu
                            ? Array.from(topMenu.querySelectorAll("[role='menuitemradio']"))
                                .filter(visible).map(rowInfo)
                            : [],
                        models: submenu
                            ? Array.from(submenu.querySelectorAll("[role='menuitemradio']"))
                                .filter(visible).map(rowInfo)
                            : [],
                    };
                }
                """
            )
            return state if isinstance(state, dict) else {}
        except Exception as exc:
            log.debug("Could not inspect nested model picker: %s", exc)
            return {}

    async def _model_picker_is_open(self) -> bool:
        return bool((await self._nested_model_picker_state()).get("topMenuOpen"))

    async def _open_model_picker(self, *_args, **_kwargs) -> bool:
        deadline = time.time() + Config.CHATGPT_MODEL_SWITCH_TIMEOUT / 1000.0
        while time.time() < deadline:
            if await self._model_picker_is_open():
                return True
            for selector in Selectors.MODEL_PICKER_BUTTON:
                try:
                    for element in await self._page.query_selector_all(selector):
                        if await element.is_visible():
                            await element.click()
                            await asyncio.sleep(0.1)
                            if await self._model_picker_is_open():
                                return True
                except Exception as exc:
                    log.debug("Model picker selector failed (%s): %s", selector, exc)
            await asyncio.sleep(0.15)
        return False

    async def _click_picker_row(self, row: dict) -> bool:
        if not isinstance(row, dict) or "x" not in row or "y" not in row:
            return False
        await self._page.mouse.move(float(row["x"]), float(row["y"]), steps=4)
        await self._page.mouse.click(float(row["x"]), float(row["y"]))
        return True

    async def _click_picker_radio(self, label: str, *, concrete_model: bool) -> bool:
        """Click an exact radio row inside the ARIA-linked active menu."""
        return bool(
            await self._page.evaluate(
                r"""
                ({ target, concreteModel }) => {
                    const normalize = (value) =>
                        (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
                    const visible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const primaryText = (el) => {
                        const primary = el.querySelector(".truncate");
                        if (primary && (primary.innerText || "").trim()) {
                            return primary.innerText.trim();
                        }
                        const first = Array.from(el.children).find(
                            (child) => (child.innerText || "").trim()
                        );
                        const raw = first ? first.innerText : (el.innerText || el.textContent || "");
                        return raw.trim().split(/\n+/)[0].trim();
                    };
                    const menus = Array.from(document.querySelectorAll("[role='menu']"))
                        .filter(visible);
                    const top = menus.find((menu) => menu.querySelector(
                        "[role='menuitem'][aria-haspopup='menu'][data-has-submenu]"
                    ));
                    if (!top) return false;
                    let scope = top;
                    if (concreteModel) {
                        const opener = top.querySelector(
                            "[role='menuitem'][aria-haspopup='menu'][data-has-submenu]"
                        );
                        scope = opener && opener.id
                            ? menus.find((menu) => menu.getAttribute("aria-labelledby") === opener.id)
                            : null;
                    }
                    if (!scope) return false;
                    const wanted = normalize(target);
                    const row = Array.from(scope.querySelectorAll("[role='menuitemradio']"))
                        .find((item) => visible(item) && normalize(primaryText(item)) === wanted);
                    if (!row || row.getAttribute("aria-disabled") === "true") return false;
                    row.click();
                    return true;
                }
                """,
                {"target": label, "concreteModel": concrete_model},
            )
        )

    async def _open_concrete_model_submenu(self) -> dict | None:
        state = await self._nested_model_picker_state()
        if state.get("submenuOpen"):
            return state
        opener = state.get("opener")
        if not opener or not await self._click_picker_row(opener):
            return None
        deadline = time.time() + Config.CHATGPT_MODEL_SWITCH_TIMEOUT / 1000.0
        while time.time() < deadline:
            state = await self._nested_model_picker_state()
            if state.get("submenuOpen") and state.get("models"):
                return state
            await asyncio.sleep(0.1)
        return None

    def _match_nested_model_label(
        self,
        target_labels: tuple[str, ...],
        available_labels: list[str],
    ) -> str:
        wanted_tokens = {normalize_model_token(label) for label in target_labels if label.strip()}
        for label in available_labels:
            if normalize_model_token(label) in wanted_tokens:
                return label
        wanted_ids = {model_label_to_public_id(label) for label in target_labels if label.strip()}
        for label in available_labels:
            if model_label_to_public_id(label) in wanted_ids:
                return label
        for wanted_id in wanted_ids:
            if not re.fullmatch(r"gpt-\d+(?:\.\d+)+", wanted_id):
                continue
            matches = [
                label for label in available_labels
                if model_label_to_public_id(label).startswith(wanted_id + "-")
            ]
            if len(matches) == 1:
                return matches[0]
        return ""

    async def _select_model_from_nested_picker(
        self,
        target_labels: tuple[str, ...],
    ) -> bool | None:
        state = await self._open_concrete_model_submenu()
        if state is None:
            return None
        rows = state.get("models") or []
        available = [row.get("label", "") for row in rows if row.get("label")]
        register_discovered_models(available)
        matched = self._match_nested_model_label(target_labels, available)
        if not matched:
            return False
        row = next(
            (item for item in rows if normalize_model_token(item.get("label", ""))
             == normalize_model_token(matched)),
            None,
        )
        if row and row.get("checked"):
            self._last_concrete_model_label = matched
            await self._dismiss_model_picker()
            return True
        if not row or not await self._click_picker_radio(matched, concrete_model=True):
            return False

        # Some concrete models take longer to settle than others. Reopen and
        # verify the authoritative checked radio row until the switch timeout.
        deadline = time.time() + Config.CHATGPT_MODEL_SWITCH_TIMEOUT / 1000.0
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            if not await self._open_model_picker():
                continue
            confirmed = await self._open_concrete_model_submenu()
            confirmed_row = next(
                (
                    item for item in (confirmed or {}).get("models", [])
                    if normalize_model_token(item.get("label", ""))
                    == normalize_model_token(matched)
                ),
                None,
            )
            success = bool(confirmed_row and confirmed_row.get("checked"))
            await self._dismiss_model_picker()
            if success:
                self._last_concrete_model_label = matched
                log.info("Confirmed concrete model through checked radio row: %s", matched)
                return True
        return False

    async def _select_reasoning_effort(self, requested_effort: str) -> str:
        await self._dismiss_model_picker()
        if not await self._open_model_picker():
            return ""
        state = await self._nested_model_picker_state()
        rows = state.get("reasoning") or []
        labels = [row.get("label", "") for row in rows if row.get("label")]
        model_label = (state.get("opener") or {}).get("label") or self._last_concrete_model_label
        if model_label:
            register_discovered_models([model_label])
            register_discovered_reasoning(model_label, labels)

        target_label, selected_effort = choose_reasoning_label(requested_effort, labels)
        row = next(
            (item for item in rows if normalize_model_token(item.get("label", ""))
             == normalize_model_token(target_label)),
            None,
        )
        if not target_label or not row:
            await self._dismiss_model_picker()
            return ""
        if row.get("checked"):
            await self._dismiss_model_picker()
            return selected_effort
        if not await self._click_picker_radio(target_label, concrete_model=False):
            await self._dismiss_model_picker()
            return ""

        await asyncio.sleep(0.2)
        if not await self._open_model_picker():
            return ""
        confirmed = await self._nested_model_picker_state()
        confirmed_row = next(
            (
                item for item in confirmed.get("reasoning", [])
                if normalize_model_token(item.get("label", "")) == normalize_model_token(target_label)
            ),
            None,
        )
        await self._dismiss_model_picker()
        return selected_effort if confirmed_row and confirmed_row.get("checked") else ""

    async def discover_available_models(self, force: bool = False) -> list[str]:
        """Discover concrete models and every model-specific reasoning row."""
        now = time.time()
        ttl = max(0, Config.CHATGPT_MODEL_DISCOVERY_TTL_SECONDS)
        if (
            self._model_capabilities_complete
            and not force
            and now - self._model_capabilities_checked_at < ttl
        ):
            return list(self._discovered_model_labels)

        previous_labels, previous_reasoning = get_discovered_catalog()
        labels: list[str] = []
        reasoning_by_model: dict[str, list[str]] = {}
        initial_model = ""
        initial_reasoning = ""
        discovery_complete = False
        restoration_complete = True

        await self._dismiss_model_picker()
        if not await self._open_model_picker():
            return []
        initial = await self._open_concrete_model_submenu()
        if not initial:
            await self._dismiss_model_picker()
            return []

        labels = [row.get("label", "") for row in initial.get("models", []) if row.get("label")]
        initial_model = next(
            (row.get("label", "") for row in initial.get("models", []) if row.get("checked")),
            "",
        )
        initial_reasoning = next(
            (row.get("label", "") for row in initial.get("reasoning", []) if row.get("checked")),
            "",
        )
        self._last_concrete_model_label = initial_model or self._last_concrete_model_label

        try:
            if not labels or not initial_model:
                log.warning("Capability discovery found no authoritative checked model")
                return list(previous_labels)

            await self._dismiss_model_picker()
            discovery_complete = True
            for label in labels:
                if normalize_model_token(label) != normalize_model_token(self._last_concrete_model_label):
                    if not await self._open_model_picker():
                        log.warning("Capability discovery could not open picker for %s", label)
                        discovery_complete = False
                        break
                    selection = await self._select_model_from_nested_picker((label,))
                    if selection is not True:
                        log.warning(
                            "Capability discovery could not confirm model %s (result=%r)",
                            label,
                            selection,
                        )
                        discovery_complete = False
                        break

                if not await self._open_model_picker():
                    log.warning("Capability discovery could not inspect reasoning rows for %s", label)
                    discovery_complete = False
                    break
                state = await self._nested_model_picker_state()
                visible_model = (state.get("opener") or {}).get("label", "")
                if normalize_model_token(visible_model) != normalize_model_token(label):
                    log.warning(
                        "Capability discovery expected %s but picker reported %s",
                        label,
                        visible_model or "<unknown>",
                    )
                    discovery_complete = False
                    break
                reasoning_by_model[label] = [
                    row.get("label", "")
                    for row in state.get("reasoning", [])
                    if row.get("label")
                ]
                self._last_concrete_model_label = label
                await self._dismiss_model_picker()
        finally:
            await self._dismiss_model_picker()
            if initial_model:
                if not await self._open_model_picker():
                    restoration_complete = False
                else:
                    restoration_complete = (
                        await self._select_model_from_nested_picker((initial_model,)) is True
                    )
                if restoration_complete and initial_reasoning:
                    restoration_complete = bool(
                        await self._select_reasoning_effort(initial_reasoning)
                    )
            await self._dismiss_model_picker()

            if discovery_complete and restoration_complete and len(reasoning_by_model) == len(labels):
                replace_discovered_catalog(labels, reasoning_by_model)
                self._discovered_model_labels = list(dict.fromkeys(labels))
                self._model_capabilities_complete = True
                self._model_capabilities_checked_at = time.time()
            else:
                replace_discovered_catalog(previous_labels, previous_reasoning)
                self._discovered_model_labels = list(previous_labels)
                self._model_capabilities_complete = False
                log.warning("Model capability discovery was incomplete and will be retried")

        return list(self._discovered_model_labels)

    async def _upload_files(self, file_paths: list[str]) -> None:
        """
        Upload files (images, PDFs, docs, etc.) to ChatGPT's input area.

        ChatGPT has a hidden <input type="file"> that accepts various file types.
        We set files on it directly (like drag-and-drop / file picker).
        """
        from pathlib import Path

        valid_paths = []
        for p in file_paths:
            path = Path(p)
            if path.exists() and path.is_file():
                valid_paths.append(str(path.resolve()))
            else:
                log.warning(f"File not found, skipping: {p}")

        if not valid_paths:
            log.warning("No valid files to upload")
            return

        log.info(f"Uploading {len(valid_paths)} file(s)...")

        # Find the file input element — ChatGPT has a hidden <input type="file">
        file_input = None
        for selector in Selectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
                if elements:
                    file_input = elements[0]
                    log.debug(f"Found file input: {selector}")
                    break
            except Exception:
                continue

        if file_input:
            # Set files directly on the input element
            await file_input.set_input_files(valid_paths)
            log.info(f"Set {len(valid_paths)} file(s) on file input")
        else:
            # Fallback: use page.set_input_files with a broad selector
            log.info("No file input found via selectors, trying broad input[type=file]")
            try:
                await self._page.set_input_files("input[type='file']", valid_paths)
                log.info(f"Set {len(valid_paths)} file(s) via broad selector")
            except Exception as e:
                log.error(f"Failed to upload files: {e}")
                raise RuntimeError(f"Could not upload files: {e}")

        # Wait for files to be processed/attached (thumbnails/badges appear)
        await asyncio.sleep(3)
        # Additional wait if multiple files
        if len(valid_paths) > 1:
            await asyncio.sleep(len(valid_paths))
        log.info("File upload complete")

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        match = re.search(r"/c/([a-f0-9-]+)", url)
        return match.group(1) if match else ""

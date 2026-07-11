from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.browser.manager import BrowserManager
from src.config import Config


async def collect(page) -> list[dict]:
    return await page.evaluate(
        r"""
        () => {
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 &&
              style.display !== "none" && style.visibility !== "hidden";
          };
          const selector = [
            "[role='menu']", "[role='menuitem']", "[role='menuitemradio']",
            "[role='option']", "[role='radio']", "[data-radix-menu-content]",
            "button[data-testid*='model' i]",
            "button.__composer-pill[aria-haspopup='menu']"
          ].join(",");
          return Array.from(document.querySelectorAll(selector))
            .filter(visible)
            .slice(0, 80)
            .map((el) => {
              const rect = el.getBoundingClientRect();
              const parent = el.parentElement?.closest(
                "[role='menu'],[role='dialog'],[data-radix-menu-content]"
              );
              return {
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute("role"),
                testid: el.getAttribute("data-testid"),
                text: (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 160),
                ariaLabel: el.getAttribute("aria-label"),
                ariaHaspopup: el.getAttribute("aria-haspopup"),
                ariaExpanded: el.getAttribute("aria-expanded"),
                ariaChecked: el.getAttribute("aria-checked"),
                dataState: el.getAttribute("data-state"),
                dataValue: el.getAttribute("data-value"),
                parentRole: parent?.getAttribute("role") || null,
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                html: el.outerHTML.slice(0, 700),
              };
            });
        }
        """
    )


async def main() -> None:
    manager = BrowserManager()
    try:
        page = await manager.start()
        await manager.navigate(Config.CHATGPT_URL)
        await asyncio.sleep(3)
        print("BEFORE")
        print(json.dumps(await collect(page), indent=2, ensure_ascii=False))

        trigger = page.locator(
            "button[data-testid='model-switcher-dropdown-button'], "
            "button.__composer-pill[aria-haspopup='menu']"
        )
        trigger_count = await trigger.count()
        if trigger_count != 1:
            raise RuntimeError(f"Expected one model picker trigger, found {trigger_count}")
        await trigger.click()
        await asyncio.sleep(1)

        print("AFTER_OPEN")
        print(json.dumps(await collect(page), indent=2, ensure_ascii=False))
        output = ROOT / "logs" / "manual" / "model-picker-open.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output))
        print(f"Screenshot written to {output}")
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())

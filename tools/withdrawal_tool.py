import random
import re
from typing import Type
from crewai.tools import BaseTool
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from config.user_context import UserAccountContext

from config.settings import settings
BROWSER_ARGS = settings.browser.flags


def is_older_than_one_week(time_text: str) -> bool:
    text = time_text.lower().strip()
    if any(unit in text for unit in ["second", "minute", "hour", "day", "yesterday", "today"]):
        return False
    if any(unit in text for unit in ["month", "year", "mo", "yr"]):
        return True
    match = re.search(r"(\d+)\s*(?:week|w)\b", text)
    return bool(match and int(match.group(1)) >= 1)


class WithdrawInput(BaseModel):
    max_withdrawals: int = Field(default=20, description="Max invitations to withdraw.")


class LinkedInWithdrawalTool(BaseTool):
    name: str = "LinkedIn Old Invitation Withdrawal"
    description: str = "Withdraws invitations >= 1 week old using verified selectors and dialog handlers."
    args_schema: Type[BaseModel] = WithdrawInput

    def _run(self, user_ctx: UserAccountContext, max_withdrawals: int = 20) -> int:
        brave_profile_dir = user_ctx.profile_dir
        brave_path = user_ctx.brave_path
        sent_url = "https://www.linkedin.com/mynetwork/invitation-manager/sent/?invitationType=PEOPLE"
        withdrawn_count = 0

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(brave_profile_dir),
                executable_path=brave_path,
                headless=True,
                args=BROWSER_ARGS,
                viewport={"width": 1280, "height": 850},
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto(sent_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)

            # Checkpoint resolution
            if any(token in page.url for token in ["login", "checkpoint", "challenge", "authwall"]):
                context.close()
                intervene = p.chromium.launch_persistent_context(
                    user_data_dir=str(brave_profile_dir),
                    executable_path=brave_path,
                    headless=False,
                    args=BROWSER_ARGS,
                )
                i_page = intervene.new_page()
                i_page.goto(sent_url, timeout=60000)
                while any(token in i_page.url for token in ["login", "checkpoint", "challenge", "authwall"]):
                    i_page.wait_for_timeout(2000)
                intervene.close()

                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(brave_profile_dir),
                    executable_path=brave_path,
                    headless=True,
                    args=BROWSER_ARGS,
                )
                page = context.new_page()
                page.goto(sent_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

            scroll_attempts_without_match = 0

            while withdrawn_count < max_withdrawals:
                withdraw_actions = page.locator(
                    "a[aria-label*='Withdraw invitation'], button[aria-label*='Withdraw invitation'], "
                    "a:has-text('Withdraw'), button:has-text('Withdraw')"
                )
                count = withdraw_actions.count()

                if count == 0:
                    empty = page.locator("section:has-text('No sent invitations'), div:has-text('No invitations')")
                    if empty.is_visible():
                        break

                target_action = None
                target_name = ""

                for i in range(count):
                    action_el = withdraw_actions.nth(i)
                    if not action_el.is_visible():
                        continue

                    card = action_el.locator("xpath=./ancestor::div[@role='listitem'] | ./ancestor::li").first
                    card_text = card.inner_text() if card.is_visible() else ""
                    if not card_text:
                        continue

                    if is_older_than_one_week(card_text):
                        target_action = action_el
                        lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                        target_name = lines[0] if lines else f"Profile #{withdrawn_count + 1}"
                        break

                if target_action:
                    scroll_attempts_without_match = 0
                    target_action.click(force=True, timeout=5000)
                    page.wait_for_timeout(1500)

                    dialog = page.locator("dialog[open], div[role='dialog'], [data-testid='dialog']").first
                    try:
                        dialog.wait_for(state="visible", timeout=4000)
                    except Exception:
                        pass

                    confirm = dialog.locator(
                        "button.artdeco-button--primary, button:has-text('Withdraw'), [data-testid='dialog-primary-button']"
                    ).last

                    if confirm.is_visible():
                        confirm.click(force=True, timeout=5000)
                        withdrawn_count += 1
                        print(f"  -> Withdrawn invitation for {target_name}")

                        try:
                            dialog.wait_for(state="detached", timeout=5000)
                        except Exception:
                            page.wait_for_timeout(1500)

                        page.wait_for_timeout(random.uniform(2500, 3500))
                        continue
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(1000)

                # Paginate down
                page.evaluate("""() => {
                    const scrollables = Array.from(document.querySelectorAll('*')).filter(el => {
                        const style = window.getComputedStyle(el);
                        return (style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
                    });
                    scrollables.forEach(s => { s.scrollTop += 2200; });
                    window.scrollBy(0, 2200);
                }""")
                page.mouse.move(600, 500)
                page.keyboard.press("PageDown")
                page.keyboard.press("PageDown")
                page.wait_for_timeout(3000)

                scroll_attempts_without_match += 1
                if scroll_attempts_without_match >= 20:
                    break

            context.close()

        return withdrawn_count
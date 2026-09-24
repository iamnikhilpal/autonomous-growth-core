import concurrent.futures
import json
import random
import urllib.parse
from datetime import date, datetime
from typing import Optional, Type

from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from config.user_context import UserAccountContext

from config.settings import settings
BROWSER_ARGS = settings.browser.flags

MAX_DAILY_REQUESTS = 8
MAX_WEEKLY_REQUESTS = 50
OPERATING_HOUR_START = settings.growth.safety.operating_hour_start
OPERATING_HOUR_END = settings.growth.safety.operating_hour_end


def normalize_url(raw_url: str) -> str:
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def run_crew_in_worker_thread(crew: Crew):
    """
    Executes a CrewAI kickoff in an isolated worker thread so it does
    not collide with an active asyncio event loop (e.g., inside CrewAI Flows).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(crew.kickoff)
        return future.result()


class OutreachInput(BaseModel):
    max_requests: int = Field(default=10, description="Max invitations to send.")
    target_company: Optional[str] = Field(
        default=None, 
        description="Optional fallback target company name to verify against if not specified in queue line."
    )


class EmployerVerification(BaseModel):
    is_match: bool = Field(
        ..., 
        description="True if the actual company matches the intended target company, False if it is a different company."
    )
    reason: str = Field(..., description="Brief 1-sentence reasoning.")


verifier_agent = Agent(
    role="Employer Verification Specialist",
    goal="Verify whether an actual employer name/entity matches the target company.",
    backstory=(
        "You determine if a company name from a LinkedIn profile corresponds to the target organization "
        "or a completely different business (e.g. consultancies, testing labs, or non-related entities sharing a keyword)."
    ),
    verbose=False,
)


def is_profile_current_employer_match(page, target_company: str) -> bool:
    """
    Extracts the current employer name and company link directly from
    the LinkedIn DOM, without any hardcoded keyword lists.
    """
    if not target_company or not target_company.strip():
        return True

    try:
        # LinkedIn renders the current company link directly inside the top hero section:
        company_anchor = page.locator(
            "main section a[href*='/company/'], "
            "main section button[aria-label*='Current company'], "
            "main [data-field='experience_company_logo']"
        ).first

        actual_company_text = ""
        company_href = ""

        if company_anchor.is_visible():
            actual_company_text = company_anchor.inner_text().strip()
            company_href = company_anchor.get_attribute("href") or ""

        # Fallback to headline if the company badge link is absent
        if not actual_company_text:
            headline_el = page.locator("main section div.text-body-medium").first
            if headline_el.is_visible():
                actual_company_text = headline_el.inner_text().strip()

        if not actual_company_text:
            return True  # If DOM cannot extract, proceed cautiously

        # Fast verification prompt
        task = Task(
            description=(
                f"Target intended company: '{target_company}'\n"
                f"Candidate's reported current company/headline: '{actual_company_text}'\n"
                f"Company profile link: '{company_href}'\n\n"
                "Question: Does the candidate genuinely work for the intended target company, "
                "or does this indicate an entirely separate/different business entity? "
                "Set is_match=True ONLY if they refer to the exact same organization."
            ),
            expected_output="Verification result with boolean flag.",
            agent=verifier_agent,
            output_pydantic=EmployerVerification,
        )

        crew = Crew(agents=[verifier_agent], tasks=[task], process=Process.sequential, verbose=False)
        output = run_crew_in_worker_thread(crew)
        return output.pydantic.is_match

    except Exception as e:
        print(f"  [Outreach Gate Warning] DOM/LLM check failed ({e}); defaulting to permit.")
        return True


class LinkedInConnectionTool(BaseTool):
    name: str = "LinkedIn Connection Outreach"
    description: str = "Sends connection requests within safety thresholds with JIT company verification."
    args_schema: Type[BaseModel] = OutreachInput

    def _run(self, user_ctx: UserAccountContext, max_requests: int = 10, target_company: Optional[str] = None) -> dict:
        curr_hour = datetime.now().hour
        if not (OPERATING_HOUR_START <= curr_hour < OPERATING_HOUR_END):
            return {"status": "skipped", "reason": "outside_operating_hours", "sent": 0}

        brave_profile_dir = user_ctx.profile_dir
        brave_path = user_ctx.brave_path
        urls_file = user_ctx.profile_urls_file
        visited_file = user_ctx.visited_urls_file
        state_file = user_ctx.safety_state_file

        # Load safety state
        today_str = str(date.today())
        state = {"history": {}}
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                pass

        for d_str in list(state.get("history", {}).keys()):
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
                if (date.today() - d).days > 7:
                    del state["history"][d_str]
            except ValueError:
                del state["history"][d_str]

        today_sent = state.get("history", {}).get(today_str, 0)
        week_sent = sum(state.get("history", {}).values())

        if today_sent >= MAX_DAILY_REQUESTS or week_sent >= MAX_WEEKLY_REQUESTS:
            return {"status": "quota_reached", "today_sent": today_sent, "week_sent": week_sent, "sent": 0}

        # Load queue with optional metadata (e.g., "url | company")
        # Load queue with optional metadata (e.g., "url | company")
        raw_items = []
        with user_ctx.shared_lock:
            if urls_file.exists():
                with open(urls_file, "r", encoding="utf-8") as f:
                    raw_items = [l.strip() for l in f if l.strip() and not l.startswith("#")]

        visited_urls = set()
        if visited_file.exists():
            try:
                with open(visited_file, "r", encoding="utf-8") as f:
                    visited_urls = {normalize_url(u) for u in json.load(f) if u}
            except Exception:
                pass

        pending_items = []
        for line in raw_items:
            if "|" in line:
                u, comp = line.split("|", 1)
                clean_u = normalize_url(u)
                if clean_u and clean_u not in visited_urls:
                    pending_items.append((clean_u, comp.strip()))
            else:
                clean_u = normalize_url(line)
                if clean_u and clean_u not in visited_urls:
                    pending_items.append((clean_u, target_company))

        if not pending_items:
            return {"status": "no_pending_urls", "sent": 0}

        sent_count = 0

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(brave_profile_dir),
                executable_path=brave_path,
                headless=True,
                args=BROWSER_ARGS,
                viewport={"width": 1280, "height": 850},
            )

            for url, comp_context in pending_items:
                if sent_count >= max_requests or today_sent >= MAX_DAILY_REQUESTS or week_sent >= MAX_WEEKLY_REQUESTS:
                    break

                page = context.new_page()
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    if any(token in page.url for token in ["login", "checkpoint", "challenge", "authwall"]):
                        print("  [Outreach Tool] Authwall/Checkpoint hit. Halting outreach loop.")
                        break

                    page.wait_for_selector("main", timeout=10000)
                    page.wait_for_timeout(random.uniform(800, 1400))

                    # ----------------- JIT Verification Gate -----------------
                    if comp_context:
                        is_match = is_profile_current_employer_match(page, comp_context)
                        if not is_match:
                            print(f"  [Outreach Gate] Skipped: {url} does not work at target '{comp_context}'.")
                            visited_urls.add(url)
                            with open(visited_file, "w", encoding="utf-8") as vf:
                                json.dump(sorted(visited_urls), vf, indent=2)
                            continue
                    # ---------------------------------------------------------

                    hero = page.locator("main section").first
                    direct_connect = hero.locator("button:has-text('Connect'), a:has-text('Connect')").first
                    
                    clicked = False
                    if direct_connect.is_visible():
                        direct_connect.click()
                        clicked = True
                    else:
                        more_btn = hero.locator("button[aria-label='More']").first
                        if more_btn.is_visible():
                            more_btn.click()
                            page.wait_for_timeout(1000)
                            menu_conn = page.locator("div[role='menu'] :has-text('Connect')").first
                            if menu_conn.is_visible():
                                menu_conn.click()
                                clicked = True
                            else:
                                page.keyboard.press("Escape")

                    if clicked:
                        page.wait_for_timeout(1000)
                        modal = page.locator("div[role='dialog'], [data-test-modal]")
                        if modal.is_visible():
                            send_btn = modal.locator(
                                "button[aria-label='Send without a note'], button:has-text('Send without a note'), button:has-text('Send')"
                            ).first
                            if send_btn.is_visible():
                                send_btn.click()
                                sent_count += 1
                                today_sent += 1
                                week_sent += 1
                                state["history"][today_str] = today_sent
                                state["total_week"] = week_sent
                                with user_ctx.user_lock:
                                    with open(state_file, "w", encoding="utf-8") as sf:
                                        json.dump(state, sf, indent=2)
                                print(f"  [Outreach Tool] Connection sent to: {url} ({sent_count}/{max_requests})")
                                page.wait_for_timeout(random.uniform(4000, 6000))

                    visited_urls.add(url)
                    with user_ctx.user_lock:
                        with open(visited_file, "w", encoding="utf-8") as vf:
                            json.dump(sorted(visited_urls), vf, indent=2)

                except Exception as e:
                    print(f"  [Outreach Tool] Failed on {url}: {e}")
                    visited_urls.add(url)
                finally:
                    page.close()

            context.close()

        return {"status": "success", "sent": sent_count, "today_sent": today_sent, "week_sent": week_sent}
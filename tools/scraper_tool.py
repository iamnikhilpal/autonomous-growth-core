import concurrent.futures
import random
import urllib.parse
from typing import List, Type

from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from config.user_context import UserAccountContext

from config.settings import settings
BROWSER_ARGS = settings.browser.flags

# Extracts URL, Title, and Snippet together from each search card
JS_CARD_EXTRACTOR = """
(() => {
    const cards = document.querySelectorAll('div.MjjYud, div.g');
    const results = [];

    cards.forEach(card => {
        const textAll = (card.innerText || '').replace(/\\s+/g, ' ').trim();
        const titleEl = card.querySelector('h3');
        const title = titleEl ? titleEl.innerText : '';

        // Exclude 500+ connections
        if (/500\\+\\s*connections/i.test(textAll)) return;
        const connMatch = textAll.match(/(\\d+[\\d,]*)\\s*connections/i);
        if (connMatch) {
            const count = parseInt(connMatch[1].replace(/,/g, ''), 10);
            if (count >= 500) return;
        }

        const anchor = card.querySelector('a.zReHs') || 
                       card.querySelector('a[jsname="UWckNb"]') || 
                       card.querySelector('a[href*="/goto"], a[href*="/url"], a[href*="linkedin.com/in/"]');
        if (!anchor) return;

        const href = anchor.getAttribute('href') || '';
        if (!href) return;

        const fullUrl = href.startsWith('http') ? href : `https://www.google.com${href}`;

        results.push({
            url: fullUrl,
            title: title,
            snippet: textAll
        });
    });

    return results;
})();
"""


# --- Pydantic Schemas for LLM Batch Verification ---

class CandidateEvaluation(BaseModel):
    url: str
    is_target_employee: bool = Field(
        ...,
        description="True if the candidate works at the intended target company. False if from an unrelated entity sharing the name."
    )
    confidence_reason: str = Field(..., description="Brief 1-sentence reason.")


class BatchVerificationOutput(BaseModel):
    evaluations: List[CandidateEvaluation]


def normalize_url(raw_url: str) -> str:
    cleaned = (raw_url or "").strip()
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def run_crew_in_worker_thread(crew: Crew):
    """Executes a CrewAI kickoff in an isolated worker thread so it does
    not collide with an active asyncio event loop (e.g., inside CrewAI Flows).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(crew.kickoff)
        return future.result()


def resolve_with_playwright(page, google_url: str) -> str:
    if "linkedin.com/in/" in google_url:
        return normalize_url(google_url)
    try:
        response = page.request.get(google_url, max_redirects=0, timeout=5000)
        location = response.headers.get("location", "")
        if "linkedin.com/in/" in location:
            cleaned = location.split("?")[0].rstrip("/")
            if not any(sub in cleaned for sub in ["/dir/", "/company/", "/jobs/", "/pulse/"]):
                return normalize_url(cleaned)
    except Exception:
        pass
    return ""


def filter_candidates_with_agent(company_name: str, context_hint: str, raw_cards: list[dict]) -> set[str]:
    """Uses a fast CrewAI evaluation agent to batch-verify search snippets against false positives."""
    if not raw_cards:
        return set()

    verifier_agent = Agent(
        role="Entity Disambiguation Specialist",
        goal="Determine if candidate search results belong to the actual target tech company or unrelated entities.",
        backstory=(
            "You review Google search snippets of LinkedIn profiles. You distinguish between a specific "
            "tech startup/company and unrelated regional consultancies, agencies, or manufacturing companies "
            "that share the same word in their name."
        ),
        verbose=False,
        memory=False,
    )

    batch_payload = [
        {"url": c["url"], "title": c["title"], "snippet": c["snippet"][:250]}
        for c in raw_cards
    ]

    task = Task(
        description=(
            f"Target Company: '{company_name}'\n"
            f"Context: '{context_hint or 'Software / Tech'}'\n\n"
            f"Evaluate these search cards:\n{batch_payload}\n\n"
            "Criteria:\n"
            f"1. Candidate MUST work at the intended company '{company_name}'.\n"
            f"2. REJECT candidates from unrelated businesses sharing the word '{company_name}' "
            f"(e.g., consultancies, agencies, or different full names like '{company_name} Infosolutions', '{company_name} Stantest').\n"
            f"3. REJECT candidates who are past alumni unless their current employer is also '{company_name}'."
        ),
        expected_output="Structured list of evaluations with boolean verification flags.",
        agent=verifier_agent,
        output_pydantic=BatchVerificationOutput,
    )

    crew = Crew(
        agents=[verifier_agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )

    output = run_crew_in_worker_thread(crew)

    approved_urls = set()
    try:
        parsed: BatchVerificationOutput = output.pydantic
        for item in parsed.evaluations:
            if item.is_target_employee:
                approved_urls.add(item.url)
    except Exception as e:
        print(f"[Verification Error] Could not parse LLM output: {e}")
        approved_urls = {c["url"] for c in raw_cards}

    return approved_urls


def get_company_context(company_name: str) -> str:
    """Uses a quick agent prompt to discover domain/flagship product context without editing files."""
    finder_agent = Agent(
        role="Company Research Specialist",
        goal="Identify key domain, sector, or primary product of a company.",
        backstory="You provide concise keywords for companies to distinguish tech companies from generic businesses.",
        verbose=False,
    )
    task = Task(
        description=(
            f"For the tech/software company '{company_name}', provide 1-2 distinguishing keywords "
            f"(e.g., domain like 'presto.com' or core product like 'restaurant automation') "
            f"to distinguish it from unrelated consultancies or manufacturing firms. "
            f"Return ONLY the keyword(s), nothing else."
        ),
        expected_output="1-2 concise keywords.",
        agent=finder_agent,
    )
    crew = Crew(
        agents=[finder_agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )

    result = run_crew_in_worker_thread(crew)
    return str(result.raw if hasattr(result, "raw") else result).strip()


# --- Tool Definition ---

class ScraperInput(BaseModel):
    company_entry: str = Field(..., description="Target company entry, optionally formatted as 'Name | Domain/Anchor'.")
    max_pages: int = Field(default=5, description="Maximum number of search pages to paginate through.")
    min_required_profiles: int = Field(default=10, description="Minimum profiles required to accept company.")


class GoogleLinkedInScraperTool(BaseTool):
    name: str = "Google LinkedIn Lead Scraper"
    description: str = "Scrapes Google results up to 5 pages with anchor targeting and LLM disambiguation."
    args_schema: Type[BaseModel] = ScraperInput

    def _run(self, user_ctx: UserAccountContext, company_name: str, max_pages: int = 5, min_required_profiles: int = 10) -> dict:
        clean_name = company_name.strip()
        chrome_profile_dir = getattr(user_ctx, "chrome_profile_dir", user_ctx.shared_dir.parent / "chrome_devtools_profile")
        collected_urls = set()

        # Resilient selectors covering standard results, alternative containers, and 0-result states
        RESULT_SELECTORS = "#search, #rso, div.MjjYud, #center_col, #rcnt"
        ZERO_RESULT_SELECTORS = "div:has-text('did not match any documents'), #topstuff"
        WAIT_SELECTOR = f"{RESULT_SELECTORS}, {ZERO_RESULT_SELECTORS}"

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(chrome_profile_dir),
                channel="chrome",
                headless=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                args=BROWSER_ARGS,
                viewport={"width": 1280, "height": 850},
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.google.com", wait_until="domcontentloaded")
            page.wait_for_timeout(random.uniform(1200, 2000))

            context_hint = get_company_context(clean_name)
            
            # Flexible query syntax to avoid 0-result lockouts
            query_parts = [f'site:linkedin.com/in/ "{clean_name}"']
            if context_hint and len(context_hint.split()) <= 3:
                query_parts.append(f'"{context_hint}"')
            query_parts.append('("Software Engineer" OR "SWE" OR "Backend" OR "Tech Lead")')
            target_query = " ".join(query_parts)

            search_input = page.locator("textarea[name='q'], input[name='q']").first
            search_input.click()
            search_input.fill(target_query.strip())
            page.keyboard.press("Enter")

            # Check for bot challenge or consent walls
            page.wait_for_timeout(2000)
            current_url = page.url.lower()
            if any(term in current_url for term in ["sorry", "captcha", "consent.google", "recaptcha"]):
                print(f"  [Google Scraper] Bot challenge or consent page detected on '{clean_name}'.")
                context.close()
                return {
                    "company": clean_name,
                    "anchor": context_hint,
                    "count": 0,
                    "urls": [],
                    "meets_threshold": False
                }

            # Resilient wait: catches normal results OR zero-result states without crashing
            try:
                page.wait_for_selector(WAIT_SELECTOR, timeout=15000)
            except Exception:
                debug_img = user_ctx.shared_dir / "google_timeout_debug.png"
                try:
                    page.screenshot(path=str(debug_img))
                    print(f"  [Google Scraper] Timeout on '{clean_name}'. Diagnostic screenshot saved: {debug_img}")
                except Exception:
                    pass
                context.close()
                return {
                    "company": clean_name,
                    "anchor": context_hint,
                    "count": 0,
                    "urls": [],
                    "meets_threshold": False
                }

            for page_num in range(1, max_pages + 1):
                page.mouse.wheel(0, random.randint(350, 650))
                page.wait_for_timeout(random.uniform(1200, 1800))

                raw_cards = []
                for _ in range(3):
                    try:
                        raw_cards = page.evaluate(JS_CARD_EXTRACTOR)
                        break
                    except Exception:
                        page.wait_for_timeout(1000)

                # Batch LLM Disambiguation
                if raw_cards:
                    approved_raw_urls = filter_candidates_with_agent(clean_name, context_hint, raw_cards)
                    print(f"  [Page {page_num}] {len(raw_cards)} candidates found -> {len(approved_raw_urls)} verified as real target employees.")

                    for raw_url in approved_raw_urls:
                        real = resolve_with_playwright(page, raw_url)
                        if real:
                            collected_urls.add(real)
                        elif raw_url and raw_url.startswith("http"):
                            collected_urls.add(raw_url)

                if page_num < max_pages:
                    next_btn = page.locator("#pnnext, a[aria-label='Next page']").first
                    if next_btn.is_visible():
                        next_btn.click()
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                            page.wait_for_selector(WAIT_SELECTOR, timeout=10000)
                        except Exception:
                            break
                    else:
                        break

            context.close()

        meets_threshold = len(collected_urls) >= min_required_profiles
        return {
            "company": clean_name,
            "anchor": context_hint,
            "count": len(collected_urls),
            "urls": list(collected_urls),
            "meets_threshold": meets_threshold
        }
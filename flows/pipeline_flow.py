import json
import random
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel
from crewai.flow.flow import Flow, listen, start

from tools.scraper_tool import GoogleLinkedInScraperTool, normalize_url
from tools.withdrawal_tool import LinkedInWithdrawalTool
from tools.outreach_tool import LinkedInConnectionTool
from config.user_context import UserAccountContext

class PipelineFlowState(BaseModel):
    user_id: str = None
    selected_company: Optional[str] = None
    harvested_count: int = 0
    withdrawn_count: int = 0
    outreach_sent: int = 0
    exhausted_companies: List[str] = []


class LinkedInGrowthFlow(Flow[PipelineFlowState]):

    def __init__(self, user_id: str = "user_nikhil", **kwargs):
        super().__init__(initial_state=PipelineFlowState(user_id=user_id), **kwargs)
        self.user_ctx = UserAccountContext(user_id=user_id)
        self.state.user_id = user_id

    def _load_available_companies(self) -> List[str]:
        with self.user_ctx.shared_lock:
            if not self.user_ctx.inqueue_companies_file.exists():
                return []

            with open(self.user_ctx.inqueue_companies_file, "r", encoding="utf-8") as f:
                all_companies = [line.strip().lstrip("* ") for line in f if line.strip()]

            exhausted = set()
            if self.user_ctx.exhausted_companies_file.exists():
                with open(self.user_ctx.exhausted_companies_file, "r", encoding="utf-8") as f:
                    exhausted = {line.strip() for line in f if line.strip()}

            self.state.exhausted_companies = list(exhausted)
            return [c for c in all_companies if c not in exhausted]

    def _mark_company_exhausted(self, company: str):
        with self.user_ctx.shared_lock:
            self.user_ctx.exhausted_companies_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.user_ctx.exhausted_companies_file, "a", encoding="utf-8") as f:
                f.write(f"{company}\n")
            if company not in self.state.exhausted_companies:
                self.state.exhausted_companies.append(company)

    def _pop_company_from_inqueue(self, company: str):
        """Removes the company from companies-inqueue.txt to prevent duplicate scraping."""
        with self.user_ctx.shared_lock:
            if not self.user_ctx.inqueue_companies_file.exists():
                return

            with open(self.user_ctx.inqueue_companies_file, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]

            target_norm = company.strip().lower()
            remaining = [l for l in lines if l.lstrip("* ").strip().lower() != target_norm]

            with open(self.user_ctx.inqueue_companies_file, "w", encoding="utf-8") as f:
                for l in remaining:
                    f.write(f"{l}\n")

    @start()
    def select_and_source_leads(self):
        print("\n[Stage 1] Selecting Target Company & Sourcing Leads...")
        scraper = GoogleLinkedInScraperTool()

        while True:
            available = self._load_available_companies()
            if not available:
                print("-> [Halt] No available companies left in queue (all exhausted or file empty). Stopping run for today.")
                return None

            candidate = random.choice(available)
            print(f"-> Candidate chosen randomly: '{candidate}'. Evaluating search depth...")

            result = scraper._run(user_ctx=self.user_ctx, company_name=candidate, max_pages=5, min_required_profiles=10)

            if not result["meets_threshold"]:
                print(f"-> '{candidate}' yielded only {result['count']} profiles across 5 pages (< 10 required). Flagging as exhausted and moving to next company...")
                self._mark_company_exhausted(candidate)
                self._pop_company_from_inqueue(candidate)
                continue

            # Qualified candidate
            self.state.selected_company = candidate
            self.state.harvested_count = result["count"]
            print(f"-> '{candidate}' passed validation with {result['count']} profiles! Saving leads...")

            # Save URLs with company metadata inside shared lock
            with self.user_ctx.shared_lock:
                existing_urls = set()
                if self.user_ctx.profile_urls_file.exists():
                    with open(self.user_ctx.profile_urls_file, "r", encoding="utf-8") as f:
                        for line in f:
                            clean_line = line.strip()
                            if clean_line and not clean_line.startswith("#"):
                                raw_u = clean_line.split("|")[0].strip()
                                existing_urls.add(normalize_url(raw_u))

                new_leads = [
                    f"{normalize_url(u)} | {candidate}"
                    for u in result["urls"]
                    if normalize_url(u) and normalize_url(u) not in existing_urls
                ]

                with open(self.user_ctx.profile_urls_file, "a", encoding="utf-8") as f:
                    for lead_record in new_leads:
                        f.write(f"{lead_record}\n")

                self._mark_company_exhausted(candidate)
                self._pop_company_from_inqueue(candidate)

            print(f"-> '{candidate}' moved from inqueue to exhausted queue.")
            return candidate

    @listen(select_and_source_leads)
    def clean_stale_invitations(self, previous_result):
        cleanup_tool = LinkedInWithdrawalTool()
        with self.user_ctx.user_lock:
            count = cleanup_tool._run(user_ctx=self.user_ctx, max_withdrawals=20)
        self.state.withdrawn_count = count if isinstance(count, int) else 0
        return self.state.withdrawn_count

    @listen(clean_stale_invitations)
    def execute_outreach(self, previous_result):
        outreach_tool = LinkedInConnectionTool()
        with self.user_ctx.user_lock:
            result = outreach_tool._run(
                user_ctx=self.user_ctx, 
                max_requests=8, 
                target_company=self.state.selected_company
            )
        
        sent = 0
        if isinstance(result, dict):
            sent = result.get("sent", 0)
        elif isinstance(result, int):
            sent = result

        self.state.outreach_sent = sent
        return result
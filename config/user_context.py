from pathlib import Path
from pydantic import BaseModel
from config.settings import settings, BASE_DIR


class LocalExecutionContext(BaseModel):
    brave_path: str = settings.browser.default_brave_path

    @property
    def artifacts_dir(self) -> Path:
        p = BASE_DIR / "artifacts"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def profile_dir(self) -> Path:
        p = self.artifacts_dir / "brave_profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def chrome_profile_dir(self) -> Path:
        p = self.artifacts_dir / "chrome_profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def safety_state_file(self) -> Path:
        return self.artifacts_dir / "linkedin_safety_state.json"

    @property
    def visited_urls_file(self) -> Path:
        return self.artifacts_dir / "linkedin_visited_urls.json"

    @property
    def inqueue_companies_file(self) -> Path:
        return self.artifacts_dir / "companies-inqueue.txt"

    @property
    def exhausted_companies_file(self) -> Path:
        return self.artifacts_dir / "companies-exhausted.txt"

    @property
    def profile_urls_file(self) -> Path:
        return self.artifacts_dir / "linkedin_profile_urls.txt"
from parsing_core.models.dataclasses import AIArtifact, Task
from parsing_core.storage.repository import Repository


class CacheService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def find_completed_task_by_file_sha256(self, sha: str) -> Task | None:
        return self.repo.find_completed_task_by_file_sha256(sha)

    def find_completed_artifact_by_section_sha256(self, sha: str) -> AIArtifact | None:
        return self.repo.find_completed_artifact_by_section_sha256(sha)

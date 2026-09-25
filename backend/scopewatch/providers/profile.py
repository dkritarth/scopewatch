"""Provider profile definitions for swappable agent and auditor models."""

from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderProfile(BaseModel):
    """Configuration profile for an OpenAI-compatible model provider."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    reasoning_param: Optional[dict[str, Any]] = Field(default=None, alias="reasoning")
    timeout_s: float = 30.0
    max_retries: int = 3
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_key_env", mode="before")
    @classmethod
    def _normalize_empty_key_env(cls, v: Any) -> Optional[str]:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def reasoning(self) -> Optional[dict[str, Any]]:
        """Convenience property for accessing the reasoning parameter dictionary."""
        return self.reasoning_param

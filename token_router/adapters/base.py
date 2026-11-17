import abc
from typing import Any, AsyncGenerator, Dict, Tuple


class ProviderAdapter(abc.ABC):
    """Abstract base class for all external provider integrations."""

    provider_name: str

    @abc.abstractmethod
    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        """Validate an API key against provider's live test/auth endpoint.

        Returns (is_valid, message, status_code).
        """
        pass

    @abc.abstractmethod
    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        """Executes a non-streaming capability request.

        Returns (status_code, response_data, token_usage_dict).
        """
        pass

    @abc.abstractmethod
    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        """Executes a streaming response generator."""
        pass

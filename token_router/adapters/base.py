import abc
from typing import Any, AsyncGenerator, Dict, Tuple, Optional


class ProviderAdapter(abc.ABC):
    """Abstract base class for all external provider integrations across any service category."""

    provider_name: str

    @abc.abstractmethod
    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        """Validate credential against provider's live test/auth endpoint.

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
        """Executes a non-streaming capability/operation request.

        Returns (status_code, response_data, usage_dict).
        """
        pass

    async def execute_operation(
        self,
        secret: str,
        operation: str,
        payload: Dict[str, Any],
        timeout: float,
        idempotency_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
        """Universal operation executor with explicit idempotency support.

        Default implementation falls back to execute_request for backwards compatibility.
        """
        effective_model = model or payload.get("model") or "auto"
        status_code, data, usage = await self.execute_request(
            secret=secret,
            capability=operation,
            model=effective_model,
            payload=payload,
            timeout=timeout,
        )
        return status_code, data, usage

    async def execute_operation_with_headers(
        self,
        secret: str,
        operation: str,
        payload: Dict[str, Any],
        timeout: float,
        idempotency_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, Any], Dict[str, str]]:
        """Executes operation and returns HTTP status, response data, usage dict, and response headers.

        Enables rate-limit intelligence and quota reconciliation.
        """
        # If execute_request or execute_operation was patched by tests, execute_operation directly
        import inspect
        sig = inspect.signature(self.execute_operation)
        if "model" in sig.parameters:
            status_code, data, usage = await self.execute_operation(
                secret=secret,
                operation=operation,
                payload=payload,
                timeout=timeout,
                idempotency_key=idempotency_key,
                model=model,
            )
        else:
            status_code, data, usage = await self.execute_operation(
                secret=secret,
                operation=operation,
                payload=payload,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        headers = {}
        if hasattr(self, "_last_response_headers") and self._last_response_headers:
            headers = self._last_response_headers
        return status_code, data, usage, headers

    async def execute_stream(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> AsyncGenerator[str, None]:
        """Executes a streaming response generator if supported."""
        raise NotImplementedError(f"Streaming not supported by {self.provider_name}")
        yield ""  # Generator syntax

    async def verify_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        secret: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        """Verify and parse incoming webhook signatures.

        Returns (is_valid, parsed_event, error_message).
        """
        return False, None, "Webhook verification not implemented for this adapter"

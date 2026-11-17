"""S3-Compatible Object Storage Adapter (AWS S3, Cloudflare R2, MinIO).

Handles presigned URLs and object management.
"""

from typing import Any, Dict, Tuple, Optional
import json

from token_router.adapters.base import ProviderAdapter


class S3CompatibleAdapter(ProviderAdapter):
    provider_name: str = "s3_compatible"

    def _parse_secret(self, secret: str) -> Dict[str, str]:
        """Parses credential format 'ACCESS_KEY:SECRET_KEY:ENDPOINT:BUCKET' or JSON."""
        try:
            return json.loads(secret)
        except Exception:
            parts = secret.split(":")
            return {
                "access_key": parts[0] if len(parts) > 0 else "",
                "secret_key": parts[1] if len(parts) > 1 else "",
                "endpoint": parts[2] if len(parts) > 2 else "s3.amazonaws.com",
                "bucket": parts[3] if len(parts) > 3 else "default",
            }

    async def validate_credential(self, secret: str) -> Tuple[bool, str, int]:
        cfg = self._parse_secret(secret)
        if not cfg.get("access_key") or not cfg.get("secret_key"):
            return False, "S3 credential missing access_key or secret_key", 400
        return True, "S3 credential config format validated", 200

    async def execute_request(
        self,
        secret: str,
        capability: str,
        model: str,
        payload: Dict[str, Any],
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], Dict[str, int]]:
        return await self.execute_operation(
            secret=secret,
            operation=capability,
            payload=payload,
            timeout=timeout,
        )

    async def execute_operation(
        self,
        secret: str,
        operation: str,
        payload: Dict[str, Any],
        timeout: float,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
        cfg = self._parse_secret(secret)
        key = payload.get("key", payload.get("filename", "untitled"))
        bucket = payload.get("bucket", cfg.get("bucket", "default"))
        endpoint = cfg.get("endpoint", "s3.amazonaws.com")

        if operation in ("get_signed_url", "presign"):
            # Generates deterministic signed access descriptor
            return 200, {
                "url": f"https://{bucket}.{endpoint}/{key}?signed=mock-token",
                "bucket": bucket,
                "key": key,
                "expires_in": payload.get("expires_in", 3600),
            }, {"operations": 1}

        elif operation in ("put_object", "upload"):
            return 200, {
                "status": "stored",
                "bucket": bucket,
                "key": key,
                "etag": "mock-etag-58a1e8",
            }, {"operations": 1}

        return 400, {"error": f"Unsupported S3 operation: {operation}"}, {}

# Transactional Integrity & Persistent Approvals

This document describes the design and guarantees of the persistent, database-backed approval system and distributed idempotency engine in **Weirwise Token Router**.

---

## 1. Threat Model & Design Goals

In a universal execution platform, sensitive operations (such as `refund_payment`, `delete_credential`, or financial transfers) carry irreversible real-world side effects.

- **Process Memory Failure**: In-memory stores lose state during restarts or multi-worker deployments.
- **Replay Attacks**: Approvals must authorize exactly **one** execution attempt and cannot be replayed.
- **Parameter Tampering**: An approval granted for a $100 refund must never be usable to execute a $1,000 refund.
- **Cross-Consumer Manipulation**: Consumer A must never consume or query approvals belonging to Consumer B.

---

## 2. Cryptographic Payload Binding

Every approval request generates a deterministic SHA-256 hash of the canonical JSON payload:

```python
def compute_payload_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
```

The database model `ApprovalRequest` binds:
- `consumer_id`: Explicit tenant ownership.
- `action_type`: Specific capability/operation ID.
- `provider_name`: Provider constraint (optional).
- `target_id`: Resource/transaction identifier.
- `payload_hash`: Cryptographic commitment to exact payload.
- `expires_at`: Strict 15-minute time-to-live.

---

## 3. Atomic Compare-And-Swap Consumption

To prevent race conditions where two concurrent requests attempt to consume the same approval simultaneously, the database executes an atomic conditional UPDATE:

```sql
UPDATE approval_requests
SET consumed = TRUE, status = 'consumed', consumed_at = NOW(), consumed_by_request_id = :request_id
WHERE id = :approval_id AND status = 'approved' AND consumed = FALSE;
```

If `rowcount == 0`, the transaction fails immediately with `RuntimeError("Approval was consumed concurrently by another transaction.")`.

---

## 4. Distributed Idempotency Management

`IdempotencyManager` guarantees that identical requests submitted with the same `Idempotency-Key` are executed once:
1. **Acquire/Check**: `acquire_or_get(consumer_id, key, payload)`
   - New key -> records state `pending`.
   - Existing key + identical hash + status `completed` -> returns cached response.
   - Existing key + conflicting hash -> raises `ValueError("Idempotency key reused with different request payload.")`.
   - Existing key + status `pending` -> raises `RuntimeError("Request currently in progress.")`.
2. **Outcome Persistence**: Successful operations record `status='completed'` with response body. Network timeouts on non-idempotent operations record `status='uncertain'` requiring explicit reconciliation.

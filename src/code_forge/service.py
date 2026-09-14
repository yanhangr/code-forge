"""Reference application service: authoritative ordering for Run acceptance."""

import hashlib
import json
from dataclasses import asdict, replace

from .contracts import AcceptedRun, DomainError, ErrorCode, RunRequest
from .ports import AuthorizationPort, RunRepository, SnapshotResolver


def request_fingerprint(request: RunRequest) -> str:
    # Transport adapter must validate shape first. Preserve input whitespace/order:
    # silently normalizing code or Skill precedence changes user intent.
    encoded = json.dumps(asdict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RunService:
    def __init__(
        self,
        repository: RunRepository,
        resolver: SnapshotResolver,
        authorization: AuthorizationPort,
    ):
        self.repository = repository
        self.resolver = resolver
        self.authorization = authorization

    async def submit(
        self,
        request: RunRequest,
        idempotency_key: str,
        *,
        reject_if_active: bool = False,
    ) -> AcceptedRun:
        if not 1 <= len(idempotency_key) <= 200 or not 1 <= len(request.input) <= 100_000:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Invalid request key or input size")
        await self.authorization.check(request.context, "run.submit", request.session_id)
        await self.repository.require_session(request.context.scope_id, request.session_id)
        fingerprint = request_fingerprint(request)
        existing = await self.repository.find_request(
            request.context.scope_id, request.session_id, idempotency_key
        )
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_CONFLICT, "Key is already bound to different input"
                )
            return replace(existing.run, reused=True)
        # Duplicate requests return before resolution: never pick up a newer Skill.
        snapshot = await self.resolver.resolve_and_store(request)
        # Repository owns concurrent dedupe and the all-or-nothing acceptance boundary.
        if reject_if_active:
            return await self.repository.accept_once(
                request,
                idempotency_key,
                fingerprint,
                snapshot,
                reject_if_active=True,
            )
        return await self.repository.accept_once(
            request,
            idempotency_key,
            fingerprint,
            snapshot,
        )

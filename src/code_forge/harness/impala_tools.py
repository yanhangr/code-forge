"""Impala dispatch through the existing Runtime tool ledger and output events."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from code_forge.contracts import DomainError, ErrorCode, ExecutionContext, RunStatus, ToolStatus
from code_forge.integrations.impala.service import ImpalaTools, atomic_json, encoded


class ImpalaToolDispatcher:
    def __init__(
        self, tools: ImpalaTools, store: Any, workspace: Any, root: Path, authorization: Any = None
    ):
        self.tools = tools
        self.store = store
        self.workspace = workspace
        self.root = root
        self.authorization = authorization
        # Bounded lock stripes attach concurrent duplicate calls to one ledger operation.
        self._dispatch_locks = [asyncio.Lock() for _ in range(64)]

    async def execute(
        self,
        run: dict[str, Any],
        attempt_id: str,
        call: dict[str, Any],
        actor: str,
        user_binding: Any = None,
    ) -> dict[str, Any]:
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (ValueError, TypeError):
            return self._error("INVALID_REQUEST", "Invalid tool JSON")
        specs = self.tools.specs(run)
        schema = next(
            (s["function"]["parameters"] for s in specs if s["function"]["name"] == name), None
        )
        if schema is None:
            return self._error("CAPABILITY_DENIED", "Impala was not enabled at acceptance")
        # Reject unexpected keys before logging them: no account/credential arguments.
        if not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]):
            return self._error("INVALID_REQUEST", "Invalid Impala arguments")
        input_text = encoded(arguments).decode()
        if (
            len(input_text.encode()) > 40000
            or not isinstance(call.get("id"), str)
            or not 1 <= len(call["id"]) <= 200
        ):
            return self._error("INVALID_REQUEST", "Input exceeds limit or lacks stable call ID")
        slot = (
            hashlib.sha256(encoded([run["scope_id"], run["id"], attempt_id, call["id"]])).digest()[
                0
            ]
            % 64
        )
        async with self._dispatch_locks[slot]:
            return await self._execute_locked(
                run, attempt_id, call, actor, user_binding, name, arguments, input_text
            )

    async def _execute_locked(
        self,
        run: dict[str, Any],
        attempt_id: str,
        call: dict[str, Any],
        actor: str,
        user_binding: Any,
        name: str,
        arguments: dict[str, Any],
        input_text: str,
    ) -> dict[str, Any]:
        scope = run["scope_id"]
        if self.authorization is not None:
            context = ExecutionContext(
                scope_id=scope,
                actor_ref=run.get("execution_context", {}).get("actor_ref"),
                user_binding=user_binding,
            )
            try:
                await self.authorization.check(context, "tool.execute", name)
            except DomainError as exc:
                return self._error(exc.code.value, exc.message)

        def cancelled() -> bool:
            current = self.store.get_run(scope, run["id"])
            return (
                not current
                or current.get("active_attempt_id") != attempt_id
                or current["status"]
                not in {RunStatus.RUNNING.value, RunStatus.WAITING_EXTERNAL.value}
            )

        if cancelled():
            return {"status": "CANCELLED", "reason": "Message is no longer executing"}
        digest = hashlib.sha256(encoded([name, arguments])).hexdigest()
        logical_key = "impala:" + hashlib.sha256(encoded([attempt_id, call["id"]])).hexdigest()
        operation, status = self.store.prepare_tool(
            scope_id=scope,
            run_id=run["id"],
            logical_call_key=logical_key,
            tool_ref=name,
            params_digest=digest,
            input_ref=f"impala-input:{logical_key}",
            execution_profile_ref="impala@1",
            input_summary=name,
            actor=actor,
            workspace_id=run["workspace_id"],
            input_text=input_text,
        )
        if user_binding is not None:
            directory = self.workspace.tool_output_dir(
                user_binding,
                run["session_id"],
                run["id"],
                operation,
            )
        else:
            directory = self.root / operation
            directory.mkdir(parents=True, exist_ok=True)
        result_path = directory / "result.json"
        if status != ToolStatus.PREPARED:
            if (
                status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.CANCELLED}
                and result_path.exists()
            ):
                return json.loads(result_path.read_bytes())
            # A RUNNING/UNKNOWN operation is evidence of dispatch, never permission to replay.
            return self._error(
                "STATE_CONFLICT", "Operation requires remote verification", "UNKNOWN"
            )
        (directory / "input.txt").write_text(input_text, encoding="utf-8")
        self.store.start_tool(scope, operation, actor)

        def progress(data: dict[str, Any]) -> None:
            self.store.append_tool_output(
                scope, operation, "stdout", encoded(data).decode(), False, actor
            )

        try:
            result = await self.tools.call(name, arguments, run, operation, cancelled, progress)
        except DomainError as exc:
            result = self._error(exc.code.value, exc.message)
        except Exception:
            result = self._error("EXECUTION_FAILED", "Impala adapter failed", "UNKNOWN")
        status = (
            ToolStatus(result["status"])
            if result["status"]
            in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "UNKNOWN",
            }
            else ToolStatus.SUCCEEDED
        )  # A control tool can return query state CANCELLING/RUNNING.
        # Control-query state is distinct from success/failure of reading that state.
        if name in {"impala_query_status", "impala_cancel_query"} and result.get("query_ref"):
            status = ToolStatus.SUCCEEDED
        try:
            atomic_json(result_path, result)
            (directory / "stdout.log").write_bytes(encoded(result))
            self.store.append_tool_output(
                scope,
                operation,
                "stdout",
                encoded(result).decode(),
                result.get("truncated", False),
                actor,
            )
            self.store.finish_tool(
                scope, operation, status, str(result_path), result.get("error"), actor
            )
        except Exception as exc:
            self.store.finish_tool(
                scope,
                operation,
                ToolStatus.UNKNOWN,
                None,
                {
                    "code": "EXECUTION_FAILED",
                    "message": "Impala result persistence failed",
                },
                actor,
            )
            raise DomainError(
                ErrorCode.EXECUTION_FAILED, "Impala result persistence failed"
            ) from exc
        if status == ToolStatus.CANCELLED:
            current = self.store.get_run(scope, run["id"])
            if current and current["status"] == RunStatus.CANCELLING.value:
                self.store.transition_run(
                    scope,
                    run["id"],
                    expected_version=current["state_version"],
                    target=RunStatus.CANCELLED,
                    actor_ref=actor,
                )
        return result

    @staticmethod
    def _error(code: str, message: str, status: str = "FAILED") -> dict[str, Any]:
        return {"status": status, "error": {"code": code, "message": message}}

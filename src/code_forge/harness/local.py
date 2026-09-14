"""Deterministic local Harness.

This is a small, inspectable tool loop used for adapter and local-flow
verification. It is not Deep Agents/LangGraph and it does not claim to be a
real LLM agent. Real DA/LG integration must be verified separately at L2/L3.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from code_forge.contracts import (
    DomainError,
    EventType,
    RunStatus,
    SkillRef,
    TaskOutcome,
    ToolStatus,
)
from code_forge.execution.local_process_backend import LocalProcessBackend
from code_forge.harness.tooling import build_operation_spec, persist_workspace_commit
from code_forge.persistence.sqlite_store import SqliteRuntimeStore
from code_forge.workspace.store import WorkspaceStore

_PYTHON_BLOCK = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.S)
_SHELL_BLOCK = re.compile(r"```(?:bash|sh|shell)\s*\n(.*?)```", re.S)


class LocalDeterministicHarness:
    """Executes explicit Python/Bash blocks and records tool facts."""

    def __init__(
        self,
        store: SqliteRuntimeStore,
        execution: LocalProcessBackend,
        workspace: WorkspaceStore,
        worker_id: str = "worker-1",
        model_adapter: Any | None = None,
        resolver: Any | None = None,
        max_tool_steps: int = 12,
    ):
        self.store = store
        self.execution = execution
        self.workspace = workspace
        self.worker_id = worker_id
        self.model_adapter = model_adapter
        self.resolver = resolver
        self.max_tool_steps = max_tool_steps

    async def execute(self, claimed: dict[str, Any]) -> dict[str, Any]:
        run = claimed["run"]
        scope_id = run["scope_id"]
        run_id = run["id"]
        attempt_id = claimed["attempt_id"]
        workspace_id = claimed["workspace_id"]
        workspace_epoch = int(claimed.get("workspace_epoch", 0))
        user_binding = claimed.get("user_binding")
        working_directory = claimed.get("working_directory")
        actor = f"system:runtime/{self.worker_id}"
        self.workspace.prepare_attempt(workspace_id, attempt_id, user_binding)
        skill_context = self._activate_skills(run, actor)

        output_parts: list[str] = []
        tool_successes = 0
        tool_failures = 0

        actions = self._plan_actions(run["input"])
        for index, (tool_ref, code) in enumerate(actions):
            current = self.store.get_run(scope_id, run_id)
            if not current or current["status"] == RunStatus.CANCELLING.value:
                break
            logical_key = f"{tool_ref}:{attempt_id}:{index}"
            params_digest = hashlib.sha256((tool_ref + "\0" + code).encode("utf-8")).hexdigest()
            input_ref = f"local-attempt:{attempt_id}:{logical_key}"
            if tool_ref == "python":
                self.workspace.write_text(
                    workspace_id,
                    attempt_id,
                    f"main_{index}.py",
                    code,
                    user_binding,
                )
                argv = ("python3", f"main_{index}.py")
            elif tool_ref == "command":
                self.workspace.write_text(
                    workspace_id,
                    attempt_id,
                    f"command_{index}.sh",
                    code,
                    user_binding,
                )
                argv = ("/bin/bash", f"command_{index}.sh")
            else:
                continue

            operation_id, _ = self.store.prepare_tool(
                scope_id=scope_id,
                run_id=run_id,
                logical_call_key=logical_key,
                tool_ref=tool_ref,
                params_digest=params_digest,
                input_ref=input_ref,
                execution_profile_ref="local@1",
                input_summary=f"{tool_ref} block {index}",
                actor=actor,
                workspace_id=workspace_id,
            )
            self.store.start_tool(scope_id, operation_id, actor)
            spec = build_operation_spec(
                operation_id=operation_id,
                run_id=run_id,
                attempt_id=attempt_id,
                workspace_id=workspace_id,
                workspace_epoch=workspace_epoch,
                user_binding=user_binding,
                working_directory=working_directory,
                argv=argv,
                timeout_seconds=30,
                output_limit_bytes=256 * 1024,
                environment_profile_ref="local@1",
            )
            await self.execution.submit(spec)
            result = await self._wait_for_operation(operation_id)
            self._emit_tool_logs(scope_id, operation_id, result, actor)
            tool_status = result.status
            tool_error = None
            result_revision_id = None
            if tool_status == ToolStatus.SUCCEEDED:
                try:
                    commit = persist_workspace_commit(
                        self.store,
                        result,
                        scope_id=scope_id,
                        workspace_id=workspace_id,
                        attempt_id=attempt_id,
                        workspace_epoch=workspace_epoch,
                        actor=actor,
                    )
                except DomainError as exc:
                    tool_status = ToolStatus.FAILED
                    tool_error = {"code": exc.code.value, "message": exc.message}
                else:
                    result_revision_id = commit.revision_id if commit is not None else None
            if tool_status == ToolStatus.SUCCEEDED:
                tool_successes += 1
                stdout_text = self._read_log(result.stdout_ref)
                if stdout_text.strip():
                    output_parts.append(stdout_text.strip())
                if result_revision_id:
                    self.store.append_event(
                        scope_id,
                        run_id,
                        EventType.WORKSPACE_COMMITTED,
                        {
                            "revision_id": result_revision_id,
                            "changed_paths": list(result.changed_paths),
                        },
                        actor,
                    )
            else:
                tool_failures += 1
                stderr_text = self._read_log(result.stderr_ref)
                if stderr_text.strip():
                    output_parts.append(stderr_text.strip())
                if tool_error is None:
                    tool_error = {
                        "code": (
                            result.error_code.value if result.error_code else "EXECUTION_FAILED"
                        ),
                        "message": self._read_log(result.stderr_ref).strip() or "Tool failed",
                    }
            self.store.finish_tool(
                scope_id,
                operation_id,
                tool_status,
                result.stdout_ref,
                tool_error,
                actor,
                result_revision_id,
            )

        if not actions:
            if self.model_adapter is not None:
                final_text, outcome = await self._run_model_loop(
                    run,
                    attempt_id,
                    workspace_id,
                    workspace_epoch,
                    user_binding,
                    working_directory,
                    actor,
                    skill_context,
                )
            else:
                final_text = (
                    "当前本地运行模式未配置真实模型，因此无法自主规划一般任务。"
                    "请提供 python 或 bash 围栏代码块，或配置 DEEPSEEK_API_KEY 接入模型。"
                )
                outcome = TaskOutcome.BLOCKED
        elif tool_failures and tool_successes == 0:
            final_text = "\n".join(output_parts) or "Tool execution failed."
            outcome = TaskOutcome.BLOCKED
        elif tool_failures:
            final_text = "\n".join(output_parts)
            outcome = TaskOutcome.PARTIAL
        else:
            final_text = "\n".join(output_parts) or "Tool execution completed."
            outcome = TaskOutcome.COMPLETED

        message_id = str(uuid4())
        self.store.append_event(
            scope_id,
            run_id,
            EventType.MESSAGE_COMPLETED,
            {"message_id": message_id, "text": final_text},
            actor,
        )
        current = self.store.get_run(scope_id, run_id)
        if not current or current["status"] != RunStatus.CANCELLING.value:
            return self.store.transition_run(
                scope_id,
                run_id,
                expected_version=int(current["state_version"]) if current else 1,
                target=RunStatus.SUCCEEDED,
                task_outcome=outcome,
                output=final_text,
                actor_ref=actor,
            )
        return current

    async def _run_model_loop(
        self,
        run: dict[str, Any],
        attempt_id: str,
        workspace_id: str,
        workspace_epoch: int,
        user_binding: Any,
        working_directory: str | None,
        actor: str,
        skill_context: list[str],
    ) -> tuple[str, TaskOutcome]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt(skill_context),
            },
            {"role": "user", "content": run["input"]},
        ]
        for _ in range(self.max_tool_steps):
            message = await self.model_adapter.complete(
                messages,
                tools=self._tool_specs(),
            )
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return message.get("content") or "模型未返回文本", TaskOutcome.COMPLETED
            messages.append(dict(message))
            for call in tool_calls:
                function = call.get("function") or {}
                tool_name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if tool_name == "execute_python":
                    code = arguments.get("code", "")
                    status, stdout_text, stderr_text = await self._execute_model_code_tool(
                        run,
                        attempt_id,
                        workspace_id,
                        workspace_epoch,
                        user_binding,
                        working_directory,
                        "python",
                        code,
                        arguments.get("skill") or "analysis-report",
                        actor,
                    )
                elif tool_name == "execute_command":
                    command = arguments.get("command", "")
                    status, stdout_text, stderr_text = await self._execute_model_code_tool(
                        run,
                        attempt_id,
                        workspace_id,
                        workspace_epoch,
                        user_binding,
                        working_directory,
                        "command",
                        command,
                        arguments.get("skill") or "analysis-report",
                        actor,
                    )
                else:
                    status = ToolStatus.FAILED
                    stdout_text = ""
                    stderr_text = f"Unsupported tool: {tool_name}"
                content = (
                    stdout_text
                    or stderr_text
                    or (
                        "Tool executed successfully."
                        if status == ToolStatus.SUCCEEDED
                        else "Tool failed."
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": content[:16000],
                    }
                )
        return "达到最大工具调用步数。", TaskOutcome.PARTIAL

    @staticmethod
    def _tool_specs() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "execute_python",
                    "description": "Run Python code in the current workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "skill": {"type": "string"},
                        },
                        "required": ["code", "skill"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Run a non-interactive shell command.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "skill": {"type": "string"},
                        },
                        "required": ["command", "skill"],
                    },
                },
            },
        ]

    async def _execute_model_code_tool(
        self,
        run: dict[str, Any],
        attempt_id: str,
        workspace_id: str,
        workspace_epoch: int,
        user_binding: Any,
        working_directory: str | None,
        tool_ref: str,
        code: str,
        skill_name: str,
        actor: str,
    ) -> tuple[ToolStatus, str, str]:
        if not code.strip():
            return ToolStatus.FAILED, "", "Tool arguments are empty"
        current = self.store.get_run(run["scope_id"], run["id"])
        if not current or current["status"] == RunStatus.CANCELLING.value:
            return ToolStatus.CANCELLED, "", "Run was cancelled"
        logical_key = f"{tool_ref}:{attempt_id}:{uuid4().hex}"
        params_digest = hashlib.sha256((tool_ref + "\0" + code).encode("utf-8")).hexdigest()
        input_ref = f"local-attempt:{attempt_id}:{logical_key}"
        if tool_ref == "python":
            filename = f"model_{uuid4().hex}.py"
            self.workspace.write_text(
                workspace_id,
                attempt_id,
                filename,
                code,
                user_binding,
            )
            argv = ("python3", filename)
        elif tool_ref == "command":
            filename = f"model_{uuid4().hex}.sh"
            self.workspace.write_text(
                workspace_id,
                attempt_id,
                filename,
                code,
                user_binding,
            )
            argv = ("/bin/bash", filename)
        else:
            return ToolStatus.FAILED, "", f"Unsupported tool: {tool_ref}"

        operation_id, _ = self.store.prepare_tool(
            scope_id=run["scope_id"],
            run_id=run["id"],
            logical_call_key=logical_key,
            tool_ref=tool_ref,
            params_digest=params_digest,
            input_ref=input_ref,
            execution_profile_ref="local@1",
            input_summary=f"model {tool_ref} call",
            actor=actor,
            workspace_id=workspace_id,
        )
        self.store.start_tool(run["scope_id"], operation_id, actor)
        self.store.append_event(
            run["scope_id"],
            run["id"],
            EventType.SKILL_STARTED,
            {
                "name": skill_name,
                "version": self._skill_version(run, skill_name),
                "operation_id": operation_id,
            },
            actor,
        )
        spec = build_operation_spec(
            operation_id=operation_id,
            run_id=run["id"],
            attempt_id=attempt_id,
            workspace_id=workspace_id,
            workspace_epoch=workspace_epoch,
            user_binding=user_binding,
            working_directory=working_directory,
            argv=argv,
            timeout_seconds=30,
            output_limit_bytes=256 * 1024,
            environment_profile_ref="local@1",
        )
        await self.execution.submit(spec)
        result = await self._wait_for_operation(operation_id)
        self._emit_tool_logs(run["scope_id"], operation_id, result, actor)
        tool_status = result.status
        tool_error = None
        result_revision_id = None
        if tool_status == ToolStatus.SUCCEEDED:
            try:
                commit = persist_workspace_commit(
                    self.store,
                    result,
                    scope_id=run["scope_id"],
                    workspace_id=workspace_id,
                    attempt_id=attempt_id,
                    workspace_epoch=workspace_epoch,
                    actor=actor,
                )
            except DomainError as exc:
                tool_status = ToolStatus.FAILED
                tool_error = {"code": exc.code.value, "message": exc.message}
            else:
                result_revision_id = commit.revision_id if commit is not None else None
                if result_revision_id:
                    self.store.append_event(
                        run["scope_id"],
                        run["id"],
                        EventType.WORKSPACE_COMMITTED,
                        {
                            "revision_id": result_revision_id,
                            "changed_paths": list(result.changed_paths),
                        },
                        actor,
                    )
        if tool_status != ToolStatus.SUCCEEDED and tool_error is None:
            tool_error = {
                "code": result.error_code.value if result.error_code else "EXECUTION_FAILED",
                "message": self._read_log(result.stderr_ref).strip() or "Tool failed",
            }
        self.store.finish_tool(
            run["scope_id"],
            operation_id,
            tool_status,
            result.stdout_ref,
            tool_error,
            actor,
            result_revision_id,
        )
        skill_finished_status = (
            tool_status.value
            if tool_status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.CANCELLED}
            else ToolStatus.FAILED.value
        )
        self.store.append_event(
            run["scope_id"],
            run["id"],
            EventType.SKILL_FINISHED,
            {
                "name": skill_name,
                "version": self._skill_version(run, skill_name),
                "operation_id": operation_id,
                "status": skill_finished_status,
            },
            actor,
        )
        return (
            tool_status,
            self._read_log(result.stdout_ref).strip(),
            self._read_log(result.stderr_ref).strip(),
        )

    def _activate_skills(self, run: dict[str, Any], actor: str) -> list[str]:
        skill_refs = run.get("config_snapshot", {}).get("skills", [])
        blocks: list[str] = []
        for item in skill_refs:
            ref = SkillRef(
                name=item["name"],
                version=item["version"],
                digest=item["digest"],
                bundle_ref=item["bundle_ref"],
            )
            self.store.append_event(
                run["scope_id"],
                run["id"],
                EventType.SKILL_ACTIVATED,
                {"name": ref.name, "version": ref.version, "digest": ref.digest},
                actor,
            )
            if self.resolver is not None:
                body = self.resolver.load_skill_body(ref)
                blocks.append(f"## {ref.name}@{ref.version}\n{body}")
        return blocks

    @staticmethod
    def _system_prompt(skill_context: list[str]) -> str:
        base = (
            "你是 Code Forge 的 Agent。你可以调用 execute_python 或 execute_command，"
            "用真实执行结果继续工作。需要计算、读文件、修改文件或生成文件时，"
            "优先用 execute_python；需要运行 shell 命令时用 execute_command。"
            "每个工具调用必须通过 skill 字段声明当前执行的是哪个 Skill。"
            "不要声称未执行的结果已经验证。"
        )
        if not skill_context:
            return base
        return base + "\n\n## Active Skills\n" + "\n\n".join(skill_context)

    @staticmethod
    def _skill_version(run: dict[str, Any], skill_name: str) -> str:
        for item in run.get("config_snapshot", {}).get("skills", []):
            if item.get("name") == skill_name:
                return item.get("version", "")
        return ""

    def _plan_actions(self, text: str) -> list[tuple[str, str]]:
        actions: list[tuple[str, str]] = []
        for match in _PYTHON_BLOCK.finditer(text):
            actions.append(("python", match.group(1)))
        for match in _SHELL_BLOCK.finditer(text):
            actions.append(("command", match.group(1)))
        return actions

    async def _wait_for_operation(self, operation_id: str) -> Any:
        while True:
            result = await self.execution.get_status(operation_id)
            if result.status != ToolStatus.RUNNING:
                return result
            await asyncio.sleep(0.05)

    def _emit_tool_logs(self, scope_id: str, operation_id: str, result: Any, actor: str) -> None:
        for stream, ref in (("stdout", result.stdout_ref), ("stderr", result.stderr_ref)):
            text = self._read_log(ref)
            if not text:
                continue
            self.store.append_tool_output(
                scope_id,
                operation_id,
                stream,
                text[:8192],
                len(text) > 8192,
                actor,
            )

    @staticmethod
    def _read_log(ref: str | None) -> str:
        if not ref:
            return ""
        path = Path(ref)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

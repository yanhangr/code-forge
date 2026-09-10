"""LangGraph-backed Agent Harness.

This adapter uses LangGraph as the graph/runtime loop and DeepSeek as the
model provider. It keeps Runtime-owned facts in the local SQLite store and
uses the same LocalProcessBackend as the deterministic fallback harness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from operator import add
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    EventType,
    OperationSpec,
    RunStatus,
    SkillRef,
    TaskOutcome,
    ToolStatus,
)
from code_forge.execution.local_process_backend import LocalProcessBackend
from code_forge.harness.context import ConversationContextManager
from code_forge.persistence.sqlite_store import SqliteRuntimeStore
from code_forge.workspace.store import WorkspaceStore


class HarnessState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], add]
    run: dict[str, Any]
    attempt_id: str
    workspace_id: str
    actor: str


class LangGraphHarness:
    """LangGraph model/tool loop with DeepSeek tool calls."""

    def __init__(
        self,
        store: SqliteRuntimeStore,
        execution: LocalProcessBackend,
        workspace: WorkspaceStore,
        model_adapter: Any,
        resolver: Any | None = None,
        context_manager: ConversationContextManager | None = None,
        worker_id: str = "worker-1",
        max_tool_steps: int = 12,
    ):
        self.store = store
        self.execution = execution
        self.workspace = workspace
        self.model_adapter = model_adapter
        self.resolver = resolver
        self.context_manager = context_manager or ConversationContextManager()
        self.worker_id = worker_id
        self.max_tool_steps = max_tool_steps
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(HarnessState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("tools", self._tools_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            self._route,
            {"tools": "tools", "finish": END},
        )
        builder.add_edge("tools", "agent")
        return builder.compile(checkpointer=MemorySaver())

    async def execute(self, claimed: dict[str, Any]) -> dict[str, Any]:
        run = claimed["run"]
        scope_id = run["scope_id"]
        run_id = run["id"]
        attempt_id = claimed["attempt_id"]
        workspace_id = claimed["workspace_id"]
        actor = f"system:runtime/{self.worker_id}"
        self.workspace.prepare_attempt(workspace_id, attempt_id)
        skill_context = self._activate_skills(run, actor)

        messages = self.context_manager.build(
            self._system_prompt(skill_context),
            self._conversation_history(run),
            run["input"],
        )
        initial_state = {
            "messages": messages,
            "run": run,
            "attempt_id": attempt_id,
            "workspace_id": workspace_id,
            "actor": actor,
        }
        try:
            result = await self.graph.ainvoke(
                initial_state,
                config={
                    "configurable": {"thread_id": attempt_id},
                    "recursion_limit": self.max_tool_steps + 2,
                },
            )
            final_message = result["messages"][-1]
            final_text = final_message.get("content") or "模型未返回文本"
            outcome = TaskOutcome.COMPLETED if final_message.get("content") else TaskOutcome.BLOCKED
        except DomainError as exc:
            self._finish_failed(scope_id, run_id, actor, exc.code, exc.message)
            return self.store.get_run(scope_id, run_id)  # type: ignore[return-value]
        except Exception as exc:
            self._finish_failed(
                scope_id,
                run_id,
                actor,
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                str(exc) or exc.__class__.__name__,
            )
            return self.store.get_run(scope_id, run_id)  # type: ignore[return-value]

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

    def _conversation_history(self, run: dict[str, Any]) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for previous in self.store.conversation_history(
            run["scope_id"],
            run["session_id"],
            run["id"],
        ):
            history.append({"role": "user", "content": previous["input"]})
            output = previous.get("output")
            if output:
                history.append({"role": "assistant", "content": output})
        return history

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
                {
                    "name": ref.name,
                    "version": ref.version,
                    "digest": ref.digest,
                },
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
            "不要声称未执行的结果已经验证。"
        )
        if not skill_context:
            return base
        return base + "\n\n## Active Skills\n" + "\n\n".join(skill_context)

    async def _agent_node(self, state: dict[str, Any]) -> dict[str, Any]:
        message_id = str(uuid4())
        actor = state["actor"]
        streaming = hasattr(self.model_adapter, "complete_stream")
        print(f"[agent_node] streaming={streaming} run={state['run']['id']}", flush=True)
        if streaming:
            message: dict[str, Any] | None = None
            async for event in self.model_adapter.complete_stream(
                state["messages"],
                tools=self._tool_specs(),
            ):
                if event["type"] == "content":
                    self.store.append_event(
                        state["run"]["scope_id"],
                        state["run"]["id"],
                        EventType.MESSAGE_DELTA,
                        {"message_id": message_id, "text": event["text"]},
                        actor,
                    )
                elif event["type"] == "message":
                    message = event["message"]
            if message is None:
                message = {"role": "assistant", "content": ""}
            print(
                f"[agent_node] final_content_len={len(message.get('content') or '')} "
                f"tool_calls={len(message.get('tool_calls') or [])}",
                flush=True,
            )
            if message.get("content"):
                self.store.append_event(
                    state["run"]["scope_id"],
                    state["run"]["id"],
                    EventType.MESSAGE_COMPLETED,
                    {"message_id": message_id, "text": message["content"]},
                    actor,
                )
            return {"messages": [dict(message)]}
        message = await self.model_adapter.complete(
            state["messages"],
            tools=self._tool_specs(),
        )
        if message.get("content"):
            self.store.append_event(
                state["run"]["scope_id"],
                state["run"]["id"],
                EventType.MESSAGE_COMPLETED,
                {"message_id": message_id, "text": message["content"]},
                actor,
            )
        return {"messages": [dict(message)]}

    async def _tools_node(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state["messages"]
        tool_calls = messages[-1].get("tool_calls") or []
        tool_messages: list[dict[str, Any]] = []
        for call in tool_calls:
            content = await self._execute_tool_call(state, call)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": content[:16000],
                }
            )
        return {"messages": tool_messages}

    @staticmethod
    def _route(state: dict[str, Any]) -> str:
        last = state["messages"][-1]
        return "tools" if last.get("tool_calls") else "finish"

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

    async def _execute_tool_call(self, state: dict[str, Any], call: dict[str, Any]) -> str:
        run = state["run"]
        attempt_id = state["attempt_id"]
        workspace_id = state["workspace_id"]
        actor = state["actor"]
        function = call.get("function") or {}
        tool_name = function.get("name", "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if tool_name == "execute_python":
            return await self._run_code_tool(
                run,
                attempt_id,
                workspace_id,
                "python",
                arguments.get("code", ""),
                arguments.get("skill") or "analysis-report",
                actor,
            )
        if tool_name == "execute_command":
            return await self._run_code_tool(
                run,
                attempt_id,
                workspace_id,
                "command",
                arguments.get("command", ""),
                arguments.get("skill") or "analysis-report",
                actor,
            )
        return f"Unsupported tool: {tool_name}"

    async def _run_code_tool(
        self,
        run: dict[str, Any],
        attempt_id: str,
        workspace_id: str,
        tool_ref: str,
        code: str,
        skill_name: str,
        actor: str,
    ) -> str:
        if not code.strip():
            return "Tool arguments are empty"
        current = self.store.get_run(run["scope_id"], run["id"])
        if not current or current["status"] == RunStatus.CANCELLING.value:
            return "Run was cancelled"
        logical_key = f"{tool_ref}:{attempt_id}:{uuid4().hex}"
        params_digest = hashlib.sha256((tool_ref + "\0" + code).encode("utf-8")).hexdigest()
        input_ref = f"local-attempt:{attempt_id}:{logical_key}"
        if tool_ref == "python":
            filename = f"model_{uuid4().hex}.py"
            self.workspace.write_text(workspace_id, attempt_id, filename, code)
            argv = ("python3", filename)
        elif tool_ref == "command":
            filename = f"model_{uuid4().hex}.sh"
            self.workspace.write_text(workspace_id, attempt_id, filename, code)
            argv = ("/bin/bash", filename)
        else:
            return f"Unsupported tool: {tool_ref}"

        operation_id, _ = self.store.prepare_tool(
            scope_id=run["scope_id"],
            run_id=run["id"],
            logical_call_key=logical_key,
            tool_ref=tool_ref,
            params_digest=params_digest,
            input_ref=input_ref,
            execution_profile_ref="local@1",
            input_summary=f"langgraph {tool_ref} call",
            actor=actor,
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
        spec = OperationSpec(
            operation_id=operation_id,
            run_id=run["id"],
            attempt_id=attempt_id,
            workspace_ref=workspace_id,
            argv=argv,
            timeout_seconds=30,
            output_limit_bytes=256 * 1024,
            environment_profile_ref="local@1",
        )
        await self.execution.submit(spec)
        result = await self._wait_for_operation(operation_id)
        for stream, ref in (("stdout", result.stdout_ref), ("stderr", result.stderr_ref)):
            text = self._read_log(ref)
            if text:
                self.store.append_tool_output(
                    run["scope_id"],
                    operation_id,
                    stream,
                    text[:8192],
                    len(text) > 8192,
                    actor,
                )
        if result.status == ToolStatus.SUCCEEDED and result.workspace_revision:
            self.store.append_event(
                run["scope_id"],
                run["id"],
                EventType.WORKSPACE_COMMITTED,
                {"revision_id": result.workspace_revision, "changed_paths": []},
                actor,
            )
        self.store.finish_tool(
            run["scope_id"],
            operation_id,
            result.status,
            result.stdout_ref,
            None if result.status != ToolStatus.FAILED else {
                "code": result.error_code.value if result.error_code else "EXECUTION_FAILED",
                "message": self._read_log(result.stderr_ref).strip() or "Tool failed",
            },
            actor,
        )
        skill_finished_status = (
            result.status.value
            if result.status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.CANCELLED}
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
        stdout_text = self._read_log(result.stdout_ref).strip()
        stderr_text = self._read_log(result.stderr_ref).strip()
        return stdout_text or stderr_text or (
            "Tool executed successfully."
            if result.status == ToolStatus.SUCCEEDED
            else "Tool failed."
        )

    @staticmethod
    def _skill_version(run: dict[str, Any], skill_name: str) -> str:
        for item in run.get("config_snapshot", {}).get("skills", []):
            if item.get("name") == skill_name:
                return item.get("version", "")
        return ""

    async def _wait_for_operation(self, operation_id: str) -> Any:
        while True:
            result = await self.execution.get_status(operation_id)
            if result.status != ToolStatus.RUNNING:
                return result
            await asyncio.sleep(0.05)

    def _finish_failed(
        self,
        scope_id: str,
        run_id: str,
        actor: str,
        code: ErrorCode,
        message: str,
    ) -> None:
        current = self.store.get_run(scope_id, run_id)
        if not current or current["status"] == RunStatus.CANCELLING.value:
            return
        self.store.transition_run(
            scope_id,
            run_id,
            expected_version=int(current["state_version"]),
            target=RunStatus.FAILED,
            error={"code": code.value, "message": message},
            actor_ref=actor,
        )

    @staticmethod
    def _read_log(ref: str | None) -> str:
        if not ref:
            return ""
        path = Path(ref)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

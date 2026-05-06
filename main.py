from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


ToolName = Literal["list_files", "read_file", "write_file"]
Decision = Literal["allow", "deny", "require_approval"]
Status = Literal["planning", "tool_ready", "observing", "verifying", "done", "blocked"]


@dataclass
class ToolCall:
    name: ToolName
    args: dict[str, Any]
    reason: str


@dataclass
class ToolResult:
    ok: bool
    content: Any
    error: str | None = None


@dataclass
class PolicyDecision:
    decision: Decision
    reasons: list[str] = field(default_factory=list)


@dataclass
class AgentState:
    user_instruction: str
    workspace: Path
    status: Status = "planning"
    step_count: int = 0
    next_tool_call: ToolCall | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str | None = None


class PlannerLLM:
    """LLM role 1: proposes the next action from the current state."""

    marker = "This line was written by the agent component loop demo."

    def propose(self, state: AgentState) -> ToolCall | None:
        known_files = self._last_observation(state, "list_files")
        readme = self._last_observation(state, "read_file", path="README.md")
        wrote_readme = self._last_observation(state, "write_file", path="README.md")

        if known_files is None:
            return ToolCall(
                name="list_files",
                args={"path": "."},
                reason="Inspect workspace before editing.",
            )

        if readme is None and "README.md" in known_files["content"]:
            return ToolCall(
                name="read_file",
                args={"path": "README.md"},
                reason="Read current README before preparing a patch.",
            )

        if wrote_readme is None:
            current = "" if readme is None else str(readme["content"])
            if self.marker in current:
                return None
            updated = current.rstrip() + f"\n\n## Agent note\n\n{self.marker}\n"
            return ToolCall(
                name="write_file",
                args={"path": "README.md", "content": updated},
                reason="Apply the requested documentation update.",
            )

        return None

    def _last_observation(
        self,
        state: AgentState,
        tool_name: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        for item in reversed(state.observations):
            if item.get("tool") != tool_name:
                continue
            if path is not None and item.get("args", {}).get("path") != path:
                continue
            return item
        return None


class VerifierLLM:
    """LLM role 2: verifies whether the observations satisfy the task."""

    def verify(self, state: AgentState) -> tuple[bool, str]:
        for item in reversed(state.observations):
            if (
                item.get("tool") == "read_file"
                and item.get("ok")
                and PlannerLLM.marker in str(item.get("content"))
            ):
                return True, "README.md already contains the agent component note."
        for item in reversed(state.observations):
            if item.get("tool") == "write_file" and item.get("ok"):
                return True, "README.md was updated after policy-gated tool execution."
        return False, "The requested file update has not completed yet."


class PolicyEngine:
    """OPA-like PDP. It decides, but does not execute."""

    def decide(self, call: ToolCall) -> PolicyDecision:
        path = str(call.args.get("path", "."))
        if ".." in Path(path).parts:
            return PolicyDecision("deny", ["path traversal is not allowed"])
        if path.startswith(".env"):
            return PolicyDecision("deny", ["protected file"])
        if call.name == "write_file" and not path.endswith(".md"):
            return PolicyDecision("deny", ["demo only allows markdown writes"])
        return PolicyDecision("allow")


class ToolGateway:
    """PEP + executor. It enforces policy before touching the workspace."""

    def __init__(self, workspace: Path, policy: PolicyEngine) -> None:
        self.workspace = workspace.resolve()
        self.policy = policy

    def execute(self, call: ToolCall) -> dict[str, Any]:
        decision = self.policy.decide(call)
        record: dict[str, Any] = {
            "tool": call.name,
            "args": call.args,
            "reason": call.reason,
            "policy_decision": decision.decision,
            "policy_reasons": decision.reasons,
        }
        if decision.decision != "allow":
            record.update({"ok": False, "error": "; ".join(decision.reasons)})
            return record

        result = self._execute_allowed(call)
        record.update({"ok": result.ok, "content": result.content, "error": result.error})
        return record

    def _execute_allowed(self, call: ToolCall) -> ToolResult:
        path = (self.workspace / str(call.args.get("path", "."))).resolve()
        if not self._is_relative_to(path, self.workspace):
            return ToolResult(False, None, "resolved path escaped workspace")

        if call.name == "list_files":
            files = sorted(p.name for p in self.workspace.iterdir() if p.is_file())
            return ToolResult(True, files)

        if call.name == "read_file":
            return ToolResult(True, path.read_text(encoding="utf-8"))

        if call.name == "write_file":
            path.write_text(str(call.args["content"]), encoding="utf-8")
            return ToolResult(True, {"bytes_written": path.stat().st_size})

        return ToolResult(False, None, f"unsupported tool: {call.name}")

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False


class AgentRuntime:
    """Tiny graph runner: plan -> tool -> observe -> verify -> loop."""

    def __init__(
        self,
        planner: PlannerLLM,
        verifier: VerifierLLM,
        gateway: ToolGateway,
        *,
        max_steps: int = 8,
    ) -> None:
        self.planner = planner
        self.verifier = verifier
        self.gateway = gateway
        self.max_steps = max_steps

    def run(self, state: AgentState) -> AgentState:
        while state.step_count < self.max_steps:
            state.step_count += 1

            call = self.planner.propose(state)
            if call is None:
                state.status = "verifying"
                ok, summary = self.verifier.verify(state)
                state.final_answer = summary
                state.status = "done" if ok else "blocked"
                return state

            state.status = "tool_ready"
            state.next_tool_call = call
            observation = self.gateway.execute(call)
            state.status = "observing"
            state.observations.append(observation)
            state.next_tool_call = None

            if not observation["ok"]:
                state.status = "blocked"
                state.final_answer = f"Blocked: {observation['error']}"
                return state

        state.status = "blocked"
        state.final_answer = "Blocked: max_steps exceeded"
        return state


def main() -> None:
    workspace = Path(__file__).with_name("workspace")
    workspace.mkdir(exist_ok=True)
    readme = workspace / "README.md"
    if not readme.exists():
        readme.write_text("# Demo workspace\n", encoding="utf-8")

    state = AgentState(
        user_instruction="README.mdにAIエージェントの構成メモを追記して",
        workspace=workspace,
    )
    runtime = AgentRuntime(
        planner=PlannerLLM(),
        verifier=VerifierLLM(),
        gateway=ToolGateway(workspace, PolicyEngine()),
    )
    final_state = runtime.run(state)

    print(f"status: {final_state.status}")
    print(f"answer: {final_state.final_answer}")
    print("observations:")
    for item in final_state.observations:
        print(f"- {item['tool']} {item['args']} -> {item['policy_decision']} / ok={item['ok']}")


if __name__ == "__main__":
    main()

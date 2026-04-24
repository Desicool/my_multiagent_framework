"""Agent — an asyncio coroutine: (model, role, ctx) + Anthropic tool-use loop."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from beidou.context import AgentContext


@dataclass
class Agent:
    model: str
    role: str  # "leader" | "member" | "specialist"
    ctx: AgentContext
    system_prompt: str
    max_iterations: int = 50  # safety cap on the tool-use loop

    async def run(self, task: str) -> str:
        await self.ctx.invoke_lifecycle("on_agent_start")

        messages: list[dict] = [{"role": "user", "content": task}]
        tool_schemas = [t.schema() for t in self.ctx.tools()]
        final_text = ""

        try:
            for _ in range(self.max_iterations):
                req: dict = {
                    "model": self.model,
                    "max_tokens": 8096,
                    "system": self.system_prompt,
                    "messages": messages,
                }
                if tool_schemas:
                    req["tools"] = tool_schemas

                resp = await self.ctx.invoke_llm(req)

                # Collect assistant content
                assistant_content = list(resp.content)
                messages.append({"role": "assistant", "content": assistant_content})

                if resp.stop_reason == "end_turn":
                    for block in assistant_content:
                        if hasattr(block, "text"):
                            final_text = block.text
                    break

                if resp.stop_reason == "tool_use":
                    tool_results: list[dict] = []
                    for block in assistant_content:
                        if block.type != "tool_use":
                            continue
                        try:
                            result = await self.ctx.invoke_tool(block.name, block.input)
                        except Exception as exc:
                            # Last-chance safety net — ResilienceLayer should normally
                            # catch this, but we never want an uncaught tool exception
                            # to kill the agent loop.
                            result = {
                                "__tool_error__": True,
                                "message": f"uncaught: {type(exc).__name__}: {exc}",
                            }

                        is_error = isinstance(result, dict) and result.get("__tool_error__") is True
                        if is_error:
                            content = result.get("message", "tool error")
                        elif isinstance(result, str):
                            content = result
                        else:
                            content = json.dumps(result)

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": content,
                                "is_error": is_error,
                            }
                        )
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # Unexpected stop reason — exit loop
                    for block in assistant_content:
                        if hasattr(block, "text"):
                            final_text = block.text
                    break
        finally:
            # Await any non-blocking teams this agent spawned that were never
            # collected via await_team. Prevents orphaned asyncio.Tasks.
            running_teams = self.ctx._kv.get("running_teams") or {}
            if running_teams:
                import asyncio as _asyncio
                pending = [t for t in running_teams.values() if not t.done()]
                if pending:
                    await _asyncio.gather(*pending, return_exceptions=True)
                running_teams.clear()
            await self.ctx.invoke_lifecycle("on_agent_stop", result=final_text)

        return final_text

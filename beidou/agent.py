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
                            result = {"error": str(exc)}

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result) if not isinstance(result, str) else result,
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
            await self.ctx.invoke_lifecycle("on_agent_stop", result=final_text)

        return final_text

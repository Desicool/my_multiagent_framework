"""Example gate handlers for custom-skill.

Gate handlers return ``Pass()`` to allow an operation or ``Block(reason)`` to
deny it. They are fail-closed: any exception is treated as a Block.
"""

from beidou.agent.context import Block, OutputContext, Pass, ToolCallContext, ToolResultContext

_DANGEROUS_PATTERNS = [
    "rm -rf /",
    "> /dev/sda",
    "mkfs.",
    "dd if=",
    ":(){ :|:& };:",  # fork bomb
    "chmod 777 /",
    "DROP TABLE",
    "DELETE FROM",
    "curl http://169.254.169.254",  # cloud metadata
]


async def security_check(ctx: ToolCallContext) -> Pass | Block:
    """Block tool calls containing dangerous patterns."""
    tool_input_str = str(ctx.tool_input).lower()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.lower() in tool_input_str:
            return Block(reason=f"blocked dangerous pattern: {pattern}")
    return Pass()


async def result_inspector(ctx: ToolResultContext) -> Pass | Block:
    """Inspect tool results for signs of credential leakage."""
    if ctx.is_error:
        return Pass()

    result_str = str(ctx.result)
    suspicious = [
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "github_pat_",
        "ghp_",
        "sk-ant-",
        "AIzaSy",
    ]
    for pattern in suspicious:
        if pattern in result_str:
            return Block(
                reason=f"credential leakage detected: {pattern[:20]}..."
            )
    return Pass()


async def output_guard(ctx: OutputContext) -> Pass | Block:
    """Block agent output containing sensitive information."""
    if "api_key" in ctx.output_text.lower() and len(ctx.output_text) > 200:
        return Block(reason="output may contain credentials")
    return Pass()

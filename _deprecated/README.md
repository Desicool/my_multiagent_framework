# Quarantined modules (pre-SDK era)

These modules are NOT on the runtime path. Do not import from these paths in
any beidou/* production code. They predate Beidou's switch to the
claude_agent_sdk and remain only so external scripts that may still import
them have a transition window. Slated for deletion after one release.

- templates/ — legacy YAML agent templates, replaced by SKILL.md.
- context.py — old AgentContext / BaseLayer protocol, replaced by direct SDK options.
- layers/ — old interceptor protocol, replaced by per-spawn MCP servers.
- tools/ — old BaseTool implementations, replaced by SDK builtins + Beidou primitives.

# agent_sdk docs

Scraped 2026-05-19 from official Anthropic docs.

- **cost-tracking.md** — Track cost and usage
  > The Claude Agent SDK provides detailed token usage information for each interaction with Claude. This guide explains how to properly track usage and understand cost reporting, especially when dealing 
- **custom-tools.md** — Give Claude custom tools
  > Custom tools extend the Agent SDK by letting you define your own functions that Claude can call during a conversation. Using the SDK's in-process MCP server, you can give Claude access to databases, e
- **hooks.md** — Intercept and control agent behavior with hooks
  > Hooks are callback functions that run your code in response to agent events, like a tool being called, a session starting, or execution stopping. With hooks, you can:
- **mcp.md** — Connect to external tools with MCP
  > The [Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/getting-started/intro) is an open standard for connecting AI agents to external tools and data sources. With MCP, your agent can
- **modifying-system-prompts.md** — Modifying system prompts
  > System prompts define Claude's behavior, capabilities, and response style. Start from the `claude_code` preset for CLI or IDE-like coding tools where a human watches and steers the work. Write your ow
- **overview.md** — Agent SDK overview
  > Build AI agents that autonomously read files, run commands, search the web, edit code, and more. The Agent SDK gives you the same tools, agent loop, and context management that power Claude Code, prog
- **permissions.md** — Configure permissions
  > The Claude Agent SDK provides permission controls to manage how Claude uses tools. Use permission modes and rules to define what's allowed automatically, and the [`canUseTool` callback](/en/agent-sdk/
- **python-sdk-readme.md** — Claude Agent SDK for Python
  > Python SDK for Claude Agent. See the [Claude Agent SDK documentation](https://platform.claude.com/docs/en/agent-sdk/python) for more information.
- **python.md** — Agent SDK reference - Python
  > pip install claude-agent-sdk
- **skills.md** — Agent Skills in the SDK
  > Agent Skills extend Claude with specialized capabilities that Claude autonomously invokes when relevant. Skills are packaged as `SKILL.md` files containing instructions, descriptions, and optional sup
- **slash-commands.md** — Slash Commands in the SDK
  > Slash commands provide a way to control Claude Code sessions with special commands that start with `/`. These commands can be sent through the SDK to perform actions like compacting context, listing c
- **streaming-vs-single-mode.md** — Streaming Input
  > The Claude Agent SDK supports two distinct input modes for interacting with agents:
- **subagents.md** — Subagents in the SDK
  > Subagents are separate agent instances that your main agent can spawn to handle focused subtasks.
- **todo-tracking.md** — Todo Lists
  > Todo tracking provides a structured way to manage tasks and display progress to users. The Claude Agent SDK includes built-in todo functionality that helps organize complex workflows and keep users in
- **typescript-sdk-readme.md** — Claude Agent SDK
  > The Claude Agent SDK enables you to programmatically build AI agents with Claude Code's capabilities. Create autonomous agents that can understand codebases, edit files, run commands, and execute comp
- **typescript.md** — Agent SDK reference - TypeScript
  > npm install @anthropic-ai/claude-agent-sdk

# Session event stream

Communication with Claude Managed Agents is event-based. You send user events to the agent, and receive agent and session events back to track status.

All Managed Agents API requests require the `managed-agents-2026-04-01` beta header. The SDK sets the beta header automatically.

## Event types

Events flow in two directions.

- **User events** are what you send to the agent to kick off a session and steer it as it progresses.
- **Session events**, **span events**, and **agent events** are sent to you for observability into your session state and agent progress.

Event type strings follow a `{domain}.{action}` naming convention. See [Event types](https://platform.claude.com/docs/en/managed-agents/reference#event-types) in the reference for the full catalog.

Every event includes a `processed_at` timestamp indicating when the event was recorded server-side. If `processed_at` is null, it means the event has been queued by the harness and is handled after preceding events finish processing.

## Integrating events

Send a `user.message` event to start or continue the agent's work:

```
client.beta.sessions.events.send(
    session.id,
    events=[
        {
            "type": "user.message",
            "content": [
                {
                    "type": "text",
                    "text": "Analyze the performance of the sort function in utils.py",
                },
            ],
        },
    ],
)
```

Send a `user.interrupt` event to stop the agent mid-execution, then follow up with a `user.message` event to redirect it:

```
# Agent is currently analyzing a file...
# Interrupt with a new direction:
client.beta.sessions.events.send(
    session.id,
    events=[
        {"type": "user.interrupt"},
        {
            "type": "user.message",
            "content": [
                {
                    "type": "text",
                    "text": "Instead, focus on fixing the bug in line 42.",
                },
            ],
        },
    ],
)
```

The agent acknowledges the interruption and switches to the new task.

## Additional scenarios

### Handling custom tool calls

When the agent invokes a [custom tool](https://platform.claude.com/docs/en/managed-agents/tools#custom-tools):

1. The session emits an `agent.custom_tool_use` event containing the tool name and input.
2. The session pauses with a `session.status_idle` event containing `stop_reason: requires_action`. The blocking event IDs are in the `stop_reason.event_ids` array.
3. Execute the tool in your system and send a `user.custom_tool_result` event for each, passing the event ID in the `custom_tool_use_id` param along with the result content.
4. Once all blocking events are resolved, the session transitions back to `running`.

```
with client.beta.sessions.events.stream(session.id) as stream:
    for event in stream:
        if event.type == "session.status_idle" and (stop := event.stop_reason):
            match stop.type:
                case "requires_action":
                    for event_id in stop.event_ids:
                        # Look up the custom tool use event and execute it
                        tool_event = events_by_id[event_id]
                        result = call_tool(tool_event.name, tool_event.input)

                        # Send the result back
                        client.beta.sessions.events.send(
                            session.id,
                            events=[
                                {
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": event_id,
                                    "content": [{"type": "text", "text": result}],
                                },
                            ],
                        )
                case "end_turn":
                    break
```

### Tool confirmation

When a [permission policy](https://platform.claude.com/docs/en/managed-agents/permission-policies) requires confirmation before a tool executes:

1. The session emits an `agent.tool_use` or `agent.mcp_tool_use` event.
2. The session pauses with a `session.status_idle` event containing `stop_reason: requires_action`. The blocking event IDs are in the `stop_reason.event_ids` array.
3. Send a `user.tool_confirmation` event for each, passing the event ID in the `tool_use_id` param. Set `result` to `"allow"` or `"deny"`. Use `deny_message` to explain a denial.
4. Once all blocking events are resolved, the session transitions back to `running`.

```
with client.beta.sessions.events.stream(session.id) as stream:
    for event in stream:
        if event.type == "session.status_idle" and (stop := event.stop_reason):
            match stop.type:
                case "requires_action":
                    for event_id in stop.event_ids:
                        # Approve the pending tool call
                        client.beta.sessions.events.send(
                            session.id,
                            events=[
                                {
                                    "type": "user.tool_confirmation",
                                    "tool_use_id": event_id,
                                    "result": "allow",
                                },
                            ],
                        )
                case "end_turn":
                    break
```

### Resuming an idle session

Sessions persist between interactions. Conversation history is preserved unless the session is explicitly deleted. When a session goes idle, its sandbox is checkpointed, preserving the full sandbox state, including the filesystem, installed packages, and any files the agent created. This allows you to resume cleanly from inactivity.

While session history is persisted until deleted, checkpoints are only preserved for 30 days after the session's last activity. If your workflow requires the full sandbox state (files, installed tools, and so on) to persist beyond 30 days, send periodic `user.message` events to reset the inactivity timer before the checkpoint expires.

To resume a session, send a `user.message` event to it as usual:

```
# Resume a previously created session by ID
client.beta.sessions.events.send(
    "sesn_01...",
    events=[
        {
            "type": "user.message",
            "content": [
                {
                    "type": "text",
                    "text": "Now run the tests against the changes you made earlier.",
                },
            ],
        },
    ],
)
```

### Tracking usage

The session object includes a `usage` field with cumulative token statistics. Fetch the session after it goes idle to read the latest totals, and use them to track costs, enforce budgets, or monitor consumption.

```
{
  "id": "sesn_01...",
  "status": "idle",
  "usage": {
    "input_tokens": 5000,
    "output_tokens": 3200,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 20000
  }
}
```

`input_tokens` reports uncached input tokens and `output_tokens` reports total output tokens across all model calls in the session. The `cache_creation_input_tokens` and `cache_read_input_tokens` fields reflect prompt caching activity. Cache entries use a 5-minute TTL, so back-to-back turns within that window benefit from cache reads, which reduce per-token cost.

## Console observability

The Console provides a visual timeline view of your agent sessions. Navigate to the Claude Managed Agents section in the Console to see:

- **Session list:** All sessions with their status, creation time, and model
- **Tracing view:** A chronological view of events (content, timestamps, token usage) within a session. Tracing views are only accessible to Developers and Admins.
- **Tool execution:** Details of each tool call and its result

## Debugging tips

- **Check session events:** Session errors are conveyed through the `session.error` event
- **Review tool results:** Tool execution failures often explain unexpected agent behavior
- **Track token usage:** Monitor token consumption to optimize prompts and reduce costs
- **Use system prompts:** Add logging instructions to the system prompt to make the agent explain its reasoning

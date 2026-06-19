# Maze Boundary

Maze is a distributed workflow runtime for LLM agent applications.

The public boundary is:

```text
Maze = Core Runtime + Workflow Agent + Workflow Workbench
```

This boundary keeps Maze focused on distributed workflow execution instead of becoming a general-purpose Agent SDK, Skills host, MCP playground, tool marketplace, or workspace chat product.

## Core Runtime

Core Runtime is the execution plane. It owns:

- Static DAG and dynamic DAG representation.
- Workflow and task validation.
- Task-level scheduling.
- Resource-aware placement.
- Worker execution.
- Retry, timeout, cancel, and failure propagation.
- Run state, task state, events, logs, and artifacts.
- Cluster resources, queue diagnostics, worker health, and task placement observability.
- Local LLM or inference engine lifecycle when used as schedulable runtime resources.

Dynamic workflow means runtime append-only DAG expansion. New tasks, edges, and sub-DAGs must be validated as a `WorkflowPatch` or equivalent structured patch before they enter the scheduler.

## Workflow Agent

Workflow Agent is an authoring helper, not a general chat or tool-calling agent.

It may:

- Generate `TaskSpec` and task code.
- Generate static `WorkflowSpec` DAGs.
- Generate `WorkflowPatch` objects from runtime results.
- Repair invalid workflow specs or patches from validation errors.
- Suggest task resources, dependencies, retry policy, timeout policy, and artifact policy.

It must not:

- Execute tools directly.
- Call MCP directly.
- Load skills as an application framework.
- Maintain long-term chat memory.
- Bypass the scheduler.
- Run an open-ended ReAct or generic agent loop as a Maze core feature.

The rule is:

```text
Agent proposes.
Core validates.
Scheduler dispatches.
Worker executes.
Workbench observes.
```

## Workflow Workbench

Workflow Workbench is the human control surface for Maze workflows.

It should focus on:

- DAG visualization.
- Manual DAG editing.
- `WorkflowSpec` and `WorkflowPatch` validation.
- Submit, retry, cancel, and run inspection.
- Runtime-expanded dynamic DAG display.
- Task placement.
- Worker and node state.
- CPU, GPU, memory, and queue views.
- Run timelines, logs, and artifacts.

It is not a general Agent playground, Skills playground, MCP playground, workspace chat, or code assistant product.

## Non-Goals

Maze Core does not aim to provide:

- A general Agent SDK.
- A Skills host.
- An MCP hosting platform.
- A tool marketplace.
- Workspace chat sessions.
- Prompt or skill marketplaces.
- General-purpose code-agent execution.

Application-level integrations can live as examples, extensions, or legacy modules, but they should not appear in the default README, public imports, CLI help, or Workbench first-run path.


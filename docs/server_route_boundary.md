# Server Route Boundary

Maze server routes are part of the public boundary only when they serve Core Runtime, Workflow Agent authoring, or Workflow Workbench observability.

The Workbench backend should expose these mainline route families:

- `/api/workspaces` and `/api/workspaces/*` for workspace selection used by workflow authoring.
- `/api/system-catalog` for Maze-native workflow and task catalog entries only.
- `/api/workspace-tasks` for user task files.
- `/api/workspace-workflows` for saved Workflow Workbench DAGs.
- `/api/workflows` for Workbench workflow drafts, validation, and execution.
- `/api/dynamic-runs` for dynamic workflow run state and append events.
- `/api/workflow-runs/static` for static workflow run history.
- `/api/runs` for unified run/task/event/log/artifact inspection.
- `/api/artifacts` for artifact metadata, downloads, promotion, and cleanup.
- `/api/cluster/resources` and `/api/cluster/queues` for resource and queue observability.
- `/api/llm/test` and `/api/llm/generate-task` only as workflow authoring helpers that produce Maze task code.
- `/api/parse-custom-function` for extracting Maze task metadata from user task code.

These route families are not part of the Maze public boundary:

- `/api/workspace-skills`
- `/api/mcp/*`
- `/api/agent/*`
- Generic ReAct run hosts
- Workspace chat sessions
- Skills or MCP discovery playground routes

Phase 1 removes the Skills, MCP, and Workspace Agent public routes. Any remaining helper code behind those old concepts is legacy/internal and must not be reintroduced into README, CLI help, public imports, or the Workbench first-run path.

Workflow Agent remains a mainline concept, but a future route for it must only produce `WorkflowSpec`, `WorkflowPatch`, `TaskSpec`, or `ResourceSpec` for Core validation and scheduler execution.

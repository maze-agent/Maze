# ReAct Workflow Examples

These examples show the first built-in agent workflow template on top of Maze `DynamicRun`.

`ReActWorkflow` keeps Maze focused on distributed task execution while making agent loops easy to run:

- The LLM decision is a normal Maze `@task`.
- Each tool is a normal Maze `@task`.
- Every decision, action, observation, repair, and final answer is persisted in the DynamicRun event log.
- The Playground Dynamic Runs inspector can show the agent trace alongside the task graph.

This gives users a practical ReAct loop without adding ReAct-specific concepts to the core scheduler.

## Start Maze

Use the project environment:

```bash
source /home/gujing/miniconda3/etc/profile.d/conda.sh
conda activate maze
```

Start the core service:

```bash
python -m maze.cli.cli start --head --port 8000 --ray-head-port 6379
```

To inspect traces in the Playground, also start:

```bash
cd web/maze_playground/backend
node src/server.js
```

```bash
cd web/maze_playground/frontend
/usr/bin/node node_modules/vite/bin/vite.js --host 0.0.0.0
```

Open `http://localhost:5173`, then use the Dynamic Runs inspector.

## Local Demo

Run the deterministic local demo first. It does not require an API key:

```bash
python examples/react_workflow/local_repair_demo.py
```

The demo intentionally makes a bad first decision by selecting a tool that is not registered. `ReActWorkflow` records a repair observation, sends that history to the next decision task, runs the calculator tool, and then finalizes the answer.

Expected behavior:

- Step 1 records `agent_repair_observation` for `tool_not_allowed`.
- Step 2 runs the `calculator` task.
- Step 3 emits `agent_final`.

## OpenAI-Compatible LLM Demo

Run the online demo when you have an OpenAI-compatible endpoint:

```bash
python examples/react_workflow/openai_compatible.py
```

The example reads a local config file:

```json
{
  "url": "https://api.example.com/v1",
  "key": "YOUR_API_KEY",
  "model": "provider/model-name"
}
```

You can also build the LLM task with `api_key_env` so the key stays in the environment instead of a file.

## Minimal Pattern

```python
from maze import MaClient, task


@task
def decide(prompt: str, history: list, tools: dict, step: int):
    if not history:
        return {"action": {"tool": "multiply", "args": {"a": 18, "b": 7}}}
    result = history[-1]["observation"]["result"]["result"]
    return {"action": {"final": f"The answer is {result}."}}


@task
def multiply(a: int, b: int):
    return {"result": a * b}


client = MaClient("http://localhost:8000")
react = client.create_react_workflow(
    llm_task=decide,
    tools=[multiply],
    max_steps=3,
)
answer = react.run("Use the calculator to compute 18 * 7.")
```

The decision task may accept any subset of:

- `prompt`
- `history`
- `tools`
- `step`
- `system_prompt`

It should return one of:

```python
{"action": {"tool": "tool_name", "args": {...}}}
```

```python
{"action": {"final": "answer"}}
```

Direct `{"tool": ...}` and `{"final": ...}` payloads are accepted too.

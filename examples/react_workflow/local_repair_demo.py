import ast
import json
import operator
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from maze import MaClient, task


OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def evaluate_arithmetic(expression: str):
    node = ast.parse(expression, mode="eval").body

    def visit(current):
        if isinstance(current, ast.Constant) and isinstance(current.value, (int, float)):
            return current.value
        if isinstance(current, ast.BinOp) and type(current.op) in OPERATORS:
            return OPERATORS[type(current.op)](visit(current.left), visit(current.right))
        if isinstance(current, ast.UnaryOp) and type(current.op) in OPERATORS:
            return OPERATORS[type(current.op)](visit(current.operand))
        raise ValueError(f"Unsupported arithmetic expression: {expression}")

    return visit(node)


@task(resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0})
def local_react_decide(prompt: str, history: list, tools: dict, step: int):
    if not history:
        return {
            "action": {
                "tool": "web_search",
                "args": {
                    "query": "18 * 7",
                },
            }
        }

    last_observation = history[-1]["observation"]
    if last_observation.get("error_type") == "tool_not_allowed":
        return {
            "action": {
                "tool": "calculator",
                "args": {
                    "expression": "18 * 7",
                },
            }
        }

    result = last_observation["result"]["result"]
    return {
        "action": {
            "final": f"The answer is {result}.",
        }
    }


@task(resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0})
def calculator(expression: str):
    result = evaluate_arithmetic(expression)
    return {"result": result}


def main():
    client = MaClient("http://localhost:8000")
    react = client.create_react_workflow(
        llm_task=local_react_decide,
        tools=[calculator],
        max_steps=4,
        timeout_seconds=60,
        task_timeout=60,
    )

    answer = react.run("Use the calculator to compute 18 * 7.")
    print(json.dumps({
        "run_id": react.run_id,
        "answer": answer,
        "status": react.status()["status"],
        "events": [
            event["type"]
            for event in react.get_events()
            if event["type"].startswith("agent_") or event["type"].startswith("react_")
        ],
    }, indent=2))


if __name__ == "__main__":
    main()

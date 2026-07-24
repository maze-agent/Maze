"""Qwen3-4B heterogeneous service workload for the C14E Ascend study."""

from __future__ import annotations

from ascend_maze import Workflow, task


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 128})
def normalize_prompt(prompt: str) -> dict[str, object]:
    normalized = " ".join(prompt.split())
    return {"normalized_prompt": normalized}


@task(
    task_kind="io",
    resources={"cpu_num": 1, "mem": 128, "io_num": 1},
)
def prepare_request(normalized_prompt: str, max_tokens: int) -> dict[str, object]:
    encoded_size = len(normalized_prompt.encode("utf-8"))
    return {
        "prepared_prompt": normalized_prompt,
        "max_tokens": max_tokens,
        "input_bytes": encoded_size,
    }


@task(
    task_kind="npu",
    resources={"cpu_num": 1, "mem": 512},
    max_retries=0,
)
def invoke_qwen(prepared_prompt: str, max_tokens: int) -> dict[str, object]:
    from ascend_maze.inference import chat

    response = chat(
        [{"role": "user", "content": prepared_prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return {
        "response_text": response.text,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 128})
def finalize_response(
    response_text: str,
    input_tokens: int,
    output_tokens: int,
    input_bytes: int,
) -> dict[str, object]:
    import hashlib

    response_digest = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    return {
        "response_digest": response_digest,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_bytes": input_bytes,
    }


def build() -> Workflow:
    workflow = Workflow("c14e-qwen3-4b-service")
    prompt = workflow.input("prompt")
    max_tokens = workflow.input("max_tokens")
    normalized = workflow.add_task(
        normalize_prompt,
        task_name="normalize_prompt",
        inputs={"prompt": prompt},
    )
    prepared = workflow.add_task(
        prepare_request,
        task_name="prepare_request",
        inputs={
            "normalized_prompt": normalized.outputs["normalized_prompt"],
            "max_tokens": max_tokens,
        },
    )
    inferred = workflow.add_task(
        invoke_qwen,
        task_name="invoke_qwen",
        model_anchor={"model": "qwen3-4b", "mode": "service"},
        inputs={
            "prepared_prompt": prepared.outputs["prepared_prompt"],
            "max_tokens": prepared.outputs["max_tokens"],
        },
    )
    workflow.add_task(
        finalize_response,
        task_name="finalize_response",
        inputs={
            "response_text": inferred.outputs["response_text"],
            "input_tokens": inferred.outputs["input_tokens"],
            "output_tokens": inferred.outputs["output_tokens"],
            "input_bytes": prepared.outputs["input_bytes"],
        },
    )
    return workflow

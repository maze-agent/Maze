"""Ascend-Maze-native OpenAGI complex multimodal VQA workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.openagi._common import (
    aggregate_feature_dicts,
    batch_feature_summary,
    chat_image_prompt_batch,
    image_records_with_features,
    list_inline_images,
    metadata_dict,
    multimodal_vqa_prompt,
    split_four_20_20_20_40,
)

SPEC = WorkflowSpec(
    name="maze-openagi-multimodal-vqa-complex",
    source="openagi",
    kind="multimodal_vqa_complex",
    nodes=nodes(
        (
            ("task1_start_receive_task", "io", None),
            ("task2_read_file", "cpu", None),
            ("task3_file_process", "cpu", None),
            ("task4a_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task4b_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task4c_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task4d_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task4_merge_results", "io", None),
            ("task5_output_final_answer", "io", None),
        )
    ),
    edges=edges(
        (
            ("task1_start_receive_task", "task2_read_file"),
            ("task2_read_file", "task3_file_process"),
            ("task3_file_process", "task4a_vlm_process"),
            ("task3_file_process", "task4b_vlm_process"),
            ("task3_file_process", "task4c_vlm_process"),
            ("task3_file_process", "task4d_vlm_process"),
            ("task4a_vlm_process", "task4_merge_results"),
            ("task4b_vlm_process", "task4_merge_results"),
            ("task4c_vlm_process", "task4_merge_results"),
            ("task4d_vlm_process", "task4_merge_results"),
            ("task4_merge_results", "task5_output_final_answer"),
        )
    ),
)

INPUTS = spec_inputs()


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task1_start_receive_task(
    dag_id: str,
    question: str,
    answer: str = "",
    supplementary_files: object = None,
    metadata: object = None,
) -> dict[str, object]:
    if not question:
        raise ValueError(f"task {dag_id} question field is empty")
    normalized_metadata = metadata_dict(metadata)
    return {
        "dag_id": dag_id,
        "question": question,
        "answer": answer,
        "supplementary_files": supplementary_files,
        "metadata": normalized_metadata,
        "status": "success",
        "curr_task_feat": {"question_length": len(question)},
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_read_file(
    dag_id: str,
    question: str,
    supplementary_files: object,
    metadata: dict[str, object],
) -> dict[str, object]:
    all_images = list_inline_images(supplementary_files)
    total_size = sum(int(image["features"]["size_bytes"]) for image in all_images)
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "all_images": all_images,
        "status": "success",
        "curr_task_feat": {
            "num_images": len(all_images),
            "total_size_bytes": total_size,
        },
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task3_file_process(
    dag_id: str,
    question: str,
    all_images: list[dict[str, object]],
    metadata: dict[str, object],
) -> dict[str, object]:
    processed_images = image_records_with_features(all_images)
    vlm_batches = split_four_20_20_20_40(processed_images)
    example_prompt = multimodal_vqa_prompt(question, processed_images[0]) if processed_images else question
    succ_task_feat = {
        "task4a_vlm_process": batch_feature_summary(vlm_batches[0], example_prompt),
        "task4b_vlm_process": batch_feature_summary(vlm_batches[1], example_prompt),
        "task4c_vlm_process": batch_feature_summary(vlm_batches[2], example_prompt),
        "task4d_vlm_process": batch_feature_summary(vlm_batches[3], example_prompt),
    }
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "processed_images": processed_images,
        "vlm_batches": vlm_batches,
        "aggregated_vision_features": aggregate_feature_dicts(processed_images),
        "status": "success",
        "succ_task_feat": succ_task_feat,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4a_vlm_process(
    dag_id: str,
    question: str,
    vlm_batches: list[list[dict[str, object]]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _answer_vqa_batch(
        question,
        vlm_batches,
        0,
        metadata,
        "vqa_a_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers_a": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4b_vlm_process(
    dag_id: str,
    question: str,
    vlm_batches: list[list[dict[str, object]]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _answer_vqa_batch(
        question,
        vlm_batches,
        1,
        metadata,
        "vqa_b_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers_b": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4c_vlm_process(
    dag_id: str,
    question: str,
    vlm_batches: list[list[dict[str, object]]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _answer_vqa_batch(
        question,
        vlm_batches,
        2,
        metadata,
        "vqa_c_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers_c": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4d_vlm_process(
    dag_id: str,
    question: str,
    vlm_batches: list[list[dict[str, object]]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _answer_vqa_batch(
        question,
        vlm_batches,
        3,
        metadata,
        "vqa_d_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers_d": answers,
        "curr_task_feat": features,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task4_merge_results(
    dag_id: str,
    final_answers_a: list[dict[str, object]],
    final_answers_b: list[dict[str, object]],
    final_answers_c: list[dict[str, object]],
    final_answers_d: list[dict[str, object]],
) -> dict[str, object]:
    final_answers = final_answers_a + final_answers_b + final_answers_c + final_answers_d
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers": final_answers,
        "curr_task_feat": {"total_answers": len(final_answers)},
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task5_output_final_answer(
    dag_id: str,
    final_answers: list[dict[str, object]],
) -> dict[str, object]:
    final_answer = "\n\n".join(
        f"Answer for {item.get('file_name', '')}:\n{item.get('answer', '')}"
        for item in sorted(final_answers, key=lambda value: str(value.get("file_name", "")))
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answer": final_answer,
        "curr_task_feat": {
            "final_answer_length": len(final_answer),
            "num_answers": len(final_answers),
        },
    }


def _answer_vqa_batch(
    question: str,
    vlm_batches: list[list[dict[str, object]]],
    batch_index: int,
    metadata: dict[str, object],
    override_key: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    batch = vlm_batches[batch_index] if batch_index < len(vlm_batches) else []
    prompts = [multimodal_vqa_prompt(question, image) for image in batch]
    answers, features = chat_image_prompt_batch(
        prompts,
        batch,
        metadata,
        override_key,
    )
    return (
        [
            {"file_name": image["file_name"], "answer": answers[index]}
            for index, image in enumerate(batch)
        ],
        features,
    )


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    answer = workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    metadata = workflow.input("metadata")

    start = workflow.add_task(
        task1_start_receive_task,
        task_name="task1_start_receive_task",
        inputs={
            "dag_id": dag_id,
            "question": question,
            "answer": answer,
            "supplementary_files": supplementary_files,
            "metadata": metadata,
        },
    )
    read_file = workflow.add_task(
        task2_read_file,
        task_name="task2_read_file",
        inputs={
            "dag_id": start.outputs["dag_id"],
            "question": start.outputs["question"],
            "supplementary_files": start.outputs["supplementary_files"],
            "metadata": start.outputs["metadata"],
        },
    )
    processed = workflow.add_task(
        task3_file_process,
        task_name="task3_file_process",
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "question": read_file.outputs["question"],
            "all_images": read_file.outputs["all_images"],
            "metadata": read_file.outputs["metadata"],
        },
    )
    batch_a = workflow.add_task(
        task4a_vlm_process,
        task_name="task4a_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": processed.outputs["dag_id"],
            "question": processed.outputs["question"],
            "vlm_batches": processed.outputs["vlm_batches"],
            "metadata": processed.outputs["metadata"],
        },
    )
    batch_b = workflow.add_task(
        task4b_vlm_process,
        task_name="task4b_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": processed.outputs["dag_id"],
            "question": processed.outputs["question"],
            "vlm_batches": processed.outputs["vlm_batches"],
            "metadata": processed.outputs["metadata"],
        },
    )
    batch_c = workflow.add_task(
        task4c_vlm_process,
        task_name="task4c_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": processed.outputs["dag_id"],
            "question": processed.outputs["question"],
            "vlm_batches": processed.outputs["vlm_batches"],
            "metadata": processed.outputs["metadata"],
        },
    )
    batch_d = workflow.add_task(
        task4d_vlm_process,
        task_name="task4d_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": processed.outputs["dag_id"],
            "question": processed.outputs["question"],
            "vlm_batches": processed.outputs["vlm_batches"],
            "metadata": processed.outputs["metadata"],
        },
    )
    merged = workflow.add_task(
        task4_merge_results,
        task_name="task4_merge_results",
        inputs={
            "dag_id": batch_a.outputs["dag_id"],
            "final_answers_a": batch_a.outputs["final_answers_a"],
            "final_answers_b": batch_b.outputs["final_answers_b"],
            "final_answers_c": batch_c.outputs["final_answers_c"],
            "final_answers_d": batch_d.outputs["final_answers_d"],
        },
    )
    workflow.add_task(
        task5_output_final_answer,
        task_name="task5_output_final_answer",
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "final_answers": merged.outputs["final_answers"],
        },
    )
    return workflow

"""Ascend-Maze-native OpenAGI document QA workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.openagi._common import (
    chat_prompt,
    chat_prompt_batch,
    document_qa_prompt,
    document_structure_prompt,
    metadata_dict,
    normalize_document_text,
    read_named_text_file,
    split_document_questions,
    text_features,
)

SPEC = WorkflowSpec(
    name="maze-openagi-document-qa",
    source="openagi",
    kind="document_qa",
    nodes=nodes(
        (
            ("task1_start_receive_task", "io", None),
            ("task2_read_file", "cpu", None),
            ("task3a_extract_text_content", "cpu", None),
            ("task3b_llm_process_extract_structure_info", "npu", "qwen3-32b"),
            ("task3c_load_questions_batch", "cpu", None),
            ("task4a_merge_document_analysis", "io", None),
            ("task4b_prepare_qa_context", "cpu", None),
            ("task5a_llm_process_batch_1", "npu", "qwen3-32b"),
            ("task5b_llm_process_batch_2", "npu", "qwen3-32b"),
            ("task5c_llm_process_batch_3", "npu", "qwen3-32b"),
            ("task7_merge_all_answers", "io", None),
            ("task8_output_final_answer", "io", None),
        )
    ),
    edges=edges(
        (
            ("task1_start_receive_task", "task2_read_file"),
            ("task2_read_file", "task3a_extract_text_content"),
            ("task2_read_file", "task3b_llm_process_extract_structure_info"),
            ("task2_read_file", "task3c_load_questions_batch"),
            ("task3a_extract_text_content", "task4a_merge_document_analysis"),
            ("task3b_llm_process_extract_structure_info", "task4a_merge_document_analysis"),
            ("task4a_merge_document_analysis", "task4b_prepare_qa_context"),
            ("task3c_load_questions_batch", "task4b_prepare_qa_context"),
            ("task4b_prepare_qa_context", "task5a_llm_process_batch_1"),
            ("task4b_prepare_qa_context", "task5b_llm_process_batch_2"),
            ("task4b_prepare_qa_context", "task5c_llm_process_batch_3"),
            ("task5a_llm_process_batch_1", "task7_merge_all_answers"),
            ("task5b_llm_process_batch_2", "task7_merge_all_answers"),
            ("task5c_llm_process_batch_3", "task7_merge_all_answers"),
            ("task7_merge_all_answers", "task8_output_final_answer"),
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
        "curr_task_feat": {
            "question_length": len(question),
            "num_files": len(supplementary_files) if hasattr(supplementary_files, "__len__") else 1,
        },
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_read_file(
    dag_id: str,
    question: str,
    answer: str,
    supplementary_files: object,
    metadata: dict[str, object],
) -> dict[str, object]:
    document = read_named_text_file(supplementary_files, "context.txt")
    document_content = str(document["content"])
    structure_prompt = document_structure_prompt(document_content)
    features = text_features(structure_prompt)
    return {
        "dag_id": dag_id,
        "question": question,
        "answer": answer,
        "metadata": metadata,
        "document_content": document_content,
        "rare_content": document_content,
        "document_file": document,
        "status": "success",
        "succ_task_feat": {
            "task3b_llm_process_extract_structure_info": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 0,
                "batch_size": 1,
            }
        },
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task3a_extract_text_content(
    dag_id: str,
    document_content: str,
) -> dict[str, object]:
    extracted_text = normalize_document_text(document_content)
    return {
        "dag_id": dag_id,
        "status": "success",
        "extracted_text": extracted_text,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3b_llm_process_extract_structure_info(
    dag_id: str,
    document_content: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    prompt = document_structure_prompt(document_content)
    structure_summary, raw_output, features = chat_prompt(
        prompt,
        metadata,
        "structure_output_override",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "metadata": metadata,
        "doc_structure": structure_summary,
        "raw_model_output": raw_output,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task3c_load_questions_batch(
    dag_id: str,
    question: str,
) -> dict[str, object]:
    question_batches = split_document_questions(question)
    return {
        "dag_id": dag_id,
        "status": "success",
        "question_batches": question_batches,
        "question_count": sum(len(batch) for batch in question_batches),
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task4a_merge_document_analysis(
    dag_id: str,
    extracted_text: str,
    doc_structure: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    merged_document_analysis = {
        "content": extracted_text,
        "structure": doc_structure,
        "rare_content": extracted_text,
    }
    return {
        "dag_id": dag_id,
        "status": "success",
        "metadata": metadata,
        "merged_document_analysis": merged_document_analysis,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task4b_prepare_qa_context(
    dag_id: str,
    merged_document_analysis: dict[str, object],
    question_batches: list[list[str]],
    metadata: dict[str, object],
) -> dict[str, object]:
    doc_content = str(merged_document_analysis["content"])[:12000]
    doc_structure = str(merged_document_analysis["structure"])
    rare_content = str(merged_document_analysis["rare_content"])
    qa_context = {
        "document_content": doc_content,
        "document_structure": doc_structure,
        "rare_content": rare_content,
        "question_batches": question_batches,
    }
    batch_keys = (
        "task5a_llm_process_batch_1",
        "task5b_llm_process_batch_2",
        "task5c_llm_process_batch_3",
    )
    succ_task_feat = {}
    for index, batch in enumerate(question_batches):
        prompts = [
            document_qa_prompt(rare_content, doc_structure, doc_content, item)
            for item in batch
        ]
        succ_task_feat[batch_keys[index]] = {
            "text_length": sum(len(prompt) for prompt in prompts),
            "token_count": sum(text_features(prompt)["token_count"] for prompt in prompts),
            "batch_size": len(prompts),
            "reason": 0,
        }
    return {
        "dag_id": dag_id,
        "status": "success",
        "metadata": metadata,
        "qa_context": qa_context,
        "succ_task_feat": succ_task_feat,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5a_llm_process_batch_1(
    dag_id: str,
    qa_context: dict[str, object],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_question_batch(
        qa_context,
        metadata,
        0,
        "batch1_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch1_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5b_llm_process_batch_2(
    dag_id: str,
    qa_context: dict[str, object],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_question_batch(
        qa_context,
        metadata,
        1,
        "batch2_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch2_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5c_llm_process_batch_3(
    dag_id: str,
    qa_context: dict[str, object],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_question_batch(
        qa_context,
        metadata,
        2,
        "batch3_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch3_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task7_merge_all_answers(
    dag_id: str,
    batch1_answers: list[str],
    batch2_answers: list[str],
    batch3_answers: list[str],
) -> dict[str, object]:
    final_answers = batch1_answers + batch2_answers + batch3_answers
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answers": final_answers,
        "curr_task_feat": {"total_answers": len(final_answers)},
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task8_output_final_answer(
    dag_id: str,
    final_answers: list[str],
) -> dict[str, object]:
    final_answer = "\n".join(final_answers)
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answer": final_answer,
        "curr_task_feat": {
            "final_answer_length": len(final_answer),
            "num_answers": len(final_answers),
        },
    }


def _process_question_batch(
    qa_context: dict[str, object],
    metadata: dict[str, object],
    batch_index: int,
    override_key: str,
) -> tuple[list[str], dict[str, object]]:
    batches = qa_context["question_batches"]
    assert isinstance(batches, list)
    questions = batches[batch_index] if batch_index < len(batches) else []
    rare_content = str(qa_context["rare_content"])
    doc_structure = str(qa_context["document_structure"])
    doc_content = str(qa_context["document_content"])
    prompts = [
        document_qa_prompt(rare_content, doc_structure, doc_content, question)
        for question in questions
    ]
    return chat_prompt_batch(prompts, metadata, override_key)


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
            "answer": start.outputs["answer"],
            "supplementary_files": start.outputs["supplementary_files"],
            "metadata": start.outputs["metadata"],
        },
    )
    extracted = workflow.add_task(
        task3a_extract_text_content,
        task_name="task3a_extract_text_content",
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "document_content": read_file.outputs["document_content"],
        },
    )
    structured = workflow.add_task(
        task3b_llm_process_extract_structure_info,
        task_name="task3b_llm_process_extract_structure_info",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "document_content": read_file.outputs["document_content"],
            "metadata": read_file.outputs["metadata"],
        },
    )
    questions = workflow.add_task(
        task3c_load_questions_batch,
        task_name="task3c_load_questions_batch",
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "question": read_file.outputs["question"],
        },
    )
    merged = workflow.add_task(
        task4a_merge_document_analysis,
        task_name="task4a_merge_document_analysis",
        inputs={
            "dag_id": extracted.outputs["dag_id"],
            "extracted_text": extracted.outputs["extracted_text"],
            "doc_structure": structured.outputs["doc_structure"],
            "metadata": structured.outputs["metadata"],
        },
    )
    qa_context = workflow.add_task(
        task4b_prepare_qa_context,
        task_name="task4b_prepare_qa_context",
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "merged_document_analysis": merged.outputs["merged_document_analysis"],
            "question_batches": questions.outputs["question_batches"],
            "metadata": merged.outputs["metadata"],
        },
    )
    batch1 = workflow.add_task(
        task5a_llm_process_batch_1,
        task_name="task5a_llm_process_batch_1",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": qa_context.outputs["dag_id"],
            "qa_context": qa_context.outputs["qa_context"],
            "metadata": qa_context.outputs["metadata"],
        },
    )
    batch2 = workflow.add_task(
        task5b_llm_process_batch_2,
        task_name="task5b_llm_process_batch_2",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": qa_context.outputs["dag_id"],
            "qa_context": qa_context.outputs["qa_context"],
            "metadata": qa_context.outputs["metadata"],
        },
    )
    batch3 = workflow.add_task(
        task5c_llm_process_batch_3,
        task_name="task5c_llm_process_batch_3",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": qa_context.outputs["dag_id"],
            "qa_context": qa_context.outputs["qa_context"],
            "metadata": qa_context.outputs["metadata"],
        },
    )
    merged_answers = workflow.add_task(
        task7_merge_all_answers,
        task_name="task7_merge_all_answers",
        inputs={
            "dag_id": batch1.outputs["dag_id"],
            "batch1_answers": batch1.outputs["batch1_answers"],
            "batch2_answers": batch2.outputs["batch2_answers"],
            "batch3_answers": batch3.outputs["batch3_answers"],
        },
    )
    workflow.add_task(
        task8_output_final_answer,
        task_name="task8_output_final_answer",
        inputs={
            "dag_id": merged_answers.outputs["dag_id"],
            "final_answers": merged_answers.outputs["final_answers"],
        },
    )
    return workflow

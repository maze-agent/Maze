"""Ascend-Maze-native OpenAGI multilingual text processing workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.openagi._common import (
    chat_prompt,
    chat_prompt_batch,
    detect_language_code,
    final_text_question_prompt,
    metadata_dict,
    read_named_text_file,
    split_questions_even,
    target_language_code_from_question,
    text_sentiment_prompt,
    text_summary_prompt,
    text_translate_prompt,
)

SPEC = WorkflowSpec(
    name="maze-openagi-text-processing-multilingual",
    source="openagi",
    kind="text_processing_multilingual",
    nodes=nodes(
        (
            ("task1_start_receive_task", "io", None),
            ("task2_read_file_and_split_questions", "io", None),
            ("task3_language_detect", "cpu", None),
            ("task4_translate_text", "npu", "qwen3-32b"),
            ("task5a_text_analysis_summarize", "npu", "qwen3-32b"),
            ("task5b_text_analysis_sentiment", "npu", "qwen3-32b"),
            ("task6_prepare_llm_batches", "cpu", None),
            ("task7a_llm_process_batch_1", "npu", "qwen3-32b"),
            ("task7b_llm_process_batch_2", "npu", "qwen3-32b"),
            ("task7c_llm_process_batch_3", "npu", "qwen3-32b"),
            ("task8_merge_answers", "io", None),
            ("task9_output_final_answer", "io", None),
        )
    ),
    edges=edges(
        (
            ("task1_start_receive_task", "task2_read_file_and_split_questions"),
            ("task2_read_file_and_split_questions", "task3_language_detect"),
            ("task3_language_detect", "task4_translate_text"),
            ("task4_translate_text", "task5a_text_analysis_summarize"),
            ("task4_translate_text", "task5b_text_analysis_sentiment"),
            ("task2_read_file_and_split_questions", "task6_prepare_llm_batches"),
            ("task5a_text_analysis_summarize", "task6_prepare_llm_batches"),
            ("task5b_text_analysis_sentiment", "task6_prepare_llm_batches"),
            ("task6_prepare_llm_batches", "task7a_llm_process_batch_1"),
            ("task6_prepare_llm_batches", "task7b_llm_process_batch_2"),
            ("task6_prepare_llm_batches", "task7c_llm_process_batch_3"),
            ("task7a_llm_process_batch_1", "task8_merge_answers"),
            ("task7b_llm_process_batch_2", "task8_merge_answers"),
            ("task7c_llm_process_batch_3", "task8_merge_answers"),
            ("task8_merge_answers", "task9_output_final_answer"),
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
    target_language = target_language_code_from_question(question)
    return {
        "dag_id": dag_id,
        "question": question,
        "answer": answer,
        "supplementary_files": supplementary_files,
        "metadata": normalized_metadata,
        "target_language": target_language,
        "status": "success",
        "curr_task_feat": {"instruction_length": len(question)},
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task2_read_file_and_split_questions(
    dag_id: str,
    question: str,
    supplementary_files: object,
    metadata: dict[str, object],
    target_language: str,
) -> dict[str, object]:
    document = read_named_text_file(supplementary_files, "text.txt")
    document_content = str(document["content"])
    question_batches = split_questions_even(question, parts=3)
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "target_language": target_language,
        "document_content": document_content,
        "document_file": document,
        "question_batches": question_batches,
        "status": "success",
        "curr_task_feat": {
            "document_length": len(document_content),
            "question_count": sum(len(batch) for batch in question_batches),
        },
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task3_language_detect(
    dag_id: str,
    question: str,
    metadata: dict[str, object],
    target_language: str,
    document_content: str,
    question_batches: list[list[str]],
) -> dict[str, object]:
    source_language = detect_language_code(document_content)
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "target_language": target_language,
        "document_content": document_content,
        "question_batches": question_batches,
        "source_language": source_language,
        "status": "success",
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4_translate_text(
    dag_id: str,
    question: str,
    metadata: dict[str, object],
    target_language: str,
    document_content: str,
    question_batches: list[list[str]],
    source_language: str,
) -> dict[str, object]:
    prompt = text_translate_prompt(document_content, source_language, target_language)
    translated_text, raw_output, features = chat_prompt(
        prompt,
        metadata,
        "translation_output_override",
    )
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "target_language": target_language,
        "source_language": source_language,
        "translated_text": translated_text,
        "question_batches": question_batches,
        "raw_model_output": raw_output,
        "status": "success",
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5a_text_analysis_summarize(
    dag_id: str,
    question: str,
    metadata: dict[str, object],
    translated_text: str,
) -> dict[str, object]:
    prompt = text_summary_prompt(translated_text)
    summary, raw_output, features = chat_prompt(
        prompt,
        metadata,
        "summary_output_override",
        max_tokens=4096,
    )
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "translated_text": translated_text,
        "summary": summary,
        "raw_model_output": raw_output,
        "status": "success",
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5b_text_analysis_sentiment(
    dag_id: str,
    translated_text: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    prompt = text_sentiment_prompt(translated_text)
    sentiment, raw_output, features = chat_prompt(
        prompt,
        metadata,
        "sentiment_output_override",
        max_tokens=4096,
    )
    return {
        "dag_id": dag_id,
        "sentiment": sentiment,
        "raw_model_output": raw_output,
        "status": "success",
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task6_prepare_llm_batches(
    dag_id: str,
    question: str,
    metadata: dict[str, object],
    question_batches: list[list[str]],
    translated_text: str,
    summary: str,
    sentiment: str,
) -> dict[str, object]:
    llm_batches = [
        [
            final_text_question_prompt(
                translated_text,
                summary,
                sentiment,
                question,
                item,
            )
            for item in batch
        ]
        for batch in question_batches
    ]
    succ_task_feat = {
        "task7a_llm_process_batch_1": _batch_features(llm_batches[0]),
        "task7b_llm_process_batch_2": _batch_features(llm_batches[1]),
        "task7c_llm_process_batch_3": _batch_features(llm_batches[2]),
    }
    return {
        "dag_id": dag_id,
        "metadata": metadata,
        "llm_batches": llm_batches,
        "status": "success",
        "succ_task_feat": succ_task_feat,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task7a_llm_process_batch_1(
    dag_id: str,
    llm_batches: list[list[str]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_text_batch(
        llm_batches,
        0,
        metadata,
        "text_batch1_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch1_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task7b_llm_process_batch_2(
    dag_id: str,
    llm_batches: list[list[str]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_text_batch(
        llm_batches,
        1,
        metadata,
        "text_batch2_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch2_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task7c_llm_process_batch_3(
    dag_id: str,
    llm_batches: list[list[str]],
    metadata: dict[str, object],
) -> dict[str, object]:
    answers, features = _process_text_batch(
        llm_batches,
        2,
        metadata,
        "text_batch3_output_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "batch3_answers": answers,
        "curr_task_feat": features,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task8_merge_answers(
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
def task9_output_final_answer(
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


def _batch_features(prompts: list[str]) -> dict[str, object]:
    from workflows.openagi._common import estimate_tokens

    return {
        "text_length": sum(len(prompt) for prompt in prompts),
        "token_count": sum(estimate_tokens(prompt) for prompt in prompts),
        "batch_size": len(prompts),
        "reason": 0,
    }


def _process_text_batch(
    llm_batches: list[list[str]],
    batch_index: int,
    metadata: dict[str, object],
    override_key: str,
) -> tuple[list[str], dict[str, object]]:
    prompts = llm_batches[batch_index] if batch_index < len(llm_batches) else []
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
        task2_read_file_and_split_questions,
        task_name="task2_read_file_and_split_questions",
        inputs={
            "dag_id": start.outputs["dag_id"],
            "question": start.outputs["question"],
            "supplementary_files": start.outputs["supplementary_files"],
            "metadata": start.outputs["metadata"],
            "target_language": start.outputs["target_language"],
        },
    )
    detected = workflow.add_task(
        task3_language_detect,
        task_name="task3_language_detect",
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "question": read_file.outputs["question"],
            "metadata": read_file.outputs["metadata"],
            "target_language": read_file.outputs["target_language"],
            "document_content": read_file.outputs["document_content"],
            "question_batches": read_file.outputs["question_batches"],
        },
    )
    translated = workflow.add_task(
        task4_translate_text,
        task_name="task4_translate_text",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": detected.outputs["dag_id"],
            "question": detected.outputs["question"],
            "metadata": detected.outputs["metadata"],
            "target_language": detected.outputs["target_language"],
            "document_content": detected.outputs["document_content"],
            "question_batches": detected.outputs["question_batches"],
            "source_language": detected.outputs["source_language"],
        },
    )
    summary = workflow.add_task(
        task5a_text_analysis_summarize,
        task_name="task5a_text_analysis_summarize",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": translated.outputs["dag_id"],
            "question": translated.outputs["question"],
            "metadata": translated.outputs["metadata"],
            "translated_text": translated.outputs["translated_text"],
        },
    )
    sentiment = workflow.add_task(
        task5b_text_analysis_sentiment,
        task_name="task5b_text_analysis_sentiment",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": translated.outputs["dag_id"],
            "translated_text": translated.outputs["translated_text"],
            "metadata": translated.outputs["metadata"],
        },
    )
    prepared = workflow.add_task(
        task6_prepare_llm_batches,
        task_name="task6_prepare_llm_batches",
        inputs={
            "dag_id": read_file.outputs["dag_id"],
            "question": read_file.outputs["question"],
            "metadata": read_file.outputs["metadata"],
            "question_batches": read_file.outputs["question_batches"],
            "translated_text": summary.outputs["translated_text"],
            "summary": summary.outputs["summary"],
            "sentiment": sentiment.outputs["sentiment"],
        },
    )
    batch1 = workflow.add_task(
        task7a_llm_process_batch_1,
        task_name="task7a_llm_process_batch_1",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": prepared.outputs["dag_id"],
            "llm_batches": prepared.outputs["llm_batches"],
            "metadata": prepared.outputs["metadata"],
        },
    )
    batch2 = workflow.add_task(
        task7b_llm_process_batch_2,
        task_name="task7b_llm_process_batch_2",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": prepared.outputs["dag_id"],
            "llm_batches": prepared.outputs["llm_batches"],
            "metadata": prepared.outputs["metadata"],
        },
    )
    batch3 = workflow.add_task(
        task7c_llm_process_batch_3,
        task_name="task7c_llm_process_batch_3",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": prepared.outputs["dag_id"],
            "llm_batches": prepared.outputs["llm_batches"],
            "metadata": prepared.outputs["metadata"],
        },
    )
    merged = workflow.add_task(
        task8_merge_answers,
        task_name="task8_merge_answers",
        inputs={
            "dag_id": batch1.outputs["dag_id"],
            "batch1_answers": batch1.outputs["batch1_answers"],
            "batch2_answers": batch2.outputs["batch2_answers"],
            "batch3_answers": batch3.outputs["batch3_answers"],
        },
    )
    workflow.add_task(
        task9_output_final_answer,
        task_name="task9_output_final_answer",
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "final_answers": merged.outputs["final_answers"],
        },
    )
    return workflow

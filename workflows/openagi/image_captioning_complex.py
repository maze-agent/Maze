"""Ascend-Maze-native OpenAGI complex image captioning workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.openagi._common import (
    aggregate_feature_dicts,
    batch_feature_summary,
    blip_prompt,
    chat_image_prompt_batch,
    format_named_answers,
    image_caption_prompt,
    image_records_with_features,
    list_inline_images,
    metadata_dict,
    ocr_prompt,
    split_four_20_20_20_40,
    target_language_from_question,
)

SPEC = WorkflowSpec(
    name="maze-openagi-image-captioning-complex",
    source="openagi",
    kind="image_captioning_complex",
    nodes=nodes(
        (
            ("task1_start_receive_task", "io", None),
            ("task2_read_and_enhance_images", "cpu", None),
            ("task3a_extract_blip_captions", "npu", "blip-image-captioning"),
            ("task3b_extract_ocr_text", "npu", "easyocr"),
            ("task4_merge_image_features", "cpu", None),
            ("task5a_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task5b_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task5c_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task5d_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task5_merge_results", "io", None),
            ("task6_output_final_answer", "cpu", None),
        )
    ),
    edges=edges(
        (
            ("task1_start_receive_task", "task2_read_and_enhance_images"),
            ("task2_read_and_enhance_images", "task3a_extract_blip_captions"),
            ("task2_read_and_enhance_images", "task3b_extract_ocr_text"),
            ("task3a_extract_blip_captions", "task4_merge_image_features"),
            ("task3b_extract_ocr_text", "task4_merge_image_features"),
            ("task4_merge_image_features", "task5a_vlm_process"),
            ("task4_merge_image_features", "task5b_vlm_process"),
            ("task4_merge_image_features", "task5c_vlm_process"),
            ("task4_merge_image_features", "task5d_vlm_process"),
            ("task5a_vlm_process", "task5_merge_results"),
            ("task5b_vlm_process", "task5_merge_results"),
            ("task5c_vlm_process", "task5_merge_results"),
            ("task5d_vlm_process", "task5_merge_results"),
            ("task5_merge_results", "task6_output_final_answer"),
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
    target_language = target_language_from_question(question)
    return {
        "dag_id": dag_id,
        "question": question,
        "answer": answer,
        "supplementary_files": supplementary_files,
        "metadata": normalized_metadata,
        "target_language": target_language,
        "status": "success",
        "curr_task_feat": {"question_length": len(question)},
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_read_and_enhance_images(
    dag_id: str,
    question: str,
    supplementary_files: object,
    metadata: dict[str, object],
    target_language: str,
) -> dict[str, object]:
    inline_images = list_inline_images(supplementary_files)
    enhanced_images = image_records_with_features(inline_images)
    total_size = sum(int(image["features"]["size_bytes"]) for image in enhanced_images)
    return {
        "dag_id": dag_id,
        "question": question,
        "metadata": metadata,
        "target_language": target_language,
        "enhanced_images": enhanced_images,
        "status": "success",
        "curr_task_feat": {
            "num_images": len(enhanced_images),
            "total_size_bytes": total_size,
        },
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3a_extract_blip_captions(
    dag_id: str,
    enhanced_images: list[dict[str, object]],
    metadata: dict[str, object],
    target_language: str,
) -> dict[str, object]:
    prompts = [blip_prompt(image) for image in enhanced_images]
    captions, features = chat_image_prompt_batch(
        prompts,
        enhanced_images,
        metadata,
        "blip_caption_overrides",
        max_tokens=4096,
    )
    blip_captions = [
        {"file_name": image["file_name"], "caption": captions[index]}
        for index, image in enumerate(enhanced_images)
    ]
    return {
        "dag_id": dag_id,
        "status": "success",
        "metadata": metadata,
        "target_language": target_language,
        "enhanced_images": enhanced_images,
        "blip_captions": blip_captions,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3b_extract_ocr_text(
    dag_id: str,
    enhanced_images: list[dict[str, object]],
    metadata: dict[str, object],
    target_language: str,
) -> dict[str, object]:
    prompts = [ocr_prompt(image, target_language) for image in enhanced_images]
    ocr_texts, features = chat_image_prompt_batch(
        prompts,
        enhanced_images,
        metadata,
        "ocr_text_overrides",
        max_tokens=4096,
    )
    ocr_results_list = [
        {"file_name": image["file_name"], "text": ocr_texts[index], "raw_results": ()}
        for index, image in enumerate(enhanced_images)
    ]
    return {
        "dag_id": dag_id,
        "status": "success",
        "ocr_results_list": ocr_results_list,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task4_merge_image_features(
    dag_id: str,
    enhanced_images: list[dict[str, object]],
    blip_captions: list[dict[str, object]],
    ocr_results_list: list[dict[str, object]],
    metadata: dict[str, object],
    target_language: str,
) -> dict[str, object]:
    caption_by_name = {
        str(item["file_name"]): str(item["caption"]) for item in blip_captions
    }
    ocr_by_name = {str(item["file_name"]): str(item["text"]) for item in ocr_results_list}
    merged_image_features = [
        {
            "file_name": image["file_name"],
            "content": image["content"],
            "features": image["features"],
            "caption": caption_by_name.get(str(image["file_name"]), ""),
            "ocr_text": ocr_by_name.get(str(image["file_name"]), ""),
        }
        for image in enhanced_images
    ]
    vlm_batches = split_four_20_20_20_40(merged_image_features)
    task_names = (
        "task5a_vlm_process",
        "task5b_vlm_process",
        "task5c_vlm_process",
        "task5d_vlm_process",
    )
    succ_task_feat = {}
    for index, batch in enumerate(vlm_batches):
        example_prompt = image_caption_prompt(target_language, "", "")
        succ_task_feat[task_names[index]] = batch_feature_summary(batch, example_prompt)
    return {
        "dag_id": dag_id,
        "status": "success",
        "metadata": metadata,
        "target_language": target_language,
        "merged_image_features": merged_image_features,
        "vlm_batches": vlm_batches,
        "aggregated_vision_features": aggregate_feature_dicts(merged_image_features),
        "succ_task_feat": succ_task_feat,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5a_vlm_process(
    dag_id: str,
    vlm_batches: list[list[dict[str, object]]],
    target_language: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    descriptions, features = _describe_image_batch(
        vlm_batches,
        0,
        target_language,
        metadata,
        "description_a_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_descriptions_a": descriptions,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5b_vlm_process(
    dag_id: str,
    vlm_batches: list[list[dict[str, object]]],
    target_language: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    descriptions, features = _describe_image_batch(
        vlm_batches,
        1,
        target_language,
        metadata,
        "description_b_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_descriptions_b": descriptions,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5c_vlm_process(
    dag_id: str,
    vlm_batches: list[list[dict[str, object]]],
    target_language: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    descriptions, features = _describe_image_batch(
        vlm_batches,
        2,
        target_language,
        metadata,
        "description_c_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_descriptions_c": descriptions,
        "curr_task_feat": features,
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5d_vlm_process(
    dag_id: str,
    vlm_batches: list[list[dict[str, object]]],
    target_language: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    descriptions, features = _describe_image_batch(
        vlm_batches,
        3,
        target_language,
        metadata,
        "description_d_overrides",
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_descriptions_d": descriptions,
        "curr_task_feat": features,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task5_merge_results(
    dag_id: str,
    final_descriptions_a: list[dict[str, object]],
    final_descriptions_b: list[dict[str, object]],
    final_descriptions_c: list[dict[str, object]],
    final_descriptions_d: list[dict[str, object]],
) -> dict[str, object]:
    final_descriptions = (
        final_descriptions_a
        + final_descriptions_b
        + final_descriptions_c
        + final_descriptions_d
    )
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_descriptions": final_descriptions,
        "curr_task_feat": {"total_items": len(final_descriptions)},
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task6_output_final_answer(
    dag_id: str,
    final_descriptions: list[dict[str, object]],
) -> dict[str, object]:
    final_answer = format_named_answers(final_descriptions, "description")
    return {
        "dag_id": dag_id,
        "status": "success",
        "final_answer": final_answer,
        "curr_task_feat": {
            "final_answer_length": len(final_answer),
            "num_answers": len(final_descriptions),
        },
    }


def _describe_image_batch(
    vlm_batches: list[list[dict[str, object]]],
    batch_index: int,
    target_language: str,
    metadata: dict[str, object],
    override_key: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    batch = vlm_batches[batch_index] if batch_index < len(vlm_batches) else []
    prompts = [
        image_caption_prompt(
            target_language,
            str(image.get("caption", "")),
            str(image.get("ocr_text", "")),
        )
        for image in batch
    ]
    answers, features = chat_image_prompt_batch(
        prompts,
        batch,
        metadata,
        override_key,
    )
    descriptions = [
        {"file_name": image["file_name"], "description": answers[index]}
        for index, image in enumerate(batch)
    ]
    return descriptions, features


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
    images = workflow.add_task(
        task2_read_and_enhance_images,
        task_name="task2_read_and_enhance_images",
        inputs={
            "dag_id": start.outputs["dag_id"],
            "question": start.outputs["question"],
            "supplementary_files": start.outputs["supplementary_files"],
            "metadata": start.outputs["metadata"],
            "target_language": start.outputs["target_language"],
        },
    )
    captions = workflow.add_task(
        task3a_extract_blip_captions,
        task_name="task3a_extract_blip_captions",
        model_anchor={"model": "blip-image-captioning", "mode": "service"},
        inputs={
            "dag_id": images.outputs["dag_id"],
            "enhanced_images": images.outputs["enhanced_images"],
            "metadata": images.outputs["metadata"],
            "target_language": images.outputs["target_language"],
        },
    )
    ocr = workflow.add_task(
        task3b_extract_ocr_text,
        task_name="task3b_extract_ocr_text",
        model_anchor={"model": "easyocr", "mode": "service"},
        inputs={
            "dag_id": images.outputs["dag_id"],
            "enhanced_images": images.outputs["enhanced_images"],
            "metadata": images.outputs["metadata"],
            "target_language": images.outputs["target_language"],
        },
    )
    merged = workflow.add_task(
        task4_merge_image_features,
        task_name="task4_merge_image_features",
        inputs={
            "dag_id": captions.outputs["dag_id"],
            "enhanced_images": captions.outputs["enhanced_images"],
            "blip_captions": captions.outputs["blip_captions"],
            "ocr_results_list": ocr.outputs["ocr_results_list"],
            "metadata": captions.outputs["metadata"],
            "target_language": captions.outputs["target_language"],
        },
    )
    batch_a = workflow.add_task(
        task5a_vlm_process,
        task_name="task5a_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "vlm_batches": merged.outputs["vlm_batches"],
            "target_language": merged.outputs["target_language"],
            "metadata": merged.outputs["metadata"],
        },
    )
    batch_b = workflow.add_task(
        task5b_vlm_process,
        task_name="task5b_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "vlm_batches": merged.outputs["vlm_batches"],
            "target_language": merged.outputs["target_language"],
            "metadata": merged.outputs["metadata"],
        },
    )
    batch_c = workflow.add_task(
        task5c_vlm_process,
        task_name="task5c_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "vlm_batches": merged.outputs["vlm_batches"],
            "target_language": merged.outputs["target_language"],
            "metadata": merged.outputs["metadata"],
        },
    )
    batch_d = workflow.add_task(
        task5d_vlm_process,
        task_name="task5d_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": merged.outputs["dag_id"],
            "vlm_batches": merged.outputs["vlm_batches"],
            "target_language": merged.outputs["target_language"],
            "metadata": merged.outputs["metadata"],
        },
    )
    all_descriptions = workflow.add_task(
        task5_merge_results,
        task_name="task5_merge_results",
        inputs={
            "dag_id": batch_a.outputs["dag_id"],
            "final_descriptions_a": batch_a.outputs["final_descriptions_a"],
            "final_descriptions_b": batch_b.outputs["final_descriptions_b"],
            "final_descriptions_c": batch_c.outputs["final_descriptions_c"],
            "final_descriptions_d": batch_d.outputs["final_descriptions_d"],
        },
    )
    workflow.add_task(
        task6_output_final_answer,
        task_name="task6_output_final_answer",
        inputs={
            "dag_id": all_descriptions.outputs["dag_id"],
            "final_descriptions": all_descriptions.outputs["final_descriptions"],
        },
    )
    return workflow

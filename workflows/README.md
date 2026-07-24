# Migrated Maze workflows

This directory contains the first-pass Ascend-Maze ports of the workflows from
the upstream Maze `src/agentos/workflows` package.

The migrated files preserve the old Maze DAG shape while replacing old Ray actor
context and decorator usage with Ascend-Maze-native explicit inputs, dict
outputs, `task_kind` values, and service-mode model anchors. Each module exposes:

- `SPEC`: migrated workflow metadata, task names, task kinds, model anchors, and
  DAG edges;
- `INPUTS`: the shared workflow input contract;
- `build()`: a function that returns an Ascend-Maze `Workflow`.

GAIA workflows now have Ascend-Maze-native runnable logic ported from Maze:

- `workflows.gaia.file`: accepts an explicit inline supplementary file payload or
  `SharedFileRef`, extracts text from common document-like formats with
  lightweight optional dependency fallbacks, calls Qwen and DeepSeek through
  `ascend_maze.inference.chat()`, then fuses the two answers.
- `workflows.gaia.reason`: prepares the GAIA text-only prompt, calls Qwen and
  DeepSeek through `ascend_maze.inference.chat()`, then fuses the two answers.
- `workflows.gaia.speech`: accepts explicit audio bytes or `SharedFileRef`,
  extracts lightweight audio metadata, uses the service inference path for
  transcription, then runs the same Qwen/DeepSeek/fusion answer path.
- `workflows.gaia.vision`: accepts explicit image bytes or `SharedFileRef`,
  extracts lightweight image metadata when Pillow is available, calls the
  service inference path with OpenAI-style text+image content parts, then emits
  the final answer.

The task-side chat contract supports the existing string content path and the
OpenAI-style text+`image_url` content-parts path. The current implementation
uses base64 `data:image/...` URLs for explicit inline images. On the current
2026-07-22 Ascend environment, Qwen2.5-VL requires the repo runtime workaround
for a lower-level `torch_npu` `aclnnUniqueConsecutive`/AICPU failure; with that
workaround and `--generation-config vllm`, the three migrated vision workflows
have passed true multimodal smoke.

OpenAGI workflows now also have Ascend-Maze-native runnable logic ported from
Maze:

- `workflows.openagi.document_qa`: reads an explicit `context.txt` payload,
  normalizes document text, analyzes document structure through
  `ascend_maze.inference.chat()`, splits questions into the original three batch
  branches, answers them, and emits the merged answer text.
- `workflows.openagi.image_captioning_complex`: reads explicit inline image
  payloads or a top-level `SharedFileRef`, produces lightweight image features,
  runs BLIP-style caption and OCR-style extraction through the service inference
  path, fans out four VLM-style captioning branches, then merges sorted image
  descriptions.
- `workflows.openagi.multimodal_vqa_complex`: reads explicit images, preserves
  the old four-way VQA fanout, calls the service inference path with
  OpenAI-style text+image content parts, and emits per-image answers.
- `workflows.openagi.text_processing_multilingual`: reads an explicit
  `text.txt` payload, splits user questions, detects source/target language,
  performs translation, summary, sentiment, three-way answer generation, and
  final merge through explicit task outputs.

For OpenAGI image and multimodal workflows, image bytes remain explicit workflow
data and are encoded into per-request `data:image/...` content parts at the
service-call boundary.

All tau-bench workflows now have Ascend-Maze-native business logic ported from
Maze:

- `workflows.tbench.retail_cancel`: loads retail backend data, calls
  `ascend_maze.inference.chat()` for cancellation extraction, executes the
  retail cancellation tool logic, and emits a structured final result.
- `workflows.tbench.retail_return`: loads retail backend data, calls
  `ascend_maze.inference.chat()` for return extraction, resolves the user,
  loads order details, executes the retail return tool logic, and emits a
  structured final result.
- `workflows.tbench.retail_modify`: loads retail backend data, calls
  `ascend_maze.inference.chat()` for modification extraction, resolves optional
  user/order context, executes pending-order payment/address/item or user-address
  modification helpers, and emits a structured final result.
- `workflows.tbench.retail_cancel_modify`: loads retail backend data, calls
  `ascend_maze.inference.chat()` for mixed cancel/modify extraction, resolves
  optional user/order context, executes cancellation plus pending-order item
  modification helpers, and emits a structured final result.
- `workflows.tbench.airline_book`: loads airline backend data, calls
  `ascend_maze.inference.chat()` for booking extraction and itinerary selection,
  searches candidate flights, loads user details, books a reservation, and emits
  a structured final result.
- `workflows.tbench.airline_cancel`: loads airline backend data, calls
  `ascend_maze.inference.chat()` for cancel/rebook extraction and replacement
  flight selection, cancels the old reservation, books the replacement
  reservation, and emits a structured final result.

The original Maze task bodies depend on Ray actor context (`context.get/put`) and
the old `@cpu/@io/@gpu` decorators, so they cannot be copied directly into
Ascend-Maze. Future dataset-specific workflow ports should follow the same
Ascend-Maze-native pattern:

- pass files through `SharedFileRef` or an explicit manifest;
- pass task data through named inputs and dict outputs;
- use Ascend-Maze inference APIs for model calls;
- keep output keys statically inferable by C1/C2.

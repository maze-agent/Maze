# SPDX-License-Identifier: Apache-2.0
"""GAIA leaderboard scoring rules pinned for reproducible validation.

This is a standard-library, behavior-preserving port of ``scorer.py`` from
the official GAIA leaderboard Space at revision
``9f133d71362e77b3539f1514f31b9c101a545fec``:

https://huggingface.co/spaces/gaia-benchmark/leaderboard/resolve/9f133d71362e77b3539f1514f31b9c101a545fec/scorer.py

The upstream SHA256 is
``0d44c07f3046eec521697c22e3eaca8719cc81e422a8eaf32695c5f22bdac6e2``.
Maze removes unused json/numpy imports and informational print calls; scoring
semantics, including exact floats and ordered list comparison, are unchanged.
See ``LICENSES/Apache-2.0.txt``.
"""

from __future__ import annotations

import re
import string
import warnings


GAIA_SCORER_REVISION = "9f133d71362e77b3539f1514f31b9c101a545fec"
GAIA_SCORER_BLOB = "ede7ece61d26cc93bf440a86a527163d617e7c12"
GAIA_SCORER_SHA256 = "0d44c07f3046eec521697c22e3eaca8719cc81e422a8eaf32695c5f22bdac6e2"
GAIA_SCORER_SOURCE_URL = (
    "https://huggingface.co/spaces/gaia-benchmark/leaderboard/resolve/"
    f"{GAIA_SCORER_REVISION}/scorer.py"
)


def normalize_number_str(number_str: str) -> float:
    for char in ["$", "%", ","]:
        number_str = number_str.replace(char, "")
    try:
        return float(number_str)
    except ValueError:
        return float("inf")


def split_string(
    value: str,
    char_list: list[str] = [",", ";"],
) -> list[str]:
    pattern = f"[{''.join(char_list)}]"
    return re.split(pattern, value)


def question_scorer(
    model_answer: str | None,
    ground_truth: str,
) -> bool:
    def is_float(element: object) -> bool:
        try:
            float(element)
            return True
        except ValueError:
            return False

    if model_answer is None:
        model_answer = "None"

    if is_float(ground_truth):
        normalized_answer = normalize_number_str(model_answer)
        return normalized_answer == float(ground_truth)

    if any(char in ground_truth for char in [",", ";"]):
        ground_truth_elements = split_string(ground_truth)
        model_answer_elements = split_string(model_answer)
        if len(ground_truth_elements) != len(model_answer_elements):
            warnings.warn(
                "Answer lists have different lengths, returning False.",
                UserWarning,
                stacklevel=2,
            )
            return False

        comparisons = []
        for model_element, ground_truth_element in zip(
            model_answer_elements,
            ground_truth_elements,
        ):
            if is_float(ground_truth_element):
                comparisons.append(
                    normalize_number_str(model_element)
                    == float(ground_truth_element)
                )
            else:
                comparisons.append(
                    normalize_str(model_element, remove_punct=False)
                    == normalize_str(ground_truth_element, remove_punct=False)
                )
        return all(comparisons)

    return normalize_str(model_answer) == normalize_str(ground_truth)


def normalize_str(input_str: str, remove_punct: bool = True) -> str:
    no_spaces = re.sub(r"\s", "", input_str)
    if remove_punct:
        translator = str.maketrans("", "", string.punctuation)
        return no_spaces.lower().translate(translator)
    return no_spaces.lower()


__all__ = [
    "GAIA_SCORER_BLOB",
    "GAIA_SCORER_REVISION",
    "GAIA_SCORER_SHA256",
    "GAIA_SCORER_SOURCE_URL",
    "normalize_number_str",
    "normalize_str",
    "question_scorer",
    "split_string",
]

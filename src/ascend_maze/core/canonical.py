"""Deterministic, deeply immutable values and canonical byte encoding."""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Iterable, Iterator, Mapping
import hashlib
import json
import math
import unicodedata
from typing import Generic, TypeAlias, TypeVar, Union

from ascend_maze.core.errors import CanonicalizationError, LiteralSizeError

K = TypeVar("K")
V = TypeVar("V")


class FrozenMap(Mapping[K, V], Generic[K, V]):
    """An insertion-stable immutable mapping backed by immutable items."""

    __slots__ = ("_items", "_hash")
    _items: tuple[tuple[K, V], ...]
    _hash: int | None

    def __init__(self, items: Iterable[tuple[K, V]] = ()) -> None:
        frozen_items = tuple(items)
        seen: set[K] = set()
        for key, value in frozen_items:
            if key in seen:
                raise CanonicalizationError(f"duplicate canonical mapping key: {key!r}")
            seen.add(key)
        object.__setattr__(self, "_items", frozen_items)
        object.__setattr__(self, "_hash", None)

    def __getitem__(self, key: K) -> V:
        for existing_key, value in self._items:
            if existing_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[K]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenMap({self._items!r})"

    def __hash__(self) -> int:
        cached = self._hash
        if cached is None:
            cached = hash(self._items)
            object.__setattr__(self, "_hash", cached)
        return cached

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __reduce__(self) -> tuple[object, tuple[tuple[tuple[K, V], ...]]]:
        return (type(self), (self._items,))

    def items_tuple(self) -> tuple[tuple[K, V], ...]:
        return self._items


CanonicalScalar: TypeAlias = Union[None, bool, int, float, str, bytes]
CanonicalValue: TypeAlias = Union[
    CanonicalScalar,
    tuple["CanonicalValue", ...],
    FrozenMap["CanonicalValue", "CanonicalValue"],
]


def _normalise_string(value: str) -> str:
    normalised = unicodedata.normalize("NFC", value)
    try:
        normalised.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("strings must be valid UTF-8") from exc
    return normalised


def _encoded_node(value: CanonicalValue) -> object:
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite floats are not canonical")
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", b64encode(value).decode("ascii")]
    if isinstance(value, tuple):
        return ["tuple", [_encoded_node(item) for item in value]]
    if isinstance(value, FrozenMap):
        return [
            "map",
            [
                [_encoded_node(key), _encoded_node(item)]
                for key, item in value.items_tuple()
            ],
        ]
    raise CanonicalizationError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def _node_bytes(value: CanonicalValue) -> bytes:
    try:
        text = json.dumps(
            _encoded_node(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise CanonicalizationError("failed to encode canonical value") from exc


def freeze_canonical(
    value: object,
    *,
    _active_container_ids: set[int] | None = None,
) -> CanonicalValue:
    """Recursively convert supported literals to immutable canonical values."""

    if value is None or isinstance(value, (bool, int, bytes)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite floats are not canonical")
        return value
    if isinstance(value, str):
        return _normalise_string(value)
    active = _active_container_ids if _active_container_ids is not None else set()
    if isinstance(value, (tuple, list, Mapping, set, frozenset)):
        identity = id(value)
        if identity in active:
            raise CanonicalizationError("recursive containers are not supported")
        active.add(identity)
        try:
            if isinstance(value, (tuple, list)):
                return tuple(
                    freeze_canonical(item, _active_container_ids=active)
                    for item in value
                )
            if isinstance(value, Mapping):
                pairs: list[tuple[CanonicalValue, CanonicalValue]] = []
                for key, item in value.items():
                    frozen_key = freeze_canonical(key, _active_container_ids=active)
                    frozen_item = freeze_canonical(item, _active_container_ids=active)
                    pairs.append((frozen_key, frozen_item))
                pairs.sort(key=lambda pair: _node_bytes(pair[0]))
                return FrozenMap(pairs)

            frozen_items = [
                freeze_canonical(item, _active_container_ids=active)
                for item in value
            ]
            frozen_items.sort(key=_node_bytes)
            return tuple(frozen_items)
        finally:
            active.remove(identity)

    raise CanonicalizationError(
        "unsupported literal type "
        f"{type(value).__module__}.{type(value).__qualname__}; "
        "use workflow.input() for runtime values"
    )


def canonical_bytes(value: object) -> bytes:
    return _node_bytes(freeze_canonical(value))


def _decoded_node(node: object) -> CanonicalValue:
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        raise CanonicalizationError("invalid canonical node")
    tag = node[0]
    if tag == "none" and len(node) == 1:
        return None
    if tag == "bool" and len(node) == 2 and isinstance(node[1], bool):
        return node[1]
    if tag == "int" and len(node) == 2 and isinstance(node[1], str):
        try:
            return int(node[1])
        except ValueError as exc:
            raise CanonicalizationError("invalid canonical integer") from exc
    if tag == "float" and len(node) == 2 and isinstance(node[1], str):
        try:
            value = float.fromhex(node[1])
        except ValueError as exc:
            raise CanonicalizationError("invalid canonical float") from exc
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite canonical float")
        return value
    if tag == "str" and len(node) == 2 and isinstance(node[1], str):
        return _normalise_string(node[1])
    if tag == "bytes" and len(node) == 2 and isinstance(node[1], str):
        try:
            return b64decode(node[1], validate=True)
        except ValueError as exc:
            raise CanonicalizationError("invalid canonical bytes") from exc
    if tag == "tuple" and len(node) == 2 and isinstance(node[1], list):
        return tuple(_decoded_node(item) for item in node[1])
    if tag == "map" and len(node) == 2 and isinstance(node[1], list):
        pairs: list[tuple[CanonicalValue, CanonicalValue]] = []
        for pair in node[1]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise CanonicalizationError("invalid canonical mapping entry")
            pairs.append((_decoded_node(pair[0]), _decoded_node(pair[1])))
        result = FrozenMap(pairs)
        if _encoded_node(result) != node:
            raise CanonicalizationError("canonical mapping is not normalized")
        return result
    raise CanonicalizationError(f"unsupported canonical node tag: {tag}")


def decode_canonical_bytes(payload: bytes) -> CanonicalValue:
    """Decode bytes produced by canonical_bytes and reject non-canonical forms."""

    if not isinstance(payload, bytes):
        raise CanonicalizationError("canonical payload must be bytes")
    try:
        node = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalizationError("invalid canonical payload") from exc
    value = _decoded_node(node)
    if canonical_bytes(value) != payload:
        raise CanonicalizationError("payload is not in canonical byte form")
    return value


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def canonical_size(value: object) -> int:
    return len(canonical_bytes(value))


def freeze_literal(value: object, *, max_bytes: int) -> CanonicalValue:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
    ):
        raise ValueError("max_bytes must be a positive integer")
    frozen = freeze_canonical(value)
    size = len(_node_bytes(frozen))
    if size > max_bytes:
        raise LiteralSizeError(
            f"literal canonical size {size} exceeds max_literal_value_bytes={max_bytes}"
        )
    return frozen

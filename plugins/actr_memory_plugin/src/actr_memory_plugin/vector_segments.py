"""Budgeted segments of one memory's embedding input, and the vector ids that follow from them.

A memory that fits the embedding provider's budget keeps one vector under its own id. One that does
not is cut into windows that each fit (``split_to_fit``, the platform's shared splitter) and every
window is its own vector: segment 0 under the memory id, segment ``k >= 1`` under ``<memory id>:<k>``.
Memory ids are generated (``mem-`` plus base36), so a trailing ``:<k>`` is always a segment suffix and
a vector hit maps back to its memory by dropping it.

The vector service keys everything by ``external_id`` and has no prefix verb, so a memory's segments
are found by asking for ``<id>:1``, ``<id>:2`` ... until one is absent. They are written without
gaps, and the memory's own id is written last (``memory_store.store_memory_vectors``): it marks the
memory complete, so a write that stops part-way leaves the memory uncovered and the reconcile redoes it.
"""

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any, Final, cast

from ananta.core.domain.enums import ActionStatus
from ananta.error_handling import FrameworkError
from ananta.interfaces.embedding_service_interface import TokenBudget
from ananta.services.embedding_service.input_budget import InputBudgetError, split_to_fit
from ananta.services.memory_service.actr.constants import VECTOR_NAMESPACE

from .plugin_helpers import build_response

logger = logging.getLogger(__name__)

SEGMENT_SEPARATOR: Final[str] = ":"
_SEGMENT_SUFFIX: Final[re.Pattern[str]] = re.compile(rf"^(?P<memory_id>.+){SEGMENT_SEPARATOR}[1-9][0-9]*$")


def segment_external_id(memory_id: str, index: int) -> str:
    """The vector ``external_id`` of segment ``index``: the memory id for 0, ``<id>:<index>`` after."""
    if index < 0:
        raise ValueError(f"segment index must be >= 0, got {index}")
    return memory_id if index == 0 else f"{memory_id}{SEGMENT_SEPARATOR}{index}"


def memory_id_of(external_id: str) -> str:
    """The memory a vector's ``external_id`` belongs to: the id itself, or the id before a ``:<k>`` suffix."""
    match = _SEGMENT_SUFFIX.match(external_id)
    return match.group("memory_id") if match else external_id


def split_embedding_input(text: str, budget: TokenBudget | None) -> list[str]:
    """The windows of ``text`` to embed, together the whole text.

    A provider that declares no budget receives the complete input, as before. Otherwise the text is
    cut by the provider's own counter so every window is accepted by it; a text that fits is one window.
    Raises FrameworkError for a text no cut can fit (empty, or one character already over the budget).
    """
    if budget is None:
        return [text]
    try:
        return split_to_fit(text, budget.fits)
    except InputBudgetError as exc:
        raise FrameworkError(
            message=f"Cannot cut the embedding input to the provider budget: {exc}",
            error_code="memory.embedding_input_unsplittable",
        ) from exc


def missing_external_ids(vector_service: Any, external_ids: Sequence[str]) -> set[str]:
    """The ids with no ACTIVE vector, via the owning vector service. Fails fast on a missing service or a non-completed envelope."""
    if vector_service is None:
        raise FrameworkError(
            message="Vector service not available for orphan reconcile",
            error_code="memory.vector_service_unavailable",
        )
    if not external_ids:
        return set()
    result = vector_service.find_missing_external_ids(namespace=VECTOR_NAMESPACE, candidate_external_ids=list(external_ids))
    if result.get("action_status") != ActionStatus.COMPLETED.value:
        raise FrameworkError(
            message=f"find_missing_external_ids failed: {result.get('error')}",
            error_code="memory.vector_query_failed",
        )
    data = cast(dict[str, Any], result.get("data", {}))
    inner = cast(dict[str, Any], data.get("result", {}))
    return set(cast(list[str], inner.get("missing", [])))


def stored_vector_ids(vector_service: Any, memory_ids: Sequence[str]) -> list[str]:
    """The memories' own ids plus every ``<id>:<k>`` the store holds, k = 1, 2 ... up to each memory's first absent index."""
    ids = list(memory_ids)
    frontier = list(memory_ids)
    index = 1
    while frontier:
        candidates = {memory_id: segment_external_id(memory_id, index) for memory_id in frontier}
        absent = missing_external_ids(vector_service, list(candidates.values()))
        present = {memory_id: external_id for memory_id, external_id in candidates.items() if external_id not in absent}
        ids.extend(present.values())
        frontier = list(present)
        index += 1
    return ids


def delete_memory_vectors(vector_service: Any, memory_ids: Sequence[str], also: Sequence[str] = ()) -> dict[str, Any]:
    """Delete every vector of ``memory_ids`` (all segments) plus the ``also`` ids; returns the delete envelope.

    A failed lookup of the segments is returned as an error envelope, so each caller keeps its own
    policy for a failed vector delete (raise, or warn). Deleting an absent id is a no-op.
    """
    try:
        ids = [*stored_vector_ids(vector_service, memory_ids), *also]
    except FrameworkError as exc:
        logger.error("Could not list the vectors of %d memories to delete: %s", len(memory_ids), exc)
        return build_response(ActionStatus.ERROR.value, {}, {"type": "vector_query_failed", "code": exc.error_code, "message": str(exc)})
    result: dict[str, Any] = vector_service.delete_by_external_ids(namespace=VECTOR_NAMESPACE, external_ids=list(dict.fromkeys(ids)))
    return result


def uncovered_memory_ids(
    vector_service: Any,
    memory_ids: Sequence[str],
    text_of: Callable[[str], str],
    budget_of: Callable[[], TokenBudget | None],
) -> list[str]:
    """The memories whose vectors do not cover them, in input order.

    One membership read answers the common case: a memory without a vector under its own id is
    uncovered, and one with a vector but no ``<id>:1`` was embedded whole. A memory that has a
    ``<id>:1`` is segmented, and only then is its text cut again (``text_of``, ``budget_of``: the
    provider's counter runs for these few, never for every memory) to require every segment's vector.
    A segmented memory whose text cannot be cut is reported uncovered, so the reindex retries it and
    reports the refusal per record instead of this scan failing for everyone.
    """
    second = {memory_id: segment_external_id(memory_id, 1) for memory_id in memory_ids}
    absent = missing_external_ids(vector_service, [*memory_ids, *second.values()])
    uncovered = {memory_id for memory_id in memory_ids if memory_id in absent}
    segmented = [memory_id for memory_id in memory_ids if memory_id not in uncovered and second[memory_id] not in absent]
    if segmented:
        uncovered |= _memories_missing_a_segment(vector_service, segmented, text_of, budget_of())
    return [memory_id for memory_id in memory_ids if memory_id in uncovered]


def _memories_missing_a_segment(vector_service: Any, memory_ids: Sequence[str], text_of: Callable[[str], str], budget: TokenBudget | None) -> set[str]:
    expected: dict[str, list[str]] = {}
    uncuttable: set[str] = set()
    for memory_id in memory_ids:
        try:
            windows = split_embedding_input(text_of(memory_id), budget)
        except FrameworkError as exc:
            logger.warning("Segmented memory %s cannot be cut to the embedding budget; reindex will report it: %s", memory_id, exc)
            uncuttable.add(memory_id)
        else:
            expected[memory_id] = [segment_external_id(memory_id, index) for index in range(1, len(windows))]
    absent = missing_external_ids(vector_service, [external_id for ids in expected.values() for external_id in ids])
    return uncuttable | {memory_id for memory_id, ids in expected.items() if any(external_id in absent for external_id in ids)}

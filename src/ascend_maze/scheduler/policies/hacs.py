"""Deterministic HACS-noTP-static ordering without placement side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math
from time import perf_counter_ns

from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.scheduler.contracts import (
    DispatchProposal,
    PolicyCapabilities,
    QueueToken,
    SchedulableTaskView,
)


@dataclass(frozen=True, slots=True)
class HacsConfig:
    alpha: float = 2.0
    beta: float = 5.0
    initial_avg_dct_seconds: float = 60.0
    dct_ema_gamma: float = 0.1
    t_pred: float = 1.0

    def __post_init__(self) -> None:
        values = {
            "alpha": self.alpha,
            "beta": self.beta,
            "initial_avg_dct_seconds": self.initial_avg_dct_seconds,
            "dct_ema_gamma": self.dct_ema_gamma,
            "t_pred": self.t_pred,
        }
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("HACS parameters must be finite")
        if self.alpha <= 0:
            raise ValueError("alpha must be positive")
        if self.beta <= 1:
            raise ValueError("beta must be greater than one")
        if self.initial_avg_dct_seconds <= 0:
            raise ValueError("initial_avg_dct_seconds must be positive")
        if not 0 < self.dct_ema_gamma <= 1:
            raise ValueError("dct_ema_gamma must be in (0, 1]")
        if self.t_pred != 1.0:
            raise ValueError("HACS-noTP-static requires t_pred=1.0")


@dataclass(slots=True)
class HacsRunState:
    run_id: str
    submitted_at_ms: int
    total_value_tasks: int
    remaining_value_tasks: int
    succeeded_value_task_ids: set[str] = field(default_factory=set)
    priority_generation: int = 0


@dataclass(slots=True)
class HacsGlobalState:
    avg_dct_seconds: float
    completed_run_count: int = 0
    dct_generation: int = 0
    last_rebuild_ms: float = 0.0
    last_rebuild_task_count: int = 0


@dataclass(frozen=True, slots=True)
class HacsScore:
    omega: float
    phi: float
    log_score: float
    rank: float
    run_wait_seconds: float
    remaining_value_tasks: int
    priority_generation: int
    dct_generation: int


@dataclass(frozen=True, slots=True)
class _QueuedTask:
    partition: str
    view: SchedulableTaskView


_HeapEntry = tuple[float, int, str, str, int, int, int, QueueToken]


class HacsNoTpStaticPolicy:
    """Paper-aligned static HACS priority with indexed generation heaps."""

    name = "hacs_no_tp"
    version = "1"
    capabilities = PolicyCapabilities(
        requires_prediction=False,
        requires_static_topology=True,
        supports_incremental_dag=False,
        uses_cluster_snapshot=False,
    )

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        config: HacsConfig | None = None,
        scheduler_epoch_ms: int | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.config = config or HacsConfig()
        self.scheduler_epoch_ms = (
            self.clock.monotonic_ms()
            if scheduler_epoch_ms is None
            else scheduler_epoch_ms
        )
        if self.scheduler_epoch_ms < 0:
            raise ValueError("scheduler_epoch_ms must be non-negative")
        self._runs: dict[str, HacsRunState] = {}
        self._global = HacsGlobalState(self.config.initial_avg_dct_seconds)
        self._active: dict[QueueToken, _QueuedTask] = {}
        self._heaps: dict[str, list[_HeapEntry]] = {}

    def register_run(
        self,
        *,
        run_id: str,
        submitted_at_ms: int,
        total_value_tasks: int,
    ) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        if submitted_at_ms < 0:
            raise ValueError("submitted_at_ms must be non-negative")
        if total_value_tasks < 0:
            raise ValueError("total_value_tasks must be non-negative")
        existing = self._runs.get(run_id)
        if existing is not None:
            if (
                existing.submitted_at_ms != submitted_at_ms
                or existing.total_value_tasks != total_value_tasks
            ):
                raise ValueError(f"HACS run registration conflict: {run_id}")
            return
        self._runs[run_id] = HacsRunState(
            run_id=run_id,
            submitted_at_ms=submitted_at_ms,
            total_value_tasks=total_value_tasks,
            remaining_value_tasks=total_value_tasks,
        )

    def unregister_run(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        for token in tuple(self._active):
            if token.task_key.run_id == run_id:
                self.depart(token)

    def enqueue(self, partition: str, task: SchedulableTaskView) -> None:
        if not partition:
            raise ValueError("partition is required")
        token = task.queue_token
        if token.task_key.run_id not in self._runs:
            raise KeyError(f"HACS run is not registered: {token.task_key.run_id}")
        existing = self._active.get(token)
        queued = _QueuedTask(partition, task)
        if existing is not None:
            if existing != queued:
                raise ValueError("QueueToken cannot identify two different queued tasks")
            return
        self._active[token] = queued
        heapq.heappush(self._heaps.setdefault(partition, []), self._heap_entry(task))

    def depart(self, token: QueueToken) -> None:
        self._active.pop(token, None)

    def propose(self, partition: str, limit: int) -> tuple[DispatchProposal, ...]:
        if limit < 1:
            return ()
        heap = self._heaps.get(partition)
        if not heap:
            return ()
        selected: list[_HeapEntry] = []
        while heap and len(selected) < limit:
            entry = heapq.heappop(heap)
            if self._entry_is_current(partition, entry):
                selected.append(entry)
        for entry in selected:
            heapq.heappush(heap, entry)
        return tuple(self._proposal(entry[-1]) for entry in selected)

    def task_succeeded(self, *, run_id: str, task_id: str, task_kind: str) -> None:
        if task_kind != "npu":
            return
        state = self._runs.get(run_id)
        if state is None or task_id in state.succeeded_value_task_ids:
            return
        if state.remaining_value_tasks <= 0:
            raise RuntimeError(f"HACS N_val underflow for run: {run_id}")
        state.succeeded_value_task_ids.add(task_id)
        state.remaining_value_tasks -= 1
        state.priority_generation += 1
        for queued in self._active.values():
            if queued.view.queue_token.task_key.run_id == run_id:
                heapq.heappush(
                    self._heaps.setdefault(queued.partition, []),
                    self._heap_entry(queued.view),
                )

    def run_terminal(
        self,
        *,
        run_id: str,
        status: str,
        finished_at_ms: int,
    ) -> None:
        state = self._runs.pop(run_id, None)
        if state is None:
            return
        for token in tuple(self._active):
            if token.task_key.run_id == run_id:
                self.depart(token)
        if status != "succeeded":
            return
        duration_seconds = max(0.0, (finished_at_ms - state.submitted_at_ms) / 1000.0)
        gamma = self.config.dct_ema_gamma
        self._global.avg_dct_seconds = (
            (1.0 - gamma) * self._global.avg_dct_seconds
            + gamma * duration_seconds
        )
        self._global.completed_run_count += 1
        self._global.dct_generation += 1
        self._rebuild_heaps()

    def score_for(
        self,
        task: SchedulableTaskView,
        *,
        now_ms: int | None = None,
    ) -> HacsScore:
        state = self._runs[task.queue_token.task_key.run_id]
        current_ms = self.clock.monotonic_ms() if now_ms is None else now_ms
        run_wait_seconds = max(0.0, (current_ms - state.submitted_at_ms) / 1000.0)
        omega = math.log2(2.0 + 2.0 * task.depth_to_exit)
        phi = (
            run_wait_seconds
            / (self.config.alpha * self._global.avg_dct_seconds)
            - state.remaining_value_tasks
        )
        log_score = math.log(omega) + phi * math.log(self.config.beta)
        return HacsScore(
            omega=omega,
            phi=phi,
            log_score=log_score,
            rank=self._rank(task, state),
            run_wait_seconds=run_wait_seconds,
            remaining_value_tasks=state.remaining_value_tasks,
            priority_generation=state.priority_generation,
            dct_generation=self._global.dct_generation,
        )

    def run_state(self, run_id: str) -> HacsRunState:
        state = self._runs[run_id]
        return HacsRunState(
            run_id=state.run_id,
            submitted_at_ms=state.submitted_at_ms,
            total_value_tasks=state.total_value_tasks,
            remaining_value_tasks=state.remaining_value_tasks,
            succeeded_value_task_ids=set(state.succeeded_value_task_ids),
            priority_generation=state.priority_generation,
        )

    @property
    def global_state(self) -> HacsGlobalState:
        return HacsGlobalState(
            avg_dct_seconds=self._global.avg_dct_seconds,
            completed_run_count=self._global.completed_run_count,
            dct_generation=self._global.dct_generation,
            last_rebuild_ms=self._global.last_rebuild_ms,
            last_rebuild_task_count=self._global.last_rebuild_task_count,
        )

    def active_count(self) -> int:
        return len(self._active)

    def heap_record_count(self) -> int:
        return sum(len(heap) for heap in self._heaps.values())

    def _rank(self, task: SchedulableTaskView, state: HacsRunState) -> float:
        omega = math.log2(2.0 + 2.0 * task.depth_to_exit)
        submitted_offset_seconds = (
            state.submitted_at_ms - self.scheduler_epoch_ms
        ) / 1000.0
        return math.log(omega) - math.log(self.config.beta) * (
            submitted_offset_seconds
            / (self.config.alpha * self._global.avg_dct_seconds)
            + state.remaining_value_tasks
        )

    def _heap_entry(self, task: SchedulableTaskView) -> _HeapEntry:
        token = task.queue_token
        state = self._runs[token.task_key.run_id]
        return (
            -self._rank(task, state),
            task.enqueue_sequence,
            token.task_key.run_id,
            token.task_key.task_id,
            token.queue_generation,
            state.priority_generation,
            self._global.dct_generation,
            token,
        )

    def _entry_is_current(self, partition: str, entry: _HeapEntry) -> bool:
        token = entry[-1]
        queued = self._active.get(token)
        state = self._runs.get(token.task_key.run_id)
        return (
            queued is not None
            and queued.partition == partition
            and state is not None
            and entry[5] == state.priority_generation
            and entry[6] == self._global.dct_generation
        )

    def _proposal(self, token: QueueToken) -> DispatchProposal:
        queued = self._active[token]
        started_ns = perf_counter_ns()
        score = self.score_for(queued.view)
        score_compute_ms = (perf_counter_ns() - started_ns) / 1_000_000
        return DispatchProposal(
            task_key=token.task_key,
            queue_generation=token.queue_generation,
            policy_metadata=(
                ("N_desc", queued.view.depth_to_exit),
                ("N_val", score.remaining_value_tasks),
                ("run_wait_seconds", score.run_wait_seconds),
                ("T_pred", self.config.t_pred),
                ("source", "disabled_constant"),
                ("avg_DCT_seconds", self._global.avg_dct_seconds),
                ("alpha", self.config.alpha),
                ("beta", self.config.beta),
                ("omega", score.omega),
                ("phi", score.phi),
                ("log_score", score.log_score),
                ("rank", score.rank),
                ("priority_generation", score.priority_generation),
                ("dct_generation", score.dct_generation),
                ("last_rebuild_ms", self._global.last_rebuild_ms),
                ("last_rebuild_task_count", self._global.last_rebuild_task_count),
            ),
            score_compute_ms=score_compute_ms,
        )

    def _rebuild_heaps(self) -> None:
        started_ns = perf_counter_ns()
        rebuilt: dict[str, list[_HeapEntry]] = {}
        for queued in self._active.values():
            rebuilt.setdefault(queued.partition, []).append(
                self._heap_entry(queued.view)
            )
        for heap in rebuilt.values():
            heapq.heapify(heap)
        self._heaps = rebuilt
        self._global.last_rebuild_ms = (perf_counter_ns() - started_ns) / 1_000_000
        self._global.last_rebuild_task_count = len(self._active)


class LinearScanReferenceQueue(HacsNoTpStaticPolicy):
    """O(N) reference selector used to validate heap event semantics."""

    def propose(self, partition: str, limit: int) -> tuple[DispatchProposal, ...]:
        if limit < 1:
            return ()
        candidates = [
            queued.view
            for queued in self._active.values()
            if queued.partition == partition
        ]
        candidates.sort(
            key=lambda task: (
                -self.score_for(task).rank,
                task.enqueue_sequence,
                task.queue_token.task_key.run_id,
                task.queue_token.task_key.task_id,
                task.queue_token.queue_generation,
            )
        )
        return tuple(
            self._proposal(task.queue_token) for task in candidates[:limit]
        )

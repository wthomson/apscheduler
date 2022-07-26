from __future__ import annotations

import os
import platform
import random
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from typing import Any, Callable, Iterable, Mapping, cast
from uuid import UUID, uuid4

import anyio
import attrs
from anyio import TASK_STATUS_IGNORED, create_task_group, move_on_after
from anyio.abc import TaskGroup, TaskStatus

from ..abc import AsyncDataStore, AsyncEventBroker, Job, Schedule, Subscription, Trigger
from ..context import current_scheduler
from ..converters import as_async_datastore, as_async_eventbroker
from ..datastores.memory import MemoryDataStore
from ..enums import CoalescePolicy, ConflictPolicy, JobOutcome, RunState
from ..eventbrokers.async_local import LocalAsyncEventBroker
from ..events import (
    Event,
    JobReleased,
    ScheduleAdded,
    SchedulerStarted,
    SchedulerStopped,
    ScheduleUpdated,
)
from ..exceptions import JobCancelled, JobDeadlineMissed, JobLookupError
from ..marshalling import callable_to_ref
from ..structures import JobResult, Task
from ..workers.async_ import AsyncWorker

_microsecond_delta = timedelta(microseconds=1)
_zero_timedelta = timedelta()


@attrs.define(eq=False)
class AsyncScheduler:
    """An asynchronous (AnyIO based) scheduler implementation."""

    data_store: AsyncDataStore = attrs.field(
        converter=as_async_datastore, factory=MemoryDataStore
    )
    event_broker: AsyncEventBroker = attrs.field(
        converter=as_async_eventbroker, factory=LocalAsyncEventBroker
    )
    identity: str = attrs.field(kw_only=True, default=None)
    start_worker: bool = attrs.field(kw_only=True, default=True)
    logger: Logger | None = attrs.field(kw_only=True, default=getLogger(__name__))

    _state: RunState = attrs.field(init=False, default=RunState.stopped)
    _task_group: TaskGroup | None = attrs.field(init=False, default=None)
    _wakeup_event: anyio.Event = attrs.field(init=False)
    _wakeup_deadline: datetime | None = attrs.field(init=False, default=None)
    _schedule_added_subscription: Subscription = attrs.field(init=False)

    def __attrs_post_init__(self) -> None:
        if not self.identity:
            self.identity = f"{platform.node()}-{os.getpid()}-{id(self)}"

    async def __aenter__(self):
        self._task_group = create_task_group()
        await self._task_group.__aenter__()
        await self._task_group.start(self._run)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
        self._task_group = None

    def _schedule_added_or_modified(self, event: Event) -> None:
        event_ = cast("ScheduleAdded | ScheduleUpdated", event)
        if not self._wakeup_deadline or (
            event_.next_fire_time and event_.next_fire_time < self._wakeup_deadline
        ):
            self.logger.debug(
                "Detected a %s event – waking up the scheduler", type(event).__name__
            )
            self._wakeup_event.set()

    async def add_schedule(
        self,
        func_or_task_id: str | Callable,
        trigger: Trigger,
        *,
        id: str | None = None,
        args: Iterable | None = None,
        kwargs: Mapping[str, Any] | None = None,
        coalesce: CoalescePolicy = CoalescePolicy.latest,
        misfire_grace_time: float | timedelta | None = None,
        max_jitter: float | timedelta | None = None,
        tags: Iterable[str] | None = None,
        conflict_policy: ConflictPolicy = ConflictPolicy.do_nothing,
    ) -> str:
        id = id or str(uuid4())
        args = tuple(args or ())
        kwargs = dict(kwargs or {})
        tags = frozenset(tags or ())
        if isinstance(misfire_grace_time, (int, float)):
            misfire_grace_time = timedelta(seconds=misfire_grace_time)

        if callable(func_or_task_id):
            task = Task(id=callable_to_ref(func_or_task_id), func=func_or_task_id)
            await self.data_store.add_task(task)
        else:
            task = await self.data_store.get_task(func_or_task_id)

        schedule = Schedule(
            id=id,
            task_id=task.id,
            trigger=trigger,
            args=args,
            kwargs=kwargs,
            coalesce=coalesce,
            misfire_grace_time=misfire_grace_time,
            max_jitter=max_jitter,
            tags=tags,
        )
        schedule.next_fire_time = trigger.next()
        await self.data_store.add_schedule(schedule, conflict_policy)
        self.logger.info(
            "Added new schedule (task=%r, trigger=%r); next run time at %s",
            task,
            trigger,
            schedule.next_fire_time,
        )
        return schedule.id

    async def get_schedule(self, id: str) -> Schedule:
        schedules = await self.data_store.get_schedules({id})
        return schedules[0]

    async def remove_schedule(self, schedule_id: str) -> None:
        await self.data_store.remove_schedules({schedule_id})

    async def add_job(
        self,
        func_or_task_id: str | Callable,
        *,
        args: Iterable | None = None,
        kwargs: Mapping[str, Any] | None = None,
        tags: Iterable[str] | None = None,
    ) -> UUID:
        """
        Add a job to the data store.

        :param func_or_task_id:
        :param args: positional arguments to call the target callable with
        :param kwargs: keyword arguments to call the target callable with
        :param tags:
        :return: the ID of the newly created job

        """
        if callable(func_or_task_id):
            task = Task(id=callable_to_ref(func_or_task_id), func=func_or_task_id)
            await self.data_store.add_task(task)
        else:
            task = await self.data_store.get_task(func_or_task_id)

        job = Job(
            task_id=task.id,
            args=args or (),
            kwargs=kwargs or {},
            tags=tags or frozenset(),
        )
        await self.data_store.add_job(job)
        return job.id

    async def get_job_result(self, job_id: UUID, *, wait: bool = True) -> JobResult:
        """
        Retrieve the result of a job.

        :param job_id: the ID of the job
        :param wait: if ``True``, wait until the job has ended (one way or another), ``False`` to
                     raise an exception if the result is not yet available
        :raises JobLookupError: if the job does not exist in the data store

        """
        wait_event = anyio.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                wait_event.set()

        with self.data_store.events.subscribe(listener, {JobReleased}):
            result = await self.data_store.get_job_result(job_id)
            if result:
                return result
            elif not wait:
                raise JobLookupError(job_id)

            await wait_event.wait()

        result = await self.data_store.get_job_result(job_id)
        assert isinstance(result, JobResult)
        return result

    async def run_job(
        self,
        func_or_task_id: str | Callable,
        *,
        args: Iterable | None = None,
        kwargs: Mapping[str, Any] | None = None,
        tags: Iterable[str] | None = (),
    ) -> Any:
        """
        Convenience method to add a job and then return its result (or raise its exception).

        :returns: the return value of the target function

        """
        job_complete_event = anyio.Event()

        def listener(event: JobReleased) -> None:
            if event.job_id == job_id:
                job_complete_event.set()

        job_id: UUID | None = None
        with self.data_store.events.subscribe(listener, {JobReleased}):
            job_id = await self.add_job(
                func_or_task_id, args=args, kwargs=kwargs, tags=tags
            )
            await job_complete_event.wait()

        result = await self.get_job_result(job_id)
        if result.outcome is JobOutcome.success:
            return result.return_value
        elif result.outcome is JobOutcome.error:
            raise result.exception
        elif result.outcome is JobOutcome.missed_start_deadline:
            raise JobDeadlineMissed
        elif result.outcome is JobOutcome.cancelled:
            raise JobCancelled
        else:
            raise RuntimeError(f"Unknown job outcome: {result.outcome}")

    async def stop(self) -> None:
        """
        Signal the scheduler that it should stop processing schedules.

        This method does not wait for the scheduler to actually stop.
        For that, see :meth:`wait_until_stopped`.

        """
        if self._state is RunState.started:
            self._state = RunState.stopping
            self._wakeup_event.set()

    async def wait_until_stopped(self) -> None:
        """
        Wait until the scheduler is in the "stopped" or "stopping" state.

        If the scheduler is already stopped or in the process of stopping, this method
        returns immediately. Otherwise, it waits until the scheduler posts the
        ``SchedulerStopped`` event.

        """
        if self._state in (RunState.stopped, RunState.stopping):
            return

        event = anyio.Event()
        with self.event_broker.subscribe(
            lambda ev: event.set(), {SchedulerStopped}, one_shot=True
        ):
            await event.wait()

    async def _run(self, *, task_status: TaskStatus = TASK_STATUS_IGNORED) -> None:
        if self._state is not RunState.stopped:
            raise RuntimeError(
                f'Cannot start the scheduler when it is in the "{self._state}" '
                f"state"
            )

        self._state = RunState.starting
        async with AsyncExitStack() as exit_stack:
            self._wakeup_event = anyio.Event()

            # Initialize the event broker
            await self.event_broker.start()
            exit_stack.push_async_exit(
                lambda *exc_info: self.event_broker.stop(force=exc_info[0] is not None)
            )

            # Initialize the data store
            await self.data_store.start(self.event_broker)
            exit_stack.push_async_exit(
                lambda *exc_info: self.data_store.stop(force=exc_info[0] is not None)
            )

            # Wake up the scheduler if the data store emits a significant schedule event
            exit_stack.enter_context(
                self.event_broker.subscribe(
                    self._schedule_added_or_modified, {ScheduleAdded, ScheduleUpdated}
                )
            )

            # Start the built-in worker, if configured to do so
            if self.start_worker:
                token = current_scheduler.set(self)
                exit_stack.callback(current_scheduler.reset, token)
                worker = AsyncWorker(
                    self.data_store, self.event_broker, is_internal=True
                )
                await exit_stack.enter_async_context(worker)

            # Signal that the scheduler has started
            self._state = RunState.started
            task_status.started()
            await self.event_broker.publish_local(SchedulerStarted())

            exception: BaseException | None = None
            try:
                while self._state is RunState.started:
                    schedules = await self.data_store.acquire_schedules(
                        self.identity, 100
                    )
                    now = datetime.now(timezone.utc)
                    for schedule in schedules:
                        # Calculate a next fire time for the schedule, if possible
                        fire_times = [schedule.next_fire_time]
                        calculate_next = schedule.trigger.next
                        while True:
                            try:
                                fire_time = calculate_next()
                            except Exception:
                                self.logger.exception(
                                    "Error computing next fire time for schedule %r of "
                                    "task %r – removing schedule",
                                    schedule.id,
                                    schedule.task_id,
                                )
                                break

                            # Stop if the calculated fire time is in the future
                            if fire_time is None or fire_time > now:
                                schedule.next_fire_time = fire_time
                                break

                            # Only keep all the fire times if coalesce policy = "all"
                            if schedule.coalesce is CoalescePolicy.all:
                                fire_times.append(fire_time)
                            elif schedule.coalesce is CoalescePolicy.latest:
                                fire_times[0] = fire_time

                        # Add one or more jobs to the job queue
                        max_jitter = (
                            schedule.max_jitter.total_seconds()
                            if schedule.max_jitter
                            else 0
                        )
                        for i, fire_time in enumerate(fire_times):
                            # Calculate a jitter if max_jitter > 0
                            jitter = _zero_timedelta
                            if max_jitter:
                                if i + 1 < len(fire_times):
                                    next_fire_time = fire_times[i + 1]
                                else:
                                    next_fire_time = schedule.next_fire_time

                                if next_fire_time is not None:
                                    # Jitter must never be so high that it would cause a
                                    # fire time to equal or exceed the next fire time
                                    jitter_s = min(
                                        [
                                            max_jitter,
                                            (
                                                next_fire_time
                                                - fire_time
                                                - _microsecond_delta
                                            ).total_seconds(),
                                        ]
                                    )
                                    jitter = timedelta(
                                        seconds=random.uniform(0, jitter_s)
                                    )
                                    fire_time += jitter

                            schedule.last_fire_time = fire_time
                            job = Job(
                                task_id=schedule.task_id,
                                args=schedule.args,
                                kwargs=schedule.kwargs,
                                schedule_id=schedule.id,
                                scheduled_fire_time=fire_time,
                                jitter=jitter,
                                start_deadline=schedule.next_deadline,
                                tags=schedule.tags,
                            )
                            await self.data_store.add_job(job)

                    # Update the schedules (and release the scheduler's claim on them)
                    await self.data_store.release_schedules(self.identity, schedules)

                    # If we received fewer schedules than the maximum amount, sleep
                    # until the next schedule is due or the scheduler is explicitly
                    # woken up
                    wait_time = None
                    if len(schedules) < 100:
                        self._wakeup_deadline = (
                            await self.data_store.get_next_schedule_run_time()
                        )
                        if self._wakeup_deadline:
                            wait_time = (
                                self._wakeup_deadline - datetime.now(timezone.utc)
                            ).total_seconds()
                            self.logger.debug(
                                "Sleeping %.3f seconds until the next fire time (%s)",
                                wait_time,
                                self._wakeup_deadline,
                            )
                        else:
                            self.logger.debug("Waiting for any due schedules to appear")

                        with move_on_after(wait_time):
                            await self._wakeup_event.wait()
                            self._wakeup_event = anyio.Event()
                    else:
                        self.logger.debug(
                            "Processing more schedules on the next iteration"
                        )
            except BaseException as exc:
                exception = exc
                raise
            finally:
                self._state = RunState.stopped
                if isinstance(exception, Exception):
                    self.logger.exception("Scheduler crashed")
                elif exception:
                    self.logger.info(
                        f"Scheduler stopped due to {exception.__class__.__name__}"
                    )
                else:
                    self.logger.info("Scheduler stopped")

                with move_on_after(3, shield=True):
                    await self.event_broker.publish_local(
                        SchedulerStopped(exception=exception)
                    )

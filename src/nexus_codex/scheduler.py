from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from nexus_codex.config import load_config
from nexus_codex.cron import CronSchedule
from nexus_codex.runtime import Runtime


class JobScheduler:
    def __init__(self, config_path: str) -> None:
        self.runtime = Runtime(config_path)
        self.loaded = load_config(config_path)
        self.schedules = {
            job_id: CronSchedule.parse(job.schedule)
            for job_id, job in self.loaded.jobs.items()
            if job.enabled
        }
        self.executor = ThreadPoolExecutor(max_workers=self.loaded.app.max_concurrent_runs)
        self.futures: dict[str, Future[int]] = {}
        now = datetime.now(self.runtime.timezone)
        self.next_runs = {
            job_id: schedule.next_after(now)
            for job_id, schedule in self.schedules.items()
        }
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        try:
            while not self.stop_event.is_set():
                self.run_tick()
                time.sleep(max(1, self.loaded.app.poll_interval_seconds))
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def run_tick(self) -> None:
        now = datetime.now(self.runtime.timezone).replace(second=0, microsecond=0)
        self._prune_futures()
        for job_id, job in self.loaded.jobs.items():
            if not job.inbox_dir or not job.enabled:
                continue
            allowed, _ = self.runtime.can_schedule_job(job_id)
            if not allowed:
                continue
            self.runtime.dispatch_inbox(job_id)
        for job_id, next_run in list(self.next_runs.items()):
            if next_run > now:
                continue
            allowed, _ = self.runtime.can_schedule_job(job_id)
            if not allowed:
                self.next_runs[job_id] = self.schedules[job_id].next_after(now)
                continue
            self.runtime.dispatch_job(job_id, trigger="schedule")
            self.next_runs[job_id] = self.schedules[job_id].next_after(now)
        for job_id, job in self.loaded.jobs.items():
            if not job.enabled or job_id in self.futures:
                continue
            allowed, _ = self.runtime.can_schedule_job(job_id)
            if not allowed:
                continue
            if self.runtime.next_ready_task(job_id) is None:
                continue
            self.futures[job_id] = self.executor.submit(self.runtime.run_next_task, job_id)

    def stop(self) -> None:
        self.stop_event.set()

    def _prune_futures(self) -> None:
        finished = [job_id for job_id, future in self.futures.items() if future.done()]
        for job_id in finished:
            del self.futures[job_id]

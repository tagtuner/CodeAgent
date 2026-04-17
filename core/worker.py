from __future__ import annotations
import asyncio
import os
import uuid
from typing import AsyncIterator


class SubWorker:
    """Persistent shell subprocess for streaming command execution."""

    def __init__(self, work_dir: str = "/tmp/codeagent-worker"):
        self.work_dir = work_dir
        self.process: asyncio.subprocess.Process | None = None
        self.buffer: list[str] = []
        self.state = "idle"
        self.current_cmd: str | None = None
        self.exit_code: int | None = None

    async def ensure_started(self):
        if self.process and self.process.returncode is None:
            return
        os.makedirs(self.work_dir, exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            "/bin/bash", "--norc", "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.work_dir,
        )
        ready_marker = f"__CA_READY_{uuid.uuid4().hex[:8]}__"
        self.process.stdin.write(f'echo "{ready_marker}"\n'.encode())
        await self.process.stdin.drain()
        while True:
            raw = await self.process.stdout.readline()
            if not raw or ready_marker in raw.decode(errors="replace"):
                break
        self.state = "idle"

    async def execute(self, command: str) -> AsyncIterator[str]:
        await self.ensure_started()
        self.state = "running"
        self.current_cmd = command
        self.exit_code = None

        marker = f"__CA_END_{uuid.uuid4().hex[:12]}__"
        wrapped = f'{command}\necho "{marker}$?"\n'
        self.process.stdin.write(wrapped.encode())
        await self.process.stdin.drain()

        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                self.state = "error"
                self.exit_code = -1
                break
            line = raw.decode(errors="replace").rstrip("\n\r")
            if marker in line:
                after = line.split(marker, 1)[1].strip()
                try:
                    self.exit_code = int(after)
                except ValueError:
                    self.exit_code = -1
                self.state = "done"
                break
            self.buffer.append(line)
            yield line

        self.current_cmd = None

    def get_buffer(self, last_n: int = 50) -> str:
        lines = self.buffer[-last_n:] if last_n else self.buffer
        return "\n".join(lines)

    def clear_buffer(self):
        self.buffer.clear()

    async def kill(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            try:
                await self.process.wait()
            except Exception:
                pass
        self.process = None
        self.state = "idle"
        self.current_cmd = None

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
        self.process = None


class WorkerPool:
    """Manages up to MAX_WORKERS parallel SubWorker instances."""

    MAX_WORKERS = 5

    def __init__(self, work_dir: str = "/tmp/codeagent-worker"):
        self.work_dir = work_dir
        self.workers: dict[int, SubWorker] = {}
        self._next_id = 1

    def create(self) -> tuple[int, SubWorker] | None:
        self._cleanup_finished()
        if len(self.workers) >= self.MAX_WORKERS:
            return None
        wid = self._next_id
        self._next_id += 1
        w = SubWorker(work_dir=f"{self.work_dir}/w{wid}")
        self.workers[wid] = w
        return wid, w

    def _cleanup_finished(self):
        finished = [wid for wid, w in self.workers.items()
                    if w.state in ("done", "idle", "error") and w.current_cmd is None]
        for wid in finished:
            del self.workers[wid]

    def get(self, worker_id: int) -> SubWorker | None:
        return self.workers.get(worker_id)

    def active_count(self) -> int:
        return sum(1 for w in self.workers.values() if w.state == "running")

    def all_buffers(self, last_n: int = 20) -> str:
        parts = []
        for wid, w in self.workers.items():
            if w.buffer:
                cmd = w.current_cmd or "(finished)"
                parts.append(f"[W{wid}: {cmd}]\n{w.get_buffer(last_n)}")
        return "\n\n".join(parts)

    async def kill(self, worker_id: int):
        w = self.workers.get(worker_id)
        if w:
            await w.kill()

    async def kill_all(self):
        for w in self.workers.values():
            await w.kill()

    async def close_all(self):
        for w in self.workers.values():
            await w.close()
        self.workers.clear()

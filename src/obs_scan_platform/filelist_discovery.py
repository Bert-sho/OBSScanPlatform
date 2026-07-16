from dataclasses import dataclass, field

from obs_scan_platform.models import RootDiscovery


@dataclass(frozen=True)
class FilelistTask:
    path: str
    depth: int


@dataclass
class FilelistDiscoveryScheduler:
    max_depth: int
    task_limit: int
    _current_level: list[FilelistTask] = field(default_factory=lambda: [FilelistTask("/", 1)])
    _next_level: list[FilelistTask] = field(default_factory=list)
    _queued_paths: set[str] = field(default_factory=lambda: {"/"})
    _scanned_paths: set[str] = field(default_factory=set)
    _discovered_prefixes: set[str] = field(default_factory=set)
    _direct_files: list[str] = field(default_factory=list)
    _rollback_prefixes: set[str] = field(default_factory=set)
    _rollback_direct_files: list[str] = field(default_factory=list)
    _current_level_depth: int = 1
    _total_tasks: int = 1
    _completed_tasks: int = 0

    @property
    def total_tasks(self) -> int:
        return self._total_tasks

    @property
    def completed_tasks(self) -> int:
        return self._completed_tasks

    @property
    def current_level_depth(self) -> int:
        return self._current_level_depth

    @property
    def pending_total_tasks(self) -> int:
        if self._next_level and self._total_tasks < self.task_limit:
            return self._total_tasks + len(self._next_level)
        return self._total_tasks

    def current_level(self) -> list[FilelistTask]:
        tasks = [task for task in self._current_level if task.path not in self._scanned_paths]
        self._current_level = []
        if tasks:
            self._rollback_prefixes = {"/"} if tasks[0].depth == 1 else set(self._discovered_prefixes)
            self._rollback_direct_files = list(self._direct_files)
            self._current_level_depth = tasks[0].depth
        return tasks

    def _metadata_files(self) -> list[str]:
        prefixes = sorted(self._discovered_prefixes)
        return [
            object_key
            for object_key in self._direct_files
            if not any(object_key.startswith(prefix) for prefix in prefixes)
        ]

    @property
    def metadata_task_count(self) -> int:
        return len(self._metadata_files())

    def rollback_current_level(self) -> int:
        self._discovered_prefixes = set(self._rollback_prefixes)
        self._direct_files = list(self._rollback_direct_files)
        self._current_level = []
        self._next_level = []
        return len(self._discovered_prefixes)

    def record_folder(self, task: FilelistTask, prefix: str) -> None:
        if not prefix:
            return
        self._discovered_prefixes.add(prefix)
        queued_path = "/" + prefix
        if task.depth < self.max_depth and queued_path not in self._queued_paths:
            self._next_level.append(FilelistTask(queued_path, task.depth + 1))
            self._queued_paths.add(queued_path)

    def record_file(self, task: FilelistTask, object_key: str) -> None:
        del task
        if object_key:
            self._direct_files.append(str(object_key))

    def record_empty(self, task: FilelistTask) -> None:
        prefix = task.path.strip("/")
        if prefix:
            empty_prefix = f"{prefix}/"
            self._rollback_prefixes.discard(empty_prefix)
            self._rollback_direct_files = [
                object_key
                for object_key in self._rollback_direct_files
                if not object_key.startswith(empty_prefix)
            ]
        self.record_expanded(task)

    def record_expanded(self, task: FilelistTask) -> None:
        prefix = task.path.strip("/")
        if prefix:
            self._discovered_prefixes.discard(f"{prefix}/")

    def record_failed(self, task: FilelistTask) -> None:
        prefix = task.path.strip("/")
        if not prefix:
            return
        failed_prefix = f"{prefix}/"
        self._discovered_prefixes = {
            discovered_prefix
            for discovered_prefix in self._discovered_prefixes
            if discovered_prefix == failed_prefix or not discovered_prefix.startswith(failed_prefix)
        }
        self._direct_files = [
            object_key for object_key in self._direct_files if not object_key.startswith(failed_prefix)
        ]
        self._next_level = [
            queued_task for queued_task in self._next_level if not queued_task.path.startswith(task.path)
        ]
        self._queued_paths = {
            queued_path
            for queued_path in self._queued_paths
            if queued_path == task.path or not queued_path.startswith(task.path)
        }

    def mark_completed(self, task: FilelistTask) -> None:
        self._scanned_paths.add(task.path)
        self._completed_tasks += 1

    def finish_level(self) -> bool:
        if not self._next_level:
            return False
        if self._total_tasks >= self.task_limit:
            self._next_level = []
            return False
        self._current_level = self._next_level
        self._next_level = []
        self._total_tasks += len(self._current_level)
        return True

    def result(self) -> RootDiscovery:
        prefixes = sorted(self._discovered_prefixes)
        return RootDiscovery(prefixes=prefixes, metadata_files=self._metadata_files())

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
    _total_tasks: int = 1
    _completed_tasks: int = 0

    @property
    def total_tasks(self) -> int:
        return self._total_tasks

    @property
    def completed_tasks(self) -> int:
        return self._completed_tasks

    @property
    def pending_total_tasks(self) -> int:
        if self._next_level and self._total_tasks < self.task_limit:
            return self._total_tasks + len(self._next_level)
        return self._total_tasks

    def current_level(self) -> list[FilelistTask]:
        tasks = [task for task in self._current_level if task.path not in self._scanned_paths]
        self._current_level = []
        return tasks

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
            self._discovered_prefixes.discard(f"{prefix}/")

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
        prefixes = self._top_level_prefixes(self._discovered_prefixes)
        metadata_files = [
            object_key
            for object_key in self._direct_files
            if not any(object_key.startswith(prefix) for prefix in prefixes)
        ]
        return RootDiscovery(prefixes=prefixes, metadata_files=metadata_files)

    def _top_level_prefixes(self, prefixes: set[str]) -> list[str]:
        selected: list[str] = []
        for prefix in sorted(prefixes):
            if not any(prefix.startswith(parent) for parent in selected):
                selected.append(prefix)
        return selected

"""Small local shared-folder storage used by the standalone merge node."""

from pathlib import Path
import pickle
import time
from typing import Any


class LocalFolderWithBytes:
    def __init__(self, directory: str | Path, retry_sleep_time: int = 3, max_retry: int = 3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retry_sleep_time = retry_sleep_time
        self.max_retry = max_retry

    def _get_success_flag_file(self, key: str) -> Path:
        return self.directory / ("success_" + key)

    def _delete_success_flag(self, key: str) -> None:
        filepath = self._get_success_flag_file(key)
        if filepath.exists():
            filepath.unlink()

    def _put_success_flag(self, key: str) -> None:
        filepath = self._get_success_flag_file(key)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text("", encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        success_flag_file = self._get_success_flag_file(key)
        patience = self.max_retry
        while not success_flag_file.exists():
            time.sleep(self.retry_sleep_time)
            patience -= 1
            if patience == 0:
                return default

        filepath = self.directory / key
        return filepath.read_bytes() if filepath.exists() else default

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise TypeError(f"value must be bytes, but got {type(value)}")
        filepath = self.directory / key
        self._delete_success_flag(key)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_bytes(value)
        self._put_success_flag(key)

    def __delitem__(self, key: str) -> None:
        filepath = self.directory / key
        if filepath.exists():
            filepath.unlink()

    def __len__(self) -> int:
        return len(list(self.directory.glob("*")))

    def items(self):
        for filepath in self.directory.glob("*"):
            key = str(filepath.relative_to(self.directory))
            yield key, self.get(key)


class LocalFolder:
    def __init__(self, directory: str | Path, retry_sleep_time: int = 3, max_retry: int = 3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.suffix = ".pkl"
        self.retry_sleep_time = retry_sleep_time
        self.max_retry = max_retry

    def _get_success_flag_file(self, key: str) -> Path:
        return self.directory / ("success_" + key)

    def _delete_success_flag(self, key: str) -> None:
        filepath = self._get_success_flag_file(key)
        if filepath.exists():
            filepath.unlink()

    def _put_success_flag(self, key: str) -> None:
        filepath = self._get_success_flag_file(key)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text("", encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        success_flag_file = self._get_success_flag_file(key)
        patience = self.max_retry
        while not success_flag_file.exists():
            time.sleep(self.retry_sleep_time)
            patience -= 1
            if patience == 0:
                return default

        filepath = self.directory / (key + self.suffix)
        if not filepath.exists():
            return default
        with filepath.open("rb") as file:
            return pickle.load(file)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if value is None:
            raise ValueError("value must not be None")
        filepath = self.directory / (key + self.suffix)
        self._delete_success_flag(key)
        with filepath.open("wb") as file:
            pickle.dump(value, file)
        self._put_success_flag(key)

    def __delitem__(self, key: str) -> None:
        filepath = self.directory / (key + self.suffix)
        if filepath.exists():
            filepath.unlink()

    def __len__(self) -> int:
        return len(list(self.directory.glob(f"*{self.suffix}")))

    def items(self):
        for filepath in self.directory.glob(f"*{self.suffix}"):
            yield self.get_parameter(filepath)

    def get_parameter(self, filepath: Path):
        key = filepath.name[: -len(self.suffix)]
        try:
            return key, self.get(key)
        except EOFError:
            return None, None

    def get_raw_folder(self) -> LocalFolderWithBytes:
        return LocalFolderWithBytes(
            directory=self.directory,
            retry_sleep_time=self.retry_sleep_time,
            max_retry=self.max_retry,
        )
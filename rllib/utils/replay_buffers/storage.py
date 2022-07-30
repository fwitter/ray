import logging
import numpy as np
import os
import pickle
import shelve
from pathlib import Path
import math

# Import ray before psutil will make sure we use psutil's bundled version
import ray  # noqa F401
import psutil
import shutil

from abc import abstractmethod
from collections.abc import Sized, Iterable
from typing import Optional, Dict, Any, Iterator, overload
from tempfile import TemporaryDirectory

from ray.rllib.utils.annotations import ExperimentalAPI, override
from ray.rllib.utils.metrics.window_stat import WindowStat
from ray.rllib.utils.typing import SampleBatchType
from ray.util.debug import log_once

logger = logging.getLogger(__name__)


@ExperimentalAPI
class LocalStorage(Sized, Iterable):
    @ExperimentalAPI
    def __init__(
        self,
        capacity_items: int = 10000,
        capacity_ts: int = math.inf,
        capacity_bytes: int = math.inf,
    ) -> None:
        """Initializes an empty LocalStorage instance for storing timesteps in a ring buffer.

        The storage is indexed for fast random access of stored items and takes care
        of properly adding and removing items with respect to its capacity.

        Args:
            capacity_items: Maximum number of items to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones. The number has to be finite in order to keep track of the
                item hit count.
            capacity_ts: Maximum number of timesteps to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones.
            capacity_bytes: Maximum number of bytes to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones.
        """
        assert all(
            c > 0 for c in [capacity_items, capacity_ts, capacity_bytes]
        ), "Replay buffer storage capacities have to be greater than zero."

        assert capacity_items != math.inf

        self._capacity_ts = capacity_ts
        self._capacity_items = capacity_items
        self._capacity_bytes = capacity_bytes

        # Whether we have already hit our capacity (and have therefore
        # started to evict older samples).
        self._eviction_started = False
        # Number of items currently in storage (num_items <= max_items)
        self._num_items = 0
        # Index of first, i.e. oldest, item in storage (offset_idx < max_items)
        self._oldest_item_idx = 0

        # Number of (single) timesteps that have been added to the buffer
        # over its lifetime. Note that each added item (batch) may contain
        # more than one timestep.
        self._num_timesteps_added = 0
        # Number of timesteps currently in storage
        # (num_items <= num_timesteps <= capacity_ts)
        self._num_timesteps = 0

        # Statistics
        # len(self._hit_count) == capacity_items
        self._hit_count = np.zeros(self._capacity_items, dtype=np.int64)
        self._evicted_hit_stats = WindowStat("evicted_hit", 1000)
        self._size_bytes = 0

    @ExperimentalAPI
    @property
    def capacity_ts(self) -> int:
        """Maximum number of timesteps the storage may contain."""
        return self._capacity_ts

    @ExperimentalAPI
    @property
    def capacity_items(self) -> int:
        """Maximum number of items the storage may contain."""
        return self._capacity_items

    @ExperimentalAPI
    @property
    def capacity_bytes(self) -> int:
        """Maximum number of bytes the storage may contain."""
        return self._capacity_bytes

    @ExperimentalAPI
    @property
    def size_bytes(self) -> int:
        """Current size of the data inside the storage in bytes."""
        return self._size_bytes

    @ExperimentalAPI
    @property
    def evicted_hit_stats(self) -> Dict[str, Any]:
        """Hit statistics for items in storage including mean, std, and quantiles."""
        return self._evicted_hit_stats.stats()

    @ExperimentalAPI
    @property
    def eviction_started(self) -> bool:
        """Whether eviction of items started, i.e. storage is "full"."""
        return self._eviction_started

    @ExperimentalAPI
    @property
    def num_timesteps_added(self) -> int:
        """Total number of timesteps added to the storage over its lifetime."""
        return self._num_timesteps_added

    @ExperimentalAPI
    @property
    def num_timesteps(self) -> int:
        """Number of timesteps currently in the storage."""
        return self._num_timesteps

    @ExperimentalAPI
    def get_state(self) -> Dict[str, Any]:
        """Returns all local state.

        Returns:
            The serializable local state.
        """
        state = {
            "_capacity_ts": self._capacity_ts,
            "_capacity_items": self._capacity_items,
            "_capacity_bytes": self.capacity_bytes,
            "_num_items": self._num_items,
            "_oldest_item_idx": self._oldest_item_idx,
            "_eviction_started": self._eviction_started,
            "_num_timesteps_added": self._num_timesteps_added,
            "_num_timesteps": self._num_timesteps,
            "_size_bytes": self._size_bytes,
        }
        return state

    @ExperimentalAPI
    def set_state(self, state: Dict[str, Any]) -> None:
        """Restores all local state to the provided `state`.

        Args:
            state: The new state to set this buffer. Can be
                obtained by calling `self.get_state()`.
        """
        self._capacity_ts = state["_capacity_ts"]
        self._capacity_items = state["_capacity_items"]
        self._capacity_bytes = state["_capacity_bytes"]
        self._num_items = state["_num_items"]
        self._oldest_item_idx = state["_oldest_item_idx"]
        self._eviction_started = state["_eviction_started"]
        self._num_timesteps_added = state["_num_timesteps_added"]
        self._num_timesteps = state["_num_timesteps"]
        self._size_bytes = state["_size_bytes"]
        self._hit_count = np.zeros(self._capacity_items, dtype=np.int64)

    @ExperimentalAPI
    def __len__(self) -> int:
        return self._num_items

    @ExperimentalAPI
    def __iter__(self) -> Iterator[SampleBatchType]:
        for i in range(len(self)):
            yield self[i]

    @overload
    def __getitem__(self, key: int) -> SampleBatchType:
        ...

    @overload
    def __getitem__(self, key: slice) -> "StorageView":
        ...

    @ExperimentalAPI
    def __getitem__(self, key):
        if isinstance(key, int):
            i = key
            while i < 0:
                i += len(self)
            while i >= len(self):
                i -= len(self)
            idx = self._get_internal_index(i)
            self._hit_count[idx] += 1
            return self._get(idx)
        elif isinstance(key, slice):
            s = key
            return StorageView(self, s)
        else:
            raise TypeError("Only single integer indices or slices are supported.")

    @ExperimentalAPI
    def __setitem__(self, i: int, item: SampleBatchType) -> None:
        if not isinstance(i, int):
            raise ValueError(
                "Only single integer indices supported for setting values."
            )
        if i >= len(self) or i < 0:
            raise IndexError("Buffer index out of range.")
        if not self.eviction_started:
            raise RuntimeError(
                "Assigning items to an index is only allowed "
                "after eviction has been started. Use .add(item) instead."
            )
        idx = self._get_internal_index(i)
        drop_item = self.__delitem__(idx)
        if drop_item.count < item.count:
            logger.warning(
                "New item consists of more timesteps than "
                "the replaced item. This violates storage capacity."
            )
        self._evicted_hit_stats.push(self._hit_count[idx])
        self._num_timesteps -= drop_item.count
        self._size_bytes -= drop_item.size_bytes()
        self._hit_count[idx] = 0
        self._num_timesteps_added += item.count
        self._num_timesteps += item.count
        self._size_bytes += item.size_bytes()
        self._set(idx, item)

    @ExperimentalAPI
    def add(self, item: SampleBatchType) -> None:
        """Add a new item to the storage. The index of the new item
        will be assigned automatically. Moreover, old items may be
        dropped with respect to the storage's capacity contraints.

        Args:
            item: Item (batch) to add to the storage.
        """
        if item.count > self._capacity_items:
            logger.warning(
                "The batch to be added consists of {} timesteps "
                "which is larger than the storage capacity of {}. "
                "Therefore, the batch has not been added.".format(
                    item.count, self._capacity_items
                )
            )
            return

        self._num_timesteps_added += item.count
        self._num_timesteps += item.count
        self._size_bytes += item.size_bytes()

        # Drop old items.
        # May require multiple drops if newly added item
        # contains more timesteps than the old items.
        while (
            self._num_timesteps > self.capacity_ts
            or self._size_bytes > self.capacity_bytes
            or self._num_items
            # >= for num_items, because we add 1 to num_items below
            >= self._capacity_items
        ):
            assert self._num_items > 0
            self._eviction_started = True
            self._evicted_hit_stats.push(self._hit_count[self._oldest_item_idx])
            self._hit_count[self._oldest_item_idx] = 0
            drop_item = self.__delitem__(self._oldest_item_idx)
            self._num_timesteps -= drop_item.count
            self._size_bytes -= drop_item.size_bytes()
            self._num_items -= 1
            self._oldest_item_idx = self._get_internal_index(1)  # Increase offset

        # Insert new item.
        # Compute index to set new item at in circular storage.
        # Wrap around once we hit capacity.
        new_idx = self._get_internal_index(self._num_items)
        self._set(new_idx, item)
        self._num_items += 1
        assert self._num_items <= self._capacity_items

    def _get_internal_index(self, idx: int):
        """Translate the given external storage index into
        the internal index space of the circular buffer.

        Args:
            idx: External storage index (0 <= idx < len(storage)).

        Returns:
            Internal index from interval [0, max_items)
        """
        if idx < 0:
            raise IndexError("Buffer index out of range")
        return (self._oldest_item_idx + idx) % max(1, self._capacity_items)

    def _get_external_index(self, idx: int):
        """Translate the given internal circular buffer index into
        the external index space of the storage.

        Args:
            idx: Internal circular Buffer index (0 <= idx < max_items).

        Returns:
            External index from interval [0, len(storage))
        """
        if idx < 0:
            raise IndexError("Buffer index out of range")
        if idx >= self._oldest_item_idx:
            return idx - self._oldest_item_idx
        else:
            return idx + self._capacity_items - self._oldest_item_idx

    @abstractmethod
    def _get(self, idx: int) -> SampleBatchType:
        """Get the item at the specified index / key.

        This method must be implementend by subclasses
        using an actual data structure for storing the data.
        This data structure must be capable of dealing with
        indices between 0 <= idx < `self._capacity_items`.

        Args:
            idx: Index of the item of interest.

        Returns:
            Item at index.
        """
        raise NotImplementedError()

    @abstractmethod
    def _set(self, idx: int, item: SampleBatchType) -> None:
        """Store the given item at the specified index / key.

        This method must be implementend by subclasses
        using an actual data structure for storing the data.
        This data structure must be capable of dealing with
        indices between 0 <= idx < `self._capacity_items`.

        Args:
            idx: Index to store the item at.
            item: Item to store at specified index.
        """
        raise NotImplementedError()

    @abstractmethod
    def __delitem__(self, idx: int) -> SampleBatchType:
        """Remove and return the item at the specified index / key.

        This method may be overridden by subclasses
        using an actual data structure for storing the data.
        This data structure must be capable of dealing with
        indices between 0 <= idx < `self._capacity_items`.

        Note: Removing the item from the actual data structure is
        not required for a properly working storage but is highly
        recommended to reduce its memory footprint.

        Args:
            idx: Index of the item of interest.

        Returns:
            Item at index that has been removed.
        """
        raise NotImplementedError()


@ExperimentalAPI
class StorageView(LocalStorage):
    @ExperimentalAPI
    @override(LocalStorage)
    def __init__(
        self,
        storage: LocalStorage,
        storage_slice: slice,
    ) -> None:
        """Initializes a read-only StorageView instance of a LocalStorage.

        Args:
            storage: Underlying storage.
            storage_slice: Slice of the storage
        """
        self._storage = storage
        step = storage_slice.step or 1
        if step > 0:
            start = storage_slice.start or 0
            stop = storage_slice.stop or len(storage)
        else:
            start = storage_slice.start or (len(storage) - 1)
            stop = storage_slice.stop or -(len(storage) + 1)
        self._slice = slice(start, stop, step)
        if step < 0 and storage_slice.stop is None:
            stop += len(storage)
        self._idx_map = list(range(start, stop, step))

    @ExperimentalAPI
    @property
    def slice(self) -> slice:
        """Slice of the StorageView."""
        return self._slice

    @ExperimentalAPI
    @property
    def capacity(self) -> int:
        """Maximum number of timesteps the storage may contain."""
        return self._storage.capacity

    @ExperimentalAPI
    @property
    def size_bytes(self) -> int:
        """Current size of the data inside the storage in bytes."""
        return self._storage.size_bytes

    @ExperimentalAPI
    @property
    def evicted_hit_stats(self) -> Dict[str, Any]:
        """Hit statistics for items in storage including mean, std, and quantiles."""
        return self._storage.evicted_hit_stats

    @ExperimentalAPI
    @property
    def eviction_started(self) -> bool:
        """Whether eviction of items started, i.e. storage is "full"."""
        return self._storage.eviction_started

    @ExperimentalAPI
    @property
    def num_timesteps_added(self) -> int:
        """Total number of timesteps added to the storage over its lifetime."""
        return self._storage.num_timesteps_added

    @ExperimentalAPI
    @property
    def num_timesteps(self) -> int:
        """Number of timesteps currently in the storage."""
        return self._storage.num_timesteps

    @override(LocalStorage)
    def get_state(self) -> Dict[str, Any]:
        raise RuntimeError("The view of a storage is stateless.")

    @override(LocalStorage)
    def set_state(self, state: Dict[str, Any]) -> None:
        raise RuntimeError("The view of a storage is stateless.")

    @ExperimentalAPI
    @override(LocalStorage)
    def __len__(self) -> int:
        return len(self._idx_map)

    @ExperimentalAPI
    @override(LocalStorage)
    def __getitem__(self, key):
        if isinstance(key, int):
            i = key
            while i < 0:
                i += len(self)
            while i >= len(self):
                i -= len(self)
            idx = self._idx_map[i]
            return self._storage[idx]
        elif isinstance(key, slice):
            s = key
            return StorageView(self, s)
        else:
            raise TypeError("Only single integer indices or slices are supported.")

    @override(LocalStorage)
    def __setitem__(self, i: int, item: SampleBatchType) -> None:
        raise RuntimeError("The view of a storage is read-only.")

    @override(LocalStorage)
    def add(self, item: SampleBatchType) -> None:
        raise RuntimeError("The view of a storage is read-only.")

    @override(LocalStorage)
    def _get(self, idx: int) -> SampleBatchType:
        raise RuntimeError("The view of a storage is read-only.")

    @override(LocalStorage)
    def _set(self, idx: int, item: SampleBatchType) -> None:
        raise RuntimeError("The view of a storage is read-only.")

    @override(LocalStorage)
    def __delitem__(self, idx: int) -> SampleBatchType:
        raise RuntimeError("The view of a storage is read-only.")


@ExperimentalAPI
class InMemoryStorage(LocalStorage):
    @ExperimentalAPI
    @override(LocalStorage)
    def __init__(
        self,
        capacity_items: int = 10000,
        capacity_ts: int = math.inf,
        capacity_bytes: int = math.inf,
    ) -> None:
        """Initializes an empty LocalStorage instance for storing timesteps in a ring buffer.

        The storage is indexed for fast random access of stored items and takes care
        of properly adding and removing items with respect to its capacity.

        Args:
            capacity_items: Maximum number of items to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones. The number has to be finite in order to keep track of the
                item hit count.
            capacity_ts: Maximum number of timesteps to store in this FIFO
            capacity_bytes: Maximum number of bytes to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones.
        """
        super().__init__(
            capacity_ts=capacity_ts,
            capacity_items=capacity_items,
            capacity_bytes=capacity_bytes,
        )
        self._samples = [None] * self._capacity_items

    @ExperimentalAPI
    @override(LocalStorage)
    def get_state(self) -> Dict[str, Any]:
        state = super().get_state()
        state["_samples"] = self._samples
        return state

    @ExperimentalAPI
    @override(LocalStorage)
    def set_state(self, state: Dict[str, Any]) -> None:
        self._samples = state["_samples"]
        super().set_state(state)

    @override(LocalStorage)
    def _get(self, i: int) -> SampleBatchType:
        return self._samples[i]

    @override(LocalStorage)
    def _set(self, idx: int, item: SampleBatchType) -> None:
        self._warn_replay_capacity(item, self._capacity_items / max(1, item.count))
        if idx == len(self._samples):
            self._samples.append(item)
        else:
            self._samples[idx] = item

    @override(LocalStorage)
    def __delitem__(self, i: int) -> SampleBatchType:
        del_sample = self._samples[i]
        self._samples[i] = None
        self._evicted_hit_stats.push(self._hit_count[i])
        self._hit_count[i] = 0
        return del_sample

    def _warn_replay_capacity(self, item: SampleBatchType, num_items: int) -> None:
        """Warn if the configured replay buffer capacity is too large."""
        item_size = item.size_bytes()
        ts_size = item_size / item.count
        psutil_mem = psutil.virtual_memory()
        free_gb = psutil_mem.available / 1e9
        if self._capacity_ts == math.inf:
            max_size_for_ts_capacity = -1
        else:
            max_size_for_ts_capacity = self.capacity_ts * ts_size

        self.est_final_size = (
            max(self._capacity_items * item_size, max_size_for_ts_capacity) / 1e9
        )
        remainder = self.est_final_size - self.size_bytes / 1e9
        msg = (
            "Estimated memory usage for replay buffer is {} GB "
            "({} batches of size {}, {} bytes each), "
            "of which {} GB are pending for allocation. "
            "Available disk space is {} GB.".format(
                self.est_final_size,
                self._capacity_items,
                item.count,
                item_size,
                remainder,
                free_gb,
            )
        )

        remainder = self.est_final_size - self.size_bytes / 1e9

        if remainder > free_gb:
            raise ValueError(msg)
        elif remainder > 0.2 * free_gb:
            if log_once("warning_replay_buffer_storage_capacity_disk"):
                logger.warning(msg)
        else:
            if log_once("warning_replay_buffer_storage_capacity_disk"):
                logger.info(msg)


@ExperimentalAPI
class OnDiskStorage(LocalStorage):
    @ExperimentalAPI
    @override(LocalStorage)
    def __init__(
        self,
        capacity_items: int = 10000,
        capacity_ts: int = math.inf,
        capacity_bytes: int = math.inf,
        buffer_dir: Optional[str] = None,
    ) -> None:
        """Initializes an OnDiskStorage instance for storing timesteps on disk.
        This allows replay buffers larger than memory.

        The storage uses Python's shelve as data structure.

        Args:
            capacity_items: Maximum number of items to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones. The number has to be finite in order to keep track of the
                item hit count.
            capacity_ts: Maximum number of timesteps to store in this FIFO
            capacity_bytes: Maximum number of bytes to store in this FIFO buffer.
                After reaching this number, older samples will be dropped to make space
                for new ones.
            buffer_dir: Optional buffer directory to write the data to. If the
                directory exists, the buffer inside will be overwritten.
        """
        super().__init__(
            capacity_ts=capacity_ts,
            capacity_items=capacity_items,
            capacity_bytes=capacity_bytes,
        )
        self._buffer_file_dir = buffer_dir
        self._rm_file_on_del = False

        if not self._buffer_file_dir:
            self._rm_file_on_del = True
            with TemporaryDirectory(prefix="rllib_replay_buffer_storage_") as d:
                self._buffer_file_dir = d

        if os.path.exists(self._buffer_file_dir):
            logger.warning(
                "On-disk replay buffer is writing to an already created db " "file."
            )

        self._buffer_file = self._buffer_file_dir + "/db"
        Path(self._buffer_file).mkdir(parents=True, exist_ok=True)

        # The actual storage (shelf / dict of SampleBatches).
        if pickle.HIGHEST_PROTOCOL < 5:
            logger.warning(
                "Recommended pickle protocol is at least 5 "
                "for fast zero-copy access of arrays. This may compromise the "
                "performance of your on-disk replay buffer."
            )
        self._samples = shelve.open(
            self._buffer_file, flag="n", protocol=pickle.HIGHEST_PROTOCOL
        )
        # Make sure shelve created correct file for storage

        matching_db_files = [
            filename
            for filename in os.listdir(self._buffer_file_dir)
            if filename.startswith("db")
        ]

        if len(matching_db_files) > 1:
            logger.warning(
                "There appear to be multiple on-disk replay buffer "
                "database files inside your storage folder {}. "
                "Delete all but one of the files {} to resolve this "
                "warning."
            )
        if len(matching_db_files) == 0:
            raise ValueError(
                "No replay buffer database file was created at {} for "
                "the on-disk replay buffer.".format(self._buffer_file)
            )

    @ExperimentalAPI
    @override(LocalStorage)
    def get_state(self) -> Dict[str, Any]:
        state = super().get_state()
        state["_buffer_file"] = self._buffer_file
        state["_rm_file_on_del"] = self._rm_file_on_del
        state["_pkl_proto"] = pickle.HIGHEST_PROTOCOL
        # Never delete file since it will be reused later
        self._rm_file_on_del = False
        return state

    @ExperimentalAPI
    @override(LocalStorage)
    def set_state(self, state: Dict[str, Any]) -> None:
        # Clean up existing storage
        self._samples.close()
        if self._buffer_file != state["_buffer_file"] and self._rm_file_on_del:
            os.remove(self._buffer_file)
        # Restore given storage
        self._buffer_file = state["_buffer_file"]
        self._rm_file_on_del = state["_rm_file_on_del"]
        self._samples = shelve.open(
            self._buffer_file[:-4], flag="w", protocol=state["_pkl_proto"]
        )
        super().set_state(state)

    @override(LocalStorage)
    def _get(self, i: int) -> SampleBatchType:
        return self._samples[str(i)]

    @override(LocalStorage)
    def _set(self, idx: int, item: SampleBatchType) -> None:
        self._warn_replay_capacity(item, self._capacity_items / item.count)
        self._samples[str(idx)] = item
        self._samples.sync()

    @override(LocalStorage)
    def __delitem__(self, i: int) -> SampleBatchType:
        # Do not delete item since this leads to continuously
        # increasing file size
        # https://github.com/python/cpython/blob/4153f2cbcb41a1a9057bfba28d5f65d48ea39283/Lib/dbm/dumb.py#L11-L12
        drop_item = self._samples[str(i)]
        self._evicted_hit_stats.push(self._hit_count[i])
        self._hit_count[i] = 0
        # del self._samples[str(i)]
        return drop_item

    def __del__(self) -> None:
        if self._samples is not None:
            self._samples.close()
        if self._rm_file_on_del and os.path.exists(self._buffer_file_dir):
            try:
                os.remove(self._buffer_file)
                os.rmdir(self._buffer_file_dir)
            except PermissionError:
                logger.error(
                    "Lacking permission to remove on-disk replay buffer "
                    "storage files at path `{}`. Remove them manually and "
                    "set permissions accordingly to avoid this error in the "
                    "future.".format(self._buffer_file_dir)
                )

    def _warn_replay_capacity(self, item: SampleBatchType, num_items: int) -> None:
        """Warn if the configured replay buffer capacity is too large."""
        item_size = item.size_bytes()
        ts_size = item_size / item.count
        shutil_du = shutil.disk_usage(os.path.dirname(self._buffer_file_dir))
        free_gb = shutil_du.free / 1e9
        if self._capacity_ts == math.inf:
            max_size_for_ts_capacity = -1
        else:
            max_size_for_ts_capacity = self.capacity_ts * ts_size

        self.est_final_size = (
            max(self._capacity_items * item_size, max_size_for_ts_capacity) / 1e9
        )
        remainder = self.est_final_size - self.size_bytes / 1e9
        msg = (
            "Estimated disk usage for replay buffer is {} GB "
            "({} batches of size {}, {} bytes each), "
            "of which {} GB are pending for allocation. "
            "Available disk space is {} GB.".format(
                self.est_final_size,
                self._capacity_items,
                item.count,
                item_size,
                remainder,
                free_gb,
            )
        )

        remainder = self.est_final_size - self.size_bytes / 1e9

        if remainder > free_gb:
            raise ValueError(msg)
        elif remainder > 0.2 * free_gb:
            if log_once("warning_replay_buffer_storage_capacity_disk"):
                logger.warning(msg)
        else:
            if log_once("warning_replay_buffer_storage_capacity_disk"):
                logger.info(msg)

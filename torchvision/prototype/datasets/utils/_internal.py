import enum
import gzip
import io
import lzma
import os
import os.path
import pathlib
import pickle
from typing import (
    Sequence,
    Callable,
    Union,
    Any,
    Tuple,
    TypeVar,
    Iterator,
    Dict,
    Optional,
    IO,
    Sized,
)
from typing import cast

import numpy as np
import PIL.Image
import torch.distributed as dist
import torch.utils.data
from torch.utils.data import IterDataPipe
from torchdata.datapipes.iter import IoPathFileLister, IoPathFileLoader
from torchdata.datapipes.utils import StreamWrapper


__all__ = [
    "INFINITE_BUFFER_SIZE",
    "BUILTIN_DIR",
    "read_mat",
    "image_buffer_from_array",
    "SequenceIterator",
    "MappingIterator",
    "Enumerator",
    "getitem",
    "path_accessor",
    "path_comparator",
    "Decompressor",
]

K = TypeVar("K")
D = TypeVar("D")

# pseudo-infinite until a true infinite buffer is supported by all datapipes
INFINITE_BUFFER_SIZE = 1_000_000_000

BUILTIN_DIR = pathlib.Path(__file__).parent.parent / "_builtin"


def read_mat(buffer: io.IOBase, **kwargs: Any) -> Any:
    try:
        import scipy.io as sio
    except ImportError as error:
        raise ModuleNotFoundError("Package `scipy` is required to be installed to read .mat files.") from error

    if isinstance(buffer, StreamWrapper):
        buffer = buffer.file_obj

    return sio.loadmat(buffer, **kwargs)


def image_buffer_from_array(array: np.ndarray, *, format: str = "png") -> io.BytesIO:
    image = PIL.Image.fromarray(array)
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    buffer.seek(0)
    return buffer


class SequenceIterator(IterDataPipe[D]):
    def __init__(self, datapipe: IterDataPipe[Sequence[D]]):
        self.datapipe = datapipe

    def __iter__(self) -> Iterator[D]:
        for sequence in self.datapipe:
            yield from iter(sequence)


class MappingIterator(IterDataPipe[Union[Tuple[K, D], D]]):
    def __init__(self, datapipe: IterDataPipe[Dict[K, D]], *, drop_key: bool = False) -> None:
        self.datapipe = datapipe
        self.drop_key = drop_key

    def __iter__(self) -> Iterator[Union[Tuple[K, D], D]]:
        for mapping in self.datapipe:
            yield from iter(mapping.values() if self.drop_key else mapping.items())  # type: ignore[call-overload]


class Enumerator(IterDataPipe[Tuple[int, D]]):
    def __init__(self, datapipe: IterDataPipe[D], start: int = 0) -> None:
        self.datapipe = datapipe
        self.start = start

    def __iter__(self) -> Iterator[Tuple[int, D]]:
        yield from enumerate(self.datapipe, self.start)


def getitem(*items: Any) -> Callable[[Any], Any]:
    def wrapper(obj: Any) -> Any:
        for item in items:
            obj = obj[item]
        return obj

    return wrapper


def path_accessor(getter: Union[str, Callable[[pathlib.Path], D]]) -> Callable[[Tuple[str, Any]], D]:
    if isinstance(getter, str):
        name = getter

        def getter(path: pathlib.Path) -> D:
            return cast(D, getattr(path, name))

    def wrapper(data: Tuple[str, Any]) -> D:
        return getter(pathlib.Path(data[0]))  # type: ignore[operator]

    return wrapper


def path_comparator(getter: Union[str, Callable[[pathlib.Path], D]], value: D) -> Callable[[Tuple[str, Any]], bool]:
    accessor = path_accessor(getter)

    def wrapper(data: Tuple[str, Any]) -> bool:
        return accessor(data) == value

    return wrapper


class CompressionType(enum.Enum):
    GZIP = "gzip"
    LZMA = "lzma"


class Decompressor(IterDataPipe[Tuple[str, io.IOBase]]):
    types = CompressionType

    _DECOMPRESSORS = {
        types.GZIP: lambda file: gzip.GzipFile(fileobj=file),
        types.LZMA: lambda file: lzma.LZMAFile(file),
    }

    def __init__(
        self,
        datapipe: IterDataPipe[Tuple[str, io.IOBase]],
        *,
        type: Optional[Union[str, CompressionType]] = None,
    ) -> None:
        self.datapipe = datapipe
        if isinstance(type, str):
            type = self.types(type.upper())
        self.type = type

    def _detect_compression_type(self, path: str) -> CompressionType:
        if self.type:
            return self.type

        # TODO: this needs to be more elaborate
        ext = os.path.splitext(path)[1]
        if ext == ".gz":
            return self.types.GZIP
        elif ext == ".xz":
            return self.types.LZMA
        else:
            raise RuntimeError("FIXME")

    def __iter__(self) -> Iterator[Tuple[str, io.IOBase]]:
        for path, file in self.datapipe:
            type = self._detect_compression_type(path)
            decompressor = self._DECOMPRESSORS[type]
            yield path, decompressor(file)


class PicklerDataPipe(IterDataPipe):
    def __init__(self, source_datapipe: IterDataPipe[Tuple[str, IO[bytes]]]) -> None:
        self.source_datapipe = source_datapipe

    def __iter__(self) -> Iterator[Any]:
        for _, fobj in self.source_datapipe:
            data = pickle.load(fobj)
            for _, d in enumerate(data):
                yield d


class SharderDataPipe(torch.utils.data.datapipes.iter.grouping.ShardingFilterIterDataPipe):
    def __init__(self, source_datapipe: IterDataPipe) -> None:
        super().__init__(source_datapipe)
        self.rank = 0
        self.world_size = 1
        if dist.is_available() and dist.is_initialized():
            self.rank = dist.get_rank()
            self.world_size = dist.get_world_size()
        self.apply_sharding(self.world_size, self.rank)

    def __iter__(self) -> Iterator[Any]:
        num_workers = self.world_size
        worker_id = self.rank
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            worker_id = worker_id + worker_info.id * num_workers
            num_workers *= worker_info.num_workers
        self.apply_sharding(num_workers, worker_id)
        yield from super().__iter__()


class TakerDataPipe(IterDataPipe):
    def __init__(self, source_datapipe: IterDataPipe, num_take: int) -> None:
        super().__init__()
        self.source_datapipe = source_datapipe
        self.num_take = num_take
        self.world_size = 1
        if dist.is_available() and dist.is_initialized():
            self.world_size = dist.get_world_size()

    def __iter__(self) -> Iterator[Any]:
        num_workers = self.world_size
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            num_workers *= worker_info.num_workers

        # TODO: this is weird as it drops more elements than it should
        num_take = self.num_take // num_workers

        for i, data in enumerate(self.source_datapipe):
            if i < num_take:
                yield data
            else:
                break

    def __len__(self) -> int:
        num_take = self.num_take // self.world_size
        if isinstance(self.source_datapipe, Sized):
            if len(self.source_datapipe) < num_take:
                num_take = len(self.source_datapipe)
        # TODO: might be weird to not take `num_workers` into account
        return num_take


def _make_sharded_datapipe(root: str, dataset_size: int) -> IterDataPipe:
    dp = IoPathFileLister(root=root)
    dp = SharderDataPipe(dp)
    dp = dp.shuffle(buffer_size=INFINITE_BUFFER_SIZE)
    dp = IoPathFileLoader(dp, mode="rb")
    dp = PicklerDataPipe(dp)
    # dp = dp.cycle(2)
    dp = TakerDataPipe(dp, dataset_size)
    return dp

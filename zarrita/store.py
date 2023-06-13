import asyncio
import io
from pathlib import Path
from typing import List, Optional, Tuple, Union

import fsspec
from fsspec.asyn import AsyncFileSystem

from zarrita.common import BytesLike, to_thread


def _dereference_path(root: str, path: str) -> str:
    assert isinstance(root, str)
    assert isinstance(path, str)
    path = f"{root}/{path}" if root != "" else path
    path = path.rstrip("/")
    return path


class StorePath:
    store: "Store"
    path: str

    def __init__(self, store: "Store", path: Optional[str] = None):
        self.store = store
        self.path = path or ""

    @classmethod
    def from_path(cls, pth: "Path") -> "StorePath":
        return cls(Store.from_path(pth))

    async def get_async(
        self, byte_range: Optional[Tuple[int, int]] = None
    ) -> Optional[BytesLike]:
        return await self.store.get_async(self.path, byte_range)

    async def set_async(
        self, value: BytesLike, byte_range: Optional[Tuple[int, int]] = None
    ) -> None:
        await self.store.set_async(self.path, value, byte_range)

    async def delete_async(self) -> None:
        await self.store.delete_async(self.path)

    async def exists_async(self) -> bool:
        return await self.store.exists_async(self.path)

    def __truediv__(self, other: str) -> "StorePath":
        return self.__class__(self.store, _dereference_path(self.path, other))

    def __str__(self) -> str:
        return _dereference_path(str(self.store), self.path)

    def __repr__(self) -> str:
        return f"StorePath({self.store.__class__.__name__}, {repr(str(self))})"


class Store:
    @classmethod
    def from_path(cls, pth: "Path") -> "Store":
        try:
            from upath import UPath

            if isinstance(pth, UPath):
                storage_options = pth._kwargs.copy()
                storage_options.pop("_url", None)
                return RemoteStore(str(pth), **storage_options)
        except ImportError:
            pass

        return LocalStore(pth)

    async def multi_get_async(
        self, keys: List[Tuple[str, Optional[Tuple[int, int]]]]
    ) -> List[Optional[BytesLike]]:
        return await asyncio.gather(
            *[self.get_async(key, byte_range) for key, byte_range in keys]
        )

    async def get_async(
        self, key: str, byte_range: Optional[Tuple[int, int]] = None
    ) -> Optional[BytesLike]:
        raise NotImplementedError

    async def multi_set_async(
        self, key_values: List[Tuple[str, BytesLike, Optional[Tuple[int, int]]]]
    ) -> None:
        await asyncio.gather(
            *[
                self.set_async(key, value, byte_range)
                for key, value, byte_range in key_values
            ]
        )

    async def set_async(
        self, key: str, value: BytesLike, byte_range: Optional[Tuple[int, int]] = None
    ) -> None:
        raise NotImplementedError

    async def delete_async(self, key: str) -> None:
        raise NotImplementedError

    async def exists_async(self, key: str) -> bool:
        raise NotImplementedError

    def __truediv__(self, other: str) -> StorePath:
        return StorePath(self, other)


class LocalStore(Store):
    root: Path
    auto_mkdir: bool

    def __init__(self, root: Union[Path, str], auto_mkdir: bool = True):
        if isinstance(root, str):
            root = Path(root)
        assert isinstance(root, Path)

        self.root = root
        self.auto_mkdir = auto_mkdir

    def _cat_file(
        self, path: Path, start: Optional[int] = None, end: Optional[int] = None
    ) -> BytesLike:
        if start is None and end is None:
            return path.read_bytes()
        with path.open("rb") as f:
            size = f.seek(0, io.SEEK_END)
            if start is not None:
                if start >= 0:
                    f.seek(start)
                else:
                    f.seek(max(0, size + start))
            if end is not None:
                if end < 0:
                    end = size + end
                return f.read(end - f.tell())
            return f.read()

    def _put_file(
        self,
        path: Path,
        value: BytesLike,
        start: Optional[int] = None,
    ):
        if self.auto_mkdir:
            path.parent.mkdir(parents=True, exist_ok=True)
        if start is not None:
            with path.open("r+b") as f:
                f.seek(start)
                f.write(value)
        else:
            return path.write_bytes(value)

    async def get_async(
        self, key: str, byte_range: Optional[Tuple[int, int]] = None
    ) -> Optional[BytesLike]:
        assert isinstance(key, str)
        path = self.root / key

        try:
            value = await (
                to_thread(self._cat_file, path, byte_range[0], byte_range[1])
                if byte_range is not None
                else to_thread(self._cat_file, path)
            )
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None

        return value

    async def set_async(
        self, key: str, value: BytesLike, byte_range: Optional[Tuple[int, int]] = None
    ) -> None:
        assert isinstance(key, str)
        path = self.root / key

        if byte_range is not None:
            await to_thread(self._put_file, path, value, byte_range[0])
        else:
            await to_thread(self._put_file, path, value)

    async def delete_async(self, key: str) -> None:
        path = self.root / key
        await to_thread(path.unlink, True)

    async def exists_async(self, key: str) -> bool:
        path = self.root / key
        return await to_thread(path.exists)

    def __str__(self) -> str:
        return f"file://{self.root}"

    def __repr__(self) -> str:
        return f"LocalStore({repr(str(self))})"


class RemoteStore(Store):
    fs: AsyncFileSystem
    root: str

    def __init__(self, url: str, **storage_options):
        assert isinstance(url, str)

        # instantiate file system
        fs, root = fsspec.core.url_to_fs(url, asynchronous=True, **storage_options)
        assert fs.__class__.async_impl, "FileSystem needs to support async operations."
        self.fs = fs
        self.root = root.rstrip("/")

    async def get_async(
        self, key: str, byte_range: Optional[Tuple[int, int]] = None
    ) -> Optional[BytesLike]:
        assert isinstance(key, str)
        path = _dereference_path(self.root, key)

        try:
            value = await (
                self.fs._cat_file(path, byte_range[0], byte_range[1])
                if byte_range
                else self.fs._cat_file(path)
            )
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None

        return value

    async def set_async(
        self, key: str, value: BytesLike, byte_range: Optional[Tuple[int, int]] = None
    ) -> None:
        assert isinstance(key, str)
        path = _dereference_path(self.root, key)

        # write data
        if byte_range:
            with self.fs._open(path, "r+b") as f:
                f.seek(byte_range[0])
                f.write(value)
        else:
            await self.fs._pipe_file(path, value)

    async def delete_async(self, key: str) -> None:
        path = _dereference_path(self.root, key)
        if await self.fs._exists(path):
            await self.fs._rm(path)

    async def exists_async(self, key: str) -> bool:
        path = _dereference_path(self.root, key)
        return await self.fs._exists(path)

    def __str__(self) -> str:
        protocol = (
            self.fs.protocol[0]
            if isinstance(self.fs.protocol, list)
            else self.fs.protocol
        )
        return f"{protocol}://{self.root}"

    def __repr__(self) -> str:
        return f"RemoteStore({repr(str(self))})"


StoreLike = Union[Store, StorePath, Path, str]


def make_store_path(store_like: StoreLike) -> StorePath:
    if isinstance(store_like, StorePath):
        return store_like
    elif isinstance(store_like, Store):
        return StorePath(store_like)
    elif isinstance(store_like, Path):
        return StorePath(Store.from_path(store_like))
    elif isinstance(store_like, str):
        try:
            from upath import UPath

            return StorePath(Store.from_path(UPath(store_like)))
        except ImportError:
            return StorePath(LocalStore(Path(store_like)))
    raise TypeError

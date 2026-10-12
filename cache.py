from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import io
from typing import Dict, TypedDict, NewType, TYPE_CHECKING

import aiohttp

from utils import json_load, json_save
from constants import URLType, CACHE_PATH, CACHE_DB

from PIL import Image as Image_module
from PIL.ImageTk import PhotoImage


if TYPE_CHECKING:
    from gui import GUIManager
    from PIL.Image import Image
    from typing_extensions import TypeAlias


ImageHash = NewType("ImageHash", str)
ImageSize: TypeAlias = "tuple[int, int]"


class ExpiringHash(TypedDict):
    hash: ImageHash
    expires: datetime


Hashes = Dict[URLType, ExpiringHash]
default_database: Hashes = {}


class ImageCache:
    LIFETIME = timedelta(days=7)

    def __init__(self, manager: GUIManager) -> None:
        self._root = manager._root
        self._twitch = manager._twitch
        cleanup: bool = False
        CACHE_PATH.mkdir(parents=True, exist_ok=True)
        loaded_hashes = json_load(CACHE_DB, None, merge=False)
        if not isinstance(loaded_hashes, dict):
            # if we can't load the mapping file, delete all existing files,
            # then reinitialize the image cache anew
            cleanup = True
            self._hashes = default_database.copy()
        else:
            self._hashes = loaded_hashes
        self._images: dict[ImageHash, Image] = {}
        self._photos: dict[tuple[ImageHash, ImageSize], PhotoImage] = {}
        self._lock = asyncio.Lock()
        self._altered: bool = False
        # cleanup the URLs
        hash_counts: dict[ImageHash, int] = {}
        now = datetime.now(timezone.utc)
        for url, hash_dict in list(self._hashes.items()):
            if not isinstance(hash_dict, dict) or "hash" not in hash_dict or "expires" not in hash_dict:
                del self._hashes[url]
                self._altered = True
                continue
            img_hash = hash_dict["hash"]
            if img_hash not in hash_counts:
                hash_counts[img_hash] = 0
            if not isinstance(hash_dict["expires"], datetime) or now >= hash_dict["expires"]:
                del self._hashes[url]
                self._altered = True
            else:
                hash_counts[img_hash] += 1
        for img_hash, count in hash_counts.items():
            if count == 0:
                # hashes come with an extension already
                CACHE_PATH.joinpath(img_hash).unlink(missing_ok=True)
                # NOTE: The hashes are deleted from self._hashes above
        if cleanup:
            # This cleanups the cache folder from unused PNG files
            orphans = [
                file.name for file in CACHE_PATH.glob("*.png") if file.name not in hash_counts
            ]
            for filename in orphans:
                CACHE_PATH.joinpath(filename).unlink(missing_ok=True)

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(CACHE_DB, self._hashes, sort=True)
            self._altered = False

    def _new_expires(self) -> datetime:
        return datetime.now(timezone.utc) + self.LIFETIME

    def _hash(self, image: Image) -> ImageHash:
        pixel_data = list(
            image.resize((10, 10), Image_module.Resampling.LANCZOS).convert('L').getdata()
        )
        avg_pixel = sum(pixel_data) / len(pixel_data)
        bits = ''.join('1' if px >= avg_pixel else '0' for px in pixel_data)
        return ImageHash(f"{int(bits, 2):x}.png")

    async def get(self, url: URLType, size: ImageSize | None = None) -> PhotoImage:
        if not url:
            image = Image_module.new("RGBA", (10, 10), (0, 0, 0, 0))
            if size is None:
                size = (10, 10)
            photo_key = (ImageHash("empty.png"), size)
            async with self._lock:
                if photo_key in self._photos:
                    return self._photos[photo_key]
                if image.size != size:
                    image = image.resize(size)
                photo = PhotoImage(master=self._root, image=image)
                self._photos[photo_key] = photo
                return photo

        # 1. Fast cache check under lock
        async with self._lock:
            img_hash: ImageHash | None = self._hashes.get(url, {}).get("hash")  # type: ignore[assignment]
            image: Image | None = None
            if img_hash:
                self._hashes[url]["expires"] = self._new_expires()
                if img_hash in self._images:
                    image = self._images[img_hash]
                else:
                    try:
                        loaded = Image_module.open(CACHE_PATH / img_hash)
                        loaded.load()  # force full decode so broken data is caught here
                        self._images[img_hash] = image = loaded
                    except (FileNotFoundError, Image_module.UnidentifiedImageError, OSError):
                        pass

            if image is not None:
                if size is None:
                    size = image.size
                photo_key = (img_hash, size)
                if photo_key in self._photos:
                    photo = self._photos.pop(photo_key)
                    self._photos[photo_key] = photo
                    return photo
                if image.size != size:
                    try:
                        resized = image.resize(size, Image_module.Resampling.LANCZOS)
                    except OSError:
                        resized = Image_module.new("RGB", size, (255, 255, 255))
                else:
                    resized = image
                self._photos[photo_key] = photo = PhotoImage(master=self._root, image=resized)
                if len(self._images) > 512:
                    for k in list(self._images.keys())[:64]:
                        del self._images[k]
                if len(self._photos) > 512:
                    for k in list(self._photos.keys())[:64]:
                        del self._photos[k]
                return photo

        # 2. Network fetch WITHOUT holding lock!
        image = None
        try:
            session = await self._twitch.get_session()
            timeout = aiohttp.ClientTimeout(total=4.0)
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    loaded = Image_module.open(io.BytesIO(await response.read()))
                    loaded.load()
                    image = loaded
        except Exception:
            pass

        is_fallback = image is None
        if is_fallback:
            # use a blank transparent image as a fallback
            image = Image_module.new("RGBA", (10, 10), (0, 0, 0, 0))

        # 3. Store result under lock
        async with self._lock:
            if url in self._hashes and self._hashes[url]["hash"] in self._images:
                img_hash = self._hashes[url]["hash"]
                image = self._images[img_hash]
            else:
                try:
                    img_hash = self._hash(image)
                    self._images[img_hash] = image
                    if not is_fallback:
                        image.save(CACHE_PATH / img_hash)
                except Exception:
                    img_hash = ImageHash("fallback.png")

                if not is_fallback and url:
                    self._hashes[url] = {
                        "hash": img_hash,
                        "expires": self._new_expires(),
                    }
                    self._altered = True

            if size is None:
                size = image.size
            photo_key = (img_hash, size)
            if photo_key in self._photos:
                photo = self._photos.pop(photo_key)
                self._photos[photo_key] = photo
                return photo
            if image.size != size:
                try:
                    resized = image.resize(size, Image_module.Resampling.LANCZOS)
                except OSError:
                    # broken image data surfaced during resize; fall back to blank placeholder
                    resized = Image_module.new("RGB", size, (255, 255, 255))
            else:
                resized = image
            self._photos[photo_key] = photo = PhotoImage(master=self._root, image=resized)
            if len(self._images) > 512:
                for k in list(self._images.keys())[:64]:
                    del self._images[k]
            if len(self._photos) > 512:
                for k in list(self._photos.keys())[:64]:
                    del self._photos[k]
            return photo

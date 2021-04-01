import logging
import os.path
from time import time as ts
from typing import Dict, List

from . import utils
from .container import ContainerRunner
from .models import Cache

logger = logging.getLogger(__name__)


class CacheManager:
    def __init__(self, container: ContainerRunner, cache_definitions: Dict[str, Cache]):
        self._container = container
        self._cache_definitions = cache_definitions

        self._ignored_caches = {"docker"}

    def upload(self, cache_names: List[str]):
        for cache in cache_names:
            self._upload_cache(cache)

    def download(self, cache_names: List[str]):
        for cache in cache_names:
            self._download_cache(cache)

    def _upload_cache(self, cache_name: str):
        if self._should_ignore(cache_name):
            logger.info("Cache '%s': Ignoring", cache_name)
            return

        local_cache_archive_path = self._get_local_cache_archive_path(cache_name)

        if not os.path.exists(local_cache_archive_path):
            logger.info("Cache '%s': Not found: Skipping", cache_name)
            return

        remote_cache_directory = self._get_remote_directory(cache_name)
        remote_cache_parent_directory = os.path.dirname(remote_cache_directory)

        cache_archive_size = os.path.getsize(local_cache_archive_path)

        logger.info("Cache '%s': Uploading", cache_name)

        t = ts()

        prepare_cache_dir_cmd = (
            f'[ -d "{remote_cache_directory}" ] && rm -rf "{remote_cache_directory}"; '
            f'mkdir -p "{remote_cache_parent_directory}"'
        )
        res, output = self._container.run_command(prepare_cache_dir_cmd)
        if res != 0:
            logger.error("Remote command failed: %s", output.decode())
            raise Exception(f"Error uploading cache: {cache_name}")

        with open(local_cache_archive_path, "rb") as f:
            success = self._container.put_archive(remote_cache_parent_directory, f)
            if not success:
                raise Exception(f"Error uploading cache: {cache_name}")

        t = ts() - t

        logger.info(
            "Cache '%s': Uploaded %s in %.3fs", cache_name, utils.get_human_readable_size(cache_archive_size), t
        )

    def _download_cache(self, cache_name: str):
        if self._should_ignore(cache_name):
            logger.info("Cache '%s': Ignoring", cache_name)
            return

        local_cache_archive_path = self._get_local_cache_archive_path(cache_name)
        remote_cache_directory = self._get_remote_directory(cache_name)
        if not remote_cache_directory:
            logger.info("Cache '%s': Ignoring", cache_name)

        logger.info("Cache '%s': Downloading", cache_name)

        t = ts()

        with open(local_cache_archive_path, "wb") as f:
            data, _ = self._container.get_archive(remote_cache_directory)
            size = 0
            for chunk in data:
                size += len(chunk)
                f.write(chunk)

        t = ts() - t

        logger.info("Cache '%s': Downloaded %s in %.3fs", cache_name, utils.get_human_readable_size(size), t)

    def _should_ignore(self, cache_name: str) -> bool:
        return cache_name in self._ignored_caches

    @staticmethod
    def _get_local_cache_archive_path(cache_name: str) -> str:
        return os.path.join(utils.get_local_cache_directory(), f"{cache_name}.tar")

    def _get_remote_directory(self, cache_name: str) -> str:
        if cache_name not in self._cache_definitions:
            raise ValueError(f"Invalid cache: {cache_name}")

        remote_dir = self._cache_definitions[cache_name].path

        return self._container.expand_path(remote_dir)

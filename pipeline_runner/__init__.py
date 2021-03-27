import logging
import os
import uuid
from time import time as ts
from typing import Optional, Union

from dotenv import load_dotenv
from slugify import slugify

from . import utils
from .artifacts import ArtifactManager
from .cache import CacheManager
from .config import config
from .container import ContainerRunner
from .models import CloneSettings, Image, ParallelStep, Pipelines, Step
from .parse import PipelinesFileParser
from .service import ServicesManager

logger = logging.getLogger(__name__)


class PipelineRunner:
    def __init__(self, pipeline: str):
        self._pipeline = pipeline
        self._uuid = str(uuid.uuid4())

    def run(self):
        self._load_env_files()

        pipeline, pipelines_definition = self._load_pipeline()

        logger.info("Running pipeline: %s", pipeline.name)
        logger.debug("Pipeline ID: %s", self._uuid)

        s = ts()
        exit_code = self._execute_pipeline(pipeline, pipelines_definition)
        logger.info("Pipeline '%s' executed in %.3fs.", pipeline.name, ts() - s)

        if exit_code:
            logger.error("Pipeline '%s': Failed", pipeline.name)
        else:
            logger.info("Pipeline '%s': Successful", pipeline.name)

    @staticmethod
    def _load_env_files():
        logger.debug("Loading .env file (if exists)")
        load_dotenv(".env", override=True)

        for env_file in config.env_files:
            if not os.path.exists(env_file):
                raise ValueError(f"Invalid env file: {env_file}")

            logger.debug("Loading env file: %s", env_file)
            load_dotenv(env_file, override=True)

    def _load_pipeline(self):
        pipelines_definition = PipelinesFileParser(config.pipeline_file).parse()

        pipeline_to_run = pipelines_definition.get_pipeline(self._pipeline)

        if not pipeline_to_run:
            msg = f"Invalid pipeline: {self._pipeline}"
            logger.error(msg)
            logger.info(
                "Available pipelines:\n\t%s", "\n\t".join(sorted(pipelines_definition.get_available_pipelines()))
            )
            raise ValueError(msg)

        return pipeline_to_run, pipelines_definition

    def _execute_pipeline(self, pipeline, definitions):
        for step in pipeline.steps:
            step_uuid = str(uuid.uuid4())
            runner = StepRunnerFactory.get(step, self._uuid, step_uuid, definitions)

            exit_code = runner.run()

            if exit_code:
                return exit_code


class StepRunner:
    def __init__(self, step: Union[Step, ParallelStep], pipeline_uuid: str, step_uuid: str, definitions: Pipelines):
        self._step = step
        self._pipeline_uuid = pipeline_uuid
        self._step_uuid = step_uuid
        self._definitions = definitions

        self._services_manager = None
        self._container_runner = None

    def run(self) -> Optional[int]:
        if not self._should_run():
            logger.info("Skipping step: %s", self._step.name)
            return

        logger.info("Running step: %s", self._step.name)
        logger.debug("Step ID: %s", self._step_uuid)

        s = ts()

        try:
            image = self._get_image()
            container_name = f"{config.project_slug}-step-{slugify(self._step.name)}"
            data_volume_name = f"{container_name}-data"
            mem_limit = self._get_build_container_memory_limit()

            self._services_manager = ServicesManager(
                self._step.services, self._definitions.services, self._step.size, data_volume_name
            )
            self._services_manager.start_services()

            services_names = self._services_manager.get_services_names()

            self._container_runner = ContainerRunner(image, container_name, mem_limit, services_names, data_volume_name)
            self._container_runner.start()

            self._build_setup()

            exit_code = self._container_runner.run_script(self._step.script)

            self._container_runner.run_script(self._step.after_script, env={"BITBUCKET_EXIT_CODE": exit_code})

            if exit_code:
                logger.error("Step '%s': FAIL", self._step.name)

            self._build_teardown(exit_code)
        finally:
            if self._services_manager:
                self._services_manager.stop_services()

            if self._container_runner:
                self._container_runner.stop()

        logger.info("Step '%s' executed in %.3fs with exit code: %s", self._step.name, ts() - s, exit_code)

        return exit_code

    def _should_run(self):
        if config.selected_steps and self._step.name not in config.selected_steps:
            return False

        return True

    def _get_image(self):
        if self._step.image:
            return self._step.image

        if self._definitions.image:
            return self._definitions.image

        return Image(config.default_image)

    def _get_build_container_memory_limit(self) -> int:
        return config.build_container_base_memory_limit * self._step.size

    def _build_setup(self):
        logger.info("Build setup: '%s'", self._step.name)
        s = ts()

        self._clone_repository()
        self._upload_artifacts()
        self._upload_caches()

        logger.info("Build setup finished in %.3fs: '%s'", ts() - s, self._step.name)

    def _upload_artifacts(self):
        am = ArtifactManager(self._container_runner, self._pipeline_uuid, self._step_uuid)
        am.upload()

    def _upload_caches(self):
        cm = CacheManager(self._container_runner, self._definitions.caches)
        cm.upload(self._step.caches)

    def _clone_repository(self):
        # GIT_LFS_SKIP_SMUDGE=1 retry 6 git clone --branch="tbd/DRCT-455-enable-build-on-commits-to-trun"
        # --depth 50 https://x-token-auth:$REPOSITORY_OAUTH_ACCESS_TOKEN@bitbucket.org/$BITBUCKET_REPO_FULL_NAME.git
        # $BUILD_DIR
        if not self._should_clone():
            logger.info("Clone disabled: skipping")
            return

        cmd = []

        if not self._should_clone_lfs():
            cmd += ["GIT_LFS_SKIP_SMUDGE=1"]

        cmd += ["git", "clone", "--branch", utils.get_git_current_branch()]

        clone_depth = self._get_clone_depth()
        if clone_depth:
            cmd += ["--depth", str(clone_depth)]

        cmd += [f"file://{config.remote_workspace_dir}", "$BUILD_DIR"]

        exit_code = self._container_runner.run_command(cmd)

        if exit_code:
            raise Exception("Error cloning repository")

        exit_code = self._container_runner.run_command(["git", "reset", "--hard", "$BITBUCKET_COMMIT"])

        if exit_code:
            raise Exception("Error resetting to HEAD commit")

    def _should_clone(self) -> bool:
        for v in (
            self._step.clone_settings.enabled,
            self._definitions.clone_settings.enabled,
            CloneSettings.default().enabled,
        ):
            if v is not None:
                return v

    def _should_clone_lfs(self) -> bool:
        for v in (
            self._step.clone_settings.lfs,
            self._definitions.clone_settings.lfs,
            CloneSettings.default().lfs,
        ):
            if v is not None:
                return v

    def _get_clone_depth(self) -> Optional[int]:
        for v in (
            self._step.clone_settings.depth,
            self._definitions.clone_settings.depth,
            CloneSettings.default().depth,
        ):
            if v is not None:
                return v

    def _build_teardown(self, exit_code):
        logger.info("Build teardown: '%s'", self._step.name)
        s = ts()

        self._download_caches(exit_code)
        self._download_artifacts()
        self._stop_services()

        logger.info("Build teardown finished in %.3fs: '%s'", ts() - s, self._step.name)

    def _download_caches(self, exit_code):
        if exit_code == 0:
            cm = CacheManager(self._container_runner, self._definitions.caches)
            cm.download(self._step.caches)
        else:
            logger.warning("Skipping caches for failed step")

    def _download_artifacts(self):
        am = ArtifactManager(self._container_runner, self._pipeline_uuid, self._step_uuid)
        am.download(self._step.artifacts)

    def _stop_services(self):
        pass


class ParallelStepRunner(StepRunner):
    def __init__(self, step: Union[Step, ParallelStep], pipeline_uuid: str, step_uuid: str, definitions: Pipelines):
        super().__init__(step, pipeline_uuid, step_uuid, definitions)

    def run(self) -> Optional[int]:
        return_code = 0
        for s in self._step.steps:
            runner = StepRunnerFactory.get(s, self._pipeline_uuid, self._step_uuid, self._definitions)
            rc = runner.run()
            if rc:
                return_code = rc

        return return_code


class StepRunnerFactory:
    @staticmethod
    def get(step: Union[Step, ParallelStep], pipeline_uuid: str, step_uuid: str, definitions: Pipelines):
        if isinstance(step, ParallelStep):
            return ParallelStepRunner(step, pipeline_uuid, step_uuid, definitions)
        else:
            return StepRunner(step, pipeline_uuid, step_uuid, definitions)

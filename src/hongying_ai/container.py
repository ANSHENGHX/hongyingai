from __future__ import annotations

from dataclasses import dataclass

from hongying_ai.application.media import MediaService
from hongying_ai.application.planner import PlannerService
from hongying_ai.application.quality import QualityService
from hongying_ai.config import Settings
from hongying_ai.domain.ports import CoordinationStore, MediaRunner, MessageBus, ObjectStore, RunRepository
from hongying_ai.infrastructure.coordination import RedisCoordinationStore
from hongying_ai.infrastructure.ffmpeg import FfmpegRunner
from hongying_ai.infrastructure.message_bus import RabbitMessageBus
from hongying_ai.infrastructure.model_gateway import DeepSeekModelClient
from hongying_ai.infrastructure.object_store import MinioObjectStore
from hongying_ai.infrastructure.repository import MySqlRunRepository


@dataclass(slots=True)
class Container:
    settings: Settings
    store: ObjectStore
    coordination: CoordinationStore
    repository: RunRepository
    runner: MediaRunner
    bus: MessageBus
    model: DeepSeekModelClient
    media: MediaService
    planner: PlannerService
    quality: QualityService

    @classmethod
    def build(cls, settings: Settings) -> Container:
        store = MinioObjectStore(settings)
        coordination = RedisCoordinationStore(settings.redis_url)
        repository = MySqlRunRepository(settings.mysql_url)
        runner = FfmpegRunner(settings.ffmpeg_path, settings.ffprobe_path)
        bus = RabbitMessageBus(settings.rabbitmq_url)
        model = DeepSeekModelClient(settings)
        return cls(
            settings=settings,
            store=store,
            coordination=coordination,
            repository=repository,
            runner=runner,
            bus=bus,
            model=model,
            media=MediaService(settings, store, runner),
            planner=PlannerService(model, repository),
            quality=QualityService(runner),
        )

    async def close(self) -> None:
        for dependency in (self.bus, self.coordination, self.repository, self.model):
            close = getattr(dependency, "close", None)
            if close:
                await close()

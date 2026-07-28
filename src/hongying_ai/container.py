from __future__ import annotations

from dataclasses import dataclass

from hongying_ai.application.media import MediaService
from hongying_ai.application.planner import PlannerService
from hongying_ai.application.quality import QualityService
from hongying_ai.config import Settings
from hongying_ai.domain.ports import (
    CoordinationStore,
    ImageGenerationClient,
    MediaRunner,
    MessageBus,
    ObjectStore,
    RunRepository,
    VideoGenerationClient,
)
from hongying_ai.infrastructure.ark_media import ArkMediaClient
from hongying_ai.infrastructure.coordination import RedisCoordinationStore
from hongying_ai.infrastructure.ffmpeg import FfmpegRunner
from hongying_ai.infrastructure.message_bus import RabbitMessageBus
from hongying_ai.infrastructure.model_gateway import DeepSeekModelClient
from hongying_ai.infrastructure.object_store import MinioObjectStore
from hongying_ai.infrastructure.repository import MySqlRunRepository
from hongying_ai.infrastructure.tts import BaiduTtsClient


@dataclass(slots=True)
class Container:
    settings: Settings
    store: ObjectStore
    coordination: CoordinationStore
    repository: RunRepository
    runner: MediaRunner
    bus: MessageBus
    model: DeepSeekModelClient
    tts: BaiduTtsClient
    image_generator: ImageGenerationClient | None
    video_generator: VideoGenerationClient | None
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
        tts = BaiduTtsClient(settings)
        media_generator = ArkMediaClient(settings) if settings.ark_media_enabled else None
        return cls(
            settings=settings,
            store=store,
            coordination=coordination,
            repository=repository,
            runner=runner,
            bus=bus,
            model=model,
            tts=tts,
            image_generator=media_generator,
            video_generator=media_generator,
            media=MediaService(settings, store, runner),
            planner=PlannerService(model, repository),
            quality=QualityService(runner),
        )

    async def close(self) -> None:
        dependencies = (
            self.bus,
            self.coordination,
            self.repository,
            self.model,
            self.tts,
            self.image_generator,
            self.video_generator,
        )
        closed: set[int] = set()
        for dependency in dependencies:
            if dependency is None or id(dependency) in closed:
                continue
            closed.add(id(dependency))
            close = getattr(dependency, "close", None)
            if close:
                await close()

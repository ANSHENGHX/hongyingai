from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from hongying_ai.domain.ports import MessageHandler


class RabbitMessageBus:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.abc.AbstractRobustChannel | None = None

    async def connect(self) -> None:
        if self.connection and not self.connection.is_closed:
            return
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=1)

    async def publish(self, routing_key: str, body: dict[str, Any], exchange: str) -> None:
        await self.connect()
        assert self.channel
        exchange_type = ExchangeType.TOPIC
        target = await self.channel.declare_exchange(exchange, exchange_type, durable=True)
        await target.publish(
            Message(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )

    async def consume(
        self,
        queue: str,
        routing_key: str,
        exchange: str,
        handler: MessageHandler,
    ) -> AsyncIterator[None]:
        await self.connect()
        assert self.channel
        deadletter = await self.channel.declare_exchange(
            "hongying.deadletter.exchange", ExchangeType.TOPIC, durable=True
        )
        dlq = await self.channel.declare_queue("ai.dlq.v1", durable=True)
        await dlq.bind(deadletter, routing_key="deadletter.#")
        target = await self.channel.declare_exchange(exchange, ExchangeType.TOPIC, durable=True)
        source = await self.channel.declare_queue(
            queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "hongying.deadletter.exchange",
                "x-dead-letter-routing-key": f"deadletter.{queue}",
            },
        )
        await source.bind(target, routing_key=routing_key)
        async with source.iterator() as iterator:
            async for message in iterator:
                async with message.process(requeue=False):
                    await handler(json.loads(message.body))
                yield None

    async def health(self) -> bool:
        try:
            await self.connect()
            return bool(self.connection and not self.connection.is_closed)
        except Exception:
            return False

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()


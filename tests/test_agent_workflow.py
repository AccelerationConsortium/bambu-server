"""Data-only agent workflow behavior. No printer or robot is contacted."""

from __future__ import annotations

import asyncio

from bambu_server.agent_workflow import EventHub


async def test_webhook_delivery_failure_does_not_fail_publication(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("receiver rejected the event")

    monkeypatch.setattr("bambu_server.agent_workflow.urlopen", fail)
    hub = EventHub(webhook_urls=["https://events.example/ingest"])

    event = await hub.publish(
        "job.finished", submission_id="a" * 32, printer_id="bambu_test_01"
    )

    await asyncio.gather(*tuple(hub._deliveries))
    assert event.type == "job.finished"
    assert [item.submission_id for item in hub.page(None, 10).events] == ["a" * 32]

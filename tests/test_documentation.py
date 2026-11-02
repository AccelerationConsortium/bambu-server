"""Documentation is public, packaged, prefix-safe and requires no printer I/O."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from bambu_server.main import create_app


@pytest.mark.parametrize("dispatch", [False, True])
def test_reference_matches_enabled_schema_without_starting_hardware(settings, dispatch):
    settings.dispatch = settings.dispatch.model_copy(update={"enabled": dispatch})

    def unexpected_backend(*args):
        pytest.fail("Documentation constructed a printer backend")

    app = create_app(settings=settings, backend_factory=unexpected_backend)
    # No lifespan: documentation must work without a monitor, store or printer.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    reference = client.get("/agent-docs/api-reference")
    assert reference.status_code == 200
    assert reference.headers["content-type"].startswith("text/markdown")
    expected = {
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        for method in methods
        if method in {"get", "post", "delete", "put", "patch", "head", "options"}
    }
    actual = set(
        re.findall(r"^## (GET|POST|DELETE|PUT|PATCH|HEAD|OPTIONS) (\S+)$", reference.text, re.M)
    )
    assert actual == expected
    assert (("POST", "/printers/{printer_id}/control/start_print") in actual) == dispatch
    assert ("StartPrintRequest" in reference.text) == dispatch
    for secret in ("secret-access-code", "secret-serial", "printer.invalid"):
        assert secret not in reference.text

    machine = client.get("/docs/agent").json()
    assert machine["equipment_id"] == "bambu_gateway"
    assert "agent_camera" in machine["guide_markdown"]
    assert {(e["method"], e["path"]) for e in machine["endpoints"]} == expected
    assert '"supportedSubmitMethods": []' in client.get("/docs").text


@pytest.mark.parametrize("prefix", ["", "/bambu", "/api/equipment/bambu_gateway/documentation"])
@pytest.mark.parametrize(
    "path",
    [
        "/llms.txt",
        "/agent-docs",
        "/agent-docs/",
        "/agent-docs/api-reference",
        "/agent-docs/api-reference/",
    ],
)
def test_document_links_keep_access_prefix(settings, prefix, path):
    client = TestClient(create_app(settings=settings))
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "{{root}}" not in response.text
    links = re.findall(r"\]\(([^)]+)\)", response.text)
    assert links
    for link in links:
        resolved = urljoin(f"http://gateway.test{prefix}{path}", link)
        assert resolved.startswith(f"http://gateway.test{prefix}/")
        local_path = resolved.removeprefix(f"http://gateway.test{prefix}")
        assert client.get(local_path).status_code == 200


def test_document_reads_do_not_add_printer_io(client, backend):
    before = (
        backend.read_count,
        backend.snapshots,
        len(backend.uploads),
        len(backend.start_commands),
    )
    for path in ("/agent-docs", "/agent-docs/api-reference", "/llms.txt", "/docs/agent"):
        assert client.get(path).status_code == 200
    assert before == (
        backend.read_count,
        backend.snapshots,
        len(backend.uploads),
        len(backend.start_commands),
    )

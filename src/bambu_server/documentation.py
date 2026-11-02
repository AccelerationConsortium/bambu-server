"""Credential-free documentation, generated without monitor or printer access."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict


class RefusalBody(BaseModel):
    """A gateway refusal; additional fields identify the gate that refused it."""

    model_config = ConfigDict(extra="allow")
    detail: str


def guide(root: str = "") -> str:
    return (
        files("bambu_server")
        .joinpath("docs", "AGENT_GUIDE.md")
        .read_text(encoding="utf-8")
        .replace("{{root}}", root)
    )


def operations(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"method": method.upper(), "path": path, **operation}
        for path, item in sorted(schema["paths"].items())
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete", "options", "head"}
    ]


def api_reference(schema: dict[str, Any], root: str = "../") -> str:
    """Render routes and schemas from this app's actual OpenAPI document."""
    lines = [
        "# Bambu gateway API reference",
        "",
        "Generated from this service's OpenAPI; only registered routes appear.",
        "",
        f"[Operating guide]({root}agent-docs) · [OpenAPI JSON]({root}openapi.json) · "
        f"[Read-only Swagger]({root}docs)",
        "",
        "Paths are relative to the gateway. Browser clients use the authenticated `/bambu` "
        "edge prefix. The dashboard documentation proxy is read-only and cannot execute "
        "these operations. Agents use the lab-skills SDK for printer I/O.",
        "",
        "## Endpoints",
        "",
        "| Method | Path | Summary |",
        "|---|---|---|",
    ]
    entries = operations(schema)
    for entry in entries:
        summary = entry.get("summary", "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {entry['method']} | `{entry['path']}` | {summary} |")
    for entry in entries:
        lines += ["", f"## {entry['method']} {entry['path']}", ""]
        if entry.get("description"):
            lines += [entry["description"], ""]
        # Reproduce the declared operation schema rather than maintaining a
        # second copy of parameter, request, response and refusal definitions.
        contract = {k: entry[k] for k in ("parameters", "requestBody", "responses") if k in entry}
        lines += ["```json", json.dumps(contract, indent=2), "```"]
    lines += [
        "",
        "## Shared schemas",
        "",
        "```json",
        json.dumps(schema.get("components", {}).get("schemas", {}), indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def register_documentation(app: FastAPI) -> None:
    markdown = {200: {"content": {"text/markdown": {}}, "description": "Agent-readable Markdown."}}
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}

    @app.get("/agent-docs/", response_class=Response, include_in_schema=False)
    @app.get("/agent-docs", response_class=Response, responses=markdown, tags=["documentation"])
    async def agent_guide(request: Request) -> Response:
        """Operating guide: authentication, claims, A1 agent mode, evidence and recovery."""
        root = "../" if request.url.path.endswith("/") else ""
        return Response(guide(root), media_type="text/markdown", headers=headers)

    @app.get("/agent-docs/api-reference/", response_class=Response, include_in_schema=False)
    @app.get(
        "/agent-docs/api-reference",
        response_class=Response,
        responses=markdown,
        tags=["documentation"],
    )
    async def generated_reference(request: Request) -> Response:
        """Routes, request/response schemas and declared refusals from the running app."""
        root = "../../" if request.url.path.endswith("/") else "../"
        return Response(
            api_reference(app.openapi(), root), media_type="text/markdown", headers=headers
        )

    @app.get("/llms.txt", response_class=PlainTextResponse, tags=["documentation"])
    async def documentation_index() -> PlainTextResponse:
        """Documentation index; links retain direct, edge and dashboard proxy prefixes."""
        return PlainTextResponse(
            "# Bambu printer gateway\n\n"
            "A multi-printer STATUS_SPEC gateway with optional, claimed print control.\n"
            "Documentation does not authorize hardware execution.\n\n"
            "## Documentation\n\n"
            "- [Agent operating guide](agent-docs)\n"
            "- [API reference](agent-docs/api-reference)\n"
            "- [OpenAPI](openapi.json)\n"
            "- [Read-only Swagger](docs)\n"
            "- [Machine-readable gateway guide](docs/agent)\n\n"
            "Discover printers using GET /printers on the authenticated gateway; "
            "the dashboard documentation proxy does not proxy live status or control.\n",
            headers=headers,
        )

    @app.get("/docs/agent", tags=["documentation"])
    async def machine_guide() -> dict[str, Any]:
        """Service-level guide for SDK discover_device_guide on bambu_gateway."""
        return {
            "documentation_version": "1.0",
            "equipment_id": "bambu_gateway",
            "equipment_kind": "other",
            "scope": "Shared gateway; enumerate printers with GET /printers. "
            "Retrieve this guide using the bambu_gateway equipment entry.",
            "guide_markdown": guide("../"),
            "discovery": {
                "guide": "../agent-docs",
                "openapi": "../openapi.json",
                "api_reference": "../agent-docs/api-reference",
                "index": "../llms.txt",
            },
            "endpoints": [
                {k: entry.get(k) for k in ("method", "path", "summary")}
                for entry in operations(app.openapi())
            ],
        }

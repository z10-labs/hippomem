from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from hippomem import actors, config, server, service
from tests.helpers import accepted_decision


@pytest.fixture
def mem(conn):
    return service.Memory(conn=conn, config=config.Config())


@pytest.fixture
def tokens(conn):
    return {
        "@pat": actors.add(conn, "@pat", "engineer"),
        "reviewer-bot": actors.add(conn, "reviewer-bot", "reviewer"),
    }


@pytest.fixture
def client(mem):
    app = server.create(mem, allowed_hosts=("testserver",)).streamable_http_app()
    with TestClient(app) as c:
        yield c


def call_tool(client, token, name, agent="claude-code", **arguments):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers={"Authorization": f"Bearer {token}", "X-Hippomem-Agent": agent, "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result["content"][0]["text"], result.get("isError", False)


def test_tools_are_listed_with_server_instructions(client, tokens):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert "hippomem_query" in response.text or "instructions" in response.json()["result"]
    assert "engineering memory" in response.json()["result"]["instructions"]


def test_query_tool_returns_decisions_and_records_usage(client, tokens, conn):
    accepted_decision(conn, "Rate limiting uses Postgres advisory locks", "no new infrastructure")
    text, is_error = call_tool(client, tokens["@pat"], "hippomem_query", text="rate limiting postgres advisory locks", repo="product-api")
    assert not is_error
    assert "D-0001" in text and "Why: no new infrastructure" in text
    usage = conn.execute("SELECT actor, agent, action, repo FROM usage").fetchall()
    assert [tuple(u) for u in usage] == [("@pat", "claude-code", "query", "product-api")]


def test_tool_without_token_is_an_error(client):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hippomem_list", "arguments": {}}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.json()["result"]["isError"]


def test_scope_decision_flow_through_tools(client, tokens, conn):
    text, _ = call_tool(
        client,
        tokens["@pat"],
        "hippomem_propose",
        title="Billing owns all money writes",
        why="one place to audit money",
        scope="system",
        systems=["billing"],
    )
    assert "Proposed P-0001" in text
    text, _ = call_tool(client, tokens["@pat"], "hippomem_decide", proposal_id="P-0001", accept=True)
    assert text.startswith("Not allowed")  # @pat proposed it, and doesn't own billing
    alex = actors.rotate(conn, "@alex")
    text, _ = call_tool(client, alex, "hippomem_decide", proposal_id="P-0001", accept=True)
    assert "D-0001 is now in force" in text
    listing, _ = call_tool(client, tokens["@pat"], "hippomem_list", system="billing")
    assert "Billing owns all money writes" in listing and "system-wide" in listing


def test_propose_validation_error_is_returned_as_text(client, tokens):
    text, _ = call_tool(client, tokens["@pat"], "hippomem_propose", title="x", why="", scope="system")
    assert text.startswith("Not proposed")


def test_get_and_chain_tools(client, tokens, conn):
    from hippomem import decisions

    a = accepted_decision(conn, "Postgres is the only datastore")
    accepted_decision(conn, "Jobs use SKIP LOCKED", links=(decisions.Link("depends-on", a),))
    text, _ = call_tool(client, tokens["@pat"], "hippomem_get", decision_id=a)
    assert "accepted by @alex (engineer)" in text
    text, _ = call_tool(client, tokens["@pat"], "hippomem_chain", decision_id=a)
    assert "depended-on-by D-0002" in text
    text, _ = call_tool(client, tokens["@pat"], "hippomem_get", decision_id="D-0999")
    assert text == "No decision D-0999."


def test_mapping_and_systems_tools(client, tokens, conn):
    text, _ = call_tool(client, tokens["@pat"], "hippomem_propose_mapping", repo="product-api", path_glob="src/web/**", system="web")
    assert "Map product-api:src/web/** to web" in text
    text, _ = call_tool(client, tokens["@pat"], "hippomem_systems")
    assert "billing  Billing  owners: @alex" in text


def test_review_tool_lists_open_proposals(client, tokens):
    assert call_tool(client, tokens["@pat"], "hippomem_review")[0] == "No proposals waiting."
    call_tool(client, tokens["@pat"], "hippomem_propose", title="Services talk through events", why="decoupling", scope="team")
    assert "Services talk through events" in call_tool(client, tokens["@pat"], "hippomem_review")[0]


def test_api_requires_a_token(client):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/whoami").status_code == 401


def test_api_query_and_decide(client, tokens, conn):
    headers = {"Authorization": f"Bearer {tokens['@pat']}"}
    assert client.get("/api/whoami", headers=headers).json() == {"id": "@pat", "kind": "engineer", "agent": ""}
    accepted_decision(conn, "Invoices are immutable once sent")
    body = client.post("/api/query", json={"text": "invoices immutable once sent"}, headers=headers).json()
    assert "Invoices are immutable" in body["text"]
    assert client.get("/api/decisions", params={"system": "billing"}, headers=headers).json()["text"].startswith("D-0001")
    assert "Why:" in client.get("/api/decision", params={"id": "D-0001"}, headers=headers).json()["text"]
    assert client.get("/api/proposals", headers=headers).json() == {"proposals": []}
    assert client.post("/api/decide", json={"proposal_id": "P-0404", "accept": True}, headers=headers).json() == {"text": "No proposal P-0404."}
    assert client.post("/api/query", json={}, headers=headers).status_code == 400

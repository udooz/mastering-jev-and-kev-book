"""The shared triage client talks to a Kev-shaped server unchanged (mock server, no GPU needed)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from triage.jev_client import KevDecisionClient

from smartplant.contract import build_questions

SEEN: dict[str, Any] = {}


class FakeKev(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:  # quiet
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.update(path=self.path, auth=self.headers.get("authorization"), body=body)
        answers: dict[str, Any] = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.71}
            elif q["type"] == "choice":
                labels = list(q["criteria"])
                probs = {l: (0.55 if i == 1 % len(labels) else 0.45 / (len(labels) - 1)) for i, l in enumerate(labels)}
                answers[qid] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs,
                                "confidence": 0.34}
            else:
                n = len(q["criteria"])
                probs = {str(i): (0.6 if i == 2 else 0.4 / (n - 1)) for i in range(n)}
                answers[qid] = {"type": "score", "score": sum(int(k) * v for k, v in probs.items()),
                                "confidence": 0.4, "probabilities": probs,
                                "legend": {str(i): str(c) for i, c in enumerate(q["criteria"])}}
        out = json.dumps({"model": "kev-latest", "answers": answers,
                          "usage": {"input_tokens": 512, "output_tokens": 40}, "latency_ms": 149}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("x-typesafe-request-id", "req-test")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def kev_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeKev)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_kev_client_uses_typesafe_sdk_against_local_server(kev_url, state_for, monkeypatch) -> None:
    monkeypatch.delenv("KEV_API_KEY", raising=False)
    st = state_for("S06")
    qs = build_questions(st)
    client = KevDecisionClient(base_url=kev_url)
    r = client.decide(st, qs)
    assert SEEN["path"] == "/v1/systemone" and SEEN["auth"] == "Bearer local"
    assert SEEN["body"]["model"] == "kev-latest"
    assert r.backend == "kev" and r.model == "kev-latest" and r.input_tokens == 512
    assert r.choice("operational_action").choice in qs["operational_action"]["criteria"]
    assert r.score("operational_severity").levels == 5
    client.close()

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import argparse
import json
import logging
import sys
import os
import hmac
import ipaddress

from .config import NativeConfig
from .epoch import ContextEpochStore
from .evidence import EvidenceWriter
from .model import DirectProvider
from .runtime import NativeRuntime
from .session import ConversationStore
from .trace import TraceStore


class Handler(BaseHTTPRequestHandler):
    runtime: NativeRuntime
    auth_token = ""

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        if not self.auth_token or not hmac.compare_digest(supplied.encode(), ("Bearer " + self.auth_token).encode()):
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def _send(self, status: int, body: dict):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "runtime": "chiyo",
                    "hermes_dependency": False,
                },
            )
            return
        if not self._authorized():
            return
        if parsed.path == "/v1/evidence/status":
            try:
                self._send(200, self.runtime.evidence.status())
            except Exception as exc:
                self._send(503, {"error": type(exc).__name__})
            return
        prefix = "/v1/traces/"
        if parsed.path.startswith(prefix):
            trace_id = unquote(parsed.path[len(prefix):])
            record = self.runtime.traces.find(trace_id)
            if record is None:
                self._send(404, {"error": "trace_not_found"})
            else:
                self._send(200, record)
            return
        prefix = "/v1/conversations/"
        if parsed.path.startswith(prefix):
            conversation_id = unquote(parsed.path[len(prefix):])
            try:
                self._send(
                    200,
                    {
                        "conversation_id": conversation_id,
                        "turns": self.runtime.store.load(conversation_id),
                    },
                )
            except Exception as exc:
                self._send(400, {"error": type(exc).__name__})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != "/v1/turn":
            self._send(404, {"error": "not_found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1024 * 1024:
                raise ValueError("invalid content length")
            data = json.loads(self.rfile.read(size).decode("utf-8"))
            conversation_id = data["conversation_id"]
            user_text = data["user_text"]
            message_id = data.get("message_id") or self.headers.get("Idempotency-Key")
            if not isinstance(conversation_id, str) or not isinstance(user_text, str):
                raise ValueError("conversation_id and user_text must be strings")
            if message_id is not None and not isinstance(message_id, str):
                raise ValueError("message_id must be a string")
            result = self.runtime.handle_turn(conversation_id, user_text, message_id)
            logger = logging.getLogger("chiyo")
            logger.info(
                "generation.ready conversation_id=%s turn_id=%s trace_id=%s",
                "redacted",
                result["turn_id"],
                result["trace_id"],
            )
            logger.info(
                "delivery.attempted conversation_id=%s turn_id=%s trace_id=%s",
                "redacted",
                result["turn_id"],
                result["trace_id"],
            )
            self._send(200, result)
            logger.info(
                "delivery.delivered conversation_id=%s turn_id=%s trace_id=%s",
                "redacted",
                result["turn_id"],
                result["trace_id"],
            )
            try:
                self.runtime.record_delivery_success(result)
            except Exception:
                logger.error("delivery evidence hook failed")
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": "invalid_request"})
        except Exception as exc:
            logging.getLogger("chiyo").error("turn failed error_class=%s", type(exc).__name__)
            self._send(502, {"error": type(exc).__name__})

    def log_message(self, format, *args):
        logging.getLogger("chiyo").info("http.request method=%s", self.command)


def create_server(config, runtime):
    token = os.environ.get(config.auth_token_env, "")
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("native HTTP requires a private auth token of at least 32 characters")
    try:
        loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        loopback = config.host == "localhost"
    if not loopback and not config.allow_remote:
        raise ValueError("non-loopback HTTP requires explicit allow_remote; use a TLS reverse proxy")
    handler = type("ConfiguredHandler", (Handler,), {"runtime": runtime, "auth_token": token})
    return ThreadingHTTPServer((config.host, config.port), handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/chiyo/config.json")
    args = parser.parse_args()
    config = NativeConfig.load(args.config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    store = ConversationStore(config.data_dir)
    traces = TraceStore(config.trace_dir)
    backend = DirectProvider(config)
    evidence = EvidenceWriter(config.data_dir)
    context_epoch = ContextEpochStore(config.data_dir)
    runtime = NativeRuntime(config, store, backend, traces, evidence, context_epoch)
    server = create_server(config, runtime)
    logging.getLogger("chiyo").info(
        "native runtime listening host=%s port=%s model=%s provider=%s",
        config.host,
        config.port,
        config.model,
        config.provider,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

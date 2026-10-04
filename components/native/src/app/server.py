from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import argparse
import json
import logging
import sys

from .config import NativeConfig
from .epoch import ContextEpochStore
from .evidence import EvidenceWriter
from .model import DirectProvider
from .runtime import NativeRuntime
from .session import ConversationStore
from .trace import TraceStore


class Handler(BaseHTTPRequestHandler):
    runtime: NativeRuntime

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
                result["conversation_id"],
                result["turn_id"],
                result["trace_id"],
            )
            logger.info(
                "delivery.attempted conversation_id=%s turn_id=%s trace_id=%s",
                result["conversation_id"],
                result["turn_id"],
                result["trace_id"],
            )
            self._send(200, result)
            logger.info(
                "delivery.delivered conversation_id=%s turn_id=%s trace_id=%s",
                result["conversation_id"],
                result["turn_id"],
                result["trace_id"],
            )
            try:
                self.runtime.record_delivery_success(result)
            except Exception:
                logger.exception("delivery evidence hook failed")
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            logging.getLogger("chiyo").exception("turn failed")
            self._send(502, {"error": type(exc).__name__})

    def log_message(self, format, *args):
        logging.getLogger("chiyo").info(format, *args)


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
    Handler.runtime = runtime
    server = ThreadingHTTPServer((config.host, config.port), Handler)
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

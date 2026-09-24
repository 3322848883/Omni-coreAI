import json
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from logger import logger


class HealthCheckHandler(BaseHTTPRequestHandler):
    health_checker = None

    def do_GET(self):
        if self.path == "/health":
            try:
                status = self.health_checker.get_status()
                is_healthy = self.health_checker.is_healthy()
                status_code = 200 if is_healthy else 503
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(status, ensure_ascii=False, indent=2).encode("utf-8"))
            except Exception as e:
                logger.error("Health check error: %s", e)
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                error_response = {"status": "error", "message": str(e)}
                self.wfile.write(json.dumps(error_response, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            not_found = {"status": "error", "message": "Not found"}
            self.wfile.write(json.dumps(not_found, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass


class HealthChecker:
    def __init__(self, port=18080):
        self.port = port
        self._status_func = None
        self._healthy_func = None
        self._server = None
        self._thread = None
        self._running = False

    def register_status_provider(self, status_func, healthy_func=None):
        self._status_func = status_func
        self._healthy_func = healthy_func

    def get_status(self):
        if self._status_func:
            return self._status_func()
        return {"status": "no_provider", "message": "Status provider not registered"}

    def is_healthy(self):
        if self._healthy_func:
            return self._healthy_func()
        return True

    def start(self):
        if self._running:
            logger.warning("Health check server already running")
            return
        HealthCheckHandler.health_checker = self
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), HealthCheckHandler)
        self._server.daemon_threads = True
        self._running = True
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        logger.info("Health check server started on port %d", self.port)

    def _run_server(self):
        try:
            self._server.serve_forever()
        except Exception as e:
            if self._running:
                logger.error("Health check server error: %s", e)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("Health check server stopped")


_health_checker = None


def get_health_checker():
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker


def start_health_check(port=18080):
    checker = get_health_checker()
    checker.port = port
    checker.start()
    return checker
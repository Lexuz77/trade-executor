"""Webhook web server."""
import logging
import time
from queue import Queue

from eth_defi.utils import is_localhost_port_listening
from webtest.http import StopableWSGIServer

from .app import create_pyramid_app
from ..state.store import JSONFileStore

logger =  logging.getLogger(__name__)


class WebhookServer(StopableWSGIServer):
    """Create a Waitress server that we can gracefully shut down.

    https://docs.pylonsproject.org/projects/waitress/en/latest/
    """

    def shutdown(self, wait_gracefully=3.0):
        super().shutdown()

        # Check that the server gets shut down.
        # Looks like this is being an issue on Github CI.
        port = int(self.effective_port)
        logger.info("Shutting down %s: %d", self.effective_host, port)
        deadline = time.time() + wait_gracefully
        while time.time() < deadline:
            if not is_localhost_port_listening(host=self.effective_host, port=port):
                return
            time.sleep(1)
        raise AssertionError(f"Could not gracefully shut down {self.effective_host}:{port}, waited {wait_gracefully} seconds")


def create_webhook_server(host: str, port: int, username: str, password: str, queue: Queue, store: JSONFileStore) -> WebhookServer:
    """Starts the webhook web  server in a separate thread.

    :param queue: The command queue for commands posted in the webhook that offers async execution.
    """

    #assert username, "Username must be given"
    #assert password, "Password must be given"

    if (not username) and (not password):
        logger.warning("Web server started without username and password")

    app = create_pyramid_app(username, password, queue, store, production=False)
    server = WebhookServer.create(app, host=host, port=port, clear_untrusted_proxy_headers=True)
    logger.info("Webhook server will spawn at %s:%d, using username %s", host, port, username)
    # Wait until the server has started
    server.wait()
    return server

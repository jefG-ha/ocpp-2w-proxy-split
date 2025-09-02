# 2 way OCPP proxy (can also be used as a 1-way simple proxy)

import asyncio
import logging
import time
from typing import Tuple
import json

import websockets
import websockets.asyncio
import websockets.asyncio.server

from enum import IntEnum
import ssl
import argparse
import configparser

__version__ = "0.2.0"

config = configparser.ConfigParser()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("proxy")

class OCPP2WProxy:  # Forward declaration
    pass

class OCPPMessageType(IntEnum):
    Call = 2
    CallResult = 3
    CallError = 4

# main class
class OCPP2WProxy:
    # Static dict of OCPP2WProxy instances. key is charger_id
    proxy_list: dict[str, "OCPP2WProxy"] = {}

    # Utility functions
    @staticmethod
    def decode_ocpp_message(message: str) -> Tuple[int, str]:
        """Decode an OCPP message from a string"""
        j = json.loads(message)
        return [j[0], j[1]]

    def __init__(self, websocket: websockets.asyncio.server.ServerConnection, charger_id: str):
        # Store the websocket for later
        logger.debug(websocket.request)
        self.ws = websocket
        self.charger_id = charger_id

        # Check that charger id looks reasonable
        if not charger_id.isalnum():
            logger.error(f"Charger ID '{charger_id}' is not alphanumeric")
            raise Exception("Charger ID is not alphanumeric")

        # Initialize table of CSMS call ids sent to the charger in order to respond back
        self.primary_call_ids = set()
        self.secondary_call_ids = set()

        # Insert new OCPP2WProxy instance in the (static) dict of instances.
        self.proxy_list[charger_id] = self

        # RFID config and routing state
        self.primary_rfid = config.get("ext-server", "primary_rfid", fallback=None)
        self.secondary_rfid = config.get("ext-server", "secondary_rfid", fallback=None)
        self.session_target = None  # "primary" or "secondary"

    async def close(self):
        """Close all connections to the charger and primary, secondary server"""
        try:
            await self.ws.close()
            await self.primary_connection.close()
            if self.secondary_connection:
                await self.secondary_connection.close()
        except Exception:
            pass  # Ignore exceptions

    @staticmethod
    async def check_delete_old(charger_id: str):
        """Check if there are any old instances of this charger in the proxy list"""
        if charger_id in OCPP2WProxy.proxy_list:
            logger.info(f"Charger ID {charger_id} already exists. Closing and deleting")
            proxy: OCPP2WProxy = OCPP2WProxy.proxy_list[charger_id]
            await proxy.close()
            del OCPP2WProxy.proxy_list[charger_id]

    async def run(self):
        """Main loop for this proxy. This is where all the magic happens."""

        # Create connections to the two CSMSes.
        headers = {}
        if "Authorization" in self.ws.request.headers:
            headers["Authorization"] = self.ws.request.headers["Authorization"]
            logger.debug(f'Authorization header set to {headers["Authorization"]}')
        user_agent = self.ws.request.headers.get("User-Agent", None)
        subprotocols = self.ws.request.headers.get("Sec-WebSocket-Protocol", ["ocpp1.6"])
        primary_url = config.get("ext-server", "server") + "/" + self.charger_id
        if config.has_option("ext-server", "secondary_server"):
            secondary_url = config.get("ext-server", "secondary_server") + "/" + self.charger_id
        else:
            secondary_url = None

        try:
            self.primary_connection = await websockets.connect(
                uri=primary_url,
                user_agent_header=user_agent,
                additional_headers=headers,
                subprotocols=[subprotocols],
            )
            logger.info(f"Connected to primary server @ {primary_url}")

            if secondary_url:
                self.secondary_connection = await websockets.connect(
                    uri=secondary_url,
                    user_agent_header=user_agent,
                    additional_headers=headers,
                    subprotocols=[subprotocols],
                )
                logger.info(f"{self.charger_id} Connected to secondary server @ {secondary_url}")
            else:
                self.secondary_connection = None
                logger.info(f"{self.charger_id} Secondary server not enabled")

            # Create tasks
            self._last_charger_update = time.time()
            self.tasks = []
            self.tasks.append(asyncio.create_task(self.receive_charger_messages()))
            self.tasks.append(asyncio.create_task(self.receive_primary_messages()))
            if self.secondary_connection is not None:
                self.tasks.append(asyncio.create_task(self.receive_secondary_messages()))
            # self.tasks.append(asyncio.create_task(self.watchdog()))

            # Wait for tasks
            done, pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            logger.debug(f"{self.charger_id} Task(s) completed: {done}, {pending}")

            for task in done:
                e = task.exception()
                if e:
                    logger.warning(
                        f"{self.charger_id} Task {task} raised exception {e} (likely connection loss)"
                    )

            for task in pending:
                task.cancel()

        except websockets.exceptions.InvalidURI:
            logger.error(f"{self.charger_id} Invalid URI")
        except websockets.exceptions.ConnectionClosedError as e:
            logger.error(f"{self.charger_id} Connection closed unexpectedly: {e}")
        except websockets.exceptions.InvalidHandshake:
            logger.error(f"{self.charger_id} Handshake with the external server failed")
        except Exception as e:
            logger.error(f"{self.charger_id} Unexpected error: {e}")
        finally:
            await self.close()

    async def receive_charger_messages(self):
        try:
            while True:
                message = await self.ws.recv()
                logger.info(f"{self.charger_id} ^ : {message}")

                [message_type, message_id] = OCPP2WProxy.decode_ocpp_message(message)

                if message_type == OCPPMessageType.Call:
                    j = json.loads(message)
                    action = j[2]
                    payload = j[3]

                    # Check RFID on Authorize/StartTransaction
                    if action in ["Authorize", "StartTransaction"]:
                        id_tag = payload.get("idTag")
                        if id_tag == self.primary_rfid:
                            self.session_target = "primary"
                            logger.info(f"{self.charger_id} Session locked to PRIMARY (RFID {id_tag})")
                        elif id_tag == self.secondary_rfid:
                            self.session_target = "secondary"
                            logger.info(f"{self.charger_id} Session locked to SECONDARY (RFID {id_tag})")
                        else:
                            self.session_target = "primary"
                            logger.info(f"{self.charger_id} Session defaulted to PRIMARY (RFID {id_tag})")

                    # Forward based on chosen target
                    if self.session_target == "secondary":
                        if self.secondary_connection:
                            await self.secondary_connection.send(message)
                    else:
                        await self.primary_connection.send(message)

                elif message_type in (OCPPMessageType.CallResult, OCPPMessageType.CallError):
                    if message_id in self.primary_call_ids:
                        logger.info(f"{self.charger_id} ^ : Result/Error forwarded to primary")
                        self.primary_call_ids.remove(message_id)
                        await self.primary_connection.send(message)
                    elif message_id in self.secondary_call_ids:
                        logger.info(f"{self.charger_id} ^ : Result/Error forwarded to secondary")
                        self.secondary_call_ids.remove(message_id)
                        if self.secondary_connection:
                            await self.secondary_connection.send(message)
                    else:
                        logger.error(
                            f"{self.charger_id} ^: Received CallResult/CallError against unknown message id {message_id}"
                        )
                else:
                    logger.error(f"{self.charger_id} ^: Unknown message type {message_type}")
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_charger_messages: {e}")

    async def receive_primary_messages(self):
        try:
            while True:
                message = await self.primary_connection.recv()
                logger.info(f"{self.charger_id} v (prim) : {message}")

                [message_type, message_id] = OCPP2WProxy.decode_ocpp_message(message)
                if message_type == OCPPMessageType.Call:
                    self.primary_call_ids.add(message_id)

                await self.ws.send(message)
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_primary_messages: {e}")

    async def receive_secondary_messages(self):
        try:
            while True:
                message = await self.secondary_connection.recv()
                logger.info(f"{self.charger_id} v (sec) : {message}")

                [message_type, message_id] = OCPP2WProxy.decode_ocpp_message(message)
                if message_type == OCPPMessageType.Call:
                    self.secondary_call_ids.add(message_id)
                    await self.ws.send(message)
                # Note: CallResults/Errors from secondary are ignored
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_secondary_messages: {e}")

    async def watchdog(self):
        """Watchdog on charger messages."""
        while True:
            await asyncio.sleep(config.getint("host", "watchdog_interval", fallback=30))
            elapsed = time.time() - self._last_charger_update
            if elapsed > config.getint("host", "watchdog_stale", fallback=300):
                logger.error(f"{self.charger_id} Watchdog timeout {elapsed} sec. Closing connections")
                return

# Connection handler
async def on_connect(websocket: websockets.asyncio.server.ServerConnection):
    path = websocket.request.path
    charger_id = path.strip("/")
    logger.info(f"{charger_id} connection request")

    try:
        if charger_id in OCPP2WProxy.proxy_list:
            await OCPP2WProxy.proxy_list[charger_id].close()
            del OCPP2WProxy.proxy_list[charger_id]

        proxy = OCPP2WProxy(websocket=websocket, charger_id=charger_id)
        await proxy.run()

    except Exception as e:
        logger.error(f"{charger_id} Error creating OCPP2WProxy: {e}")
    finally:
        logger.info(f"{charger_id} closed/done")

# Main
async def main():
    parser = argparse.ArgumentParser(description="ocpp-2w-proxy: A two way OCPP proxy")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=str,
        default="ocpp-2w-proxy.ini",
        help="Configuration file (INI format). Default ocpp-2w-proxy.ini",
    )
    args = parser.parse_args()

    logger.warning(f"Reading config from {args.config}")
    config.read(args.config)

    # Adjust log levels
    for logger_name in config["logging"]:
        logger.warning(f'Setting log level for {logger_name} to {config.get("logging", logger_name)}')
        logging.getLogger(logger_name).setLevel(level=config.get("logging", logger_name))

    host = config.get("host", "addr")
    port = config.get("host", "port")
    cert_chain = config.get("host", "cert_chain", fallback=None)
    cert_key = config.get("host", "cert_key", fallback=None)

    if cert_chain and cert_key:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=cert_chain, keyfile=cert_key)
        server = await websockets.serve(
            on_connect,
            host,
            port,
            subprotocols=["ocpp1.6", "ocpp2.0.1"],
            ssl=ssl_context,
            ping_timeout=config.getint("host", "ping_timeout"),
        )
    else:
        server = await websockets.serve(
            on_connect,
            host,
            port,
            subprotocols=["ocpp1.6", "ocpp2.0.1"],
            ping_timeout=config.getint("host", "ping_timeout"),
        )

    logger.info("Proxy ready. Waiting for new connections...")
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit(0)
